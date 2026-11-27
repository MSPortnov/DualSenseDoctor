"""Actions of the engine on the system: hide/restore the driver, watch how Steam opened the controller, early connect,
the L2CAP answer, self-tuning. A mixin: it uses the state of Engine (cfg, cur, ev, spawn ...)."""
import asyncio
import glob
import os
import re

from . import hid as hid_mod
from . import i18n, stats
from .btmon import INJECT_SCRIPT, find_lost_sdp_request
from .config import (BIND_MAX, BIND_STEP, L2CAP_DELAY, L2CAP_MAX_SENDS, L2CAP_REPEAT_S, STEAM_BIND_MIN_HOLD,
                     STEAM_HOLD_DELAY, STEAM_PATH_WAIT, STEAM_WAIT, decky, log)
from .i18n import tr
from .util import dev_path, first_hci, mono, run, which


class Actions:
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
        txt = ("%g" % new).replace(".", ",") if i18n.current() == "ru" else "%g" % new
        msg = tr("Steam дважды подряд неверно открыл контроллер после сна. Время возврата драйвера увеличено до %s с.",
                 "Steam opened the controller the wrong way twice in a row after wake. The driver return time was increased to %s s.") % txt
        self.elog("TUNE", "steam_bind_at %g -> %g (%s)" % (cur, new, msg))
        self.ev(s, "TUNE steam_bind_at %g -> %g" % (cur, new))
        if decky is not None:
            try:
                await decky.emit("dsd_tuned", new, msg)
            except Exception as e:
                log.error("emit: %s", e)
