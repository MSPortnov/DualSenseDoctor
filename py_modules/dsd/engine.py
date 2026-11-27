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
from . import report, stats
from .base import (BIND_MAX, BIND_STEP, BTMON_AFTER_RESUME_S, BTMON_ARM_MAX_S, DATA_DIR, DEFAULTS, EARLY_CONNECT_ATTEMPTS,
                   EARLY_CONNECT_DELAY, FIX_KEYS, HOT_CAP, KERN_RE, L2CAP_DELAY, L2CAP_MAX_SENDS, L2CAP_REPEAT_S, MAX_BTMON_FILES,
                   NOHID_TIMEOUT, RANGES, RESUME_WINDOW, STEAM_BIND_MIN_HOLD, STEAM_BIND_SETTLE, STEAM_HOLD, STEAM_HOLD_DELAY,
                   STEAM_PATH_WAIT, STEAM_WAIT, VERSION, decky, dev_path, first_hci, iso, log, mac_from_path, mono, norm_range, read,
                   run, tr, wall_of, which, write_sys)
from . import base
from .base import set_lang as _set_lang
from .btmon import HEX_RE, INJECT_SCRIPT, NOISE_RE, find_lost_sdp_request
from .store import Store
from .streams import DbusParser, LineStream

class Engine:
    def __init__(self):
        self.store = Store()
        self.cfg = self.store.settings
        _set_lang(self.cfg.get("lang"))
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
        self.ensure_adapter_awake(tr("после сна", "after sleep"))
        irq = read("/sys/power/pm_wakeup_irq").strip()
        self.elog("RESUME", "slept=%.0fs wake_sources=%s pm_wakeup_irq=%s" % (slept, srcs or "?", irq or "-"))

    async def _tick(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                self.check_resume()
                self.wake_snap = self.read_wakeup()
                if self.btmon_demand and mono() > self.btmon_arm_until:
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
    async def _hold_hid(self, s, hid):
        """Hides the controller from Steam while Steam is busy after wake (see STEAM_HOLD) and restores the driver after
        the session's "bind_at" seconds. The driver is restored in every case, even if the session dropped, the plugin is
        unloaded (the task is cancelled) or an error occurred; the restore is verified and retried."""
        cfg, mac = self.cfg, s["mac"]
        loop = asyncio.get_running_loop()
        unbound = False
        try:
            await asyncio.sleep(STEAM_HOLD_DELAY)
            if self.cur.get(mac) is not s or not cfg["steam_pickup"]:
                return
            if not s.get("after_resume"):
                return  # a controller switched on at the desktop is not delayed: Steam is not busy waking up
            if not await loop.run_in_executor(None, hid_mod.steam_running):
                return
            s["steam_hold"] = True  # a preventive step, not a fix: it is not counted in fixes_applied
            async with self.action_lock:
                unbound, steps = await loop.run_in_executor(None, hid_mod.sync_unbind, hid)
                if unbound:
                    self.detached.add(hid)
            self.ev(s, tr("драйвер отключён, пока Steam занят после пробуждения: %s", "driver detached while Steam is busy after wake: %s") % "; ".join(steps))
            if not unbound:
                return
            s["_hold"] = True
            release_at = max(s["bind_at"], mono() - s["_mono"] + STEAM_BIND_MIN_HOLD)
            while self.cur.get(mac) is s and mono() - s["_mono"] < release_at:
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("hold_hid: %s", e)
        finally:
            if unbound:
                try:
                    async with self.action_lock:
                        ok, steps = await loop.run_in_executor(None, hid_mod.sync_restore, hid)
                    s["_bind_el"] = round(mono() - s["_mono"], 2)
                    self.detached.discard(hid)
                    self.ev(s, tr("драйвер возвращён: %s", "driver restored: %s") % "; ".join(steps))
                    if not ok:
                        self.elog("WARN", tr("драйвер не вернулся: %s", "the driver did not come back: %s") % hid)
                        if not self.stopping and not hid_mod.device_present(hid):
                            # the link is gone: ask the controller to connect again (it does not always come back by itself)
                            await self.do_bt_connect(s, "after-restore")
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
        t0 = mono()
        t_seen = None
        try:
            while alive():
                await asyncio.sleep(0.5)
                seen, path = await loop.run_in_executor(None, hid_mod.steam_device_state, marks)
                if seen:
                    if t_seen is None:
                        t_seen = mono()
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
                            marks = await loop.run_in_executor(None, hid_mod.steam_log_marks)
                            t0 = mono()
                            t_seen = None
                            continue
                        s["steam_path"] = "evdev"
                        s["steam_seen_s"] = round(mono() - s["_mono"], 1)
                        self.ev(s, tr("Steam открыл контроллер неверно (evdev), исправить не удалось (rebinds=%d)", "Steam opened the controller the wrong way (evdev), could not fix it (rebinds=%d)") % rebinds)
                        self.store.save_sessions()
                        return
                    if path == "hidapi" or mono() - t_seen >= STEAM_PATH_WAIT:
                        s["steam_path"] = path
                        s["steam_seen_s"] = round(mono() - s["_mono"], 1)
                        self.ev(s, tr("Steam заметил контроллер (через %.1fс от подключения, режим %s)",
                              "Steam noticed the controller (%.1fs after connection, mode %s)") % (
                            s["steam_seen_s"], path or tr("не определён", "not determined")))
                        self.store.save_sessions()
                        return
                    continue
                if mono() - t0 >= STEAM_WAIT:
                    if not await loop.run_in_executor(None, hid_mod.steam_running):
                        self.ev(s, tr("Steam не запущен, проверка пропущена", "Steam is not running, check skipped"))
                        return
                    if cfg["steam_pickup"] and rebinds < 2:
                        rebinds += 1
                        if "steam_pickup" not in s["fixes_applied"]:
                            s["fixes_applied"].append("steam_pickup")
                        self.ev(s, tr("Steam не заметил контроллер за %.0fс -> rebind #%d", "Steam did not notice the controller within %.0fs -> rebind #%d") % (STEAM_WAIT, rebinds))
                        await self.do_rebind(s, hid)
                        t0 = mono()
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
            s["steam_done"] = True
            self.store.save_sessions()
            try:
                await self.maybe_tune(s)
            except Exception as e:
                log.error("tune: %s", e)

    async def do_rebind(self, s, hid):
        loop = asyncio.get_running_loop()
        async with self.action_lock:
            steps = await loop.run_in_executor(None, hid_mod.sync_rebind, hid)
            self.ev(s, "REBIND %s: %s" % (hid, "; ".join(steps)))
            await asyncio.sleep(0.5)

    async def do_bt_connect(self, s, n):
        t = mono()
        rc, out = await run(["busctl", "--timeout=4", "call", "org.bluez", dev_path(s["mac"], first_hci()),
                             "org.bluez.Device1", "Connect"], 7)
        self.ev(s, "early_connect #%s rc=%s (%.1fs) %s" % (n, rc, mono() - t, out.strip()[:160]))

    async def _l2cap_answer(self, s):
        """On wake the kernel drops the controller's L2CAP request (SDP), and about 27 s later the controller ends the link itself.
        We answer it "wait" (pending) directly through a raw HCI socket: L2CAP_MAX_SENDS times, L2CAP_REPEAT_S apart.
        (A "refuse" response makes the DualSense turn off, verified.)"""
        mac = s["mac"]
        alive = lambda: self.cur.get(mac) is s
        try:
            while mono() - s["_mono"] < L2CAP_DELAY:
                await asyncio.sleep(0.25)
                if not alive():
                    return
            handle = await self.acl_handle(mac)
            if handle is None:
                self.ev(s, tr("L2CAP: handle соединения не найден", "L2CAP: connection handle not found"))
                return
            ident, scid, src = find_lost_sdp_request(self.btmon_ring, s["_mono"] - 8)
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
        lang = _set_lang(lang)
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

    async def maybe_tune(self, s):
        """Self-tuning: Steam opened the controller wrongly (or missed it) after two wake-ups in a row with the same driver
        return time -> the time grows by BIND_STEP (up to BIND_MAX) and the user gets a notification."""
        if self.stopping or not self.cfg["steam_pickup"]:
            return
        cur = self.cfg["steam_bind_at"]
        if cur >= BIND_MAX or not stats.pickup_streak_bad(self.sessions, s, cur):
            return
        new = min(cur + BIND_STEP, BIND_MAX)
        self.cfg["steam_bind_at"] = new
        self.store.save_settings()
        txt = ("%g" % new).replace(".", ",") if base.LANG == "ru" else "%g" % new
        msg = tr("Steam дважды подряд неверно открыл контроллер после сна. Время возврата драйвера увеличено до %s с.",
                 "Steam opened the controller the wrong way twice in a row after wake. The driver return time was increased to %s s.") % txt
        self.elog("TUNE", "steam_bind_at %g -> %g (%s)" % (cur, new, msg))
        self.ev(s, "TUNE steam_bind_at %g -> %g" % (cur, new))
        if decky is not None:
            try:
                await decky.emit("dsd_tuned", new, msg)
            except Exception as e:
                log.error("emit: %s", e)

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
