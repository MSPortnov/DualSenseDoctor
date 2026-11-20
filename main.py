# DualSense Doctor - backend (Decky Loader, runs as root)
# Two parts: the Bluetooth engine (Engine) and the backend of the Lab (per-game launch options).
# Python standard library only. External tools: bluetoothctl, journalctl,
# dbus-monitor (recommended), btmon (recommended), hciconfig, busctl, lsusb.
import asyncio
import base64
import collections
import datetime
import glob
import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import time
from pathlib import Path

try:
    import decky
except Exception:  # running outside Decky (tests)
    decky = None


def _d(name, default):
    return getattr(decky, name, default) if decky else default


SETTINGS_DIR = _d("DECKY_PLUGIN_SETTINGS_DIR", "/tmp/dsd/settings")
DATA_DIR = _d("DECKY_PLUGIN_RUNTIME_DIR", "/tmp/dsd/data")
PLUGIN_LOG_DIR = _d("DECKY_PLUGIN_LOG_DIR", "/tmp/dsd/log")
DECKY_USER = _d("DECKY_USER", "deck")
USER_HOME = _d("DECKY_USER_HOME", "/home/deck")
log = decky.logger if decky else logging.getLogger("dsd")

VERSION = "0.9.1"
HID_DIR = "/sys/bus/hid/devices"
HOT_CAP = 8000            # max. btmon lines per session
NOHID_TIMEOUT = 25.0      # connected, but the HID device never appeared
RESUME_WINDOW = 40.0      # a connection within N s after resume counts as "after sleep"
MAX_SESSIONS = 300
MAX_BTMON_FILES = 40
# Steam opens the controller the wrong way (A and B swapped, default lightbar) if it first sees it in the first seconds
# after wake, while it is still busy itself. The HID device is needed right away (otherwise the PC drops the link at the 4th second), so it is created
# early, but its driver is detached for that time: the /dev/hidrawN and input nodes disappear while the link stays. The driver is restored
# STEAM_BIND_AT seconds after the connection, when Steam is calm: it sees the controller for the first time and opens it the right way.
# On-demand HCI trace (btmon) for the L2CAP answer: started shortly before sleep, stopped after wake.
BTMON_AFTER_RESUME_S = 90  # how long the trace keeps running after wake
BTMON_ARM_MAX_S = 300      # ... and at most this long when the sleep never happens
STEAM_HOLD = True
STEAM_HOLD_DELAY = 0.15   # pause after HID appears before the driver is detached, s
STEAM_BIND_AT = 5.0       # when to restore the driver (seconds after connection)
STEAM_BIND_SETTLE = 1.2   # how long to wait after the driver is restored before checking HID, s
STEAM_PATH_WAIT = 4.0     # how long to wait for Steam's log entry about how it opened the controller
STEAM_WAIT = 8.0          # how long to wait for Steam to notice the controller before doing a rebind
EARLY_CONNECT_ATTEMPTS = 4  # how many times "Early connect" tries to open the HID channel
L2CAP_DELAY = 3.0         # how many seconds after the connection to start answering the lost request
# "Pending" response to the controller's lost request: L2CAP_MAX_SENDS times, L2CAP_REPEAT_S seconds apart, no further repeats.
# Every response puts a "hcon ... sent 0 < count 1" line in the kernel log, so no extra responses are sent.
# L2CAP_MAX_SENDS = 0 means "unlimited". If the controller drops the link 1-5 minutes after wake,
# there are too few responses: increase L2CAP_MAX_SENDS.
L2CAP_REPEAT_S = 15.0
L2CAP_MAX_SENDS = 4

FIX_KEYS = ["early_connect", "l2cap_answer", "steam_pickup"]
# Language of the plugin's messages and reports (ru / en). Set by the interface ("Language" setting), stored in settings.json.
LANG = "en"


def tr(ru, en):
    return ru if LANG == "ru" else en


DEFAULTS = {
    "lang": "en",
    "early_connect": True,
    "early_connect_delay": 1.0,
    "l2cap_answer": True,
    "steam_pickup": True,
    "no_autosuspend": True,
    "btmon": False,
}
RANGES = {"early_connect_delay": (0.3, 3.5)}

# btmon prints a received packet as "> ACL: Handle 256 ..." (before the link type is known) or "> BR-ACL: ...", a sent one with "<"
ACL_RX_RE = re.compile(r"^\s*>\s+(?:BR-|LE-)?ACL\b")
ACL_TX_RE = re.compile(r"^\s*<\s+(?:BR-|LE-)?ACL\b")
NOISE_RE = re.compile(r"^\s*[<>]\s+(?:BR-|LE-)?ACL\b|Number of Completed|Num handles|^\s+Count:|Latency:|#\d+: len|Channel: \d+ len")
HEX_RE = re.compile(r"^\s+[0-9a-f]{2}( [0-9a-f]{2}){3,}")
INJECT_SCRIPT = r"""
import socket, struct, sys
dev, handle, result, ident, scid = [int(x) for x in sys.argv[1:6]]
sig = struct.pack("<BBHHHHH", 3, ident, 8, 0, scid, result, 0)
l2 = struct.pack("<HH", len(sig), 1) + sig
acl = struct.pack("<HH", handle | (2 << 12), len(l2)) + l2
pkt = b"\x02" + acl
s = socket.socket(31, socket.SOCK_RAW, 1)
s.bind((dev,))
print("sent", s.send(pkt), "bytes", pkt.hex())
"""
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
KERN_RE = re.compile(r"bluetooth|hci\d|hid|playstation|uhid|PM: |btusb|btintel|"
                     r"usb \d|input:|wake|suspend|resume", re.I)


# ---------------------------------------------------------------- utils
def make_env():
    env = dict(os.environ)
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)
    env["LANG"] = "C"
    env["SYSTEMD_COLORS"] = "0"
    env["SYSTEMD_PAGER"] = ""
    env["NO_COLOR"] = "1"
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "_MEIPASS2"):
        env.pop(k, None)
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
    return env


ENV = make_env()


def which(name):
    return shutil.which(name, path=ENV["PATH"])


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")


def read(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def write_sys(path, value):
    with open(path, "w") as f:
        f.write(value)


async def run(cmd, timeout=10):
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=ENV)
    except FileNotFoundError:
        return 127, "command not found: %s" % cmd[0]
    except Exception as e:
        return -1, "spawn error: %s" % e
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, ANSI.sub("", out.decode(errors="replace"))
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        return -9, "timeout after %ss" % timeout


def mac_from_path(path):
    m = re.search(r"dev_([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})$", path or "")
    return m.group(1).replace("_", ":").upper() if m else None


def dev_path(mac, hci="hci0"):
    return "/org/bluez/%s/dev_%s" % (hci, mac.replace(":", "_"))


def first_hci():
    names = sorted(glob.glob("/sys/class/bluetooth/hci[0-9]*"))
    names = [os.path.basename(n) for n in names if ":" not in os.path.basename(n)]
    return names[0] if names else "hci0"


# ---------------------------------------------------------------- HID / sysfs
def list_ds_hid():
    try:
        names = os.listdir(HID_DIR)
    except Exception:
        return []
    return sorted(n for n in names if ":054C:" in n.upper() and n.startswith("0005:"))


def hid_uniq(n):
    m = re.search(r"^HID_UNIQ=(.*)$", read(os.path.join(HID_DIR, n, "uevent")), re.M)
    return m.group(1).strip() if m and m.group(1).strip() else None


def hid_info(n):
    base = os.path.join(HID_DIR, n)
    drv = None
    try:
        drv = os.path.basename(os.readlink(os.path.join(base, "driver")))
    except OSError:
        pass
    inputs = [read(p + "/name").strip() for p in glob.glob(os.path.join(base, "input", "input*"))]
    leds = []
    for l in glob.glob("/sys/class/leds/*"):
        try:
            if n in os.path.realpath(l):
                leds.append(os.path.basename(l))
        except Exception:
            pass
    hidraw = [os.path.basename(x) for x in glob.glob(os.path.join(base, "hidraw", "hidraw*"))]
    motion = any("Motion Sensors" in i for i in inputs)
    touch = any("Touchpad" in i for i in inputs)
    problems = []
    if drv != "playstation":
        problems.append("driver=%s" % drv)
    if not motion:
        problems.append("no_motion")
    if not leds:
        problems.append("no_leds")
    return {"dev": n, "driver": drv, "inputs": inputs, "motion": motion, "touchpad": touch,
            "leds": leds, "hidraw": hidraw, "uniq": hid_uniq(n),
            "healthy": not problems, "problems": problems}


