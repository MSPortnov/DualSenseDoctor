"""HID devices in sysfs, Steam logs, driver detach/attach."""
import glob, os, re, time
from .base import HID_DIR, USER_HOME, read, write_sys

HID_BUS = "/sys/bus/hid"
DRIVER = "playstation"

def list_ds_hid():
    try:
        names = os.listdir(HID_DIR)
    except Exception:
        return []
    return sorted(n for n in names if ":054C:" in n.upper() and n.startswith("0005:"))


def hid_uniq(n):
    m = re.search(r"^HID_UNIQ=(.*)$", read(os.path.join(HID_DIR, n, "uevent")), re.M)
    return m.group(1).strip() if m and m.group(1).strip() else None


def hid_info(n):
    base = os.path.join(HID_DIR, n)
    drv = None
    try:
        drv = os.path.basename(os.readlink(os.path.join(base, "driver")))
    except OSError:
        pass
    inputs = [read(p + "/name").strip() for p in glob.glob(os.path.join(base, "input", "input*"))]
    leds = []
    for l in glob.glob("/sys/class/leds/*"):
        try:
            if n in os.path.realpath(l):
                leds.append(os.path.basename(l))
        except Exception:
            pass
    hidraw = [os.path.basename(x) for x in glob.glob(os.path.join(base, "hidraw", "hidraw*"))]
    motion = any("Motion Sensors" in i for i in inputs)
    touch = any("Touchpad" in i for i in inputs)
    problems = []
    if drv != "playstation":
        problems.append("driver=%s" % drv)
    if not motion:
        problems.append("no_motion")
    if not leds:
        problems.append("no_leds")
    return {"dev": n, "driver": drv, "inputs": inputs, "motion": motion, "touchpad": touch,
            "leds": leds, "hidraw": hidraw, "uniq": hid_uniq(n),
            "healthy": not problems, "problems": problems}

def find_hid(mac):
    for n in list_ds_hid():
        u = hid_uniq(n)
        if u is None or u.upper() == mac.upper():
            return n
    return None


def hidraw_holders(names):
    out = []
    targets = {"/dev/" + n for n in names}
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(fd) in targets:
                pid = fd.split("/")[2]
                out.append("%s:%s" % (pid, read("/proc/%s/comm" % pid).strip()))
        except Exception:
            pass
    return sorted(set(out))


STEAM_LOGS = ("controller.txt", "controller_ui.txt")
STEAM_MARKERS = ("Local Device Found", "device opened for index", "connected, configuring")


def steam_log_marks():
    """Remember the sizes of Steam's logs so that only new entries are seen later."""
    marks = {}
    for d in (USER_HOME + "/.local/share/Steam/logs", USER_HOME + "/.steam/steam/logs"):
        for name in STEAM_LOGS:
            path = os.path.realpath(os.path.join(d, name))
            try:
                marks[path] = os.path.getsize(path)
            except OSError:
                pass
    return marks


STEAM_BAD_SERIAL = "Invalid or missing unit serial number"


def steam_device_state(marks):
    """What Steam wrote to its logs after the mark. Returns (noticed, way):
    "hidapi" - the normal way (serial number read, lightbar and Steam Input layout work);
    "evdev"  - Steam opened the controller as an ordinary kernel device: serial number not read, default lightbar,
               buttons A and B swapped;
    None     - the way is not clear yet (Steam has not finished writing the lines)."""
    seen, path = False, None
    for fpath, size in marks.items():
        try:
            with open(fpath, "rb") as f:
                f.seek(0, 2)
                end = f.tell()
                f.seek(size if end >= size else 0)  # the log may have been reset
                data = f.read().decode(errors="replace")
        except OSError:
            continue
        if any(k in data for k in STEAM_MARKERS):
            seen = True
        i = data.rfind("Local Device Found")
        if i >= 0:
            tail = data[i:]
            if STEAM_BAD_SERIAL in tail:
                path = "evdev"
            elif "HIDAPI driver" in tail and path != "evdev":
                path = "hidapi"
    return seen, path


def steam_running():
    for c in glob.glob("/proc/[0-9]*/comm"):
        if read(c).strip() == "steam":
            return True
    return False


def sync_unbind(n):
    """Detach the current driver from the HID device (the hidraw and input nodes disappear, the link stays)."""
    try:
        drv = os.path.basename(os.readlink(os.path.join(HID_DIR, n, "driver")))
    except OSError:
        return False, ["no driver bound"]
    try:
        write_sys(HID_BUS + "/drivers/%s/unbind" % drv, n)
        return True, ["unbind(%s) ok" % drv]
    except OSError as e:
        return False, ["unbind(%s) err %s" % (drv, e)]


def sync_bind(n):
    """Attach the playstation driver (fallback: drivers_probe)."""
    try:
        write_sys(HID_BUS + "/drivers/playstation/bind", n)
        return ["bind(playstation) ok"]
    except OSError as e:
        steps = ["bind(playstation) err %s" % e]
        try:
            write_sys(HID_BUS + "/drivers_probe", n)
            steps.append("drivers_probe ok")
        except OSError as e2:
            steps.append("drivers_probe err %s" % e2)
        return steps


def sync_rebind(n):
    """Unbind from the current driver, then bind to playstation."""
    steps = []
    drv = None
    try:
        drv = os.path.basename(os.readlink(os.path.join(HID_DIR, n, "driver")))
    except OSError:
        pass
    if drv:
        try:
            write_sys(HID_BUS + "/drivers/%s/unbind" % drv, n)
            steps.append("unbind(%s) ok" % drv)
        except OSError as e:
            steps.append("unbind(%s) err %s" % (drv, e))
    else:
        steps.append("no driver bound")
    time.sleep(0.4)
    try:
        write_sys(HID_BUS + "/drivers/playstation/bind", n)
        steps.append("bind(playstation) ok")
    except OSError as e:
        steps.append("bind(playstation) err %s" % e)
        try:
            write_sys(HID_BUS + "/drivers_probe", n)
            steps.append("drivers_probe ok")
        except OSError as e2:
            steps.append("drivers_probe err %s" % e2)
    return steps


def driver_of(n):
    """Name of the driver bound to the HID device, or None."""
    try:
        return os.path.basename(os.readlink(os.path.join(HID_DIR, n, "driver")))
    except OSError:
        return None


def sync_restore(n, pause=0.4):
    """Attach the playstation driver and CHECK that it is really attached. The first step is the same plain bind as always
    (nothing is waited for when it works); only if the driver is still missing are up to two more attempts made:
    drivers_probe, then a full rebind. Returns (ok, steps)."""
    steps = []
    for i in range(3):
        if not os.path.isdir(os.path.join(HID_DIR, n)):
            steps.append("device is gone (the link dropped)")
            return False, steps
        if i == 0:
            steps += sync_bind(n)
        elif i == 1:
            try:
                write_sys(HID_BUS + "/drivers_probe", n)
                steps.append("retry: drivers_probe ok")
            except OSError as e:
                steps.append("retry: drivers_probe err %s" % e)
        else:
            steps += ["retry: " + x for x in sync_rebind(n)]
        if driver_of(n) == DRIVER:
            return True, steps
        steps.append("driver not attached yet (attempt %d)" % (i + 1))
        time.sleep(pause)
    return False, steps


def sync_orphans():
    """DualSense devices that have no driver at all (the plugin was killed while it held the driver detached): attach it."""
    out = []
    for n in list_ds_hid():
        if driver_of(n) is None:
            ok, steps = sync_restore(n)
            out.append((n, ok, steps))
    return out


def device_present(n):
    return os.path.isdir(os.path.join(HID_DIR, n))
