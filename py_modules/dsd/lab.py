"""Backend of the Lab: the per-game journal file, the list of Proton builds, Steam's VDF files."""
import json
import os
import re
from pathlib import Path

from .config import SETTINGS_DIR, USER_HOME, log

LAB_FILE = Path(SETTINGS_DIR) / "lab.json"
HOME = Path(USER_HOME)
STEAM_ROOTS = [HOME / ".steam" / "steam", HOME / ".local" / "share" / "Steam"]


def parse_vdf(text: str) -> dict:
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


def ci_get(d, key: str):
    if not isinstance(d, dict):
        return None
    for k, v in d.items():
        if k.lower() == key.lower():
            return v
    return None


def valve_tool_name(folder: str):
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


def library_paths() -> list:
    paths, seen = [], set()

    def add(p: Path):
        if str(p) not in seen:
            seen.add(str(p))
            paths.append(p)

    for root in STEAM_ROOTS:
        add(root)
        lf = root / "steamapps" / "libraryfolders.vdf"
        if lf.is_file():
            try:
                node = ci_get(parse_vdf(lf.read_text(encoding="utf-8", errors="replace")), "libraryfolders")
                if isinstance(node, dict):
                    for v in node.values():
                        if isinstance(v, dict) and v.get("path"):
                            add(Path(v["path"]))
            except Exception as e:
                log.error(f"libraryfolders parse failed: {e}")
    return paths


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
        except OSError:
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

    for root in STEAM_ROOTS:
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
                    tools = ci_get(parse_vdf(vdf.read_text(encoding="utf-8", errors="replace")), "compatibilitytools")
                    tools = ci_get(tools, "compat_tools")
                    if isinstance(tools, dict) and tools:
                        name = next(iter(tools))
                        display = ci_get(tools[name], "display_name") or name
            except Exception as e:
                log.error(f"compat vdf parse failed for {sub}: {e}")
            if re.search("proton", name, re.I):  # skip runtimes such as scout_ldlp
                add(name, display, "custom")

    for lib in library_paths():
        common = lib / "steamapps" / "common"
        if not common.is_dir():
            continue
        for sub in sorted(common.iterdir()):
            tool = valve_tool_name(sub.name)
            if tool and (sub / "proton").is_file():
                add(tool, sub.name, "valve")
    return out

def get_compat_tool(appid: int) -> str:
    """Tool mapped to the app in config.vdf (falls back to the global default mapping)."""
    for root in STEAM_ROOTS:
        cfg = root / "config" / "config.vdf"
        if not cfg.is_file():
            continue
        try:
            node = parse_vdf(cfg.read_text(encoding="utf-8", errors="replace"))
            for part in ("InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping"):
                node = ci_get(node, part)
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
