import asyncio
import time
import unittest
from unittest import mock

import helpers  # noqa: F401
from dsd import actions as actions_mod
from dsd import engine as eng_mod
from dsd import hid as hid_mod
from dsd.util import mono

MAC = "3C:4D:5E:11:22:33"
HID = "0005:054C:0CE6.0003"


class Calls:
    """Replaces the sysfs functions: records the calls and the time they were made."""

    def __init__(self, restore_ok=True, present=True):
        self.log = []
        self.restore_ok = restore_ok
        self.present = present
        self.t = mono()

    def unbind(self, n):
        self.log.append(("unbind", round(mono() - self.t, 2)))
        return True, ["unbind ok"]

    def restore(self, n, pause=0.4):
        self.log.append(("restore", round(mono() - self.t, 2)))
        return self.restore_ok, ["restore " + ("ok" if self.restore_ok else "FAILED")]

    def names(self):
        return [x[0] for x in self.log]


def make_session(bind_at=0.5, after=True, **kw):
    s = {"id": 1, "mac": MAC, "after_resume": after, "bind_at": bind_at, "_mono": mono(), "events": [],
         "result": "RUNNING", "fixes_applied": [], "t0": time.time()}
    s.update(kw)
    return s


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from dsd import store as store_mod
        d = tempfile.mkdtemp()   # every test starts with empty settings and sessions
        self.dirs = [mock.patch.object(store_mod, "SETTINGS_DIR", d), mock.patch.object(store_mod, "DATA_DIR", d)]
        for p in self.dirs:
            p.start()
        self.e = eng_mod.Engine()
        self.e.action_lock = asyncio.Lock()
        self.e.q = asyncio.Queue()
        self.calls = Calls()
        self.patches = [
            mock.patch.object(hid_mod, "steam_running", lambda: True),
            mock.patch.object(hid_mod, "sync_unbind", self.calls.unbind),
            mock.patch.object(hid_mod, "sync_restore", self.calls.restore),
            mock.patch.object(hid_mod, "device_present", lambda n: self.calls.present),
            mock.patch.object(actions_mod, "STEAM_HOLD_DELAY", 0.05),
        ]
        for p in self.patches:
            p.start()
        self.e.cfg["steam_pickup"] = True

    async def asyncTearDown(self):
        for p in self.patches + self.dirs:
            p.stop()


class Hold(Base):
    async def test_normal_timeline(self):
        s = make_session(bind_at=0.5)
        self.e.cur[MAC] = s
        await self.e._hold_hid(s, HID)
        self.assertEqual(self.calls.names(), ["unbind", "restore"])
        self.assertGreaterEqual(self.calls.log[1][1], 0.45)   # not earlier than bind_at
        self.assertTrue(s["steam_hold"])
        self.assertNotIn(HID, self.e.detached)
        self.assertFalse(s["_hold"])
        self.assertIn("_bind_el", s)

    async def test_not_after_resume_does_nothing(self):
        s = make_session(after=False)
        self.e.cur[MAC] = s
        await self.e._hold_hid(s, HID)
        self.assertEqual(self.calls.log, [])

    async def test_setting_off_does_nothing(self):
        self.e.cfg["steam_pickup"] = False
        s = make_session()
        self.e.cur[MAC] = s
        await self.e._hold_hid(s, HID)
        self.assertEqual(self.calls.log, [])

    async def test_session_ends_during_hold_restores_at_once(self):
        s = make_session(bind_at=5.0)
        self.e.cur[MAC] = s
        t = asyncio.ensure_future(self.e._hold_hid(s, HID))
        await asyncio.sleep(0.3)
        self.e.cur.pop(MAC)
        await asyncio.wait_for(t, 2)
        self.assertEqual(self.calls.names(), ["unbind", "restore"])
        self.assertLess(self.calls.log[1][1], 1.0)

    async def test_cancel_restores_the_driver(self):
        s = make_session(bind_at=5.0)
        self.e.cur[MAC] = s
        t = asyncio.ensure_future(self.e._hold_hid(s, HID))
        await asyncio.sleep(0.3)
        self.assertIn(HID, self.e.detached)
        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t
        self.assertEqual(self.calls.names(), ["unbind", "restore"])
        self.assertNotIn(HID, self.e.detached)

    async def test_unload_restores_the_driver(self):
        s = make_session(bind_at=5.0)
        self.e.cur[MAC] = s
        self.e.spawn(self.e._hold_hid(s, HID))
        await asyncio.sleep(0.3)
        await asyncio.wait_for(self.e.stop(), 3)
        self.assertEqual(self.calls.names(), ["unbind", "restore"])
        self.assertEqual(self.e.detached, set())
        self.assertEqual(len(self.e.bg), 0)

    async def test_safety_net_on_unload(self):
        self.e.detached.add(HID)   # as if some task had lost track of it
        await self.e.stop()
        self.assertEqual(self.calls.names(), ["restore"])

    async def test_failed_restore_with_lost_link_asks_for_a_connect(self):
        self.calls.restore_ok = False
        self.calls.present = False
        asked = []

        async def fake_connect(s, n):
            asked.append(n)
        self.e.do_bt_connect = fake_connect
        s = make_session(bind_at=0.3)
        self.e.cur[MAC] = s
        await self.e._hold_hid(s, HID)
        self.assertEqual(asked, ["after-restore"])

    async def test_failed_restore_with_link_alive_does_not_reconnect(self):
        self.calls.restore_ok = False
        asked = []

        async def fake_connect(s, n):
            asked.append(n)
        self.e.do_bt_connect = fake_connect
        s = make_session(bind_at=0.3)
        self.e.cur[MAC] = s
        await self.e._hold_hid(s, HID)
        self.assertEqual(asked, [])


