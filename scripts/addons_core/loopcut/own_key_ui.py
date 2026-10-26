"""The own-key check in Blender (own_key.py): started after the key, the base URL or the model changes, and shown
under them in Preferences (settings.py) and on onboarding's own-key screen, and, while it is a failure, as one line
above the chat panel's composer (ui/layout.py), so a user who never opens Preferences reads it before a turn fails.

Never on launch or when a panel opens: only settings.py's update callbacks (and Check again) call request(). A burst
of changes makes one check (DEBOUNCE), its requests run on a worker thread, and whatever touches bpy goes through
mainthread. Not for Loopcut's account or the ChatGPT plan (own_key.HOSTED), which have their own readiness.
"""

import threading

import bpy

from . import config, credentials, mainthread, own_key

DEBOUNCE = 1.5  # Seconds after the last change: a script, a menu pick and the provider switch each set several fields.
AGAIN_DELAY = 0.1

_result = {"status": "idle", "code": "", "text": "", "settings": None}  # status: idle | checking | done
_cancel = threading.Event()
_again = False
_looked: set = set()  # The settings whose saved probe result status() looked up already (one file read each).


def _settings(prefs) -> tuple:
    return prefs.provider, str(getattr(prefs, "base_url", "")).strip().rstrip("/"), str(getattr(prefs, "model", "")).strip()


def _prefs():
    from . import settings
    prefs = settings.preferences()
    return prefs if prefs is not None and prefs.provider not in own_key.HOSTED else None


def _about(prefs, key: str = "") -> dict:
    from . import settings
    return own_key.who(prefs.provider, settings.provider_label(prefs.provider), _settings(prefs)[1], key)


def _redraw() -> None:
    mainthread.request_redraw()


def _usable(prefs) -> tuple[str, str] | None:
    """(base URL, key) when there is something to check: an https endpoint with a key, or one on this computer.
    Otherwise the fields above already say what's missing (config.load's error, the key field)."""
    base_url = _settings(prefs)[1]
    try:
        config._check_base_url(base_url, "Base URL")
    except config.ConfigError:
        return None
    key = credentials.api_key(base_url) if base_url else ""
    return (base_url, key) if base_url and (key or base_url.startswith("http://127.0.0.1")) else None


def request(again: bool = False) -> None:
    """Main thread, after the key, the base URL or the model changed (settings.py), or Check again: restart the wait,
    so the last of a burst of changes starts the one check."""
    global _again
    _again = _again or again
    _cancel.set()  # A check of the earlier settings would show a stale answer.
    prefs = _prefs()
    # Headless: no one to show it to. Without a key (or a URL) yet, the fields already say so; saving one asks again.
    if prefs is None or bpy.app.background or _usable(prefs) is None:
        _result.update(status="idle", code="", text="", settings=None)
        return
    _result.update(status="checking", code="", text="", settings=_settings(prefs))
    timers = bpy.app.timers
    if timers.is_registered(_start):
        timers.unregister(_start)
    timers.register(_start, first_interval=AGAIN_DELAY if again else DEBOUNCE)


def _start():
    """Timer, main thread: read the settings and the key here, then check on a worker."""
    global _cancel, _again
    again, _again = _again, False
    prefs = _prefs()
    usable = _usable(prefs) if prefs is not None else None
    if usable is None:
        _result.update(status="idle", code="", text="", settings=None)
        _redraw()
        return None
    base_url, key = usable
    try:
        folder = credentials.folder()
    except OSError:
        folder = None  # Nothing saved: the probe may run again next time.
    settings = _settings(prefs)
    about, model = _about(prefs, key), settings[2]
    cancel = threading.Event()
    _cancel = cancel
    _result.update(status="checking", code="", text="", settings=settings)
    _redraw()

    def run():
        try:
            result = own_key.check(about, base_url, key, model, folder, again=again, cancelled=cancel.is_set)
        except Exception:  # The worker always reports back.
            import traceback
            traceback.print_exc()
            result = {"code": own_key.OTHER, "text": own_key.message(own_key.OTHER, about, model)}
        mainthread.run_on_main(lambda: _finished(result, settings, cancel))
    threading.Thread(target=run, name="loopcut-own-key-check", daemon=True).start()
    return None


def _finished(result: dict, settings: tuple, cancel) -> None:
    prefs = _prefs()
    if cancel.is_set() or result.get("code") == "cancelled" or prefs is None or _settings(prefs) != settings:
        return
    _result.update(status="done", code=result["code"], text=result["text"], settings=settings)
    _redraw()


def status(prefs=None) -> dict:
    """The check for the settings as they are now (prefs: the page's own, else Loopcut's): {status, code, text}.
    After a relaunch, the saved probe result of these settings, with no request."""
    if prefs is None:
        prefs = _prefs()
    elif prefs.provider in own_key.HOSTED:
        prefs = None
    if prefs is None:
        return {"status": "idle", "code": "", "text": ""}
    settings = _settings(prefs)
    if _result["settings"] != settings and settings not in _looked:
        _looked.add(settings)
        try:
            _show_saved(prefs, settings)
        except Exception as ex:  # Drawn from a panel: a saved result that can't be read is no result.
            print(f"Loopcut: could not read the saved key check: {type(ex).__name__}")
    if _result["settings"] != settings:
        return {"status": "idle", "code": "", "text": ""}
    return {key: _result[key] for key in ("status", "code", "text")}


def _show_saved(prefs, settings: tuple) -> None:
    usable = _usable(prefs)
    if usable is None or not settings[2]:
        return
    known = own_key.saved(credentials.folder(), usable[1], usable[0], settings[2])
    if known is not None:
        about = _about(prefs, usable[1])
        _result.update(status="done", code=known["code"], settings=settings,
                       text=own_key.message(known["code"], about, settings[2], known.get("detail", "")))


def panel_line() -> str:
    """The line above the chat's composer: only a failure, so the panel stays quiet once the key works."""
    shown = status()
    code = shown["code"]
    if shown["status"] != "done" or code in own_key.WORKS or code in own_key.PASSING:
        return ""
    return shown["text"]


def draw(layout, prefs) -> None:
    """Under the key and model fields of the page drawing prefs: Checking…, then the result and a small Check
    again."""
    from . import settings
    shown = status(prefs)
    if shown["status"] == "checking":
        layout.label(text=f"Checking with {_about(prefs)['name']}…", icon="TIME")
        return
    if shown["status"] != "done" or not shown["text"]:
        return
    code = shown["code"]
    icon = "CHECKMARK" if code in own_key.WORKS else "INFO" if code in own_key.PASSING else "ERROR"
    column = layout.column(align=True)
    lines = settings.wrap(shown["text"], 70)
    for n, line in enumerate(lines):
        row = column.row(align=True)
        row.alignment = "LEFT"  # Each item at its own width: the whole sentence, then the link right after it.
        row.label(text=line, icon=icon if n == 0 else "BLANK1")
        if n == len(lines) - 1:
            row.operator("loopcut.own_key_check", text="Check again", emboss=False)


class LOOPCUT_OT_own_key_check(bpy.types.Operator):
    """Check this key, base URL and model with your provider again"""
    bl_idname = "loopcut.own_key_check"
    bl_label = "Check Again"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        request(again=True)
        return {"FINISHED"}


_CLASSES = (LOOPCUT_OT_own_key_check,)


def register() -> None:
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister() -> None:
    _cancel.set()
    if not bpy.app.background and bpy.app.timers.is_registered(_start):
        bpy.app.timers.unregister(_start)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
