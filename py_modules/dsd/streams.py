"""Line streams over subprocesses (journalctl, dbus-monitor, btmon) and the D-Bus parser."""
import asyncio, os, re, time
from .config import log
from .util import ANSI, ENV

class LineStream:
    def __init__(self, name, cmd, on_line, use_pty=False):
        self.name, self.cmd, self.on_line, self.use_pty = name, cmd, on_line, use_pty
        self.proc = None
        self.task = None
        self.running = False
        self.missing = False

    def start(self):
        self.running = True
        self.task = asyncio.ensure_future(self._loop())

    async def stop(self):
        self.running = False
        try:
            if self.proc and self.proc.returncode is None:
                self.proc.terminate()
        except ProcessLookupError:
            pass
        if self.task:
            self.task.cancel()

    def _emit(self, raw):
        text = ANSI.sub("", raw).replace("\r", "").rstrip("\n")
        if text:
            try:
                self.on_line(text)
            except Exception as e:
                log.error("stream %s handler: %s", self.name, e)

    async def _loop(self):
        backoff = 2
        while self.running:
            started = time.monotonic()
            try:
                if self.use_pty:
                    await self._run_pty()
                else:
                    await self._run_pipe()
            except asyncio.CancelledError:
                raise
            except FileNotFoundError:
                self.missing = True
                log.warning("stream %s: %s not found", self.name, self.cmd[0])
                return
            except Exception as e:
                log.error("stream %s failed: %s", self.name, e)
            if not self.running:
                return
            if time.monotonic() - started > 30:
                backoff = 2
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _run_pipe(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=ENV)
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            self._emit(line.decode(errors="replace"))
        await self.proc.wait()

    async def _run_pty(self):
        master, slave = os.openpty()
        try:
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.cmd, stdin=asyncio.subprocess.DEVNULL, stdout=slave, stderr=slave, env=ENV)
            finally:
                os.close(slave)
        except Exception:
            os.close(master)
            raise
        loop = asyncio.get_running_loop()
        os.set_blocking(master, False)
        eof = asyncio.Event()
        state = {"buf": b""}

        def on_read():
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                return
            except OSError:
                data = b""
            if not data:
                try:
                    loop.remove_reader(master)
                except (ValueError, OSError):
                    pass
                eof.set()
                return
            parts = (state["buf"] + data).split(b"\n")
            state["buf"] = parts.pop()
            for p in parts:
                self._emit(p.decode(errors="replace"))

        loop.add_reader(master, on_read)
        try:
            await eof.wait()
        finally:
            try:
                loop.remove_reader(master)
            except (ValueError, OSError):
                pass
            try:
                os.close(master)
            except OSError:
                pass
            if self.proc.returncode is None:
                try:
                    self.proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                pass


class DbusParser:
    """Parse dbus-monitor output: catch Connected / ServicesResolved on org.bluez.Device1."""
    WATCH = ("Connected", "ServicesResolved")
    RE_PATH = re.compile(r"path=([^;\s]+);")

    def __init__(self, cb):
        self.cb = cb
        self.path = None
        self.iface = None
        self.pending = None

    def feed(self, line):
        if line.startswith("signal "):
            m = self.RE_PATH.search(line)
            self.path = m.group(1) if m else None
            self.iface = None
            self.pending = None
            return
        if self.path is None:
            return
        s = line.strip()
        if self.pending and s.startswith("variant"):
            v = s[len("variant"):].strip()
            mb = re.match(r"boolean (true|false)", v)
            if mb and self.iface == "org.bluez.Device1":
                self.cb(self.path, self.pending, mb.group(1) == "true")
            self.pending = None
            return
        m = re.match(r'string "([^"]*)"$', s)
        if m:
            val = m.group(1)
            if val.startswith("org.") and self.pending is None:
                self.iface = val
            elif val in self.WATCH:
                self.pending = val
