"""Settings, sessions and the event log on disk."""
import glob
import json
import os

from .config import DATA_DIR, DEFAULTS, log, MAX_SESSIONS, norm_range, PLUGIN_LOG_DIR, RANGES, SETTINGS_DIR
from .util import iso
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
        self.history_version_path = os.path.join(DATA_DIR, "history_version.json")
        try:
            with open(self.settings_path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = None
        self.settings = validate_settings(saved)
        try:
            with open(self.sessions_path) as f:
                loaded = json.load(f)
            self.sessions = [s for s in loaded if isinstance(s, dict)] if isinstance(loaded, list) else []
        except (OSError, ValueError):
            self.sessions = []
        self.next_id = max([s.get("id", 0) for s in self.sessions if isinstance(s.get("id", 0), int)] + [0]) + 1
        self.journal_enabled = bool(self.settings["action_journal"])
        self.persist_from_id = 1 if self.journal_enabled else self.next_id

    def clear_persisted_history(self):
        """Remove saved session statistics and Bluetooth action logs, preserving settings and adapter state."""
        paths = [self.sessions_path, self.events_path, self.events_path + ".1"]
        paths.extend(glob.glob(os.path.join(DATA_DIR, "btmon_*.txt")))
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.error("remove history %s: %s", path, e)

    def ensure_history_version(self, version):
        """Start a fresh on-disk history when the plugin version changes; keep settings intact."""
        try:
            with open(self.history_version_path) as f:
                saved = json.load(f)
            if isinstance(saved, dict) and saved.get("version") == version:
                return False
        except (OSError, ValueError):
            pass
        self.clear_persisted_history()
        self.sessions.clear()
        self.next_id = 1
        self.persist_from_id = 1
        try:
            atomic_write_json(self.history_version_path, {"version": version})
        except (OSError, TypeError, ValueError) as e:
            log.error("save history version: %s", e)
        return True

    def set_journal_enabled(self, enabled):
        if self.journal_enabled == enabled:
            return
        self.journal_enabled = enabled
        if self.journal_enabled:
            # Do not backfill sessions gathered while the journal was off.
            self.persist_from_id = self.next_id
        else:
            self.clear_persisted_history()

    def save_settings(self):
        try:
            atomic_write_json(self.settings_path, self.settings, indent=1)
        except (OSError, TypeError, ValueError) as e:
            log.error("save settings: %s", e)

    def save_sessions(self):
        if len(self.sessions) > MAX_SESSIONS:
            del self.sessions[:-MAX_SESSIONS]
        if not self.journal_enabled:
            return
        clean = [{k: v for k, v in s.items() if not k.startswith("_")}
                 for s in self.sessions
                 if self.should_persist_session(s)]
        try:
            atomic_write_json(self.sessions_path, clean, ensure_ascii=False)
        except (OSError, TypeError, ValueError) as e:
            log.error("save sessions: %s", e)

    def should_persist_session(self, session):
        sid = session.get("id")
        return self.journal_enabled and isinstance(sid, int) and sid >= self.persist_from_id

    def elog(self, kind, msg):
        if not self.journal_enabled:
            return
        try:
            if os.path.exists(self.events_path) and os.path.getsize(self.events_path) > 4_000_000:
                os.replace(self.events_path, self.events_path + ".1")
            with open(self.events_path, "a") as f:
                f.write("%s [%s] %s\n" % (iso(time.time()), kind, msg))
        except OSError as e:
            log.error("elog: %s", e)