class Spawn(Base):
    async def test_tasks_are_kept_and_forgotten(self):
        done = asyncio.Event()

        async def work():
            await done.wait()
        t = self.e.spawn(work())
        self.assertIn(t, self.e.bg)
        done.set()
        await t
        await asyncio.sleep(0)
        self.assertNotIn(t, self.e.bg)


def bad(**kw):
    d = {"after_resume": True, "steam_hold": True, "bind_at": 2.0, "steam_done": True, "steam_path": "evdev",
         "fixes_applied": ["steam_pickup"], "events": [], "_mono": mono(), "t0": time.time(), "id": 0}
    d.update(kw)
    return d


class Tune(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sent = []

        class D:
            async def emit(_, *a):
                self.sent.append(a)
        self.p = mock.patch.object(actions_mod, "decky", D())
        self.p.start()

    async def asyncTearDown(self):
        self.p.stop()
        await super().asyncTearDown()

    async def test_two_wrong_openings_raise_the_time_and_notify(self):
        a, b = bad(id=1), bad(id=2)
        self.e.sessions[:] = [a, b]
        await self.e.maybe_tune(a)
        self.assertEqual(self.e.cfg["steam_bind_at"], 2.0)    # one is not enough
        await self.e.maybe_tune(b)
        self.assertEqual(self.e.cfg["steam_bind_at"], 2.5)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][:2], ("dsd_tuned", 2.5))
        self.assertIn("2.5", self.sent[0][2])

    async def test_russian_text_uses_a_comma(self):
        from dsd import i18n
        i18n.set_lang("ru")
        try:
            a, b = bad(id=1), bad(id=2)
            self.e.sessions[:] = [a, b]
            await self.e.maybe_tune(b)
            self.assertIn("2,5", self.sent[0][2])
        finally:
            i18n.set_lang("en")

    async def test_next_step_needs_two_new_failures(self):
        a, b = bad(id=1), bad(id=2)
        self.e.sessions[:] = [a, b]
        await self.e.maybe_tune(b)                      # -> 2.5
        c = bad(id=3, bind_at=2.5)
        self.e.sessions.append(c)
        await self.e.maybe_tune(c)                      # the old sessions were at 2.0: they do not count
        self.assertEqual(self.e.cfg["steam_bind_at"], 2.5)
        d = bad(id=4, bind_at=2.5)
        self.e.sessions.append(d)
        await self.e.maybe_tune(d)
        self.assertEqual(self.e.cfg["steam_bind_at"], 3.0)

    async def test_stops_at_the_maximum(self):
        self.e.cfg["steam_bind_at"] = 6.0
        a, b = bad(id=1, bind_at=6.0), bad(id=2, bind_at=6.0)
        self.e.sessions[:] = [a, b]
        await self.e.maybe_tune(b)
        self.assertEqual(self.e.cfg["steam_bind_at"], 6.0)
        self.assertEqual(self.sent, [])

    async def test_off_when_pickup_is_off(self):
        self.e.cfg["steam_pickup"] = False
        a, b = bad(id=1), bad(id=2)
        self.e.sessions[:] = [a, b]
        await self.e.maybe_tune(b)
        self.assertEqual(self.e.cfg["steam_bind_at"], 2.0)

    async def test_setting_is_saved(self):
        a, b = bad(id=1), bad(id=2)
        self.e.sessions[:] = [a, b]
        await self.e.maybe_tune(b)
        from dsd.store import Store
        self.assertEqual(Store().settings["steam_bind_at"], 2.5)   # (the dirs are still patched here)


