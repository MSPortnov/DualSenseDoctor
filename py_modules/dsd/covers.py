"""Game covers for the Lab grid: lookup in Steam's files and reading as a data: URI."""
import base64
import glob
import os

from .config import USER_HOME

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
