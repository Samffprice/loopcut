"""Repair Blender 5.2's stale driver relations after shape_key_remove.

Removing a driven key block frees its FCurve without tagging relations. Read before
evaluation, then briefly add/remove a driver to tag them before Blender rebuilds the
graph. No package imports: workspace Blenders also run this file with --python.
The count fast path observes changes between readings, not every RNA mutation.
"""

import json
import re
import traceback

import bpy

NOTICE_PREFIX = "Loopcut: key block"
_BLOCK = re.compile(r'key_blocks\["((?:[^"\\]|\\.)*)"\]')
_REFRESH_PATH = "loopcut_shape_key_guard_refresh"
# load_post: the reading a removal is compared with, for keys in a file loaded after install (the shell
# shim installs before Blender loads the agent's .blend, and a script may open another file).
_HANDLERS = ("depsgraph_update_pre", "frame_change_pre", "save_pre", "load_post")
# Only Python values, never references to RNA that a removal or file load may free.
# Keep the last reading and pending notices through importlib.reload.
_state = globals().get("_state", {})
_notices = globals().get("_notices", [])
_echo = False
_checking = False


def _driven(key):
    paths = {}
    ad = key.animation_data
    for curve in ad.drivers if ad else ():
        path = curve.data_path
        match = _BLOCK.match(path)
        if match:
            # RNA escapes quotes and backslashes, not JSON's Unicode/control escapes.
            name = re.sub(r'\\(.)', r'\1', match[1])
            paths.setdefault(name, []).append(path)
    return paths


def _rebuild(drivers):
    path = _REFRESH_PATH
    occupied = {curve.data_path for curve in drivers}
    while path in occupied:
        path += "_"
    curve = drivers.new(path)
    # Remove immediately, before formatting or emitting anything that could fail.
    drivers.remove(curve)


def _read():
    seen = {}
    for key in bpy.data.shape_keys:
        uid = key.session_uid
        ad = key.animation_data
        counts = (len(key.key_blocks), len(ad.drivers) if ad else 0)
        old = _state.get(uid)
        if old is not None and old[0] == counts:
            seen[uid] = old
            continue
        driven = _driven(key)
        seen[uid] = (counts, driven)
        if old is None or counts[1] >= old[0][1]:
            continue
        lost = old[1].keys() - driven.keys()
        if not lost or ad is None:
            continue
        # Also rebuild after remove + re-add with the same name; it freed the old
        # FCurve even though a block of that name exists again. driver_remove first
        # already tags relations: this extra rebuild is harmless and stays quiet.
        _rebuild(ad.drivers)
        gone = sorted(lost - {block.name for block in key.key_blocks})
        if not gone:
            continue
        names = ", ".join(json.dumps(name, ensure_ascii=False) for name in gone)
        paths = ", ".join(path.replace("\n", "\\n").replace("\r", "\\r")
                          for name in gone for path in old[1][name])
        drivers = "driver" if sum(len(old[1][name]) for name in gone) == 1 else "drivers"
        plural = len(gone) > 1
        user = key.user
        owner = f" ({user.id_type.lower()} {json.dumps(user.name, ensure_ascii=False)})" if user else ""
        notice = (f"{NOTICE_PREFIX}{'s' if plural else ''} {names} of shape keys "
                  f"{json.dumps(key.name, ensure_ascii=False)}{owner} "
                  f"{'were' if plural else 'was'} removed and Blender removed "
                  f"{'their' if plural else 'its'} {drivers} with {'them' if plural else 'it'} "
                  f"({paths}); the dependency graph was rebuilt so Blender 5.2 "
                  f"does not evaluate the removed {drivers}.")
        _notices.append(notice)
        if _echo:
            print(notice, flush=True)
    _state.clear()
    _state.update(seen)


def check():
    """Read without evaluating; guard failures must not abort the caller's script."""
    global _checking
    if _checking:
        return
    _checking = True
    try:
        _read()
    except Exception:
        # Deliberate callback boundary: report the bug, never propagate into Blender.
        traceback.print_exc()
    finally:
        _checking = False


def _loopcut_shape_key_guard(*_args):
    check()


# What bpy.app.handlers.persistent does, without touching handlers at import time.
_loopcut_shape_key_guard._bpy_persistent = None
_loopcut_shape_key_guard._loopcut_shape_key_guard = True


def install(echo=False):
    """Replace older handlers by marker/name, including those from a module reload."""
    global _echo
    _echo = echo
    for name in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        for handler in list(handlers):
            if (getattr(handler, "_loopcut_shape_key_guard", False)
                    or getattr(handler, "__name__", "") == "_loopcut_shape_key_guard"):
                handlers.remove(handler)
        handlers.append(_loopcut_shape_key_guard)
    check()


def take():
    """Return and clear notices since the last tool call."""
    notices = list(_notices)
    _notices.clear()
    return notices


if __name__ == "__main__":
    install(echo=True)
