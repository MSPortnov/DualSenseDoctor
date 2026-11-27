"""Bluetooth report: collecting, masking MAC addresses, safe creation of the files, removal of old reports."""
import glob
import json
import os
import pwd
import re
import shutil
import tarfile
import time

from .base import DATA_DIR, DECKY_USER, PLUGIN_LOG_DIR, USER_HOME, VERSION, first_hci, log, read, run, tr
from .stats import compute_stats

# AA:BB:CC:DD:EE:FF or AA_BB_CC_DD_EE_FF (the form in D-Bus paths): the last three bytes are replaced with XX,
# the first three (the vendor part) stay so that the controller model is still recognisable.
MAC_RE = re.compile(r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f][:_])([0-9A-Fa-f]{2}([:_])[0-9A-Fa-f]{2}\2[0-9A-Fa-f]{2})\2"
                    r"[0-9A-Fa-f]{2}\2[0-9A-Fa-f]{2}\2[0-9A-Fa-f]{2}(?![0-9A-Fa-f])(?![:_][0-9A-Fa-f])")
REPORT_RE = re.compile(r"^dualsense-doctor-\d{8}-\d{6}(?:-\d+)?(?:-summary\.txt|\.tar\.gz)$")


def mask_macs(text):
    return MAC_RE.sub(lambda m: "%s%sXX%sXX%sXX" % (m.group(1), m.group(2), m.group(2), m.group(2)), text)


def _owner():
    try:
        pw = pwd.getpwnam(DECKY_USER)
        return pw.pw_uid, pw.pw_gid
    except Exception:
        return None


