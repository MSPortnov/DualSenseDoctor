"""Settings, sessions and the event log on disk."""
import json
import os

from .base import DATA_DIR, DEFAULTS, MAX_SESSIONS, PLUGIN_LOG_DIR, RANGES, SETTINGS_DIR, iso, log, norm_range
import time


def atomic_write_json(path, obj, **kw):
    """Write to a temporary file in the same folder, flush it to disk, then replace the target in one step:
    a power cut or a crash never leaves a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, **kw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def validate_settings(saved):
    """Settings from a file: only known keys whose value has the right type and range. Everything else stays default."""
    out = json.loads(json.dumps(DEFAULTS))
    if not isinstance(saved, dict):
        return out
    for k, v in saved.items():
        if k not in DEFAULTS:
            continue
        if k == "lang":
            if v in ("ru", "en"):
                out[k] = v
        elif k in RANGES:
            n = norm_range(k, v)
            if n is not None:
                out[k] = n
        elif isinstance(DEFAULTS[k], bool):
            if isinstance(v, bool):
                out[k] = v
    return out


class Store:
    def __init__(self):
        for d in (SETTINGS_DIR, DATA_DIR, PLUGIN_LOG_DIR):
            os.makedirs(d, exist_ok=True)
        self.settings_path = os.path.join(SETTINGS_DIR, "settings.json")
        self.sessions_path = os.path.join(DATA_DIR, "sessions.json")
        self.events_path = os.path.join(DATA_DIR, "events.log")
        try:
            with open(self.settings_path) as f:
                saved = json.load(f)
        except Exception:
            saved = None
        self.settings = validate_settings(saved)
        try:
            with open(self.sessions_path) as f:
                loaded = json.load(f)
            self.sessions = [s for s in loaded if isinstance(s, dict)] if isinstance(loaded, list) else []
        except Exception:
            self.sessions = []
        self.next_id = max([s.get("id", 0) for s in self.sessions if isinstance(s.get("id", 0), int)] + [0]) + 1

    def save_settings(self):
        try:
            atomic_write_json(self.settings_path, self.settings, indent=1)
        except Exception as e:
            log.error("save settings: %s", e)

    def save_sessions(self):
        if len(self.sessions) > MAX_SESSIONS:
            del self.sessions[:-MAX_SESSIONS]
        clean = [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.sessions]
        try:
            atomic_write_json(self.sessions_path, clean, ensure_ascii=False)
        except Exception as e:
            log.error("save sessions: %s", e)

    def elog(self, kind, msg):
        try:
            if os.path.exists(self.events_path) and os.path.getsize(self.events_path) > 4_000_000:
                os.replace(self.events_path, self.events_path + ".1")
            with open(self.events_path, "a") as f:
                f.write("%s [%s] %s\n" % (iso(time.time()), kind, msg))
        except Exception as e:
            log.error("elog: %s", e)
