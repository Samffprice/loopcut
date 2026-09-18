"""Drive the panel with simulated events:
    Blender --factory-startup --enable-event-simulate --python harness/input_check.py"""
import os
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension"))
import loopcut  # noqa: E402
from loopcut import state  # noqa: E402
from loopcut.ui import host  # noqa: E402

STATE: dict = {}
STEPS: list = []


def step(fn):
    STEPS.append(fn)
    return fn


def finish(code: int, message: str):
    print(message, file=sys.stderr if code else sys.stdout)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def run_next():
    try:
        STEPS.pop(0)()
    except Exception:
        traceback.print_exc()
        finish(1, "INPUT FAILED")
    if not STEPS:
        finish(0, "INPUT OK")
    return 0.35


def key(kind, unicode=""):
    STATE["window"].event_simulate(type=kind, value="PRESS", unicode=unicode)
    STATE["window"].event_simulate(type=kind, value="RELEASE")


@step
def split():
    window = bpy.context.window_manager.windows[0]
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in view.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=view, region=region):
        bpy.ops.screen.area_split(direction="VERTICAL", factor=0.7)
    STATE["window"] = window


@step
def open_panel():
    window = STATE["window"]
    panel = max((a for a in window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
    with bpy.context.temp_override(window=window, area=panel):
        bpy.ops.loopcut.open()
    STATE["panel"] = panel
    state.reset()


@step
def click_input():
    panel = STATE["panel"]
    display = host._displays[panel.as_pointer()]
    card = next(h for h in display["hits"] if h["id"] == "input.card")
    region = next(r for r in panel.regions if r.type == "WINDOW")
    x = region.x + int(card["x"] + card["w"] / 2)
    y = region.y + int(display["height"] - (card["y"] + card["h"] / 2))
    window = STATE["window"]
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


@step
def type_text():
    assert state.session()["focused"], "click on the input card did not focus the panel"
    key("H", "h")
    key("I", "i")
    key("ONE", "!")


@step
def edit_text():
    session = state.session()
    assert session["input"] == "hi!", repr(session["input"])
    key("BACK_SPACE")
    key("LEFT_ARROW")
    key("O", "o")


@step
def escape():
    session = state.session()
    assert (session["input"], session["cursor"]) == ("hoi", 2), (session["input"], session["cursor"])
    assert "Cube" in bpy.data.objects, "typing must not leak to the viewport (X/H would hide or delete)"
    key("ESC")


@step
def verify_unfocused():
    assert not state.session()["focused"], "Esc did not release focus"


bpy.context.preferences.view.show_splash = False
loopcut.register()
bpy.app.timers.register(run_next, first_interval=0.6)
