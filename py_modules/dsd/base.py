"""Shared pieces: paths, constants, language, small helpers (no plugin logic)."""
import asyncio
import datetime
import glob
import logging
import os
import re
import shutil
import time

try:
    import decky
except Exception:  # running outside Decky (tests)
    decky = None


def _d(name, default):
    return getattr(decky, name, default) if decky else default


_ROOT = os.environ.get("DSD_TEST_DIR", "/tmp/dsd")  # outside Decky (tests) everything lives here
SETTINGS_DIR = _d("DECKY_PLUGIN_SETTINGS_DIR", _ROOT + "/settings")
DATA_DIR = _d("DECKY_PLUGIN_RUNTIME_DIR", _ROOT + "/data")
PLUGIN_LOG_DIR = _d("DECKY_PLUGIN_LOG_DIR", _ROOT + "/log")
DECKY_USER = _d("DECKY_USER", "deck")
USER_HOME = _d("DECKY_USER_HOME", "/home/deck")
log = decky.logger if decky else logging.getLogger("dsd")

VERSION = "0.10.0"
HID_DIR = "/sys/bus/hid/devices"
HOT_CAP = 8000            # max. btmon lines per session
NOHID_TIMEOUT = 25.0      # connected, but the HID device never appeared
RESUME_WINDOW = 40.0      # a connection within N s after resume counts as "after sleep"
MAX_SESSIONS = 300
MAX_BTMON_FILES = 40
# Steam opens the controller the wrong way (A and B swapped, default lightbar) if it first sees it in the first seconds
# after wake, while it is still busy itself. The HID device is needed right away (otherwise the PC drops the link at the 4th second), so it is created
# early, but its driver is detached for that time: the /dev/hidrawN and input nodes disappear while the link stays. The driver is restored
# "steam_bind_at" seconds after the connection (a setting, default 2 s, 2-6 s), when Steam is calm: it sees the controller for the first time and opens it the right way.
# On-demand HCI trace (btmon) for the L2CAP answer: started shortly before sleep, stopped after wake.
BTMON_AFTER_RESUME_S = 90  # how long the trace keeps running after wake
BTMON_ARM_MAX_S = 300      # ... and at most this long when the sleep never happens
EARLY_CONNECT_DELAY = 0.3  # seconds after the connection before the plugin opens the HID channel itself
STEAM_HOLD = True
STEAM_HOLD_DELAY = 0.15   # pause after HID appears before the driver is detached, s
STEAM_BIND_MIN_HOLD = 0.5  # the driver stays detached at least this long, s
STEAM_BIND_SETTLE = 1.2   # how long to wait after the driver is restored before checking HID, s
STEAM_PATH_WAIT = 4.0     # how long to wait for Steam's log entry about how it opened the controller
STEAM_WAIT = 8.0          # how long to wait for Steam to notice the controller before doing a rebind
EARLY_CONNECT_ATTEMPTS = 4  # how many times "Early connect" tries to open the HID channel
L2CAP_DELAY = 3.0         # how many seconds after the connection to start answering the lost request
# "Pending" response to the controller's lost request: L2CAP_MAX_SENDS times, L2CAP_REPEAT_S seconds apart, no further repeats.
# Every response puts a "hcon ... sent 0 < count 1" line in the kernel log, so no extra responses are sent.
# L2CAP_MAX_SENDS = 0 means "unlimited". If the controller drops the link 1-5 minutes after wake,
# there are too few responses: increase L2CAP_MAX_SENDS.
L2CAP_REPEAT_S = 15.0
L2CAP_MAX_SENDS = 4

FIX_KEYS = ["early_connect", "l2cap_answer", "steam_pickup"]
# Language of the plugin's messages and reports (ru / en). Set by the interface ("Language" setting), stored in settings.json.
LANG = "en"


def tr(ru, en):
    return ru if LANG == "ru" else en


def set_lang(lang):
    """Sets the language of messages and reports: "ru" or (anything else) "en". Returns the value set."""
    global LANG
    LANG = "ru" if lang == "ru" else "en"
    return LANG


# Numeric settings: (min, max, step). The value is clamped and snapped to the step when it is loaded and when it is set.
RANGES = {"steam_bind_at": (2.0, 6.0, 0.5)}
BIND_STEP = 0.5   # self-tuning: how much the driver return time grows after two wrong Steam pickups in a row
BIND_MAX = RANGES["steam_bind_at"][1]


def norm_range(key, value):
    """Number for a ranged setting, or None if the value is not a number."""
    lo, hi, step = RANGES[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    v = min(max(float(value), lo), hi)
    return round(round((v - lo) / step) * step + lo, 3)


def mono():
    """Monotonic seconds: intervals are measured with it, never with the wall clock (NTP corrects it right after wake)."""
    return time.monotonic()


def wall_of(mono_ts):
    """Wall-clock time (for display only) of a moment taken with mono()."""
    return time.time() - (time.monotonic() - mono_ts)


DEFAULTS = {
    "lang": "en",
    "early_connect": True,
    "l2cap_answer": True,
    "steam_pickup": True,
    "steam_bind_at": 2.0,
    "no_autosuspend": True,
    "btmon": False,
}

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
KERN_RE = re.compile(r"bluetooth|hci\d|hid|playstation|uhid|PM: |btusb|btintel|"
                     r"usb \d|input:|wake|suspend|resume", re.I)


# ---------------------------------------------------------------- utils
def make_env():
    env = dict(os.environ)
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if orig is not None:
        env["LD_LIBRARY_PATH"] = orig
    else:
        env.pop("LD_LIBRARY_PATH", None)
    env["LANG"] = "C"
    env["SYSTEMD_COLORS"] = "0"
    env["SYSTEMD_PAGER"] = ""
    env["NO_COLOR"] = "1"
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONNOUSERSITE", "_MEIPASS2"):
        env.pop(k, None)
    env["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
    return env


ENV = make_env()

def which(name):
    return shutil.which(name, path=ENV["PATH"])


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")


def read(path, default=""):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def write_sys(path, value):
    with open(path, "w") as f:
        f.write(value)


async def run(cmd, timeout=10):
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=ENV)
    except FileNotFoundError:
        return 127, "command not found: %s" % cmd[0]
    except Exception as e:
        return -1, "spawn error: %s" % e
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, ANSI.sub("", out.decode(errors="replace"))
    except asyncio.TimeoutError:
        try:
            p.kill()
        except Exception:
            pass
        return -9, "timeout after %ss" % timeout


def mac_from_path(path):
    m = re.search(r"dev_([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})$", path or "")
    return m.group(1).replace("_", ":").upper() if m else None


def dev_path(mac, hci="hci0"):
    return "/org/bluez/%s/dev_%s" % (hci, mac.replace(":", "_"))


def first_hci():
    names = sorted(glob.glob("/sys/class/bluetooth/hci[0-9]*"))
    names = [os.path.basename(n) for n in names if ":" not in os.path.basename(n)]
    return names[0] if names else "hci0"

