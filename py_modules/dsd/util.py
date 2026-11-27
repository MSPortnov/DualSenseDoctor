"""Small helpers: subprocess environment, running commands, sysfs reads/writes, clocks, names of bluetooth objects."""
import asyncio
import datetime
import glob
import os
import re
import shutil
import subprocess
import time

def mono():
    """Monotonic seconds: intervals are measured with it, never with the wall clock (NTP corrects it right after wake)."""
    return time.monotonic()


def wall_of(mono_ts):
    """Wall-clock time (for display only) of a moment taken with mono()."""
    return time.time() - (time.monotonic() - mono_ts)


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


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
    except (OSError, ValueError):
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
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return -1, "spawn error: %s" % e
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode, ANSI.sub("", out.decode(errors="replace"))
    except asyncio.TimeoutError:
        try:
            p.kill()
        except ProcessLookupError:
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

