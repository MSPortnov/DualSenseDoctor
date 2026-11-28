"""The Bluetooth engine: watches the controller's connections, applies the fixes, keeps sessions and statistics."""
import asyncio
import collections
import datetime
import glob
import json
import os
import re
import time

from . import hid as hid_mod
from . import i18n
from . import report, stats
from .config import (
    BTMON_AFTER_RESUME_S,
    BTMON_ARM_MAX_S,
    DATA_DIR,
    DEFAULTS,
    EARLY_CONNECT_ATTEMPTS,
    EARLY_CONNECT_DELAY,
    FIX_KEYS,
    HOT_CAP,
    log,
    MAX_BTMON_FILES,
    NOHID_TIMEOUT,
    norm_range,
    RANGES,
    RESUME_WINDOW,
    STEAM_BIND_SETTLE,
    STEAM_HOLD,
    VERSION,
    WAKE_SNAP_EVERY,
)
from .i18n import tr
from .actions import Actions
from .btmon import HEX_RE, NOISE_RE
from .store import atomic_write_json, Store
from .streams import DbusParser, LineStream
from .util import dev_path, first_hci, iso, mac_from_path, mono, read, run, wall_of, which, write_sys

# kernel log lines that are kept in the event log
KERN_RE = re.compile(r"bluetooth|hci\d|hid|playstation|uhid|PM: |btusb|btintel|"
                     r"usb \d|input:|wake|suspend|resume", re.I)


