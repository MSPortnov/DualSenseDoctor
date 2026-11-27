import json
import os
import tempfile
import unittest

import helpers  # noqa: F401
from dsd import config, store


class Validate(unittest.TestCase):
    def test_defaults_for_garbage(self):
        for bad in (None, [], "x", 5):
            self.assertEqual(store.validate_settings(bad), config.DEFAULTS)

    def test_types_are_checked(self):
        s = store.validate_settings({"early_connect": "yes", "l2cap_answer": 1, "steam_pickup": False,
                                    "lang": "de", "unknown": 1, "btmon": True})
        self.assertTrue(s["early_connect"])      # not a bool: default stays
        self.assertTrue(s["l2cap_answer"])
        self.assertFalse(s["steam_pickup"])
        self.assertEqual(s["lang"], "en")
        self.assertTrue(s["btmon"])
        self.assertNotIn("unknown", s)

    def test_bind_time_range_and_step(self):
        v = lambda x: store.validate_settings({"steam_bind_at": x})["steam_bind_at"]
        self.assertEqual(v(2.5), 2.5)
        self.assertEqual(v(1), 2.0)
        self.assertEqual(v(99), 6.0)
        self.assertEqual(v(2.7), 2.5)
        self.assertEqual(v("3"), 2.0)
        self.assertEqual(v(True), 2.0)
        self.assertEqual(v(float("nan")), 2.0)


class Disk(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._old = (config.SETTINGS_DIR, store.SETTINGS_DIR, store.DATA_DIR)
        store.SETTINGS_DIR = store.DATA_DIR = self.dir

    def tearDown(self):
        store.SETTINGS_DIR = self._old[1]
        store.DATA_DIR = self._old[2]

    def test_atomic_write_leaves_no_temp_file(self):
        p = os.path.join(self.dir, "a.json")
        store.atomic_write_json(p, {"a": 1})
        self.assertEqual(json.load(open(p)), {"a": 1})
        self.assertEqual(os.listdir(self.dir), ["a.json"])

    def test_failed_write_keeps_old_file(self):
        p = os.path.join(self.dir, "a.json")
        store.atomic_write_json(p, {"a": 1})
        with self.assertRaises(TypeError):
            store.atomic_write_json(p, {"a": object()})
        self.assertEqual(json.load(open(p)), {"a": 1})

    def test_store_loads_and_repairs(self):
        open(os.path.join(self.dir, "settings.json"), "w").write('{"steam_bind_at": 3.5, "early_connect": "x"}')
        open(os.path.join(self.dir, "sessions.json"), "w").write('[{"id": 4}, 7, "x"]')
        st = store.Store()
        self.assertEqual(st.settings["steam_bind_at"], 3.5)
        self.assertTrue(st.settings["early_connect"])
        self.assertEqual(st.sessions, [{"id": 4}])
        self.assertEqual(st.next_id, 5)

    def test_corrupt_files(self):
        open(os.path.join(self.dir, "settings.json"), "w").write("{broken")
        open(os.path.join(self.dir, "sessions.json"), "w").write("{broken")
        st = store.Store()
        self.assertEqual(st.settings, config.DEFAULTS)
        self.assertEqual(st.sessions, [])

    def test_save_and_reload(self):
        st = store.Store()
        st.settings["steam_bind_at"] = 4.0
        st.save_settings()
        self.assertEqual(store.Store().settings["steam_bind_at"], 4.0)


if __name__ == "__main__":
    unittest.main()
