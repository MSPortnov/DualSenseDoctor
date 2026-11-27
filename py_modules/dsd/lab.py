"""Game covers, Proton list, Steam VDF files, LAN address (backend of the Lab)."""
import base64, glob, json, os, re, shutil, socket, struct, subprocess, time
from pathlib import Path
from .base import SETTINGS_DIR, USER_HOME, log

COVER_MAX_BYTES = 1_500_000
COVER_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
COVER_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def steam_root():
    for r in (os.path.join(USER_HOME, ".local/share/Steam"), os.path.join(USER_HOME, ".steam/steam")):
        if os.path.isdir(os.path.join(r, "appcache", "librarycache")) or os.path.isdir(os.path.join(r, "userdata")):
            return r
    return None


def find_cover_file(appid):
    """Path to the game's portrait cover (or None). Order: custom cover from userdata/*/config/grid
    (the only one for non-Steam games, overrides the stock one for Steam games), the stock cover from Steam's library cache,
    a wide picture from grid (cropped to the center in the card)."""
    root = steam_root()
    if not root:
        return None
    aid = int(appid) & 0xFFFFFFFF
    grids = glob.glob(os.path.join(root, "userdata", "*", "config", "grid"))
    for ext in COVER_EXTS:
        for g in grids:
            p = os.path.join(g, "%dp%s" % (aid, ext))
            if os.path.isfile(p):
                return p
    base = os.path.join(root, "appcache", "librarycache")
    found = []
    for dirpath, _dirs, files in os.walk(os.path.join(base, str(aid))):
        for f in files:
            if f.lower().startswith("library_600x900") and os.path.splitext(f)[1].lower() in COVER_MIME:
                found.append(os.path.join(dirpath, f))
    for ext in COVER_EXTS:  # old cache layout: files lie in the root
        p = os.path.join(base, "%d_library_600x900%s" % (aid, ext))
        if os.path.isfile(p):
            found.append(p)

    def rank(path):
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        return (0 if stem == "library_600x900" else 1, len(stem))

    for p in sorted(found, key=rank):
        try:
            if os.path.getsize(p) <= COVER_MAX_BYTES:
                return p
        except OSError:
            continue
    for ext in COVER_EXTS:
        for g in grids:
            p = os.path.join(g, "%d%s" % (aid, ext))
            if os.path.isfile(p):
                return p
    return None


def read_cover(appid):
    """data: URI of the cover, or an empty string."""
    p = find_cover_file(appid)
    if not p:
        return ""
    try:
        if os.path.getsize(p) > COVER_MAX_BYTES * 2:
            return ""
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return "data:%s;base64,%s" % (COVER_MIME.get(os.path.splitext(p)[1].lower(), "image/png"), base64.b64encode(raw).decode())
LAB_FILE = Path(SETTINGS_DIR) / "lab.json"
FX_HOME = Path(USER_HOME)
FX_STEAM_ROOTS = [FX_HOME / ".steam" / "steam", FX_HOME / ".local" / "share" / "Steam"]


def _fx_parse_vdf(text: str) -> dict:
    """Minimal Valve KeyValues parser: enough for config.vdf and compatibilitytool.vdf."""
    toks = re.findall(r'"((?:[^"\\]|\\.)*)"|([{}])', text)
    pos = 0

    def parse_obj() -> dict:
        nonlocal pos
        obj: dict = {}
        while pos < len(toks):
            s, brace = toks[pos]
            if brace == "}":
                pos += 1
                return obj
            if brace == "{":
                pos += 1
                continue
            key = s
            pos += 1
            if pos >= len(toks):
                break
            s2, brace2 = toks[pos]
            if brace2 == "{":
                pos += 1
                obj[key] = parse_obj()
            else:
                obj[key] = s2
                pos += 1
        return obj

    return parse_obj()


def _fx_ci_get(d, key: str):
    if not isinstance(d, dict):
        return None
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


