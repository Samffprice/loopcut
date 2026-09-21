"""Runtime capabilities. RNA's static enum_items omits dynamically registered engines."""

import ast


def render_engines(render) -> list[str]:
    # Blender validates enum membership before invoking the setter. An invalid sentinel leaves
    # the scene unchanged and the TypeError carries the actual dynamic items for this instance.
    # Using bl_rna.properties['engine'].enum_items instead silently hides Cycles and Workbench.
    try:
        render.engine = "__LOOPCUT_ENUM_PROBE__"
    except TypeError as error:
        _, separator, values = str(error).partition("not found in ")
        if separator:
            try:
                items = ast.literal_eval(values)
            except (ValueError, SyntaxError):
                items = None
            if isinstance(items, tuple) and all(isinstance(item, str) for item in items):
                return list(items)
        raise RuntimeError("Blender did not return its available render engines") from error
    raise RuntimeError("Blender unexpectedly accepted the enum probe")
