"""Whole sequences of the engine with the system replaced by fakes: connection supervision, Steam watch, L2CAP answer."""
import asyncio
import struct
import sys
import types
import unittest
from unittest import mock

import helpers  # noqa: F401
from dsd import actions as actions_mod
from dsd import engine as eng_mod
from dsd import hid as hid_mod
from dsd.btmon import INJECT_SCRIPT
from dsd.util import mono
from test_engine import HID, MAC, Base, make_session


class InjectPacket(unittest.TestCase):
    """The raw packet that answers the controller: built by the script that runs as a separate process."""

    def build(self, dev, handle, result, ident, scid):
        sent = []

        class Sock:
            def __init__(self, *a):
                pass

            def bind(self, addr):
                sent.append(("bind", addr))

            def send(self, data):
                sent.append(("send", data))
                return len(data)
        fake = types.SimpleNamespace(socket=Sock, SOCK_RAW=3)
        argv = ["x", str(dev), str(handle), str(result), str(ident), str(scid)]
        with mock.patch.dict(sys.modules, {"socket": fake}), mock.patch.object(sys, "argv", argv), \
                mock.patch("builtins.print"):
            exec(compile(INJECT_SCRIPT, "inject", "exec"), {})
        return sent

    def test_layout(self):
        sent = self.build(0, 256, 1, 5, 70)
        self.assertEqual(sent[0], ("bind", (0,)))
        pkt = sent[1][1]
        self.assertEqual(pkt[0], 0x02)                                  # HCI ACL data packet
        h, dlen = struct.unpack("<HH", pkt[1:5])
        self.assertEqual(h & 0x0FFF, 256)                               # connection handle
        self.assertEqual(h >> 12, 2)                                    # start of a packet, point-to-point
        l2len, cid = struct.unpack("<HH", pkt[5:9])
        self.assertEqual(cid, 1)                                        # L2CAP signalling channel
        code, ident, slen = struct.unpack("<BBH", pkt[9:13])
        self.assertEqual((code, ident, slen), (3, 5, 8))                # Connection Response, our ident
        dcid, scid, result, status = struct.unpack("<HHHH", pkt[13:21])
        self.assertEqual((dcid, scid, result, status), (0, 70, 1, 0))   # result 1 = "pending"
        self.assertEqual(dlen, len(pkt) - 5)
        self.assertEqual(l2len, len(pkt) - 9)


RING_RX = [
    "> ACL: Handle 256 flags 0x02 dlen 12",
    "      L2CAP: Connection Request (0x02) ident 5 len 4",
    "        PSM: 1 (0x0001)",
    "        Source CID: 70",
]