COVER_MAX_BYTES = 1_500_000
COVER_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def steam_root():
    for r in (os.path.join(USER_HOME, ".local/share/Steam"), os.path.join(USER_HOME, ".steam/steam")):
        if os.path.isdir(os.path.join(r, "appcache", "librarycache")) or os.path.isdir(os.path.join(r, "userdata")):
            return r
    return None


def find_cover_file(appid):
    """Path to the game's portrait cover (or None). Order: custom cover from userdata/*/config/grid
    (the only one for non-Steam games, overrides the stock one for Steam games), the stock cover from Steam's library cache,
    a wide picture from grid (cropped to the center in the card)."""
    root = steam_root()
    if not root:
        return None
    aid = int(appid) & 0xFFFFFFFF
    grids = glob.glob(os.path.join(root, "userdata", "*", "config", "grid"))
    for ext in COVER_EXTS:
        for g in grids:
            p = os.path.join(g, "%dp%s" % (aid, ext))
            if os.path.isfile(p):
                return p
    base = os.path.join(root, "appcache", "librarycache")
    found = []
    for dirpath, _dirs, files in os.walk(os.path.join(base, str(aid))):
        for f in files:
            if f.lower().startswith("library_600x900") and os.path.splitext(f)[1].lower() in COVER_MIME:
                found.append(os.path.join(dirpath, f))
    for ext in COVER_EXTS:  # old cache layout: files lie in the root
        p = os.path.join(base, "%d_library_600x900%s" % (aid, ext))
        if os.path.isfile(p):
            found.append(p)

    def rank(path):
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        return (0 if stem == "library_600x900" else 1, len(stem))

    for p in sorted(found, key=rank):
        try:
            if os.path.getsize(p) <= COVER_MAX_BYTES:
                return p
        except OSError:
            continue
    for ext in COVER_EXTS:
        for g in grids:
            p = os.path.join(g, "%d%s" % (aid, ext))
            if os.path.isfile(p):
                return p
    return None


def read_cover(appid):
    """data: URI of the cover, or an empty string."""
    p = find_cover_file(appid)
    if not p:
        return ""
    try:
        if os.path.getsize(p) > COVER_MAX_BYTES * 2:
            return ""
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return "data:%s;base64,%s" % (COVER_MIME.get(os.path.splitext(p)[1].lower(), "image/png"), base64.b64encode(raw).decode())


def find_hid(mac):
    for n in list_ds_hid():
        u = hid_uniq(n)
        if u is None or u.upper() == mac.upper():
            return n
    return None


def hidraw_holders(names):
    out = []
    targets = {"/dev/" + n for n in names}
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(fd) in targets:
                pid = fd.split("/")[2]
                out.append("%s:%s" % (pid, read("/proc/%s/comm" % pid).strip()))
        except Exception:
            pass
    return sorted(set(out))


STEAM_LOGS = ("controller.txt", "controller_ui.txt")
STEAM_MARKERS = ("Local Device Found", "device opened for index", "connected, configuring")


def steam_log_marks():
    """Remember the sizes of Steam's logs so that only new entries are seen later."""
    marks = {}
    for d in (USER_HOME + "/.local/share/Steam/logs", USER_HOME + "/.steam/steam/logs"):
        for name in STEAM_LOGS:
            path = os.path.realpath(os.path.join(d, name))
            try:
                marks[path] = os.path.getsize(path)
            except OSError:
                pass
    return marks


STEAM_BAD_SERIAL = "Invalid or missing unit serial number"


def steam_device_state(marks):
    """What Steam wrote to its logs after the mark. Returns (noticed, way):
    "hidapi" - the normal way (serial number read, lightbar and Steam Input layout work);
    "evdev"  - Steam opened the controller as an ordinary kernel device: serial number not read, default lightbar,
               buttons A and B swapped;
    None     - the way is not clear yet (Steam has not finished writing the lines)."""
    seen, path = False, None
    for fpath, size in marks.items():
        try:
            with open(fpath, "rb") as f:
                f.seek(0, 2)
                end = f.tell()
                f.seek(size if end >= size else 0)  # the log may have been reset
                data = f.read().decode(errors="replace")
        except OSError:
            continue
        if any(k in data for k in STEAM_MARKERS):
            seen = True
        i = data.rfind("Local Device Found")
        if i >= 0:
            tail = data[i:]
            if STEAM_BAD_SERIAL in tail:
                path = "evdev"
            elif "HIDAPI driver" in tail and path != "evdev":
                path = "hidapi"
    return seen, path


def steam_running():
    for c in glob.glob("/proc/[0-9]*/comm"):
        if read(c).strip() == "steam":
            return True
    return False


def sync_unbind(n):
    """Detach the current driver from the HID device (the hidraw and input nodes disappear, the link stays)."""
    try:
        drv = os.path.basename(os.readlink(os.path.join(HID_DIR, n, "driver")))
    except OSError:
        return False, ["no driver bound"]
    try:
        write_sys("/sys/bus/hid/drivers/%s/unbind" % drv, n)
        return True, ["unbind(%s) ok" % drv]
    except OSError as e:
        return False, ["unbind(%s) err %s" % (drv, e)]


def sync_bind(n):
    """Attach the playstation driver (fallback: drivers_probe)."""
    try:
        write_sys("/sys/bus/hid/drivers/playstation/bind", n)
        return ["bind(playstation) ok"]
    except OSError as e:
        steps = ["bind(playstation) err %s" % e]
        try:
            write_sys("/sys/bus/hid/drivers_probe", n)
            steps.append("drivers_probe ok")
        except OSError as e2:
            steps.append("drivers_probe err %s" % e2)
        return steps


def sync_rebind(n):
    """Unbind from the current driver, then bind to playstation."""
    steps = []
    drv = None
    try:
        drv = os.path.basename(os.readlink(os.path.join(HID_DIR, n, "driver")))
    except OSError:
        pass
    if drv:
        try:
            write_sys("/sys/bus/hid/drivers/%s/unbind" % drv, n)
            steps.append("unbind(%s) ok" % drv)
        except OSError as e:
            steps.append("unbind(%s) err %s" % (drv, e))
    else:
        steps.append("no driver bound")
    time.sleep(0.4)
    try:
        write_sys("/sys/bus/hid/drivers/playstation/bind", n)
        steps.append("bind(playstation) ok")
    except OSError as e:
        steps.append("bind(playstation) err %s" % e)
        try:
            write_sys("/sys/bus/hid/drivers_probe", n)
            steps.append("drivers_probe ok")
        except OSError as e2:
            steps.append("drivers_probe err %s" % e2)
    return steps


# ---------------------------------------------------------------- streams
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
        except Exception:
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
                except Exception:
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
            except Exception:
                pass
            try:
                os.close(master)
            except Exception:
                pass
            if self.proc.returncode is None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
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


