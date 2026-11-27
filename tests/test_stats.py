import unittest

import helpers  # noqa: F401
from dsd import stats


def sess(result, profile="p", fixes=(), after=True, t=1.0, **kw):
    d = {"result": result, "profile": profile, "fixes_applied": list(fixes), "after_resume": after, "t_usable": t}
    d.update(kw)
    return d


class Stats(unittest.TestCase):
    def test_counts(self):
        ss = [sess("OK"), sess("FIXED", fixes=["early_connect"]), sess("NOHID"),
              sess("OK", after=False), sess("RUNNING"), sess("ABORTED"),
              sess("FIXED", fixes=["steam_pickup"], t=3.0)]
        p = stats.compute_stats(ss)["p"]
        self.assertEqual((p["n"], p["ok"], p["fixed"], p["failed"], p["steam_rebind"]), (4, 1, 2, 1, 1))
        self.assertEqual(p["avg_ttu"], 1.7)

    def test_profiles_are_separate(self):
        st = stats.compute_stats([sess("OK", profile="a"), sess("OK", profile="b")])
        self.assertEqual(sorted(st), ["a", "b"])

    def test_fix_log(self):
        ss = [sess("OK"), sess("FIXED", fixes=["early_connect"], id=2), sess("NOSTEAM", id=3), sess("FIXED", id=4)]
        self.assertEqual([x["id"] for x in stats.fix_log(ss)], [3, 2])
        self.assertEqual(len(stats.fix_log(ss, limit=1)), 1)


def tracked(bad, bind_at=2.0, **kw):
    s = sess("FIXED" if bad else "OK", steam_hold=True, bind_at=bind_at, steam_done=True,
             steam_path="evdev" if bad else "hidapi", fixes_applied=["steam_pickup"] if bad else [])
    s.update(kw)
    return s


class Streak(unittest.TestCase):
    def test_two_bad_in_a_row(self):
        a, b = tracked(True), tracked(True)
        self.assertTrue(stats.pickup_streak_bad([a, b], b, 2.0))

    def test_one_bad_is_not_enough(self):
        a, b = tracked(False), tracked(True)
        self.assertFalse(stats.pickup_streak_bad([a, b], b, 2.0))

    def test_good_in_between_resets(self):
        a, g, b = tracked(True), tracked(False), tracked(True)
        self.assertFalse(stats.pickup_streak_bad([a, g, b], b, 2.0))

    def test_other_bind_time_does_not_count(self):
        a, b = tracked(True, bind_at=2.0), tracked(True, bind_at=2.5)
        self.assertFalse(stats.pickup_streak_bad([a, b], b, 2.5))

    def test_session_without_hold_does_not_count(self):
        a = tracked(True, steam_hold=False)
        b = tracked(True)
        self.assertFalse(stats.pickup_streak_bad([a, b], b, 2.0))

    def test_current_must_be_bad(self):
        a, b = tracked(True), tracked(False)
        self.assertFalse(stats.pickup_streak_bad([a, b], b, 2.0))


if __name__ == "__main__":
    unittest.main()
