import os
import tempfile
import unittest

import helpers  # noqa: F401
from dsd import hid

DEV = "0005:054C:0CE6.0003"


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


if __name__ == "__main__":
    unittest.main()
