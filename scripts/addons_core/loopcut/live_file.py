"""The user's open file stays theirs: nothing the model's code runs in the live Blender changes
bpy.data.filepath, so the user's next Cmd+S writes where they saved, not into Loopcut's folders (C28).

Why: in the v0.2.0 RC smoke test (2026-10-09, 2 of 7 jobs) the workspace agent built the scene live
with run_python, then saved it with bpy.ops.wm.save_as_mainfile(<workspace>/x.blend) to render a
preview of it with run_blender. A plain Save As rebinds the open document to the file it writes, so the
user's work then lived in datafiles/loopcut/workspaces/<id>/ and their Cmd+S went there.

While the model's code runs (tools.run_python), every operator it looks up passes through here, the
way cost_guard and op_notes hook bpy.ops' _op_create_function:
- wm.save_as_mainfile to any other path than the open file's saves a copy (copy=True): the same file
  is written and the open file stays where it was. The model gets one line saying so.
- wm.save_mainfile to another path does the same through save_as_mainfile(copy=True).
- Operators that would put another file in the user's place (open, revert, recover, the startup file,
  factory settings, an incremental save) are refused with what to do instead; nothing is changed.
A save to the open file's own path is the user's file saved where it is, and runs as written. The
user's own saves never pass through here: the hook is only on while the model's code runs.

No package imports at module level, like op_notes: tests run it against a stand-in bpy.ops.
"""

import contextlib
import os
import sys

NOTICE_PREFIX = "Loopcut note:"
# Operators that load another file in place of the user's, or move it to a new name.
REPLACES = frozenset({"wm.open_mainfile", "wm.revert_mainfile", "wm.recover_last_session", "wm.recover_auto_save",
                      "wm.read_homefile", "wm.read_factory_settings"})
GUARDED = REPLACES | {"wm.save_as_mainfile", "wm.save_mainfile"}
SAVE_AS_ONLY = frozenset({"exit", "incremental"})  # save_mainfile's own properties, which save_as_mainfile lacks.
_notices: list = []


def _current() -> str:
    import bpy
    return bpy.data.filepath


def _absolute(path: str) -> str:
    """A path as Blender would write it: // is the open .blend's folder, other relative paths the process's."""
    try:
        import bpy
        path = bpy.path.abspath(path)
    except (ImportError, AttributeError):
        pass
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _elsewhere(kwargs: dict) -> bool:
    """Whether a save with these arguments writes another file than the open one (an unsaved scene has none)."""
    target = kwargs.get("filepath")
    if not target:
        return False  # Blender saves the open file where it is, or refuses an unsaved one.
    current = _current()
    return not current or _absolute(str(target)) != _absolute(current)


def refusal(idname: str) -> str:
    return (f"Refused: {idname} in run_python would put another file in place of the user's open file, so "
            f"their next save would go somewhere else. Nothing was changed. To use another .blend, bring what "
            f"you need in with bpy.data.libraries.load, or work on it in a background Blender (run_blender) "
            f"where you have one. To save the scene to a file, use bpy.ops.wm.save_as_mainfile(filepath=..., "
            f"copy=True).")


def _note(path: str) -> None:
    current = _current()
    where = current if current else "unsaved, as it was"
    _notices.append(f"{NOTICE_PREFIX} saved {os.path.basename(str(path))} as a copy; the user's open file is "
                    f"still {where}.")


class _KeptOp:
    """A file operator from bpy.ops; only its call is looked at. Everything else is the operator's own."""
    __slots__ = ("_op", "_idname", "_create")
    __doc__ = property(lambda self: self._op.__doc__)

    def __init__(self, op, idname, create):
        self._op, self._idname, self._create = op, idname, create

    def __call__(self, *args, **kwargs):
        if self._idname in REPLACES or self._idname == "wm.save_mainfile" and kwargs.get("incremental"):
            raise RuntimeError(refusal(self._idname))
        if self._idname == "wm.save_as_mainfile" and _elsewhere(kwargs) and not kwargs.get("copy"):
            result = self._op(*args, **{**kwargs, "copy": True})
            _note(kwargs["filepath"])
            return result
        if self._idname == "wm.save_mainfile" and _elsewhere(kwargs):
            save_as = self._create("wm", "save_as_mainfile")
            result = save_as(*args, **{k: v for k, v in kwargs.items() if k not in SAVE_AS_ONLY}, copy=True)
            _note(kwargs["filepath"])
            return result
        return self._op(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._op, name)

    def __repr__(self):
        return repr(self._op)

    def __str__(self):
        return str(self._op)

    def __dir__(self):
        return dir(self._op)


@contextlib.contextmanager
def watching_ops():
    """While the model's code runs: saves elsewhere are copies, loads in the user's place are refused."""
    module = sys.modules.get("bpy.ops")
    real = getattr(module, "_op_create_function", None)
    if real is None:
        yield
        return

    def create(submodule, name):
        op = real(submodule, name)
        idname = f"{submodule}.{name}"
        return _KeptOp(op, idname, real) if idname in GUARDED else op

    module._op_create_function = create
    try:
        yield
    finally:
        module._op_create_function = real  # One the code kept (a timer it registered) stays guarded.


def take() -> list:
    """The notes since the last take, for the step's result."""
    notices = list(_notices)
    _notices.clear()
    return notices