class L2capAnswer(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.runs = []

        async def fake_run(cmd, timeout=10):
            self.runs.append(cmd)
            return 0, "sent 21 bytes"
        self.extra = [mock.patch.object(actions_mod, "run", fake_run),
                      mock.patch.object(actions_mod, "L2CAP_DELAY", 0.0),
                      mock.patch.object(actions_mod, "L2CAP_MAX_SENDS", 2),
                      mock.patch.object(actions_mod, "L2CAP_REPEAT_S", 0.25),
                      mock.patch.object(actions_mod, "first_hci", lambda: "hci0")]
        for p in self.extra:
            p.start()

        async def handle(mac):
            return 256
        self.e.acl_handle = handle

    async def asyncTearDown(self):
        for p in self.extra:
            p.stop()
        await super().asyncTearDown()

    async def test_answer_uses_the_values_from_the_trace(self):
        s = make_session(bind_at=2.0)
        self.e.cur[MAC] = s
        now = mono()
        for i, l in enumerate(RING_RX):
            self.e.btmon_ring.append((now - 3 + i * 0.01, l))
        await self.e._l2cap_answer(s)
        self.assertEqual(len(self.runs), 2)
        cmd = self.runs[0]
        self.assertEqual(cmd[0], "python3")
        self.assertEqual(cmd[3:], ["0", "256", "1", "5", "70"])         # dev, handle, "pending", ident, scid
        self.assertEqual(s["l2cap_params"], "trace")
        self.assertIn("l2cap_answer", s["fixes_applied"])

    async def test_guessed_values_are_not_counted_as_a_fix(self):
        s = make_session()
        self.e.cur[MAC] = s
        await self.e._l2cap_answer(s)
        self.assertEqual(self.runs[0][5:], ["1", "1", "64"])            # fallback ident=1, scid=64
        self.assertEqual(s["l2cap_params"], "guess")
        self.assertNotIn("l2cap_answer", s["fixes_applied"])

    async def test_stops_when_the_session_ends(self):
        s = make_session()
        self.e.cur[MAC] = s
        t = asyncio.ensure_future(self.e._l2cap_answer(s))
        await asyncio.sleep(0.1)
        self.e.cur.pop(MAC)
        await asyncio.wait_for(t, 2)
        self.assertLessEqual(len(self.runs), 1)

    async def test_no_handle_no_answer(self):
        async def none(mac):
            return None
        self.e.acl_handle = none
        s = make_session()
        self.e.cur[MAC] = s
        await self.e._l2cap_answer(s)
        self.assertEqual(self.runs, [])


class Supervise(Base):
    """Connection -> HID appears -> check -> verdict."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.hid_at = 0.0           # when the HID device appears (seconds after the connection)
        self.healthy = True
        self.connects = []
        self.t0 = mono()
        self.e.ctrl[MAC] = {"name": "DualSense", "is_ds": True, "connected": True, "wake_allowed": True}
        self.e.cfg["l2cap_answer"] = False

        def find_hid(mac):
            return HID if mono() - self.t0 >= self.hid_at else None

        def hid_info(n):
            return {"dev": n, "driver": "playstation" if self.healthy else None, "inputs": ["x"], "leds": ["l"],
                    "healthy": self.healthy, "problems": [] if self.healthy else ["no_motion"], "hidraw": []}

        async def connect(s, n):
            self.connects.append(n)
        self.e.do_bt_connect = connect
        self.extra = [mock.patch.object(hid_mod, "find_hid", find_hid),
                      mock.patch.object(hid_mod, "hid_info", hid_info),
                      mock.patch.object(hid_mod, "steam_device_state", lambda *_: (True, "hidapi")),
                      mock.patch.object(hid_mod, "steam_log_marks", lambda: {})]
        for p in self.extra:
            p.start()

    async def asyncTearDown(self):
        await self.e.stop()
        for p in self.extra:
            p.stop()
        await super().asyncTearDown()

    def start(self):
        self.supervision = self.e.start_session(MAC)
        return self.e.cur[MAC]

    async def finish(self):
        await asyncio.wait_for(self.supervision, 8)

    async def test_normal_connection_is_ok(self):
        s = self.start()
        await self.finish()
        self.assertEqual(s["result"], "OK")
        self.assertEqual(s["fixes_applied"], [])
        self.assertIsNotNone(s["t_hid"])
        self.assertIsNotNone(s["t_hid_ready"])
        self.assertNotIn(self.supervision, self.e.bg)
        latest = (await self.e.get_state())["latest_session"]
        self.assertEqual(latest["hid_status"], "ready")
        self.assertEqual(latest["steam_status"], "confirmed")
        self.assertEqual(self.connects, [])

    async def test_late_hid_triggers_early_connect_and_counts_as_fixed(self):
        self.hid_at = 0.9
        s = self.start()
        await self.finish()
        self.assertEqual(s["result"], "FIXED")
        self.assertIn("early_connect", s["fixes_applied"])
        self.assertGreaterEqual(len(self.connects), 1)

    async def test_early_connect_off_does_nothing(self):
        self.e.cfg["early_connect"] = False
        self.hid_at = 0.9
        s = self.start()
        await self.finish()
        self.assertEqual(self.connects, [])
        self.assertEqual(s["result"], "OK")

    async def test_hid_never_appears(self):
        self.hid_at = 999
        with mock.patch.object(eng_mod, "NOHID_TIMEOUT", 1.0):
            s = self.start()
            await self.finish()
        self.assertEqual(s["result"], "NOHID")
        self.assertEqual(s["hid_status"], "missing")
        self.assertEqual(s["steam_status"], "not_checked")

    async def test_unhealthy_hid(self):
        self.healthy = False
        s = self.start()
        await self.finish()
        self.assertEqual(s["result"], "NOGYRO")

    async def test_disconnect_before_the_verdict_is_aborted(self):
        self.hid_at = 999
        s = self.start()
        await asyncio.sleep(0.5)
        self.e.ctrl[MAC]["connected"] = False
        await self.e.end_session(MAC)
        await self.finish()
        self.assertEqual(s["result"], "ABORTED")

    async def test_ready_hid_with_pending_steam_does_not_show_fixed(self):
        self.hid_at = 0.9
        with mock.patch.object(hid_mod, "steam_device_state", return_value=(False, None)):
            s = self.start()
            await self.finish()
        self.assertEqual(s["hid_status"], "ready")
        self.assertEqual(s["steam_status"], "checking")
        self.assertEqual(s["result"], "OK")
        self.assertIn("early_connect", s["fixes_applied"])
        self.assertEqual(self.e.fix_log(), [])


class SteamWatch(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.rebinds = []
        self.state = (True, "hidapi")

        async def fake_rebind(s, hid):
            self.rebinds.append(hid)
        self.e.do_rebind = fake_rebind
        self.extra = [mock.patch.object(hid_mod, "steam_device_state", lambda *_: self.state),
                      mock.patch.object(hid_mod, "steam_log_marks", lambda: {}),
                      mock.patch.object(actions_mod, "STEAM_WAIT", 0.6)]
        for p in self.extra:
            p.start()

    async def asyncTearDown(self):
        for p in self.extra:
            p.stop()
        await super().asyncTearDown()

    def session(self, result="OK"):
        s = make_session(result=result, _steam_marks={})
        self.e.cur[MAC] = s
        return s

    async def test_normal_pickup(self):
        s = self.session()
        await asyncio.wait_for(self.e._steam_watch(s, HID), 5)
        self.assertEqual(self.rebinds, [])
        self.assertEqual(s["steam_path"], "hidapi")
        self.assertEqual(s["result"], "OK")
        self.assertTrue(s["steam_done"])

    async def test_wrong_opening_is_rebound_twice_then_reported(self):
        self.state = (True, "evdev")
        s = self.session()
        await asyncio.wait_for(self.e._steam_watch(s, HID), 10)
        self.assertEqual(len(self.rebinds), 2)
        self.assertEqual(s["steam_path"], "evdev")
        self.assertEqual(s["result"], "NOSTEAM")
        self.assertEqual(s["steam_status"], "wrong_mode")
        self.assertNotIn("steam_pickup", s["fixes_applied"])

    async def test_wrong_opening_with_pickup_off_is_only_reported(self):
        self.e.cfg["steam_pickup"] = False
        self.state = (True, "evdev")
        s = self.session()
        await asyncio.wait_for(self.e._steam_watch(s, HID), 5)
        self.assertEqual(self.rebinds, [])
        self.assertEqual(s["result"], "NOSTEAM")

    async def test_steam_never_notices(self):
        self.state = (False, None)
        s = self.session()
        with mock.patch.object(hid_mod, "steam_running", lambda: True):
            await asyncio.wait_for(self.e._steam_watch(s, HID), 10)
        self.assertEqual(len(self.rebinds), 2)
        self.assertIsNone(s["steam_seen_s"])
        self.assertEqual(s["result"], "NOSTEAM")
        self.assertEqual(s["steam_status"], "not_seen")
        self.assertEqual(s["steam_rebinds"], 2)
        self.assertNotIn("steam_pickup", s["fixes_applied"])

    async def test_steam_pickup_is_fixed_only_when_confirmed_after_rebind(self):
        s = self.session()
        position = 0

        async def rebind(session, hid):
            nonlocal position
            position = 1

        def state(marks, hid, mac):
            if position == 0:
                return True, "evdev"
            if marks["log"] == 0:
                return True, "hidapi"
            return False, None

        self.e.do_rebind = rebind
        with mock.patch.object(hid_mod, "steam_log_marks", side_effect=lambda: {"log": position}), \
                mock.patch.object(hid_mod, "steam_device_state", side_effect=state):
            await asyncio.wait_for(self.e._steam_watch(s, HID), 5)
        self.assertEqual(s["steam_status"], "confirmed")
        self.assertEqual(s["result"], "FIXED")
        self.assertEqual(s["steam_rebinds"], 1)
        self.assertIn("steam_pickup", s["fixes_applied"])

    async def test_steam_log_without_a_known_mode_is_unconfirmed(self):
        s = self.session()
        self.state = (True, None)
        with mock.patch.object(actions_mod, "STEAM_PATH_WAIT", 0):
            await asyncio.wait_for(self.e._steam_watch(s, HID), 3)
        self.assertEqual(s["steam_status"], "unconfirmed")
        self.assertEqual(s["result"], "NOSTEAM")
        self.assertEqual(self.rebinds, [])

    async def test_steam_not_running_skips_the_check(self):
        self.state = (False, None)
        s = self.session()
        with mock.patch.object(hid_mod, "steam_running", lambda: False):
            await asyncio.wait_for(self.e._steam_watch(s, HID), 5)
        self.assertEqual(self.rebinds, [])
        self.assertEqual(s["steam_status"], "not_running")
        self.assertEqual(s["result"], "OK")

    async def test_two_wrong_sessions_through_the_watch_raise_the_time(self):
        """The whole chain: two sessions end with a wrong Steam opening -> the time grows."""
        self.state = (True, "evdev")
        sent = []

        class D:
            async def emit(_, *a):
                sent.append(a)
        with mock.patch.object(actions_mod, "decky", D()):
            for i in (1, 2):
                s = self.session()
                s.update({"id": i, "steam_hold": True, "bind_at": 2.0})
                self.e.sessions.append(s)
                await asyncio.wait_for(self.e._steam_watch(s, HID), 10)
        self.assertEqual(self.e.cfg["steam_bind_at"], 2.5)
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
