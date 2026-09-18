"""Interactive dev session: Loopcut registered from this checkout, panel open on the right,
hot reload on save. Started by scripts/dev.sh."""

import sys
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension"))
import loopcut  # noqa: E402

STATE: dict = {}


def split():
    window = bpy.context.window_manager.windows[0]
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in view.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=view, region=region):
        bpy.ops.screen.area_split(direction="VERTICAL", factor=0.7)
    STATE["window"] = window
    # Area geometry is only updated on the next redraw, so choose the panel side a tick later.
    bpy.app.timers.register(open_panel, first_interval=0.3)


def open_panel():
    window = STATE["window"]
    panel = max((a for a in window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
    with bpy.context.temp_override(window=window, area=panel):
        bpy.ops.loopcut.open()


bpy.context.preferences.view.show_splash = False
loopcut.register()
bpy.app.timers.register(split, first_interval=0.5)