class Engine(Actions):
    def __init__(self):
        self.store = Store()
        self.store.ensure_history_version(VERSION)
        if not self.store.journal_enabled:
            self.store.clear_persisted_history()
            self.store.sessions.clear()
            self.store.next_id = 1
        self.cfg = self.store.settings
        i18n.set_lang(self.cfg.get("lang"))
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
        self.last_resume_mono = None   # monotonic moment of the last wake-up
        self.last_wake_sources = []
        self.last_slept = 0
        self.wake_snap = {}
        self._snap_at = 0.0     # monotonic moment of the next fallback refresh of wake_snap
        self._last_bt = None
        self._last_mt = None
        self.last_report = None
        self.streams = {}
        self.tasks = []
        self.dbus_missing = False
        self.action_lock = None
        self.q = None
        self.sup_tasks = {}
        self.bg = set()         # background tasks: held here so that they are neither garbage-collected nor left running on unload
        self.detached = set()   # HID devices whose driver this plugin has detached right now
        self.stopping = False
        self.pm_file = os.path.join(os.path.dirname(self.store.sessions_path), "adapter_pm.json")
        self.pm_orig = self.load_pm_orig()   # adapter power/control before the plugin set it to "on" (kept on disk: survives a crash)
        self.report_busy = False

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
        # the plugin was killed while a driver was detached: attach it back (nothing happens when all drivers are in place)
        try:
            for n, ok, steps in await asyncio.get_running_loop().run_in_executor(None, hid_mod.sync_orphans):
                self.elog("FIX", tr("контроллер без драйвера после перезапуска, драйвер возвращён: %s %s", "controller without a driver after a restart, driver restored: %s %s") % (n, "; ".join(steps)))
        except Exception as e:
            log.error("orphans: %s", e)

    def spawn(self, coro):
        """Start a background task and keep a reference to it (asyncio itself holds only a weak one)."""
        t = asyncio.ensure_future(coro)
        self.bg.add(t)
        t.add_done_callback(self.bg.discard)
        return t

    async def stop(self):
        self.stopping = True
        for t in self.tasks:
            t.cancel()
        for t in list(self.sup_tasks.values()):
            t.cancel()
        pending = list(self.bg)
        for t in pending:
            t.cancel()
        if pending:
            # a cancelled _hold_hid puts the driver back in its "finally"; wait for it
            await asyncio.gather(*pending, return_exceptions=True)
        for n in list(self.detached):  # safety net: whatever is still detached is attached now
            try:
                ok, steps = await asyncio.get_running_loop().run_in_executor(None, hid_mod.sync_restore, n)
                self.elog("FIX", tr("при выгрузке плагина драйвер возвращён: %s %s", "driver restored on unload: %s %s") % (n, "; ".join(steps)))
            except Exception as e:
                log.error("stop restore: %s", e)
        self.detached.clear()
        self.restore_adapter_pm(tr("выгрузка плагина", "plugin unloaded"))
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
                self.snap_wakeup()   # the exact "before sleep" counters, so that the wake source is found right
                self.arm_btmon("PrepareForSleep")
            return
        if self.dbus_parser:
            self.dbus_parser.feed(line)

    def arm_btmon(self, why):
        """Start btmon shortly before sleep. The controller's request that the kernel drops arrives about 2.5 s BEFORE the
        kernel prints "PM: suspend exit", so a trace started on resume would be too late: it has to be running already."""
        if not self.cfg["l2cap_answer"] or not which("btmon"):
            return
        self.btmon_arm_until = mono() + BTMON_ARM_MAX_S
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
            self.spawn(st.stop())
            del self.streams["btmon"]

    def profile(self):
        c = self.cfg
        fx = []
        for k in FIX_KEYS:
            if c[k]:
                if k == "l2cap_answer":
                    k += "@pending"
                elif k == "steam_pickup":
                    k += "@%g" % c["steam_bind_at"]
                fx.append(k)
        return "+".join(fx) or "none"

    # ------------------------------------------------------------ USB autosuspend of the adapter
    def adapter_pm_path(self):
        return os.path.realpath("/sys/class/bluetooth/%s/device/.." % first_hci()) + "/power/control"

    def ensure_adapter_awake(self, why):
        """Kernel 7.x enabled USB autosuspend of the adapter (power/control=auto), while on kernel 6.18 it was "on". Set it back to "on"."""
        if not self.cfg.get("early_connect"):
            return None   # the adapter stays as SteamOS has it; autosuspend is only turned off together with early connect
        try:
            p = self.adapter_pm_path()
            cur = read(p).strip()
            if cur and cur != "on":
                if self.pm_orig is None:
                    self.pm_orig = cur   # what to put back when early connect is turned off or the plugin is unloaded
                    self.save_pm_orig()
                write_sys(p, "on")
                self.elog("FIX", tr("автосон Bluetooth-адаптера отключён (%s): было %s -> on", "Bluetooth adapter autosuspend turned off (%s): was %s -> on") % (why, cur))
            return cur
        except Exception as e:
            log.error("ensure_adapter_awake: %s", e)
            return None

    def load_pm_orig(self):
        try:
            with open(self.pm_file) as f:
                v = json.load(f).get("orig")
        except (OSError, ValueError, AttributeError):
            return None
        return v if v == "auto" else None

    def save_pm_orig(self):
        try:
            if self.pm_orig is None:
                if os.path.exists(self.pm_file):
                    os.remove(self.pm_file)
            else:
                atomic_write_json(self.pm_file, {"orig": self.pm_orig})
        except OSError as e:
            log.error("adapter_pm file: %s", e)

    def restore_adapter_pm(self, why):
        """Put the adapter's autosuspend back to what it was before the plugin changed it."""
        if self.pm_orig is None:
            return
        orig, self.pm_orig = self.pm_orig, None
        self.save_pm_orig()
        try:
            write_sys(self.adapter_pm_path(), orig)
            self.elog("FIX", tr("автосон адаптера возвращён (%s): %s", "adapter autosuspend restored (%s): %s") % (why, orig))
        except Exception as e:
            log.error("autosuspend restore: %s", e)

    # ------------------------------------------------------------ stream handlers
    def on_kernel_line(self, line):
        if "PM: suspend entry" in line:
            self.arm_btmon("PM: suspend entry")
        if not KERN_RE.search(line):
            return
        now = mono()
        short = re.sub(r"^\S+\s+\S+\s+kernel:\s*", "", line)
        self.recent.append((now, "KERN", short))
        self.elog("KERN", short)
        if "unknown connection handle" in line:
            self.unknown_handle_ts.append(now)

    def on_bluez_line(self, line):
        if "Endpoint registered" in line:
            return
        now = mono()
        short = re.sub(r"^\S+\s+\S+\s+", "", line)
        self.recent.append((now, "BLUEZ", short))
        self.elog("BLUEZ", short)

    def on_btmon_line(self, line):
        if HEX_RE.match(line):
            return
        ts = mono()
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
            except OSError:
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
        self.last_resume_mono = mono()
        if self.btmon_demand:
            self.btmon_arm_until = mono() + BTMON_AFTER_RESUME_S
        new = self.read_wakeup()
        srcs = []
        for k, (name, dev, ev, wk) in new.items():
            old = self.wake_snap.get(k)
            if old and (ev > old[2] or wk > old[3]):
                srcs.append("%s[%s]" % (name, dev))
        self.last_wake_sources = srcs
        self.last_slept = slept
        self.wake_snap, self._snap_at = new, mono() + WAKE_SNAP_EVERY
        self.ensure_adapter_awake(tr("после сна", "after sleep"))
        irq = read("/sys/power/pm_wakeup_irq").strip()
        self.elog("RESUME", "slept=%.0fs wake_sources=%s pm_wakeup_irq=%s" % (slept, srcs or "?", irq or "-"))

    def snap_wakeup(self):
        self.wake_snap = self.read_wakeup()
        self._snap_at = mono() + WAKE_SNAP_EVERY

    async def tick_once(self):
        """Once a second: two clock reads (resume detection) and one stat call (report request). The wake-source counters
        (dozens of sysfs files) are read only before sleep and as a rare fallback, not every second."""
        self.check_resume()
        self.check_report_request()
        if mono() >= self._snap_at:
            self.snap_wakeup()
        if self.btmon_demand and mono() > self.btmon_arm_until:
            self.disarm_btmon()
        if self.dbus_missing:
            await self._poll_conn()

    async def _tick(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("tick: %s", e)

    def check_report_request(self):
        """A report asked for by "deploy.sh fetch" (a file in the user's home): collect it like the button does."""
        if self.report_busy or self.stopping or not report.take_request():
            return
        self.report_busy = True
        self.elog("REPORT", tr("отчёт запрошен командой deploy.sh fetch", "report requested by deploy.sh fetch"))
        self.spawn(self._auto_report())

    async def _auto_report(self):
        try:
            await self.build_report()
        except Exception as e:
            log.error("auto report: %s", e)
            self.elog("REPORT", "failed: %s" % e)
        finally:
            self.report_busy = False

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
        rel = round(mono() - s["_mono"], 2)
        s["events"].append([rel, text])
        self.elog("SESS%d" % s["id"], "+%.2fs %s" % (rel, text))

    def start_session(self, mac):
        if mac in self.cur:
            return
        self.check_resume()
        now = time.time()   # wall clock: only for the date shown in the report
        nm = mono()         # intervals are measured with the monotonic clock
        since = (nm - self.last_resume_mono) if self.last_resume_mono is not None else None
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
            "unknown_handle": sum(1 for t in self.unknown_handle_ts if nm - 15 <= t <= nm + 1),
            "t_hid": None, "t_usable": None, "problems": [],
            "fixes_applied": [], "events": [], "steam_path": None,
            "_mono": nm, "bind_at": self.cfg["steam_bind_at"],
            "_steam_marks": hid_mod.steam_log_marks(),
        }
        self.cur[mac] = s
        self.sessions.append(s)
        self.hot[sid] = [(t, l) for (t, l) in self.btmon_ring if t >= nm - 6]
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
        next_ec = EARLY_CONNECT_DELAY
        try:
            while s["result"] == "RUNNING":
                await asyncio.sleep(0.2)
                el = time.monotonic() - t0
                c = self.ctrl.get(mac)
                if not c or not c["connected"]:
                    return
                hid = hid_mod.find_hid(mac)
                if hid is None:
                    if (cfg["early_connect"] and ec_count < EARLY_CONNECT_ATTEMPTS
                            and el >= next_ec and (ec_task is None or ec_task.done())):
                        ec_count += 1
                        next_ec = el + 1.0
                        if "early_connect" not in s["fixes_applied"]:
                            s["fixes_applied"].append("early_connect")
                        ec_task = self.spawn(self.do_bt_connect(s, ec_count))
                    if el >= NOHID_TIMEOUT:
                        self.finalize(s, "NOHID", tr("подключён, но HID не создан за %.0fс", "connected, but no HID was created within %.0fs") % el)
                        return
                    continue
                if s["t_hid"] is None:
                    s["t_hid"] = round(el, 2)
                    self.spawn(self._steam_watch(s, hid))
                    if STEAM_HOLD:
                        self.spawn(self._hold_hid(s, hid))
                    next_check = el + 3.0
                    self.ev(s, tr("HID появился: %s", "HID appeared: %s") % hid)
                if el < next_check:
                    continue
                if s.get("_hold") or (s.get("_bind_el") is not None and el < s["_bind_el"] + STEAM_BIND_SETTLE):
                    continue  # the driver is detached right now or was just restored: too early to check HID
                h = await asyncio.get_running_loop().run_in_executor(None, hid_mod.hid_info, hid)
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
        healthy = result in ("OK", "FIXED")
        if healthy and s.get("steam_path") == "evdev":
            result = "NOSTEAM"
        s["result"] = result
        s["t_final"] = round(mono() - s["_mono"], 2)
        if note:
            s["note"] = note
        self.ev(s, "RESULT %s %s" % (result, note))
        self.store.save_sessions()
        self.spawn(self._post_final(s))
        if (healthy and self.cfg["l2cap_answer"]
                and "early_connect" in s["fixes_applied"]):
            self.spawn(self._l2cap_answer(s))

    async def _post_final(self, s):
        try:
            await asyncio.sleep(3.0)
            if s["result"] not in ("OK", "FIXED"):
                s["diag"] = await self.collect_diag(s["mac"])
            lo, hi = s["_mono"] - 15, s["_mono"] + s.get("t_final", 0) + 3
            s["kernel"] = ["%s [%s] %s" % (iso(wall_of(t)), k, l) for (t, k, l) in self.recent if lo <= t <= hi][-40:]
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

    # ------------------------------------------------------------ diagnostics
    async def collect_diag(self, mac=None):
        loop = asyncio.get_running_loop()
        lines = []
        for n in hid_mod.list_ds_hid():
            info = await loop.run_in_executor(None, hid_mod.hid_info, n)
            lines.append("HID " + json.dumps(info, ensure_ascii=False))
            holders = await loop.run_in_executor(None, hid_mod.hidraw_holders, info["hidraw"])
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
        lang = i18n.set_lang(lang)
        if self.cfg.get("lang") != lang:
            self.cfg["lang"] = lang
            self.store.save_settings()
        return lang

    def public_settings(self):
        return {k: v for k, v in self.cfg.items() if not k.startswith("_")}

    async def set_setting(self, key, value):
        if key not in DEFAULTS or key.startswith("_"):
            return self.public_settings()
        if key in RANGES:
            v = norm_range(key, value)
            if v is None:
                return self.public_settings()
            self.cfg[key] = v
            self.elog("SETTING", "%s=%s" % (key, v))
            self.store.save_settings()
            return self.public_settings()
        if not isinstance(DEFAULTS[key], bool):
            return self.public_settings()  # only the on/off switches are changed here (the language has its own call)
        self.cfg[key] = bool(value)
        if key == "action_journal":
            self.store.set_journal_enabled(self.cfg[key])
            self.store.save_settings()
            self.elog("SETTING", "%s=%s" % (key, self.cfg[key]))
            return self.public_settings()
        self.elog("SETTING", "%s=%s" % (key, value))
        self.store.save_settings()
        if key == "btmon":
            self.update_btmon()
        if key == "early_connect":   # the adapter is kept awake together with early connect
            if value:
                self.ensure_adapter_awake(tr("настройка", "setting"))
            else:
                self.restore_adapter_pm(tr("настройка", "setting"))
        return self.public_settings()

    def dump_btmon(self, s):
        if not self.store.journal_enabled:
            return
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
                    f.write("%s %s\n" % (datetime.datetime.fromtimestamp(wall_of(ts)).strftime("%H:%M:%S.%f")[:-3], line))
            files = sorted(glob.glob(os.path.join(DATA_DIR, "btmon_*.txt")))
            for old in files[:-MAX_BTMON_FILES]:
                os.remove(old)
        except Exception as e:
            log.error("dump_btmon: %s", e)

    def compute_stats(self):
        return stats.compute_stats(self.sessions)

    def fix_log(self, limit=3):
        return stats.fix_log(self.sessions, limit)

    async def build_report(self):
        return await report.build_report(self)

    async def get_state(self):
        return {"version": VERSION, "settings": self.public_settings(), "fix_log": self.fix_log()}
