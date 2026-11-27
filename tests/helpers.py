"""Common test setup: the package is imported from py_modules, all files go to a temporary folder."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DSD_TEST_DIR", tempfile.mkdtemp(prefix="dsd-test-"))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
