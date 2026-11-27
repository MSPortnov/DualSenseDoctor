"""Statistics and the fix log over the list of sessions (pure functions)."""

# a session where Steam opened the controller the wrong way or missed it counts as "bad" for self-tuning
def bad_pickup(s):
    return s.get("steam_path") == "evdev" or "steam_pickup" in s.get("fixes_applied", [])


def compute_stats(sessions):
    """Per settings profile, how the connections after sleep ended: OK = nothing was needed, FIXED = something was
    actually corrected, the rest are failures. steam_rebind = how often a rebind was needed."""
    stats = {}
    for s in sessions:
        r = s.get("result")
        if not s.get("after_resume") or r in ("RUNNING", "ABORTED"):
            continue
        p = stats.setdefault(s["profile"], {"n": 0, "ok": 0, "fixed": 0, "failed": 0, "steam_rebind": 0, "_ttu": []})
        p["n"] += 1
        if "steam_pickup" in s.get("fixes_applied", []):
            p["steam_rebind"] += 1
        if r == "OK":
            p["ok"] += 1
        elif r == "FIXED":
            p["fixed"] += 1
        else:
            p["failed"] += 1
        if r in ("OK", "FIXED") and s.get("t_usable") is not None:
            p["_ttu"].append(s["t_usable"])
    for p in stats.values():
        t = p.pop("_ttu")
        p["avg_ttu"] = round(sum(t) / len(t), 1) if t else None
    return stats


def fix_log(sessions, limit=3):
    """The latest events where the plugin intervened or could not help (for the "Fix log" block)."""
    out = []
    for s in reversed(sessions):
        r = s.get("result")
        if (r == "FIXED" and s.get("fixes_applied")) or r in ("NOHID", "NOGYRO", "NOSTEAM"):
            out.append({k: s.get(k) for k in ("id", "iso", "result", "t_hid",
                                              "fixes_applied", "unknown_handle", "after_resume")})
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
