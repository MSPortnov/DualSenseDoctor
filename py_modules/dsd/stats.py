"""Statistics and the fix log over the list of sessions (pure functions)."""

def ready_result(s):
    """Overall result once the HID driver and its input devices have been verified."""
    steam_status = s.get("steam_status")
    if steam_status in ("wrong_mode", "not_seen", "unconfirmed"):
        return "NOSTEAM"
    if steam_status not in ("checking", "interrupted") and s.get("fixes_applied"):
        return "FIXED"
    return "OK"


def bad_pickup(s):
    steam_status = s.get("steam_status")
    return steam_status in ("wrong_mode", "not_seen") or s.get("steam_path") == "evdev" or had_rebind(s)


def had_rebind(s):
    return bool(s.get("steam_rebinds", "steam_pickup" in s.get("fixes_applied", [])))


def compute_stats(sessions):
    """Per settings profile, how the connections after sleep ended: OK = nothing was needed, FIXED = something was
    actually corrected, the rest are failures. steam_rebind = how often a rebind was needed."""
    stats = {}
    for s in sessions:
        r = s.get("result")
        if not s.get("after_resume") or r in ("RUNNING", "ABORTED") or s.get("steam_status") == "checking":
            continue
        p = stats.setdefault(s["profile"], {"n": 0, "ok": 0, "fixed": 0, "failed": 0, "steam_rebind": 0, "_hid_times": []})
        p["n"] += 1
        if had_rebind(s):
            p["steam_rebind"] += 1
        if r == "OK":
            p["ok"] += 1
        elif r == "FIXED":
            p["fixed"] += 1
        else:
            p["failed"] += 1
        hid_ready_at = s.get("t_hid_ready")
        if "t_hid_ready" not in s and r in ("OK", "FIXED"):
            hid_ready_at = s.get("t_usable")
        if hid_ready_at is not None:
            p["_hid_times"].append(hid_ready_at)
    for p in stats.values():
        t = p.pop("_hid_times")
        p["avg_hid_ready_s"] = round(sum(t) / len(t), 1) if t else None
    return stats


def session_summary(s):
    return {k: s.get(k) for k in ("id", "iso", "result", "t_hid", "t_hid_ready", "hid_status",
                                 "steam_status", "steam_seen_s", "steam_rebinds",
                                 "fixes_applied", "unknown_handle", "after_resume")}


def fix_log(sessions, limit=3):
    """The latest events where the plugin intervened or could not help (for the "Fix log" block)."""
    out = []
    for s in reversed(sessions):
        r = s.get("result")
        if (r == "FIXED" and s.get("fixes_applied")) or r in ("NOHID", "NOGYRO", "HIDERROR", "NOSTEAM"):
            out.append(session_summary(s))
        if len(out) >= limit:
            break
    return out


def pickup_streak_bad(sessions, current, bind_at):
    """Self-tuning rule: True if the two latest finished sessions that used the driver hold at this very return time
    both ended with Steam opening the controller wrongly (or missing it). `current` is the session that just ended."""
    before = sessions
    for i, x in enumerate(sessions):
        if x is current:  # identity, not equality: two sessions can have identical contents
            before = sessions[:i]
            break
    tracked = [s for s in before
               if s.get("after_resume") and s.get("steam_hold")
               and s.get("bind_at") == bind_at and s.get("steam_done")]
    if not (current.get("after_resume") and current.get("steam_hold") and current.get("bind_at") == bind_at):
        return False
    if not bad_pickup(current) or not tracked:
        return False
    return bad_pickup(tracked[-1])
