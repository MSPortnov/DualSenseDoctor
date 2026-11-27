"""LAN address of this device (shown in the panel as the target of the helper commands)."""
import re
import shutil
import socket
import struct
import subprocess
import time

from .config import log

_IP_CACHE = {"ts": -1e9, "ip": ""}


def _usable_ip(ip):
    return bool(ip) and not ip.startswith("127.") and not ip.startswith("169.254.") and ip != "0.0.0.0"


def _ip_via_default_route():
    """Address of the interface that holds the default route (/proc/net/route + SIOCGIFADDR): no commands needed."""
    import fcntl
    iface = ""
    with open("/proc/net/route") as f:
        next(f)
        for line in f:
            p = line.split()
            if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 2:
                iface = p[0]
                break
    if not iface:
        return ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))  # SIOCGIFADDR
        return socket.inet_ntoa(r[20:24])
    finally:
        s.close()


def _ip_via_udp_connect():
    """UDP connect sends nothing, it only makes the kernel pick the outgoing address."""
    for target in ("10.254.254.254", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 1))
            ip = s.getsockname()[0]
            if _usable_ip(ip):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    return ""


def _ip_via_command():
    path = "/usr/bin:/bin:/usr/sbin:/sbin"
    ipc = shutil.which("ip", path=path)
    if ipc:
        out = subprocess.run([ipc, "-4", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"\bsrc (\d+\.\d+\.\d+\.\d+)", out)
        if m and _usable_ip(m.group(1)):
            return m.group(1)
    hn = shutil.which("hostname", path=path)
    if hn:
        out = subprocess.run([hn, "-I"], capture_output=True, text=True, timeout=3).stdout.split()
        for x in out:
            if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", x) and _usable_ip(x):
                return x
    return ""


def lan_ip():
    """LAN IPv4 address of this device (empty string if unknown). Cached for 30 s."""
    now = time.monotonic()
    if _IP_CACHE["ip"] and now - _IP_CACHE["ts"] < 30:
        return _IP_CACHE["ip"]
    ip = ""
    for fn in (_ip_via_default_route, _ip_via_udp_connect, _ip_via_command):
        try:
            ip = fn()
        except Exception as e:
            log.info("lan ip: %s failed: %s", fn.__name__, e)
            ip = ""
        if _usable_ip(ip):
            break
        ip = ""
    _IP_CACHE.update(ts=now, ip=ip)
    return ip
