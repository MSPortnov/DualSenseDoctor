import os
import tempfile
import unittest

import helpers  # noqa: F401
from dsd import report


class Mask(unittest.TestCase):
    def test_colon_form(self):
        self.assertEqual(report.mask_macs("Device 3C:4D:5E:11:22:33 (Sony)"), "Device 3C:4D:5E:XX:XX:XX (Sony)")

    def test_dbus_path_form(self):
        self.assertEqual(report.mask_macs("/org/bluez/hci0/dev_3C_4D_5E_11_22_33/fd1"),
                         "/org/bluez/hci0/dev_3C_4D_5E_XX_XX_XX/fd1")

    def test_lowercase_and_many(self):
        self.assertEqual(report.mask_macs("a:b 3c:4d:5e:aa:bb:cc and 3c:4d:5e:aa:bb:cd"),
                         "a:b 3c:4d:5e:XX:XX:XX and 3c:4d:5e:XX:XX:XX")

    def test_not_a_mac(self):
        for t in ("09:08:06.123 kernel", "00 11 22 33 44 55", "12:34:56", "0005:054C:0CE6.0003", "1:2:3:4:5:6"):
            self.assertEqual(report.mask_macs(t), t)

    def test_longer_hex_run_is_left_alone(self):
        t = "11:22:33:44:55:66:77"
        self.assertEqual(report.mask_macs(t), t)


class Files(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_create_refuses_existing_file(self):
        p = os.path.join(self.d, "x")
        open(p, "w").write("keep")
        with self.assertRaises(FileExistsError):
            report.create_file(p)
        self.assertEqual(open(p).read(), "keep")

    def test_create_does_not_follow_a_symlink(self):
        target = os.path.join(self.d, "victim")
        open(target, "w").write("secret")
        link = os.path.join(self.d, "dualsense-doctor-20250101-000000.tar.gz")
        os.symlink(target, link)
        with self.assertRaises(OSError):
            report.create_file(link)
        with self.assertRaises(OSError):
            report.write_text_file(link, "x")
        self.assertEqual(open(target).read(), "secret")

    def test_new_paths_skip_taken_names(self):
        name = "dualsense-doctor-20250101-000000"
        t1, s1 = report.new_report_paths(self.d, name)
        open(t1, "w").close()
        t2, s2 = report.new_report_paths(self.d, name)
        self.assertNotEqual(t1, t2)
        self.assertTrue(report.REPORT_RE.match(os.path.basename(t2)))
        self.assertTrue(report.REPORT_RE.match(os.path.basename(s2)))

    def test_tar_and_text(self):
        work = os.path.join(self.d, "w", "n")
        os.makedirs(work)
        open(os.path.join(work, "f.txt"), "w").write("hi")
        tp = os.path.join(self.d, "r.tar.gz")
        report.write_tar(tp, os.path.join(self.d, "w", "n"), "n")
        import tarfile
        with tarfile.open(tp) as tf:
            self.assertEqual(tf.getnames(), ["n", "n/f.txt"])

    def test_remove_old_reports(self):
        names = ["dualsense-doctor-20250101-000000.tar.gz", "dualsense-doctor-20250101-000000-summary.txt",
                 "dualsense-doctor-20250202-101010-1.tar.gz"]
        keep = os.path.join(self.d, "dualsense-doctor-20251111-111111.tar.gz")
        other = os.path.join(self.d, "dualsense-doctor-notes.txt")      # not ours: not matching the exact pattern
        mine = os.path.join(self.d, "notes.txt")
        for n in names:
            open(os.path.join(self.d, n), "w").close()
        for p in (keep, other, mine):
            open(p, "w").close()
        victim = os.path.join(self.d, "victim")
        open(victim, "w").write("v")
        os.symlink(victim, os.path.join(self.d, "dualsense-doctor-20240101-000000.tar.gz"))
        os.makedirs(os.path.join(self.d, "dualsense-doctor-20240303-000000.tar.gz.d"))
        n = report.remove_old_reports(self.d, keep=(keep,))
        self.assertEqual(n, 4)
        left = sorted(os.listdir(self.d))
        self.assertEqual(left, sorted([os.path.basename(keep), "dualsense-doctor-notes.txt", "notes.txt", "victim",
                                       "dualsense-doctor-20240303-000000.tar.gz.d"]))
        self.assertEqual(open(victim).read(), "v")   # the link was removed, its target was not touched


class Request(unittest.TestCase):
    """The file that "deploy.sh fetch" leaves in the home folder to ask for a report."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, ".dsd-report-request")

    def test_no_file_no_request(self):
        self.assertFalse(report.take_request(self.d))

    def test_regular_file_is_a_request_and_is_removed(self):
        open(self.p, "w").close()
        self.assertTrue(report.take_request(self.d))
        self.assertFalse(os.path.lexists(self.p))
        self.assertFalse(report.take_request(self.d))   # one request, one report

    def test_symlink_is_not_a_request_and_its_target_stays(self):
        target = os.path.join(self.d, "target")
        open(target, "w").write("t")
        os.symlink(target, self.p)
        self.assertFalse(report.take_request(self.d))
        self.assertFalse(os.path.lexists(self.p))
        self.assertEqual(open(target).read(), "t")

    def test_file_of_another_user_is_not_a_request(self):
        from unittest import mock
        open(self.p, "w").close()
        with mock.patch.object(report, "_owner", lambda: (os.getuid() + 1, 0)), \
                mock.patch.object(report.os, "geteuid", lambda: 0):
            self.assertFalse(report.take_request(self.d))
        self.assertFalse(os.path.lexists(self.p))

    def test_folder_is_not_a_request(self):
        os.mkdir(self.p)
        self.assertFalse(report.take_request(self.d))


if __name__ == "__main__":
    unittest.main()