# ---------------------------------------------------------------- store
class Store:
    def __init__(self):
        for d in (SETTINGS_DIR, DATA_DIR, PLUGIN_LOG_DIR):
            os.makedirs(d, exist_ok=True)
        self.settings_path = os.path.join(SETTINGS_DIR, "settings.json")
        self.sessions_path = os.path.join(DATA_DIR, "sessions.json")
        self.events_path = os.path.join(DATA_DIR, "events.log")
        self.settings = json.loads(json.dumps(DEFAULTS))
        try:
            with open(self.settings_path) as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in DEFAULTS:
                    self.settings[k] = v
        except Exception:
            pass
        try:
            with open(self.sessions_path) as f:
                self.sessions = json.load(f)
        except Exception:
            self.sessions = []
        self.next_id = max([s.get("id", 0) for s in self.sessions] + [0]) + 1

    def save_settings(self):
        try:
            with open(self.settings_path, "w") as f:
                json.dump(self.settings, f, indent=1)
        except Exception as e:
            log.error("save settings: %s", e)

    def save_sessions(self):
        if len(self.sessions) > MAX_SESSIONS:
            del self.sessions[:-MAX_SESSIONS]
        clean = [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.sessions]
        try:
            tmp = self.sessions_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(clean, f, ensure_ascii=False)
            os.replace(tmp, self.sessions_path)
        except Exception as e:
            log.error("save sessions: %s", e)

    def elog(self, kind, msg):
        try:
            if os.path.exists(self.events_path) and os.path.getsize(self.events_path) > 4_000_000:
                os.replace(self.events_path, self.events_path + ".1")
            with open(self.events_path, "a") as f:
                f.write("%s [%s] %s\n" % (iso(time.time()), kind, msg))
        except Exception as e:
            log.error("elog: %s", e)


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self):
        self.store = Store()
        self.cfg = self.store.settings
        global LANG
        LANG = self.cfg.get("lang") if self.cfg.get("lang") in ("ru", "en") else "en"
        self.sessions = self.store.sessions
        self.ctrl = {}
        self.cur = {}
        self.hot = {}
        self.btmon_ring = collections.deque(maxlen=30000)
        self.btmon_demand = False   # btmon was started by the plugin (not by the "HCI trace" switch)
        self.btmon_arm_until = 0.0
        self._login1 = False
        self.dbus_parser = None
        self.recent = collections.deque(maxlen=500)
        self.unknown_handle_ts = collections.deque(maxlen=50)
        self.last_resume_ts = None
        self.last_wake_sources = []
        self.last_slept = 0
        self.wake_snap = {}
        self._last_bt = None
        self._last_mt = None
        self.last_report = None
        self.streams = {}
        self.tasks = []
        self.dbus_missing = False
        self.action_lock = None
        self.q = None
        self.sup_tasks = {}

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        self.q = asyncio.Queue()
        self.action_lock = asyncio.Lock()
        self.elog("START", "plugin v%s settings=%s" % (VERSION, json.dumps(
            {k: v for k, v in self.cfg.items() if not k.startswith("_")})))
        self.store.save_settings()  # rewrites the file with the current keys only
        self.ensure_adapter_awake(tr("старт", "start"))
        await self.refresh_controllers()
        self.streams["kernel"] = LineStream(
            "kernel", ["journalctl", "-k", "-f", "-n", "0", "--no-pager", "-o", "short-iso-precise"],
            self.on_kernel_line)
        self.streams["bluez"] = LineStream(
            "bluez", ["journalctl", "-u", "bluetooth", "-f", "-n", "0", "--no-pager", "-o", "short-iso-precise"],
            self.on_bluez_line)
        if which("dbus-monitor"):
            self.dbus_parser = DbusParser(lambda p, k, v: self.q.put_nowait(("prop", (p, k, v))))
            self.streams["dbus"] = LineStream(
                "dbus", ["dbus-monitor", "--system",
                         "type='signal',interface='org.freedesktop.DBus.Properties',"
                         "member='PropertiesChanged',path_namespace='/org/bluez'",
                         "type='signal',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'"],
                self.feed_dbus, use_pty=True)
        else:
            self.dbus_missing = True
            self.elog("WARN", tr("dbus-monitor не найден, использую опрос bluetoothctl раз в секунду", "dbus-monitor not found, polling bluetoothctl once a second"))
        for st in self.streams.values():
            st.start()
        self.update_btmon()
        self.tasks = [asyncio.ensure_future(self._consume()), asyncio.ensure_future(self._tick())]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for t in list(self.sup_tasks.values()):
            t.cancel()
        for st in self.streams.values():
            await st.stop()
        self.elog("STOP", "plugin unloaded")

    def elog(self, kind, msg):
        self.store.elog(kind, msg)

    def feed_dbus(self, line):
        """Lines of dbus-monitor: the Bluetooth parser gets everything except logind's sleep signal, which arms the HCI trace."""
        if line and not line[0].isspace():  # a new D-Bus message starts
            self._login1 = line.startswith("signal ") and "member=PrepareForSleep" in line
            if self._login1:
                return
        elif self._login1:
            if "boolean true" in line:
                self.arm_btmon("PrepareForSleep")
            return
        if self.dbus_parser:
            self.dbus_parser.feed(line)

    def arm_btmon(self, why):
        """Start btmon shortly before sleep. The controller's request that the kernel drops arrives about 2.5 s BEFORE the
        kernel prints "PM: suspend exit", so a trace started on resume would be too late: it has to be running already."""
        if not self.cfg["l2cap_answer"] or not which("btmon"):
            return
        self.btmon_arm_until = time.time() + BTMON_ARM_MAX_S
        if "btmon" in self.streams:
            return
        self.btmon_demand = True
        self.update_btmon()
        self.elog("HCI", tr("трасса btmon включена перед сном (%s)", "btmon trace started before sleep (%s)") % why)

    def disarm_btmon(self):
        if not self.btmon_demand:
            return
        self.btmon_demand = False
        self.update_btmon()
        self.elog("HCI", tr("трасса btmon остановлена", "btmon trace stopped"))

    def update_btmon(self):
        want = (bool(self.cfg["btmon"]) or self.btmon_demand) and bool(which("btmon"))
        st = self.streams.get("btmon")
        if want and not st:
            st = LineStream("btmon", ["btmon"], self.on_btmon_line, use_pty=True)
            self.streams["btmon"] = st
            st.start()
        elif not want and st:
            asyncio.ensure_future(st.stop())
            del self.streams["btmon"]

    def profile(self):
        c = self.cfg
        fx = []
        for k in FIX_KEYS:
            if c[k]:
                if k == "early_connect":
                    k += "@%g" % c["early_connect_delay"]
                elif k == "l2cap_answer":
                    k += "@pending"
                elif k == "steam_pickup":
                    k += "@%g" % STEAM_WAIT
                fx.append(k)
        return "+".join(fx) or "none"

    # ------------------------------------------------------------ USB autosuspend of the adapter
    def adapter_pm_path(self):
        return os.path.realpath("/sys/class/bluetooth/%s/device/.." % first_hci()) + "/power/control"

    def ensure_adapter_awake(self, why):
        """Kernel 7.x enabled USB autosuspend of the adapter (power/control=auto), while on kernel 6.18 it was "on". Set it back to "on"."""
        if not self.cfg.get("no_autosuspend"):
            return None
        try:
            p = self.adapter_pm_path()
            cur = read(p).strip()
            if cur and cur != "on":
                write_sys(p, "on")
                self.elog("FIX", tr("автосон Bluetooth-адаптера отключён (%s): было %s -> on", "Bluetooth adapter autosuspend turned off (%s): was %s -> on") % (why, cur))
            return cur
        except Exception as e:
            log.error("ensure_adapter_awake: %s", e)
            return None

    # ------------------------------------------------------------ stream handlers
    def on_kernel_line(self, line):
        if "PM: suspend entry" in line:
            self.arm_btmon("PM: suspend entry")
        if not KERN_RE.search(line):
            return
        now = time.time()
        short = re.sub(r"^\S+\s+\S+\s+kernel:\s*", "", line)
        self.recent.append((now, "KERN", short))
        self.elog("KERN", short)
        if "unknown connection handle" in line:
            self.unknown_handle_ts.append(now)

    def on_bluez_line(self, line):
        if "Endpoint registered" in line:
            return
        now = time.time()
        short = re.sub(r"^\S+\s+\S+\s+", "", line)
        self.recent.append((now, "BLUEZ", short))
        self.elog("BLUEZ", short)

    def on_btmon_line(self, line):
        if HEX_RE.match(line):
            return
        ts = time.time()
        self.btmon_ring.append((ts, line))
        for buf in self.hot.values():
            if len(buf) < HOT_CAP:
                buf.append((ts, line))

    # ------------------------------------------------------------ resume / wakeup
    def read_wakeup(self):
        snap = {}
        for d in glob.glob("/sys/class/wakeup/wakeup*"):
            name = read(d + "/name").strip() or os.path.basename(d)
            try:
                dev = os.path.basename(os.path.realpath(d + "/device"))
            except Exception:
                dev = ""
            try:
                ev = int(read(d + "/event_count", "0") or 0)
                wk = int(read(d + "/wakeup_count", "0") or 0)
            except ValueError:
                ev = wk = 0
            snap[os.path.basename(d)] = (name, dev, ev, wk)
        return snap

    def check_resume(self):
        bt = time.clock_gettime(time.CLOCK_BOOTTIME)
        mt = time.monotonic()
        if self._last_bt is not None:
            drift = (bt - self._last_bt) - (mt - self._last_mt)
            if drift > 3.0:
                self.on_resume(time.time(), drift)
        self._last_bt, self._last_mt = bt, mt

    def on_resume(self, now, slept):
        self.last_resume_ts = now
        if self.btmon_demand:
            self.btmon_arm_until = now + BTMON_AFTER_RESUME_S
        new = self.read_wakeup()
        srcs = []
        for k, (name, dev, ev, wk) in new.items():
            old = self.wake_snap.get(k)
            if old and (ev > old[2] or wk > old[3]):
                srcs.append("%s[%s]" % (name, dev))
        self.last_wake_sources = srcs
        self.last_slept = slept
        self.ensure_adapter_awake(tr("после сна", "after sleep"))
        irq = read("/sys/power/pm_wakeup_irq").strip()
        self.elog("RESUME", "slept=%.0fs wake_sources=%s pm_wakeup_irq=%s" % (slept, srcs or "?", irq or "-"))

    async def _tick(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                self.check_resume()
                self.wake_snap = self.read_wakeup()
                if self.btmon_demand and time.time() > self.btmon_arm_until:
                    self.disarm_btmon()
                if self.dbus_missing:
                    await self._poll_conn()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("tick: %s", e)

    async def _poll_conn(self):
        for mac, c in list(self.ctrl.items()):
            if not c.get("is_ds"):
                continue
            rc, out = await run(["bluetoothctl", "info", mac], 5)
            conn = "Connected: yes" in out
            if conn != c["connected"]:
                self.q.put_nowait(("prop", (dev_path(mac), "Connected", conn)))

    # ------------------------------------------------------------ controllers
    async def probe(self, mac, name=None):
        rc, out = await run(["bluetoothctl", "info", mac], 8)
        nm = re.search(r"^\s*Name: (.*)$", out, re.M)
        mod = re.search(r"Modalias: (\S+)", out)
        name = (nm.group(1).strip() if nm else name) or mac
        is_ds = bool((mod and "v054C" in mod.group(1).upper()) or
                     re.search(r"DualSense|DualShock|Wireless Controller", name, re.I))
        c = {"name": name, "is_ds": is_ds, "connected": "Connected: yes" in out,
             "wake_allowed": "WakeAllowed: yes" in out}
        self.ctrl[mac] = c
        return c

    async def refresh_controllers(self):
        rc, out = await run(["bluetoothctl", "devices", "Paired"], 8)
        for m in re.finditer(r"Device ([0-9A-Fa-f:]{17}) (.*)", out):
            await self.probe(m.group(1).upper(), m.group(2).strip())

    def ds_macs(self):
        return [m for m, c in self.ctrl.items() if c.get("is_ds")]

    # ------------------------------------------------------------ event consumer
    async def _consume(self):
        while True:
            kind, args = await self.q.get()
            try:
                if kind == "prop":
                    await self._handle_prop(*args)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("consume: %s", e)

    async def _handle_prop(self, path, prop, val):
        mac = mac_from_path(path)
        if not mac:
            return
        c = self.ctrl.get(mac)
        if c is None:
            c = await self.probe(mac)
        if not c["is_ds"]:
            return
        if prop == "Connected":
            c["connected"] = bool(val)
            self.elog("BT", "%s Connected=%s" % (mac, val))
            if val:
                self.start_session(mac)
            else:
                await self.end_session(mac)
        elif prop == "ServicesResolved":
            s = self.cur.get(mac)
            if s:
                self.ev(s, "ServicesResolved=%s" % val)

    # ------------------------------------------------------------ sessions
    def ev(self, s, text):
        rel = round(time.time() - s["t0"], 2)
        s["events"].append([rel, text])
        self.elog("SESS%d" % s["id"], "+%.2fs %s" % (rel, text))

    def start_session(self, mac):
        if mac in self.cur:
            return
        self.check_resume()
        now = time.time()
        since = (now - self.last_resume_ts) if self.last_resume_ts else None
        after = since is not None and 0 <= since <= RESUME_WINDOW
        sid = self.store.next_id
        self.store.next_id += 1
        usb_pm = self.ensure_adapter_awake(tr("подключение", "connection"))
        if usb_pm is None:
            usb_pm = read(self.adapter_pm_path()).strip() if os.path.exists("/sys/class/bluetooth") else None
        s = {
            "usb_pm_before": usb_pm,
            "id": sid, "mac": mac, "name": self.ctrl[mac]["name"],
            "t0": now, "iso": iso(now), "result": "RUNNING", "profile": self.profile(),
            "after_resume": after, "since_resume_s": round(since, 2) if after else None,
            "wake_sources": self.last_wake_sources if after else [],
            "slept_s": round(self.last_slept) if after else None,
            "unknown_handle": sum(1 for t in self.unknown_handle_ts if now - 15 <= t <= now + 1),
            "t_hid": None, "t_usable": None, "problems": [],
            "fixes_applied": [], "events": [], "steam_path": None,
            "_mono": time.monotonic(),
            "_steam_marks": steam_log_marks(),
        }
        self.cur[mac] = s
        self.sessions.append(s)
        self.hot[sid] = [(t, l) for (t, l) in self.btmon_ring if t >= now - 6]
        self.ev(s, "CONNECTED after_resume=%s since_resume=%s unknown_handle=%d profile=%s" % (
            after, s["since_resume_s"], s["unknown_handle"], s["profile"]))
        self.store.save_sessions()
        self.sup_tasks[sid] = asyncio.ensure_future(self._supervise(s))

    async def _supervise(self, s):
        cfg = self.cfg
        mac = s["mac"]
        t0 = s["_mono"]
        next_check = None
        ec_count = 0
        ec_task = None
        next_ec = cfg["early_connect_delay"]
        try:
            while s["result"] == "RUNNING":
                await asyncio.sleep(0.2)
                el = time.monotonic() - t0
                c = self.ctrl.get(mac)
                if not c or not c["connected"]:
                    return
                hid = find_hid(mac)
                if hid is None:
                    if (cfg["early_connect"] and ec_count < EARLY_CONNECT_ATTEMPTS
                            and el >= next_ec and (ec_task is None or ec_task.done())):
                        ec_count += 1
                        next_ec = el + 1.0
                        if "early_connect" not in s["fixes_applied"]:
                            s["fixes_applied"].append("early_connect")
                        ec_task = asyncio.ensure_future(self.do_bt_connect(s, ec_count))
                    if el >= NOHID_TIMEOUT:
                        self.finalize(s, "NOHID", tr("подключён, но HID не создан за %.0fс", "connected, but no HID was created within %.0fs") % el)
                        return
                    continue
                if s["t_hid"] is None:
                    s["t_hid"] = round(el, 2)
                    asyncio.ensure_future(self._steam_watch(s, hid))
                    if STEAM_HOLD:
                        asyncio.ensure_future(self._hold_hid(s, hid))
                    next_check = el + 3.0
                    self.ev(s, tr("HID появился: %s", "HID appeared: %s") % hid)
                if el < next_check:
                    continue
                if s.get("_hold") or (s.get("_bind_el") is not None and el < s["_bind_el"] + STEAM_BIND_SETTLE):
                    continue  # the driver is detached right now or was just restored: too early to check HID
                h = await asyncio.get_running_loop().run_in_executor(None, hid_info, hid)
                s["problems"] = h["problems"]
                s["driver"] = h["driver"]
                if h["healthy"]:
                    s["t_usable"] = round(el, 2)
                    res = "FIXED" if any(k in s["fixes_applied"] for k in ("early_connect", "steam_pickup")) else "OK"
                    self.finalize(s, res, "driver=%s inputs=%s leds=%s" % (h["driver"], h["inputs"], h["leds"]))
                    return
                self.ev(s, tr("HID нездоров: %s", "HID is unhealthy: %s") % h["problems"])
                self.finalize(s, "NOGYRO", "problems=%s" % h["problems"])
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("supervise: %s", e)
            self.ev(s, "ERROR supervise: %s" % e)

    def finalize(self, s, result, note=""):
        if s["result"] != "RUNNING":
            return
        now = time.time()
        healthy = result in ("OK", "FIXED")
        if healthy and s.get("steam_path") == "evdev":
            result = "NOSTEAM"
        s["result"] = result
        s["t_final"] = round(now - s["t0"], 2)
        if note:
            s["note"] = note
        self.ev(s, "RESULT %s %s" % (result, note))
        self.store.save_sessions()
        asyncio.ensure_future(self._post_final(s))
        if (healthy and self.cfg["l2cap_answer"]
                and "early_connect" in s["fixes_applied"]):
            asyncio.ensure_future(self._l2cap_answer(s))

    async def acl_handle(self, mac):
        if which("hcitool"):
            rc, out = await run(["hcitool", "con"], 5)
            m = re.search(r"ACL %s handle (\d+)" % re.escape(mac), out, re.I)
            if m:
                return int(m.group(1))
        hs = []
        for d in glob.glob("/sys/class/bluetooth/hci*:*"):
            try:
                hs.append(int(os.path.basename(d).split(":")[1]))
            except ValueError:
                pass
        return hs[0] if len(hs) == 1 else None

    def find_lost_sdp_request(self, since):
        """From the HCI trace: the controller's SDP request (PSM 1) that the kernel dropped.
        Returns (ident, scid, "trace"), or the usual first-request values (1, 64, "guess") when the trace has no such request."""
        direction, ident, psm, scid = None, None, None, None
        for ts, line in self.btmon_ring:
            if ts < since:
                continue
            if line.lstrip()[:1] in (">", "<", "@", "="):  # a new packet or event starts: remember its direction
                direction = "rx" if ACL_RX_RE.match(line) else "tx" if ACL_TX_RE.match(line) else None
            m = re.search(r"L2CAP: Connection Request \(0x02\) ident (\d+)", line)
            if m:
                ident, psm, scid = int(m.group(1)), None, None
                continue
            if ident is not None:
                mp = re.search(r"PSM: (\d+)", line)
                if mp:
                    psm = int(mp.group(1))
                ms = re.search(r"Source CID: (\d+)", line)
                if ms:
                    scid = int(ms.group(1))
                if psm == 1 and scid is not None:
                    if direction == "rx":
                        return ident, scid, "trace"
                    ident, psm, scid = None, None, None  # our own request (TX): not the lost one
        return 1, 64, "guess"

    async def _post_final(self, s):
        try:
            await asyncio.sleep(3.0)
            if s["result"] not in ("OK", "FIXED"):
                s["diag"] = await self.collect_diag(s["mac"])
            lo, hi = s["t0"] - 15, s["t0"] + s.get("t_final", 0) + 3
            s["kernel"] = ["%s [%s] %s" % (iso(t), k, l) for (t, k, l) in self.recent if lo <= t <= hi][-40:]
            self.dump_btmon(s)
            self.hot.pop(s["id"], None)
            self.store.save_sessions()
        except Exception as e:
            log.error("post_final: %s", e)

    async def end_session(self, mac):
        s = self.cur.pop(mac, None)
        if not s:
            return
        self.ev(s, "DISCONNECTED")
        if s["result"] == "RUNNING":
            # the controller left before the check was finished (switched off, or the PC went to sleep): no verdict
            s["result"] = "ABORTED"
            self.hot.pop(s["id"], None)
        self.store.save_sessions()

    # ------------------------------------------------------------ actions
    async def _hold_hid(self, s, hid):
        """Hides the controller from Steam while Steam is busy after wake (see STEAM_HOLD) and restores the driver at
        STEAM_BIND_AT. The driver is restored in every case, even if the session dropped or an error occurred."""
        cfg, mac = self.cfg, s["mac"]
        loop = asyncio.get_running_loop()
        unbound = False
        try:
            await asyncio.sleep(STEAM_HOLD_DELAY)
            if self.cur.get(mac) is not s or not cfg["steam_pickup"]:
                return
            if not s.get("after_resume"):
                return  # a controller switched on at the desktop is not delayed: Steam is not busy waking up
            if not await loop.run_in_executor(None, steam_running):
                return
            s["steam_hold"] = True  # a preventive step, not a fix: it is not counted in fixes_applied
            async with self.action_lock:
                unbound, steps = await loop.run_in_executor(None, sync_unbind, hid)
            self.ev(s, tr("драйвер отключён, пока Steam занят после пробуждения: %s", "driver detached while Steam is busy after wake: %s") % "; ".join(steps))
            if not unbound:
                return
            s["_hold"] = True
            while self.cur.get(mac) is s and time.monotonic() - s["_mono"] < STEAM_BIND_AT:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("hold_hid: %s", e)
        finally:
            if unbound:
                try:
                    async with self.action_lock:
                        steps = await loop.run_in_executor(None, sync_bind, hid)
                    s["_bind_el"] = round(time.monotonic() - s["_mono"], 2)
                    self.ev(s, tr("драйвер возвращён: %s", "driver restored: %s") % "; ".join(steps))
                except Exception as e:
                    log.error("hold_hid bind: %s", e)
                finally:
                    s["_hold"] = False

    async def _steam_watch(self, s, hid):
        """Steam may miss the device appearing during wake or open it the wrong way
        (buttons A and B swapped, default lightbar). In both cases we do a rebind: Steam notices a fresh udev event again."""
        cfg, mac = self.cfg, s["mac"]
        alive = lambda: self.cur.get(mac) is s
        marks = s.get("_steam_marks") or {}
        loop = asyncio.get_running_loop()
        rebinds = 0
        t0 = time.time()
        t_seen = None
        try:
            while alive():
                await asyncio.sleep(0.5)
                seen, path = await loop.run_in_executor(None, steam_device_state, marks)
                if seen:
                    if t_seen is None:
                        t_seen = time.time()
                    if path == "evdev":
                        # rebind only after the HID check has a result, otherwise it would disturb that check
                        while alive() and s["result"] == "RUNNING":
                            await asyncio.sleep(0.25)
                        if not alive():
                            return
                        if cfg["steam_pickup"] and rebinds < 2:
                            rebinds += 1
                            if "steam_pickup" not in s["fixes_applied"]:
                                s["fixes_applied"].append("steam_pickup")
                            self.ev(s, tr("Steam открыл контроллер неверно (серийник не прочитан: подсветка заводская, A и B перепутаны) -> rebind #%d",
                                          "Steam opened the controller the wrong way (serial number not read: default lightbar, "
                                          "A and B swapped) -> rebind #%d") % rebinds)
                            await self.do_rebind(s, hid)
                            marks = await loop.run_in_executor(None, steam_log_marks)
                            t0 = time.time()
                            t_seen = None
                            continue
                        s["steam_path"] = "evdev"
                        s["steam_seen_s"] = round(time.time() - s["t0"], 1)
                        self.ev(s, tr("Steam открыл контроллер неверно (evdev), исправить не удалось (rebinds=%d)", "Steam opened the controller the wrong way (evdev), could not fix it (rebinds=%d)") % rebinds)
                        self.store.save_sessions()
                        return
                    if path == "hidapi" or time.time() - t_seen >= STEAM_PATH_WAIT:
                        s["steam_path"] = path
                        s["steam_seen_s"] = round(time.time() - s["t0"], 1)
                        self.ev(s, tr("Steam заметил контроллер (через %.1fс от подключения, режим %s)",
                              "Steam noticed the controller (%.1fs after connection, mode %s)") % (
                            s["steam_seen_s"], path or tr("не определён", "not determined")))
                        self.store.save_sessions()
                        return
                    continue
                if time.time() - t0 >= STEAM_WAIT:
                    if not await loop.run_in_executor(None, steam_running):
                        self.ev(s, tr("Steam не запущен, проверка пропущена", "Steam is not running, check skipped"))
                        return
                    if cfg["steam_pickup"] and rebinds < 2:
                        rebinds += 1
                        if "steam_pickup" not in s["fixes_applied"]:
                            s["fixes_applied"].append("steam_pickup")
                        self.ev(s, tr("Steam не заметил контроллер за %.0fс -> rebind #%d", "Steam did not notice the controller within %.0fs -> rebind #%d") % (STEAM_WAIT, rebinds))
                        await self.do_rebind(s, hid)
                        t0 = time.time()
                        continue
                    s["steam_seen_s"] = None
                    self.ev(s, tr("Steam НЕ заметил контроллер за %.0fс (rebinds=%d)", "Steam did NOT notice the controller within %.0fs (rebinds=%d)") % (STEAM_WAIT, rebinds))
                    self.store.save_sessions()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("steam_watch: %s", e)
        finally:
            if s["result"] in ("OK", "FIXED") and s.get("steam_path") == "evdev":
                s["result"] = "NOSTEAM"
            elif "steam_pickup" in s["fixes_applied"] and s["result"] == "OK":
                s["result"] = "FIXED"
            self.store.save_sessions()

    async def do_rebind(self, s, hid):
        loop = asyncio.get_running_loop()
        async with self.action_lock:
            steps = await loop.run_in_executor(None, sync_rebind, hid)
            self.ev(s, "REBIND %s: %s" % (hid, "; ".join(steps)))
            await asyncio.sleep(0.5)

    async def do_bt_connect(self, s, n):
        t = time.time()
        rc, out = await run(["busctl", "--timeout=4", "call", "org.bluez", dev_path(s["mac"], first_hci()),
                             "org.bluez.Device1", "Connect"], 7)
        self.ev(s, "early_connect #%d rc=%s (%.1fs) %s" % (n, rc, time.time() - t, out.strip()[:160]))

    async def _l2cap_answer(self, s):
        """On wake the kernel drops the controller's L2CAP request (SDP), and about 27 s later the controller ends the link itself.
        We answer it "wait" (pending) directly through a raw HCI socket: L2CAP_MAX_SENDS times, L2CAP_REPEAT_S apart.
        (A "refuse" response makes the DualSense turn off, verified.)"""
        mac = s["mac"]
        alive = lambda: self.cur.get(mac) is s
        try:
            while time.time() - s["t0"] < L2CAP_DELAY:
                await asyncio.sleep(0.25)
                if not alive():
                    return
            handle = await self.acl_handle(mac)
            if handle is None:
                self.ev(s, tr("L2CAP: handle соединения не найден", "L2CAP: connection handle not found"))
                return
            ident, scid, src = self.find_lost_sdp_request(s["t0"] - 8)
            s["l2cap_params"] = src
            dev = int(re.sub(r"\D", "", first_hci()) or 0)
            n = 0
            while alive() and (not L2CAP_MAX_SENDS or n < L2CAP_MAX_SENDS):
                rc, out = await run(["python3", "-c", INJECT_SCRIPT, str(dev), str(handle), "1",
                                     str(ident), str(scid)], 6)
                if L2CAP_MAX_SENDS or n == 0 or rc != 0 or n % 20 == 0:
                    self.ev(s, "L2CAP pending #%d handle=%d ident=%d scid=%d (%s) rc=%s %s" % (
                        n + 1, handle, ident, scid,
                        tr("из трассы", "from the trace") if src == "trace" else tr("угадано: в трассе запроса нет", "guessed: no request in the trace"),
                        rc, out.strip()[:120]))
                if rc != 0:
                    return
                # only a response built from the real request counts as a fix: a guessed one cannot be checked
                if n == 0 and src == "trace" and "l2cap_answer" not in s["fixes_applied"]:
                    s["fixes_applied"].append("l2cap_answer")
                n += 1
                if L2CAP_MAX_SENDS and n >= L2CAP_MAX_SENDS:
                    break
                for _ in range(int(L2CAP_REPEAT_S / 0.25)):
                    await asyncio.sleep(0.25)
                    if not alive():
                        return
            if L2CAP_MAX_SENDS and alive():
                self.ev(s, tr("L2CAP: отправлено %d ответов, дальше не повторяю", "L2CAP: sent %d responses, not repeating further") % n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("l2cap_answer: %s", e)

    # ------------------------------------------------------------ diagnostics
    async def collect_diag(self, mac=None):
        loop = asyncio.get_running_loop()
        lines = []
        for n in list_ds_hid():
            info = await loop.run_in_executor(None, hid_info, n)
            lines.append("HID " + json.dumps(info, ensure_ascii=False))
            holders = await loop.run_in_executor(None, hidraw_holders, info["hidraw"])
            lines.append("hidraw holders: %s" % holders)
        block = read("/proc/bus/input/devices")
        for b in block.split("\n\n"):
            if "054C" in b.upper() or "DualSense" in b:
                lines.append(b.strip())
        lines.append("leds: %s" % [os.path.basename(x) for x in glob.glob("/sys/class/leds/*")
                                    if "0005:054C" in os.path.realpath(x)])
        if mac:
            rc, out = await run(["bluetoothctl", "info", mac], 6)
            lines.append(out.strip())
        return "\n".join(lines)[:6000]

    # ------------------------------------------------------------ API
    async def set_lang(self, lang):
        global LANG
        LANG = "ru" if lang == "ru" else "en"
        if self.cfg.get("lang") != LANG:
            self.cfg["lang"] = LANG
            self.store.save_settings()
        return LANG

    def public_settings(self):
        return {k: v for k, v in self.cfg.items() if not k.startswith("_")}

    async def set_setting(self, key, value):
        if key not in DEFAULTS or key.startswith("_"):
            return self.public_settings()
        d = DEFAULTS[key]
        if isinstance(d, bool):
            value = bool(value)
        else:
            lo, hi = RANGES[key]
            value = min(max(float(value), lo), hi)
            if isinstance(d, int):
                value = int(round(value))
        self.cfg[key] = value
        self.elog("SETTING", "%s=%s" % (key, value))
        self.store.save_settings()
        if key == "btmon":
            self.update_btmon()
        if key == "no_autosuspend":
            if value:
                self.ensure_adapter_awake(tr("настройка", "setting"))
            else:
                try:
                    write_sys(self.adapter_pm_path(), "auto")
                    self.elog("FIX", tr("автосон адаптера возвращён: auto", "adapter autosuspend restored: auto"))
                except Exception as e:
                    log.error("autosuspend restore: %s", e)
        return self.public_settings()

    def dump_btmon(self, s):
        ok = s["result"] in ("OK", "FIXED")
        buf = self.hot.get(s["id"], [])
        if not buf:
            return
        limit = 2500 if ok else HOT_CAP
        path = os.path.join(DATA_DIR, "btmon_%04d_%s.txt" % (s["id"], s["result"]))
        try:
            with open(path, "w") as f:
                f.write("# session %d result=%s start=%s after_resume=%s\n" % (s["id"], s["result"], s["iso"], s["after_resume"]))
                for ts, line in [x for x in buf if not NOISE_RE.search(x[1])][:limit]:
                    f.write("%s %s\n" % (datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3], line))
            files = sorted(glob.glob(os.path.join(DATA_DIR, "btmon_*.txt")))
            for old in files[:-MAX_BTMON_FILES]:
                os.remove(old)
        except Exception as e:
            log.error("dump_btmon: %s", e)

    def compute_stats(self):
        """Per settings profile, how the connections after sleep ended: OK = nothing was needed, FIXED = something was
        actually corrected, the rest are failures. steam_rebind = how often a rebind was needed."""
        stats = {}
        for s in self.sessions:
            r = s.get("result")
            if not s.get("after_resume") or r in ("RUNNING", "ABORTED"):
                continue
            p = stats.setdefault(s["profile"], {"n": 0, "ok": 0, "fixed": 0, "failed": 0, "steam_rebind": 0, "_ttu": []})
            p["n"] += 1
            if "steam_pickup" in s.get("fixes_applied", []):
                p["steam_rebind"] += 1
            if r == "OK":
                p["ok"] += 1
            elif r == "FIXED":
                p["fixed"] += 1
            else:
                p["failed"] += 1
            if r in ("OK", "FIXED") and s.get("t_usable") is not None:
                p["_ttu"].append(s["t_usable"])
        for p in stats.values():
            t = p.pop("_ttu")
            p["avg_ttu"] = round(sum(t) / len(t), 1) if t else None
        return stats

    def fix_log(self, limit=3):
        """The latest events where the plugin intervened or could not help (for the "Fix log" block)."""
        out = []
        for s in reversed(self.sessions):
            r = s.get("result")
            if (r == "FIXED" and s.get("fixes_applied")) or r in ("NOHID", "NOGYRO", "NOSTEAM"):
                out.append({k: s.get(k) for k in ("id", "iso", "result", "t_hid",
                                                  "fixes_applied", "unknown_handle", "after_resume")})
            if len(out) >= limit:
                break
        return out

    async def get_state(self):
        return {"version": VERSION, "settings": self.public_settings(), "fix_log": self.fix_log()}

    async def clear_data(self):
        self.sessions.clear()
        self.store.save_sessions()
        for f in glob.glob(os.path.join(DATA_DIR, "btmon_*.txt")):
            os.remove(f)
        for f in (self.store.events_path, self.store.events_path + ".1"):
            if os.path.exists(f):
                os.remove(f)
        self.elog("CLEAR", tr("данные очищены", "data cleared"))
        return True

    # ------------------------------------------------------------ report
    async def journal_filtered(self, args, pattern, tail):
        rc, out = await run(["journalctl", "--no-pager", "-o", "short-iso-precise"] + args, 30)
        if rc != 0:
            return "rc=%s %s" % (rc, out[:200])
        lines = [l for l in out.splitlines() if re.search(pattern, l, re.I)]
        return "\n".join(lines[-tail:])

    async def sh(self, cmds, timeout=10):
        res = ""
        for cmd in cmds:
            rc, out = await run(cmd, timeout)
            res += "$ %s   (rc=%s)\n%s\n\n" % (" ".join(cmd), rc, out.strip())
        return res

    async def build_report(self):
        ts = time.strftime("%Y%m%d-%H%M%S")
        name = "dualsense-doctor-%s" % ts
        work = os.path.join(DATA_DIR, "report_tmp", name)
        os.makedirs(work, exist_ok=True)

        def wf(rel, text):
            p = os.path.join(work, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", errors="replace") as f:
                f.write(text)

        macs = self.ds_macs()
        wf("system.txt", await self.sh([["uname", "-a"], ["cat", "/etc/os-release"], ["cat", "/proc/cmdline"],
                                        ["cat", "/sys/power/mem_sleep"], ["bluetoothctl", "--version"],
                                        ["systemctl", "is-active", "bluetooth"], ["uptime"]]))
        wf("bluetooth.txt", await self.sh(
            [["lsusb"], ["lsusb", "-t"], ["hciconfig", "-a"], ["bluetoothctl", "show"],
             ["bluetoothctl", "devices", "Paired"], ["btmgmt", "info"], ["rfkill", "list"]] +
            [["bluetoothctl", "info", m] for m in macs] + [["hcitool", "con"]]))
        hci = first_hci()
        usbdev = os.path.realpath("/sys/class/bluetooth/%s/device/.." % hci)
        up = "usb device: %s\n" % usbdev
        for f in ("power/control", "power/wakeup", "power/autosuspend_delay_ms", "power/runtime_status"):
            up += "%s = %s\n" % (f, read(os.path.join(usbdev, f)).strip())
        wf("usb_power.txt", up)
        wk = ""
        for k, (nm, dev, ev, wc) in sorted(self.read_wakeup().items()):
            wk += "%s name=%s device=%s event_count=%s wakeup_count=%s\n" % (k, nm, dev, ev, wc)
        wf("wakeup_sources.txt", wk)
        params = ""
        for mod in ("btusb", "bluetooth", "btintel"):
            for f in sorted(glob.glob("/sys/module/%s/parameters/*" % mod)):
                params += "%s.%s = %s\n" % (mod, os.path.basename(f), read(f).strip())
        for f in ("/etc/bluetooth/main.conf", "/etc/bluetooth/input.conf"):
            lines = [l for l in read(f).splitlines() if l.strip() and not l.strip().startswith("#")]
            params += tr("\n## %s (без комментариев)\n%s\n", "\n## %s (without comments)\n%s\n") % (f, "\n".join(lines))
        wf("bt_params.txt", params)
        wf("hid_now.txt", await self.collect_diag(macs[0] if macs else None))
        pat_k = r"bluetooth|hci\d|hid|playstation|uhid|PM:|btusb|btintel|usb|input:|wake|suspend|resume|ACPI"
        wf("journal_kernel_boot.txt", await self.journal_filtered(["-k", "-b"], pat_k, 6000))
        wf("journal_kernel_prev_boot.txt", await self.journal_filtered(["-k", "-b", "-1"], pat_k, 6000))
        wf("journal_bluetooth_boot.txt", await self.journal_filtered(["-u", "bluetooth", "-b"], r"^(?!.*Endpoint registered)", 1500))
        wf("journal_sleep_boot.txt", await self.journal_filtered(["-b"], r"PM: (suspend|hibernation)|systemd-sleep|Waking up|sleep\.target|Entering sleep|Finished.*sleep", 400))
        seen = set()
        for d in (USER_HOME + "/.local/share/Steam/logs", USER_HOME + "/.steam/steam/logs"):
            for f in glob.glob(d + "/*ontroller*") + glob.glob(d + "/*luetooth*"):
                rp = os.path.realpath(f)
                if rp in seen or not os.path.isfile(rp):
                    continue
                seen.add(rp)
                wf("steam_logs/" + os.path.basename(f), "".join(read(rp).splitlines(True)[-500:]))
        for f in (self.store.events_path, self.store.events_path + ".1", self.store.sessions_path,
                  self.store.settings_path):
            if os.path.exists(f):
                shutil.copy(f, os.path.join(work, os.path.basename(f)))
        for f in glob.glob(os.path.join(DATA_DIR, "btmon_*.txt")):
            os.makedirs(os.path.join(work, "btmon"), exist_ok=True)
            shutil.copy(f, os.path.join(work, "btmon", os.path.basename(f)))
        for f in glob.glob(os.path.join(PLUGIN_LOG_DIR, "*.log")):
            shutil.copy(f, os.path.join(work, "plugin_" + os.path.basename(f)))

        # a single text file for sending quickly
        summ = ["# DualSense Doctor %s report %s" % (VERSION, ts), "profile now: " + self.profile(),
                "settings: " + json.dumps(self.public_settings()), "",
                "## stats " + tr("(эпизоды после сна)", "(episodes after sleep)"), json.dumps(self.compute_stats(), ensure_ascii=False, indent=1), "",
                "## sessions " + tr("(последние 40)", "(last 40)")]
        for s in self.sessions[-40:]:
            summ.append(json.dumps({k: v for k, v in s.items() if not k.startswith("_")}, ensure_ascii=False))
        for rel in ("system.txt", "bluetooth.txt", "usb_power.txt", "bt_params.txt", "hid_now.txt", "journal_sleep_boot.txt"):
            summ += ["", "## " + rel, read(os.path.join(work, rel))]
        for f in sorted(glob.glob(os.path.join(work, "steam_logs", "*"))):
            summ += ["", "## steam log %s " % os.path.basename(f) + tr("(последние 150 строк)", "(last 150 lines)")] + read(f).splitlines()[-150:]
        ev = read(self.store.events_path).splitlines()
        summ += ["", "## events.log " + tr("(последние 1500 строк)", "(last 1500 lines)")] + ev[-1500:]
        bt = sorted(glob.glob(os.path.join(DATA_DIR, "btmon_*.txt")))
        bad = [f for f in bt if not re.search(r"_(OK|FIXED|END)\.txt$", f)][-2:]
        okf = [f for f in bt if f.endswith("_FIXED.txt")][-1:] + [f for f in bt if f.endswith("_OK.txt")][-1:]
        endf = [f for f in bt if f.endswith("_END.txt")][-3:]
        for f in endf:
            summ += ["", "## btmon %s " % os.path.basename(f) + tr("(разрыв, без потока отчётов)", "(drop, without the report stream)")] + read(f).splitlines()[-450:]
        for f in bad + okf:
            summ += ["", "## btmon %s " % os.path.basename(f) + tr("(первые 450 строк без потока отчётов)", "(first 450 lines without the report stream)")] + read(f).splitlines()[:450]
        summary_text = "\n".join(summ)[:1_800_000]
        wf("summary.txt", summary_text)

        out_dir = USER_HOME if os.path.isdir(USER_HOME) else DATA_DIR
        tar_path = os.path.join(out_dir, name + ".tar.gz")
        sum_path = os.path.join(out_dir, name + "-summary.txt")
        rc, out = await run(["tar", "-czf", tar_path, "-C", os.path.dirname(work), name], 60)
        if rc != 0:
            self.elog("REPORT", "tar failed: %s" % out[:200])
        shutil.copy(os.path.join(work, "summary.txt"), sum_path)
        shutil.rmtree(os.path.join(DATA_DIR, "report_tmp"), ignore_errors=True)
        for p in (tar_path, sum_path):
            try:
                shutil.chown(p, user=DECKY_USER)
            except Exception:
                pass
        self.last_report = {"tar": tar_path, "summary": sum_path, "size_kb": round(os.path.getsize(tar_path) / 1024) if os.path.exists(tar_path) else 0}
        self.elog("REPORT", json.dumps(self.last_report))
        return self.last_report


# ---------------------------------------------------------------------------------------------------
# Lab: the Proton list, cover lookup, reading Steam files.
# The plugin runs as root, so the home directory is taken from USER_HOME (the Decky user).
# ---------------------------------------------------------------------------------------------------
LAB_FILE = Path(SETTINGS_DIR) / "lab.json"
FX_HOME = Path(USER_HOME)
FX_STEAM_ROOTS = [FX_HOME / ".steam" / "steam", FX_HOME / ".local" / "share" / "Steam"]


def _fx_parse_vdf(text: str) -> dict:
    """Minimal Valve KeyValues parser: enough for config.vdf and compatibilitytool.vdf."""
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    pos = 0

    def parse_obj() -> dict:
        nonlocal pos
        obj: dict = {}
        while pos < len(toks):
            s, brace = toks[pos]
            if brace == "}":
                pos += 1
                return obj
            if brace == "{":
                pos += 1
                continue
            key = s
            pos += 1
            if pos >= len(toks):
                break
            s2, brace2 = toks[pos]
            if brace2 == "{":
                pos += 1
                obj[key] = parse_obj()
            else:
                obj[key] = s2
                pos += 1
        return obj

    return parse_obj()


def _fx_ci_get(d, key: str):
    if not isinstance(d, dict):
        return None
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


def _fx_valve_tool_name(folder: str):
    """Map a Valve Proton folder name in steamapps/common to Steam's internal tool name."""
    low = folder.lower()
    if low == "proton - experimental":
        return "proton_experimental"
    if low == "proton hotfix":
        return "proton_hotfix"
    m = re.match(r"^proton (\d+)\.(\d+)", low)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2))
    if major >= 7 or (major == 5 and minor == 0):
        return f"proton_{major}"
    return f"proton_{major}{minor}"