class Settings(Base):
    async def test_slider_value(self):
        r = await self.e.set_setting("steam_bind_at", 3.5)
        self.assertEqual(r["steam_bind_at"], 3.5)
        r = await self.e.set_setting("steam_bind_at", 100)
        self.assertEqual(r["steam_bind_at"], 6.0)
        r = await self.e.set_setting("steam_bind_at", "abc")
        self.assertEqual(r["steam_bind_at"], 6.0)    # garbage is ignored

    async def test_profile_shows_the_time(self):
        self.assertIn("steam_pickup@2", self.e.profile())
        await self.e.set_setting("steam_bind_at", 2.5)
        self.assertIn("steam_pickup@2.5", self.e.profile())


class Clock(Base):
    async def test_wall_clock_jump_after_wake_does_not_matter(self):
        self.e.ctrl[MAC] = {"name": "DualSense", "is_ds": True, "connected": True, "wake_allowed": True}
        self.e.last_resume_mono = mono() - 5.0
        real = time.time
        with mock.patch("time.time", lambda: real() + 3600):   # NTP corrected the clock by an hour
            self.e.start_session(MAC)
        s = self.e.cur[MAC]
        self.assertTrue(s["after_resume"])
        self.assertAlmostEqual(s["since_resume_s"], 5.0, delta=0.5)
        for t in self.e.sup_tasks.values():
            t.cancel()
        await asyncio.gather(*self.e.sup_tasks.values(), return_exceptions=True)

    async def test_connection_long_after_resume_is_not_after_sleep(self):
        self.e.ctrl[MAC] = {"name": "DualSense", "is_ds": True, "connected": True, "wake_allowed": True}
        self.e.last_resume_mono = mono() - 300
        self.e.start_session(MAC)
        self.assertFalse(self.e.cur[MAC]["after_resume"])
        for t in self.e.sup_tasks.values():
            t.cancel()
        await asyncio.gather(*self.e.sup_tasks.values(), return_exceptions=True)


if __name__ == "__main__":
    unittest.main()


class Adapter(Base):
    """The adapter is kept awake together with early connect and put back as it was."""

    def setUp(self):
        import os
        import tempfile
        self.pm = os.path.join(tempfile.mkdtemp(), "control")

    def put(self, v):
        with open(self.pm, "w") as f:
            f.write(v)

    def get(self):
        with open(self.pm) as f:
            return f.read().strip()

    def patched(self):
        return mock.patch.object(self.e, "adapter_pm_path", lambda: self.pm), \
            mock.patch.object(eng_mod, "write_sys", lambda p, v: self.put(v))

    async def test_early_connect_on_turns_autosuspend_off(self):
        self.put("auto")
        a, b = self.patched()
        with a, b:
            self.assertEqual(self.e.ensure_adapter_awake("t"), "auto")
            self.assertEqual(self.get(), "on")
            self.assertEqual(self.e.pm_orig, "auto")

    async def test_early_connect_off_leaves_the_adapter_alone(self):
        self.put("auto")
        self.e.cfg["early_connect"] = False
        a, b = self.patched()
        with a, b:
            self.assertIsNone(self.e.ensure_adapter_awake("t"))
            self.assertEqual(self.get(), "auto")

    async def test_user_turning_it_back_on_is_undone(self):
        a, b = self.patched()
        with a, b:
            self.put("auto")
            self.e.ensure_adapter_awake("t")
            self.put("auto")   # someone sets autosuspend again
            self.e.ensure_adapter_awake("after sleep")
            self.assertEqual(self.get(), "on")
            self.assertEqual(self.e.pm_orig, "auto")   # the original value is not overwritten by a later pass

    async def test_switch_off_restores_the_original(self):
        a, b = self.patched()
        with a, b:
            self.put("auto")
            self.e.ensure_adapter_awake("t")
            await self.e.set_setting("early_connect", False)
            self.assertEqual(self.get(), "auto")
            self.assertIsNone(self.e.pm_orig)
            await self.e.set_setting("early_connect", True)
            self.assertEqual(self.get(), "on")

    async def test_unload_restores_the_original(self):
        a, b = self.patched()
        with a, b:
            self.put("auto")
            self.e.ensure_adapter_awake("t")
            self.e.restore_adapter_pm("unload")
            self.assertEqual(self.get(), "auto")

    async def test_nothing_to_restore_when_it_was_already_on(self):
        a, b = self.patched()
        with a, b:
            self.put("on")
            self.e.ensure_adapter_awake("t")
            self.e.restore_adapter_pm("unload")
            self.assertEqual(self.get(), "on")

    async def test_old_setting_is_not_a_setting_any_more(self):
        from dsd.store import validate_settings
        self.assertNotIn("no_autosuspend", validate_settings({"no_autosuspend": False}))
        r = await self.e.set_setting("no_autosuspend", False)
        self.assertNotIn("no_autosuspend", r)

    async def test_original_value_survives_a_restart(self):
        a, b = self.patched()
        with a, b:
            self.put("auto")
            self.e.ensure_adapter_awake("t")
            e2 = eng_mod.Engine()           # the plugin was killed and started again: the adapter is "on" now
            self.assertEqual(e2.pm_orig, "auto")
            e2.adapter_pm_path = lambda: self.pm
            e2.restore_adapter_pm("setting")
            self.assertEqual(self.get(), "auto")
            self.assertIsNone(eng_mod.Engine().pm_orig)   # nothing left on disk after the restore

    async def test_damaged_file_is_ignored(self):
        open(self.e.pm_file, "w").write("{not json")
        self.assertIsNone(self.e.load_pm_orig())
        open(self.e.pm_file, "w").write('{"orig": "rm -rf"}')
        self.assertIsNone(self.e.load_pm_orig())