def _fx_valve_tool_name(folder: str):
    """Map a Valve Proton folder name in steamapps/common to Steam's internal tool name."""
    low = folder.lower()
    if low == "proton - experimental":
        return "proton_experimental"
    if low == "proton hotfix":
        return "proton_hotfix"
    m = re.match(r"^proton (\d+)\.(\d+)", low)
    if not m:
        return None
    major, minor = int(m.group(1)), int(m.group(2))
    if major >= 7 or (major == 5 and minor == 0):
        return f"proton_{major}"
    return f"proton_{major}{minor}"


def _fx_library_paths() -> list:
    paths, seen = [], set()

    def add(p: Path):
        if str(p) not in seen:
            seen.add(str(p))
            paths.append(p)

    for root in FX_STEAM_ROOTS:
        add(root)
        lf = root / "steamapps" / "libraryfolders.vdf"
        if lf.is_file():
            try:
                node = _fx_ci_get(_fx_parse_vdf(lf.read_text(encoding="utf-8", errors="replace")), "libraryfolders")
                if isinstance(node, dict):
                    for v in node.values():
                        if isinstance(v, dict) and v.get("path"):
                            add(Path(v["path"]))
            except Exception as e:
                log.error(f"libraryfolders parse failed: {e}")
    return paths



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
        except Exception:
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


def _lan_ip():
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


# ---------------------------------------------------------------- Lab data (called by the Plugin class)
def load_data() -> dict:
    """Return persisted per-game journal (or {} if none / unreadable)."""
    try:
        return json.loads(LAB_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # corrupt file: keep a copy, start clean
        log.error(f"load_data failed: {e}")
        try:
            LAB_FILE.replace(LAB_FILE.with_suffix(".bak"))
        except Exception:
            pass
        return {}

def save_data(data: dict) -> bool:
    try:
        LAB_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LAB_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, LAB_FILE)
        return True
    except Exception as e:
        log.error(f"save_data failed: {e}")
        return False

def list_compat_tools() -> list:
    """Installed Proton builds: user-installed (GE etc.) plus Valve's own from steamapps/common."""
    out, seen = [], set()

    def add(name: str, display: str, kind: str):
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "display": display, "kind": kind})

    for root in FX_STEAM_ROOTS:
        d = root / "compatibilitytools.d"
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            if not sub.is_dir():
                continue
            name, display = sub.name, sub.name
            vdf = sub / "compatibilitytool.vdf"
            try:
                if vdf.is_file():
                    tools = _fx_ci_get(_fx_parse_vdf(vdf.read_text(encoding="utf-8", errors="replace")), "compatibilitytools")
                    tools = _fx_ci_get(tools, "compat_tools")
                    if isinstance(tools, dict) and tools:
                        name = next(iter(tools))
                        display = _fx_ci_get(tools[name], "display_name") or name
            except Exception as e:
                log.error(f"compat vdf parse failed for {sub}: {e}")
            if re.search("proton", name, re.I):  # skip runtimes such as scout_ldlp
                add(name, display, "custom")

    for lib in _fx_library_paths():
        common = lib / "steamapps" / "common"
        if not common.is_dir():
            continue
        for sub in sorted(common.iterdir()):
            tool = _fx_valve_tool_name(sub.name)
            if tool and (sub / "proton").is_file():
                add(tool, sub.name, "valve")
    return out

def get_compat_tool(appid: int) -> str:
    """Tool mapped to the app in config.vdf (falls back to the global default mapping)."""
    for root in FX_STEAM_ROOTS:
        cfg = root / "config" / "config.vdf"
        if not cfg.is_file():
            continue
        try:
            node = _fx_parse_vdf(cfg.read_text(encoding="utf-8", errors="replace"))
            for part in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
                node = _fx_ci_get(node, part)
                if node is None:
                    break
            if isinstance(node, dict):
                entry = node.get(str(appid)) or node.get("0")
                if isinstance(entry, dict):
                    return entry.get("name", "") or ""
            return ""
        except Exception as e:
            log.error(f"get_compat_tool failed: {e}")
    return ""
