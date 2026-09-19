"""Dev only (LOOPCUT_DEV=1): reload changed modules on save and redraw, with Blender left open.

state.py is never reloaded and keeps the session in bpy.app.driver_namespace, so the
conversation on screen survives.
"""

import importlib
import sys
import traceback
from pathlib import Path

import bpy

_PACKAGE = __package__
_ROOT = Path(__file__).parent
# Dependency order: a module comes after everything it imports.
# state (the session) and settings (registered preferences) are left alone on purpose.
_MODULES = ("config", "credentials", "llm", "scene_diff", "object_info", "api_docs", "tools", "scene_context",
            "checkpoints", "conversations", "agent", "ui.theme", "ui.textedit", "ui.layout", "ui.draw", "ui.host")
_INTERVAL = 0.4
_mtimes: dict[str, float] = {}


def _path(name: str) -> Path:
    return _ROOT / (name.replace(".", "/") + ".py")


def _changed() -> list[str]:
    changed = []
    for name in _MODULES:
        mtime = _path(name).stat().st_mtime
        if _mtimes.get(name) not in (None, mtime):
            changed.append(name)
        _mtimes[name] = mtime
    return changed


def _tick() -> float:
    changed = _changed()
    if not changed:
        return _INTERVAL
    from .ui import host
    try:
        # host.py is last in the order, so it is always among the reloaded modules, and a reload
        # would orphan its registered classes and draw handler. Unregistering also drops focus;
        # Blender clears the modal handlers of an operator type being removed.
        host.unregister()
        for name in _MODULES[_MODULES.index(changed[0]):]:
            module = sys.modules.get(f"{_PACKAGE}.{name}")
            if module is not None:
                importlib.reload(module)
        host.register()
        print(f"Loopcut: reloaded after change to {', '.join(changed)}")
    except Exception:
        # A syntax error mid-edit must not kill the watcher; the next save retries.
        traceback.print_exc()
    host.tag_redraw_all()
    return _INTERVAL


def register() -> None:
    _changed()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