class ReportRequest(Base):
    async def test_request_starts_one_report(self):
        calls = []

        async def fake():
            calls.append(1)
            return {}
        self.e.build_report = fake
        with mock.patch.object(eng_mod.report, "take_request", lambda: True):
            self.e.check_report_request()
            self.e.check_report_request()   # a report is already being made
            await asyncio.gather(*list(self.e.bg))
        self.assertEqual(calls, [1])
        self.assertFalse(self.e.report_busy)

    async def test_no_request_no_report(self):
        with mock.patch.object(eng_mod.report, "take_request", lambda: False):
            self.e.check_report_request()
        self.assertEqual(len(self.e.bg), 0)

    async def test_failed_report_frees_the_flag(self):
        async def boom():
            raise OSError("disk full")
        self.e.build_report = boom
        with mock.patch.object(eng_mod.report, "take_request", lambda: True):
            self.e.check_report_request()
            await asyncio.gather(*list(self.e.bg))
        self.assertFalse(self.e.report_busy)


class Tick(Base):
    """The once-a-second tick must not read dozens of sysfs files."""

    async def test_counters_are_not_read_every_second(self):
        reads = []
        self.e.read_wakeup = lambda: reads.append(1) or {}
        self.e.dbus_missing = False
        with mock.patch.object(eng_mod.report, "take_request", lambda: False):
            for _ in range(10):
                await self.e.tick_once()
        self.assertEqual(len(reads), 1)   # the first pass only; the next refresh is WAKE_SNAP_EVERY seconds later

    async def test_fallback_refresh_after_the_interval(self):
        reads = []
        self.e.read_wakeup = lambda: reads.append(1) or {}
        with mock.patch.object(eng_mod.report, "take_request", lambda: False):
            await self.e.tick_once()
            self.e._snap_at = mono() - 1
            await self.e.tick_once()
        self.assertEqual(len(reads), 2)

    async def test_sleep_announcement_takes_the_exact_snapshot(self):
        snaps = [{"wakeup1": ("pwr", "d", 5, 5)}]
        self.e.read_wakeup = lambda: snaps[0]
        self.e.cfg["l2cap_answer"] = False
        for line in ("signal time=1 sender=:1 -> destination=(null destination) path=/org/freedesktop/login1; interface=org.freedesktop.login1.Manager; member=PrepareForSleep",
                     "   boolean true"):
            self.e.feed_dbus(line)
        self.assertEqual(self.e.wake_snap, snaps[0])

    async def test_wake_source_is_found_against_the_snapshot_before_sleep(self):
        before = {"wakeup1": ("pwr", "d", 5, 5), "wakeup2": ("usb", "e", 1, 1)}
        after = {"wakeup1": ("pwr", "d", 5, 5), "wakeup2": ("usb", "e", 2, 2)}
        self.e.wake_snap = before
        self.e.read_wakeup = lambda: after
        self.e.on_resume(time.time(), 30.0)
        self.assertEqual(self.e.last_wake_sources, ["usb[e]"])
        self.assertEqual(self.e.wake_snap, after)   # the new baseline
