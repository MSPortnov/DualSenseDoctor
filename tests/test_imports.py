import importlib
import pkgutil
import unittest

import helpers  # noqa: F401
import dsd


class Imports(unittest.TestCase):
    def test_every_module_imports(self):
        names = [m.name for m in pkgutil.iter_modules(dsd.__path__)]
        self.assertGreaterEqual(len(names), 10)
        for n in names:
            importlib.import_module("dsd." + n)


if __name__ == "__main__":
    unittest.main()
