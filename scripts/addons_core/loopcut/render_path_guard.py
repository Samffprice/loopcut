"""Resolve workspace still-render paths before the operator runs.

No package imports: workspace Blenders run this prelude with --python. Both
launch paths start in the workspace; a script's later chdir is respected too.
"""

import os
import sys

import bpy

_noted = False


def _resolve(kwargs):
    global _noted
    try:
        if not bpy.app.background or not kwargs.get("write_still") or kwargs.get("animation"):
            return
        # Sequencer-editor renders use a different context scene, and are outside
        # the workspace's background still-render path.
        if kwargs.get("use_sequencer_scene"):
            return
        scene = bpy.context.scene
        if kwargs.get("scene"):
            scene = bpy.data.scenes.get(kwargs["scene"]) or scene
        path = scene.render.filepath
        if not path or path.startswith("//") or os.path.isabs(path):
            return
        resolved = os.path.abspath(path)
        # A trailing separator means a directory, not an output filename prefix.
        if (path.endswith(os.sep) or (os.altsep and path.endswith(os.altsep))) and not resolved.endswith(os.sep):
            resolved += os.sep
        scene.render.filepath = resolved
        if not _noted:
            _noted = True
            print("Loopcut note: relative still-render output paths resolve against the current working directory.",
                  flush=True)
    except Exception:
        # A failed path read/write or log stream must never replace the operator's outcome.
        return


class _RenderOp:
    __slots__ = ("_op",)
    __doc__ = property(lambda self: self._op.__doc__)

    def __init__(self, op):
        self._op = op

    def __call__(self, *args, **kwargs):
        _resolve(kwargs)
        return self._op(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._op, name)

    def __repr__(self):
        return repr(self._op)

    def __str__(self):
        return str(self._op)

    def __dir__(self):
        return dir(self._op)


def install():
    """Hook only background operators; importing this file never installs it."""
    try:
        if not bpy.app.background:
            return
        module = sys.modules.get("bpy.ops")
        real = getattr(module, "_op_create_function", None)
        if real is None or getattr(real, "_loopcut_render_path_guard", False):
            return

        def create(submodule, name):
            op = real(submodule, name)
            return _RenderOp(op) if (submodule, name) == ("render", "render") else op

        create._loopcut_render_path_guard = True
        module._op_create_function = create
    except Exception:
        # Optional prelude: an unavailable hook must not stop the script either.
        return


if __name__ == "__main__":
    install()
