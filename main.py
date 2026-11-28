# DualSense Doctor - backend entry point (Decky Loader, runs as root).
# The logic lives in the package py_modules/dsd (Decky adds py_modules to sys.path; the path is also added here so that
# the plugin does not depend on it). Python standard library only.
import asyncio
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "py_modules"), _HERE):
    if os.path.isdir(os.path.join(_p, "dsd")) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dsd import covers, i18n, lab
    from dsd.config import log
    from dsd.engine import Engine
    IMPORT_ERROR = None
except Exception:  # the interface shows this text instead of silently failing to load
    IMPORT_ERROR = traceback.format_exc()
    i18n = lab = covers = Engine = None
    try:
        import decky
        log = decky.logger
    except Exception:
        import logging
        log = logging.getLogger("dsd")
    log.error("DualSense Doctor: import failed:\n%s", IMPORT_ERROR)

ENGINE = None
START_ERROR = IMPORT_ERROR


async def _start_engine():
    global ENGINE, START_ERROR
    if IMPORT_ERROR:
        return
    try:
        eng = Engine()
        ENGINE = eng
        await eng.start()
        log.info("DualSense Doctor started")
    except Exception:
        START_ERROR = traceback.format_exc()
        log.error("DualSense Doctor start failed:\n%s", START_ERROR)


def _guard():
    if ENGINE is None or START_ERROR:
        return {"error": START_ERROR or "engine not created"}
    return None


def _require_engine():
    g = _guard()
    if g:
        raise RuntimeError(g["error"])


class Plugin:
    async def _main(self):
        # do not block _main: the engine starts in the background, errors go to the log and to the UI
        self._boot = asyncio.ensure_future(_start_engine())

    async def _unload(self):
        boot = getattr(self, "_boot", None)
        if ENGINE:
            ENGINE.stopping = True
        elif boot:
            boot.cancel()
        if boot:
            await asyncio.gather(boot, return_exceptions=True)
        if ENGINE:
            await ENGINE.stop()

    async def get_cover(self, appid):
        """Game cover for the Lab grid (data: URI), an empty string if there is no file."""
        try:
            return await asyncio.get_running_loop().run_in_executor(None, covers.read_cover, int(appid))
        except Exception as e:
            log.error("get_cover(%s): %s", appid, e)
            return ""

    async def get_state(self):
        try:
            g = _guard()
            if g:
                return g
            st = await ENGINE.get_state()
            return st
        except Exception:
            tb = traceback.format_exc()
            log.error("get_state failed:\n%s", tb)
            return {"error": tb}

    async def set_lang(self, lang):
        """Language of messages and reports: the interface reports it on load and on change."""
        if i18n is None:
            return "en"
        if ENGINE is None:
            return i18n.set_lang(lang)
        return await ENGINE.set_lang(lang)

    async def set_setting(self, key, value):
        _require_engine()
        return await ENGINE.set_setting(key, value)

    async def build_report(self):
        _require_engine()
        return await ENGINE.build_report()

    # ------------------------------------------------------------------ Lab
    async def load_data(self) -> dict:
        """Return persisted per-game journal (or {} if none / unreadable)."""
        return lab.load_data()

    async def save_data(self, data: dict) -> bool:
        return lab.save_data(data)

    async def list_compat_tools(self) -> list:
        """Installed Proton builds: user-installed (GE etc.) plus Valve's own from steamapps/common."""
        return lab.list_compat_tools()

    async def get_compat_tool(self, appid: int) -> str:
        """Tool mapped to the app in config.vdf (falls back to the global default mapping)."""
        return lab.get_compat_tool(appid)
