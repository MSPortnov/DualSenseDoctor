import os
from pathlib import Path
import tempfile
import unittest

import helpers  # noqa: F401
from dsd import hid

DEV = "0005:054C:0CE6.0003"
MAC = "AA:BB:CC:DD:EE:FF"


class SteamPickup(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.addCleanup(setattr, hid, "HID_DIR", hid.HID_DIR)
        hid.HID_DIR = str(self.root)
        (self.root / DEV).mkdir()
        self.log = self.root / "controller.txt"

    def entry(self, model="054c 0ce6", serial="", mode="hidapi"):
        vid, pid = model.split()
        opening = {
            "hidapi": f"Controller using HIDAPI driver, vid=0x{vid}, pid=0x{pid}",
            "evdev": "Invalid or missing unit serial number",
            None: "Product: Wireless Controller",
        }[mode]
        return (
            f"[2026-11-28 19:13:34] Local Device Found\r\n  type: {model}\r\n"
            f"  path: sdl://18\r\n  serial_number: {serial} - 0\r\n"
            f"[2026-11-28 19:13:34] {opening}\r\n"
        )

    def state(self, entries, offset=0):
        self.log.write_bytes(entries.encode())
        return hid.steam_device_state({str(self.log): offset}, DEV, MAC)

    def test_other_controller_does_not_confirm_pickup(self):
        self.assertEqual(self.state(self.entry(model="045e 028e")), (False, None))

    def test_edge_does_not_confirm_pickup_of_standard_dualsense(self):
        self.assertEqual(self.state(self.entry(model="054c 0df2")), (False, None))

    def test_single_controller_with_empty_serial_is_confirmed(self):
        self.assertEqual(self.state(self.entry()), (True, "hidapi"))

    def test_same_model_with_different_address_is_ignored(self):
        self.assertEqual(self.state(self.entry(serial="001122334455")), (False, None))

    def test_same_model_with_empty_serial_is_ambiguous(self):
        (self.root / "0003:054C:0CE6.0004").mkdir()
        self.assertEqual(self.state(self.entry()), (True, None))
        self.assertEqual(self.state(self.entry(mode="evdev")), (True, None))

    def test_target_address_disambiguates_identical_models(self):
        (self.root / "0005:054C:0CE6.0004").mkdir()
        content = self.entry(serial="aa-bb-cc-dd-ee-ff") + self.entry(serial="001122334455", mode="evdev")
        self.assertEqual(self.state(content), (True, "hidapi"))

    def test_error_for_selected_controller_is_attributed_to_it(self):
        content = self.entry(serial=MAC, mode="evdev") + self.entry(model="045e 028e")
        self.assertEqual(self.state(content), (True, "evdev"))

    def test_wrong_device_hidapi_line_does_not_confirm_current_entry(self):
        content = self.entry(mode=None) + "Controller using HIDAPI driver, vid=0x045e, pid=0x028e\r\n"
        self.assertEqual(self.state(content), (True, None))

    def test_old_opening_before_offset_does_not_confirm_pickup(self):
        previous = self.entry(serial=MAC)
        self.assertEqual(self.state(previous + self.entry(model="045e 028e"), offset=len(previous.encode())), (False, None))


class FakeSysfs(unittest.TestCase):
    """A small fake of /sys/bus/hid: bind creates the driver link, unbind removes it."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.devs = os.path.join(self.root, "devices")
        os.makedirs(os.path.join(self.devs, DEV))
        os.makedirs(os.path.join(self.root, "drivers", "playstation"))
        self.old = (hid.HID_DIR, hid.HID_BUS, hid.write_sys)
        hid.HID_DIR, hid.HID_BUS = self.devs, self.root
        self.bind_fail = 0      # how many bind attempts fail
        self.probe_works = True
        self.log = []
        hid.write_sys = self.fake_write
        self.link = os.path.join(self.devs, DEV, "driver")

    def tearDown(self):
        hid.HID_DIR, hid.HID_BUS, hid.write_sys = self.old

    def attach(self):
        if not os.path.lexists(self.link):
            os.symlink(os.path.join(self.root, "drivers", "playstation"), self.link)

    def fake_write(self, path, value):
        self.log.append(os.path.relpath(path, self.root))
        if path.endswith("/unbind"):
            if os.path.lexists(self.link):
                os.unlink(self.link)
        elif path.endswith("playstation/bind"):
            if self.bind_fail > 0:
                self.bind_fail -= 1
                raise OSError("busy")
            self.attach()
        elif path.endswith("drivers_probe"):
            if not self.probe_works:
                raise OSError("no")
            self.attach()


class Restore(FakeSysfs):
    def test_unbind_then_plain_bind_is_one_step(self):
        self.attach()
        ok, steps = hid.sync_unbind(DEV)
        self.assertTrue(ok)
        self.assertIsNone(hid.driver_of(DEV))
        ok, steps = hid.sync_restore(DEV, pause=0)
        self.assertTrue(ok)
        self.assertEqual(hid.driver_of(DEV), "playstation")
        self.assertEqual(self.log[-1], "drivers/playstation/bind")   # the normal path: a single plain bind
        self.assertEqual(len([x for x in self.log if x.endswith("/bind")]), 1)

    def test_failed_bind_falls_back_to_probe(self):
        self.bind_fail = 99
        ok, steps = hid.sync_restore(DEV, pause=0)
        self.assertTrue(ok)
        self.assertEqual(hid.driver_of(DEV), "playstation")

    def test_everything_fails(self):
        self.bind_fail = 99
        self.probe_works = False
        ok, steps = hid.sync_restore(DEV, pause=0)
        self.assertFalse(ok)
        self.assertIn("driver not attached yet (attempt 3)", steps)

    def test_device_gone(self):
        os.rmdir(os.path.join(self.devs, DEV))
        ok, steps = hid.sync_restore(DEV, pause=0)
        self.assertFalse(ok)
        self.assertIn("device is gone", steps[0])
        self.assertFalse(hid.device_present(DEV))

    def test_orphans(self):
        res = hid.sync_orphans()
        self.assertEqual([(n, ok) for n, ok, _ in res], [(DEV, True)])
        self.assertEqual(hid.sync_orphans(), [])   # all drivers are in place: nothing is touched
        self.assertEqual(len([x for x in self.log if x.endswith("/bind")]), 1)

    def test_orphans_ignore_other_devices(self):
        os.makedirs(os.path.join(self.devs, "0003:046D:C52B.0001"))
        self.assertEqual([n for n, _, _ in hid.sync_orphans()], [DEV])

    def test_exact_address_wins_over_unnamed_device(self):
        target = "0005:054C:0CE6.0004"
        other = Path(self.devs) / target
        other.mkdir()
        (other / "uevent").write_text("HID_UNIQ=" + MAC.lower() + "\n")
        self.assertEqual(hid.find_hid(MAC), target)

    def test_multiple_unnamed_hid_devices_are_ambiguous(self):
        (Path(self.devs) / "0005:054C:0CE6.0004").mkdir()
        self.assertIsNone(hid.find_hid(MAC))


if __name__ == "__main__":
    unittest.main()