def create_file(path):
    """Create a NEW file: O_EXCL fails if anything already exists at that path, O_NOFOLLOW refuses a symlink.
    The plugin runs as root and the folder belongs to the user, so a planted link must never be followed.
    The owner is set on the open descriptor, not on the path."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        own = _owner()
        if own and os.geteuid() == 0:
            os.fchown(fd, own[0], own[1])
    except Exception:
        pass
    return fd


def new_report_paths(out_dir, name):
    """Paths of the archive and the summary; a numeric suffix is added in the unlikely case the names are taken."""
    for i in range(100):
        base = name if i == 0 else "%s-%d" % (name, i)
        tar_path = os.path.join(out_dir, base + ".tar.gz")
        sum_path = os.path.join(out_dir, base + "-summary.txt")
        if not os.path.lexists(tar_path) and not os.path.lexists(sum_path):
            return tar_path, sum_path
    raise OSError("no free report name")


def write_text_file(path, text):
    fd = create_file(path)
    with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as f:
        f.write(text)


def write_tar(path, work_dir, arcname):
    fd = create_file(path)
    with os.fdopen(fd, "wb") as raw:
        with tarfile.open(fileobj=raw, mode="w:gz") as tf:
            tf.add(work_dir, arcname=arcname)


def remove_old_reports(out_dir, keep=()):
    """Delete earlier reports (only files named exactly like ours; a link is removed itself, never followed)."""
    keep = {os.path.abspath(k) for k in keep}
    n = 0
    for p in glob.glob(os.path.join(out_dir, "dualsense-doctor-*")):
        if os.path.abspath(p) in keep or not REPORT_RE.match(os.path.basename(p)):
            continue
        try:
            if os.path.islink(p) or os.path.isfile(p):
                os.unlink(p)
                n += 1
        except OSError as e:
            log.error("remove old report %s: %s", p, e)
    return n


async def journal_filtered(args, pattern, tail):
    rc, out = await run(["journalctl", "--no-pager", "-o", "short-iso-precise"] + args, 30)
    if rc != 0:
        return "rc=%s %s" % (rc, out[:200])
    lines = [l for l in out.splitlines() if re.search(pattern, l, re.I)]
    return "\n".join(lines[-tail:])

async def sh(cmds, timeout=10):
    res = ""
    for cmd in cmds:
        rc, out = await run(cmd, timeout)
        res += "$ %s   (rc=%s)\n%s\n\n" % (" ".join(cmd), rc, out.strip())
    return res

async def build_report(eng):
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = "dualsense-doctor-%s" % ts
    work = os.path.join(DATA_DIR, "report_tmp", name)
    os.makedirs(work, exist_ok=True)

    def wf(rel, text):
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", errors="replace") as f:
            f.write(mask_macs(text))

    def cp(src, rel):
        """Copy a text file into the report with the MAC addresses masked (never a plain copy)."""
        wf(rel, read(src))

    macs = eng.ds_macs()
    wf("system.txt", await sh([["uname", "-a"], ["cat", "/etc/os-release"], ["cat", "/proc/cmdline"],
                                    ["cat", "/sys/power/mem_sleep"], ["bluetoothctl", "--version"],
                                    ["systemctl", "is-active", "bluetooth"], ["uptime"]]))
    wf("bluetooth.txt", await sh(
        [["lsusb"], ["lsusb", "-t"], ["hciconfig", "-a"], ["bluetoothctl", "show"],
         ["bluetoothctl", "devices", "Paired"], ["btmgmt", "info"], ["rfkill", "list"]] +
        [["bluetoothctl", "info", m] for m in macs] + [["hcitool", "con"]]))
    hci = first_hci()
    usbdev = os.path.realpath("/sys/class/bluetooth/%s/device/.." % hci)
    up = "usb device: %s\n" % usbdev
    for f in ("power/control", "power/wakeup", "power/autosuspend_delay_ms", "power/runtime_status"):
        up += "%s = %s\n" % (f, read(os.path.join(usbdev, f)).strip())
    wf("usb_power.txt", up)
    wk = ""
    for k, (nm, dev, ev, wc) in sorted(eng.read_wakeup().items()):
        wk += "%s name=%s device=%s event_count=%s wakeup_count=%s\n" % (k, nm, dev, ev, wc)
    wf("wakeup_sources.txt", wk)
    params = ""
    for mod in ("btusb", "bluetooth", "btintel"):
        for f in sorted(glob.glob("/sys/module/%s/parameters/*" % mod)):
            params += "%s.%s = %s\n" % (mod, os.path.basename(f), read(f).strip())
    for f in ("/etc/bluetooth/main.conf", "/etc/bluetooth/input.conf"):
        lines = [l for l in read(f).splitlines() if l.strip() and not l.strip().startswith("#")]
        params += tr("\n## %s (без комментариев)\n%s\n", "\n## %s (without comments)\n%s\n") % (f, "\n".join(lines))
    wf("bt_params.txt", params)
    wf("hid_now.txt", await eng.collect_diag(macs[0] if macs else None))
    pat_k = r"bluetooth|hci\d|hid|playstation|uhid|PM:|btusb|btintel|usb|input:|wake|suspend|resume|ACPI"
    wf("journal_kernel_boot.txt", await journal_filtered(["-k", "-b"], pat_k, 6000))
    wf("journal_kernel_prev_boot.txt", await journal_filtered(["-k", "-b", "-1"], pat_k, 6000))
    wf("journal_bluetooth_boot.txt", await journal_filtered(["-u", "bluetooth", "-b"], r"^(?!.*Endpoint registered)", 1500))
    wf("journal_sleep_boot.txt", await journal_filtered(["-b"], r"PM: (suspend|hibernation)|systemd-sleep|Waking up|sleep\.target|Entering sleep|Finished.*sleep", 400))
    seen = set()
    for d in (USER_HOME + "/.local/share/Steam/logs", USER_HOME + "/.steam/steam/logs"):
        for f in glob.glob(d + "/*ontroller*") + glob.glob(d + "/*luetooth*"):
            rp = os.path.realpath(f)
            if rp in seen or not os.path.isfile(rp):
                continue
            seen.add(rp)
            wf("steam_logs/" + os.path.basename(f), "".join(read(rp).splitlines(True)[-500:]))
    for f in (eng.store.events_path, eng.store.events_path + ".1", eng.store.sessions_path,
              eng.store.settings_path):
        if os.path.exists(f):
            cp(f, os.path.basename(f))
    for f in glob.glob(os.path.join(DATA_DIR, "btmon_*.txt")):
        cp(f, "btmon/" + os.path.basename(f))
    for f in glob.glob(os.path.join(PLUGIN_LOG_DIR, "*.log")):
        cp(f, "plugin_" + os.path.basename(f))

    # a single text file for sending quickly
    summ = ["# DualSense Doctor %s report %s" % (VERSION, ts), "profile now: " + eng.profile(),
            "settings: " + json.dumps(eng.public_settings()), "",
            "## stats " + tr("(эпизоды после сна)", "(episodes after sleep)"), json.dumps(compute_stats(eng.sessions), ensure_ascii=False, indent=1), "",
            "## sessions " + tr("(последние 40)", "(last 40)")]
    for s in eng.sessions[-40:]:
        summ.append(json.dumps({k: v for k, v in s.items() if not k.startswith("_")}, ensure_ascii=False))
    for rel in ("system.txt", "bluetooth.txt", "usb_power.txt", "bt_params.txt", "hid_now.txt", "journal_sleep_boot.txt"):
        summ += ["", "## " + rel, read(os.path.join(work, rel))]
    for f in sorted(glob.glob(os.path.join(work, "steam_logs", "*"))):
        summ += ["", "## steam log %s " % os.path.basename(f) + tr("(последние 150 строк)", "(last 150 lines)")] + read(f).splitlines()[-150:]
    ev = read(eng.store.events_path).splitlines()
    summ += ["", "## events.log " + tr("(последние 1500 строк)", "(last 1500 lines)")] + ev[-1500:]
    bt = sorted(glob.glob(os.path.join(DATA_DIR, "btmon_*.txt")))
    bad = [f for f in bt if not re.search(r"_(OK|FIXED|END)\.txt$", f)][-2:]
    okf = [f for f in bt if f.endswith("_FIXED.txt")][-1:] + [f for f in bt if f.endswith("_OK.txt")][-1:]
    endf = [f for f in bt if f.endswith("_END.txt")][-3:]
    for f in endf:
        summ += ["", "## btmon %s " % os.path.basename(f) + tr("(разрыв, без потока отчётов)", "(drop, without the report stream)")] + read(f).splitlines()[-450:]
    for f in bad + okf:
        summ += ["", "## btmon %s " % os.path.basename(f) + tr("(первые 450 строк без потока отчётов)", "(first 450 lines without the report stream)")] + read(f).splitlines()[:450]
    summary_text = mask_macs("\n".join(summ)[:1_800_000])
    wf("summary.txt", summary_text)

    out_dir = USER_HOME if os.path.isdir(USER_HOME) else DATA_DIR
    tar_path, sum_path = new_report_paths(out_dir, name)
    written = False
    try:
        write_tar(tar_path, work, name)
        write_text_file(sum_path, summary_text)
        written = True
    except Exception as e:
        eng.elog("REPORT", "write failed: %s" % e)
        log.error("report write: %s", e)
    shutil.rmtree(os.path.join(DATA_DIR, "report_tmp"), ignore_errors=True)
    removed = remove_old_reports(out_dir, keep=(tar_path, sum_path)) if written else 0
    eng.last_report = {"tar": tar_path, "summary": sum_path,
                       "size_kb": round(os.path.getsize(tar_path) / 1024) if os.path.exists(tar_path) else 0}
    eng.elog("REPORT", json.dumps(eng.last_report) + " old removed: %d" % removed)
    return eng.last_report
