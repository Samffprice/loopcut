"""Open Blender, show the Loopcut panel with a canned conversation, screenshot it, quit.

    Blender --factory-startup -p 0 0 1440 900 --python harness/shot.py -- \
        --fixture harness/fixtures/full.json --out out/full

Writes <out>.png (whole window), <out>.panel.png (just the panel) and <out>.json (the frame's
display list). No network and no API key needed.
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

import bpy

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "extension"))

import loopcut  # noqa: E402
from loopcut import state  # noqa: E402
from loopcut.ui import host  # noqa: E402


def parse_args() -> argparse.Namespace:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="Output path without extension")
    parser.add_argument("--panel-fraction", type=float, default=0.3)
    return parser.parse_args(argv)


ARGS = parse_args()
STATE: dict = {}


def fail(message: str) -> None:
    print(f"HARNESS FAILED: {message}", file=sys.stderr)
    sys.stderr.flush()
    import os
    os._exit(1)  # quit_blender would exit 0 and hide the failure from the caller.


def _guarded(step):
    def run():
        try:
            return step()
        except Exception:
            traceback.print_exc()
            fail(f"{step.__name__} raised")
    return run


def split():
    window = bpy.context.window_manager.windows[0]
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in view.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=view, region=region):
        bpy.ops.screen.area_split(direction="VERTICAL", factor=1.0 - ARGS.panel_fraction)
    STATE["window"] = window
    # Area geometry is only updated on the next redraw, so choose the panel side a tick later.
    bpy.app.timers.register(_guarded(open_panel), first_interval=0.3)


def apply_fixture():
    fixture = json.loads(ARGS.fixture.read_text(encoding="utf-8"))
    session = state.session()
    unknown = set(fixture) - set(session)
    if unknown:
        fail(f"fixture has unknown session keys: {sorted(unknown)}")
    session.update(state.new_session())
    session.update(fixture)


def open_panel():
    window = STATE["window"]
    panel = max((a for a in window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
    with bpy.context.temp_override(window=window, area=panel):
        bpy.ops.loopcut.open()
    STATE["panel"] = panel
    apply_fixture()
    panel.tag_redraw()
    bpy.app.timers.register(_guarded(settle), first_interval=0.5)


def settle():
    # Real mouse or trackpad input can reach this window; reset to the fixture just before capture.
    apply_fixture()
    STATE["panel"].tag_redraw()
    bpy.app.timers.register(capture, first_interval=0.15)


def capture():
    try:
        window, panel = STATE["window"], STATE["panel"]
        ARGS.out.parent.mkdir(parents=True, exist_ok=True)
        full = ARGS.out.with_suffix(".png")
        host.dump_display(str(ARGS.out.with_suffix(".json")))
        with bpy.context.temp_override(window=window):
            bpy.ops.screen.screenshot(filepath=str(full))

        import numpy as np
        image = bpy.data.images.load(str(full))
        width, height = image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        pixels = pixels.reshape(height, width, 4)
        crop = pixels[panel.y:panel.y + panel.height, panel.x:panel.x + panel.width]
        cropped = bpy.data.images.new("loopcut_panel", crop.shape[1], crop.shape[0], alpha=True)
        cropped.pixels.foreach_set(np.ascontiguousarray(crop).ravel())
        cropped.filepath_raw = str(ARGS.out.with_suffix(".panel.png"))
        cropped.file_format = "PNG"
        cropped.save()
        print(f"HARNESS OK: {full}")
    except Exception:
        traceback.print_exc()
        fail("capture raised")
    bpy.ops.wm.quit_blender()
    return None


bpy.context.preferences.view.show_splash = False
loopcut.register()
bpy.app.timers.register(_guarded(split), first_interval=0.5)