def _fx_library_paths() -> list:
    paths, seen = [], set()

    def add(p: Path):
        if str(p) not in seen:
            seen.add(str(p))
            paths.append(p)

    for root in FX_STEAM_ROOTS:
        add(root)
        lf = root / "steamapps" / "libraryfolders.vdf"
        if lf.is_file():
            try:
                node = _fx_ci_get(_fx_parse_vdf(lf.read_text(encoding="utf-8", errors="replace")), "libraryfolders")
                if isinstance(node, dict):
                    for v in node.values():
                        if isinstance(v, dict) and v.get("path"):
                            add(Path(v["path"]))
            except Exception as e:
                log.error(f"libraryfolders parse failed: {e}")
    return paths



_IP_CACHE = {"ts": 0.0, "ip": ""}


def _usable_ip(ip):
    return bool(ip) and not ip.startswith("127.") and not ip.startswith("169.254.") and ip != "0.0.0.0"


def _ip_via_default_route():
    """Address of the interface that holds the default route (/proc/net/route + SIOCGIFADDR): no commands needed."""
    import fcntl
    iface = ""
    with open("/proc/net/route") as f:
        next(f)
        for line in f:
            p = line.split()
            if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 2:
                iface = p[0]
                break
    if not iface:
        return ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))  # SIOCGIFADDR
        return socket.inet_ntoa(r[20:24])
    finally:
        s.close()


