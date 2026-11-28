"""Paths, constants and settings defaults. No logic besides the range check of numeric settings."""
import logging
import os

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

VERSION = "0.10.9"
REPORT_REQUEST_NAME = ".dsd-report-request"  # deploy.sh fetch creates this file in the user's home to ask for a report
HID_DIR = "/sys/bus/hid/devices"
HOT_CAP = 8000            # max. btmon lines per session
NOHID_TIMEOUT = 25.0      # connected, but the HID device never appeared
WAKE_SNAP_EVERY = 20.0    # fallback refresh of the wake-source counters (the exact one is taken when logind announces the sleep)
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



DEFAULTS = {
    "lang": "en",
    "early_connect": True,
    "l2cap_answer": True,
    "steam_pickup": True,
    "steam_bind_at": 2.0,
    "btmon": False,
    "action_journal": False,
}