def _ip_via_udp_connect():
    """UDP connect sends nothing, it only makes the kernel pick the outgoing address."""
    for target in ("10.254.254.254", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 1))
            ip = s.getsockname()[0]
            if _usable_ip(ip):
                return ip
        except Exception:
            pass
        finally:
            s.close()
    return ""


def _ip_via_command():
    path = "/usr/bin:/bin:/usr/sbin:/sbin"
    ipc = shutil.which("ip", path=path)
    if ipc:
        out = subprocess.run([ipc, "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", out)
        if m and _usable_ip(m.group(1)):
            return m.group(1)
    hn = shutil.which("hostname", path=path)
    if hn:
        out = subprocess.run([hn, "-I"], capture_output=True, text=True, timeout=3).stdout.split()
        for x in out:
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", x) and _usable_ip(x):
                return x
    return ""


def _lan_ip():
    """LAN IPv4 address of this device (empty string if unknown). Cached for 30 s."""
    now = time.time()
    if _IP_CACHE["ip"] and now - _IP_CACHE["ts"] < 30:
        return _IP_CACHE["ip"]
    ip = ""
    for fn in (_ip_via_default_route, _ip_via_udp_connect, _ip_via_command):
        try:
            ip = fn()
        except Exception as e:
            log.info("lan ip: %s failed: %s", fn.__name__, e)
            ip = ""
        if _usable_ip(ip):
            break
        ip = ""
    _IP_CACHE.update(ts=now, ip=ip)
    return ip


ENGINE = None
START_ERROR = None


async def _start_engine():
    global ENGINE, START_ERROR
    try:
        eng = Engine()
        ENGINE = eng
        await eng.start()
        log.info("DualSense Doctor started")
    except Exception:
        import traceback
        START_ERROR = traceback.format_exc()
        log.error("DualSense Doctor start failed:\n%s", START_ERROR)


def _guard():
    if ENGINE is None or START_ERROR:
        return {"error": START_ERROR or "engine not created"}
    return None


def _require_engine():
    g = _guard()
    if g:
        raise RuntimeError(g["error"])


class Plugin:
    async def _main(self):
        # do not block _main: the engine starts in the background, errors go to the log and to the UI
        asyncio.ensure_future(_start_engine())

    async def _unload(self):
        if ENGINE:
            await ENGINE.stop()

    async def get_cover(self, appid):
        """Game cover for the Lab grid (data: URI), an empty string if there is no file."""
        try:
            return await asyncio.get_running_loop().run_in_executor(None, read_cover, int(appid))
        except Exception as e:
            log.error("get_cover(%s): %s", appid, e)
            return ""

    async def get_state(self):
        try:
            g = _guard()
            if g:
                return g
            st = await ENGINE.get_state()
            st["host_ip"] = _lan_ip()
            return st
        except Exception:
            import traceback
            tb = traceback.format_exc()
            log.error("get_state failed:\n%s", tb)
            return {"error": tb}

    async def set_lang(self, lang):
        """Language of messages and reports: the interface reports it on load and on change."""
        global LANG
        if ENGINE is None:
            LANG = "ru" if lang == "ru" else "en"
            return LANG
        return await ENGINE.set_lang(lang)

    async def set_setting(self, key, value):
        _require_engine()
        return await ENGINE.set_setting(key, value)

    async def build_report(self):
        _require_engine()
        return await ENGINE.build_report()

    async def clear_data(self):
        _require_engine()
        return await ENGINE.clear_data()

    # ------------------------------------------------------------------ Lab
    async def load_data(self) -> dict:
        """Return persisted per-game journal (or {} if none / unreadable)."""
        try:
            return json.loads(LAB_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as e:  # corrupt file: keep a copy, start clean
            log.error(f"load_data failed: {e}")
            try:
                LAB_FILE.replace(LAB_FILE.with_suffix(".bak"))
            except Exception:
                pass
            return {}

    async def save_data(self, data: dict) -> bool:
        try:
            LAB_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = LAB_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, LAB_FILE)
            return True
        except Exception as e:
            log.error(f"save_data failed: {e}")
            return False

    async def list_compat_tools(self) -> list:
        """Installed Proton builds: user-installed (GE etc.) plus Valve's own from steamapps/common."""
        out, seen = [], set()

        def add(name: str, display: str, kind: str):
            if name and name not in seen:
                seen.add(name)
                out.append({"name": name, "display": display, "kind": kind})

        for root in FX_STEAM_ROOTS:
            d = root / "compatibilitytools.d"
            if not d.is_dir():
                continue
            for sub in sorted(d.iterdir()):
                if not sub.is_dir():
                    continue
                name, display = sub.name, sub.name
                vdf = sub / "compatibilitytool.vdf"
                try:
                    if vdf.is_file():
                        tools = _fx_ci_get(_fx_parse_vdf(vdf.read_text(encoding="utf-8", errors="replace")), "compatibilitytools")
                        tools = _fx_ci_get(tools, "compat_tools")
                        if isinstance(tools, dict) and tools:
                            name = next(iter(tools))
                            display = _fx_ci_get(tools[name], "display_name") or name
                except Exception as e:
                    log.error(f"compat vdf parse failed for {sub}: {e}")
                if re.search("proton", name, re.I):  # skip runtimes such as scout_ldlp
                    add(name, display, "custom")

        for lib in _fx_library_paths():
            common = lib / "steamapps" / "common"
            if not common.is_dir():
                continue
            for sub in sorted(common.iterdir()):
                tool = _fx_valve_tool_name(sub.name)
                if tool and (sub / "proton").is_file():
                    add(tool, sub.name, "valve")
        return out

    async def get_compat_tool(self, appid: int) -> str:
        """Tool mapped to the app in config.vdf (falls back to the global default mapping)."""
        for root in FX_STEAM_ROOTS:
            cfg = root / "config" / "config.vdf"
            if not cfg.is_file():
                continue
            try:
                node = _fx_parse_vdf(cfg.read_text(encoding="utf-8", errors="replace"))
                for part in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
                    node = _fx_ci_get(node, part)
                    if node is None:
                        break
                if isinstance(node, dict):
                    entry = node.get(str(appid)) or node.get("0")
                    if isinstance(entry, dict):
                        return entry.get("name", "") or ""
                return ""
            except Exception as e:
                log.error(f"get_compat_tool failed: {e}")
        return ""
