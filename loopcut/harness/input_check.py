"""Drive the panel with simulated events:
    Blender --factory-startup --enable-event-simulate --python harness/input_check.py"""
import os
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
# Harness runs must not write conversations or checkpoints into the user's real Loopcut data.
import os as _os
import tempfile as _tempfile
_os.environ.setdefault("LOOPCUT_DATA_DIR", _tempfile.mkdtemp(prefix="loopcut-harness-"))

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


def key(kind, unicode="", **modifiers):
    STATE["window"].event_simulate(type=kind, value="PRESS", unicode=unicode, **modifiers)
    STATE["window"].event_simulate(type=kind, value="RELEASE", **modifiers)


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
def select_and_replace():
    session = state.session()
    assert (session["input"], session["cursor"]) == ("hoi", 2), (session["input"], session["cursor"])
    assert "Cube" in bpy.data.objects, "typing must not leak to the viewport (X/H would hide or delete)"
    key("A", ctrl=True)     # Select all, then type over it.
    key("Y", "y")


@step
def undo_typing():
    session = state.session()
    assert session["input"] == "y", repr(session["input"])
    key("Z", ctrl=True)


@step
def start_mention():
    session = state.session()
    assert session["input"] == "hoi", f"undo should bring the replaced text back, got {session['input']!r}"
    key("END")
    key("SPACE", " ")
    key("TWO", "@")
    key("C", "c")


@step
def pick_mention():
    rows = state.ui["mentions"]
    assert [name for name, _ in rows][:2] == ["Cube", "Camera"], f"selected object first, got {rows}"
    display = host._displays[STATE["panel"].as_pointer()]
    assert any(h["id"] == "mentions.row0" for h in display["hits"]), "the completion list is not on screen"
    key("DOWN_ARROW")
    key("TAB")


def hit_center(id: str) -> tuple[int, int]:
    panel = STATE["panel"]
    display = host._displays[panel.as_pointer()]
    hit = next(h for h in display["hits"] if h["id"] == id)
    region = next(r for r in panel.regions if r.type == "WINDOW")
    return region.x + int(hit["x"] + hit["w"] / 2), region.y + int(display["height"] - (hit["y"] + hit["h"] / 2))


def move_to(id: str) -> None:
    x, y = hit_center(id)
    STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)


def click(id: str) -> None:
    x, y = hit_center(id)
    STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    STATE["window"].event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    STATE["window"].event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


def prims() -> dict:
    return {p["id"]: p for p in host._displays[STATE["panel"].as_pointer()]["prims"]}


@step
def open_picker():
    assert state.session()["input"] == "hoi @Camera ", repr(state.session()["input"])
    assert state.ui["mentions"] == [], "the list closes after a pick"
    click("input.add")


@step
def search_picker():
    picker = state.ui["picker"]
    assert picker is not None, "the + chip did not open the picker"
    assert picker["rows"][0] == ("Image file…", "image") and ("Cube", "object") in picker["rows"], picker["rows"]
    assert "picker.title" in prims(), "the picker is not on screen"
    key("C", "c")
    key("U", "u")


@step
def pick_from_picker():
    picker = state.ui["picker"]
    assert picker["query"] == "cu" and picker["rows"][0] == ("Cube", "object"), picker
    key("RET")


@step
def hover_settings():
    session = state.session()
    assert session["input"] == "hoi @Camera @Cube ", repr(session["input"])
    assert state.ui["picker"] is None, "the picker stays open after a pick"
    move_to("header.settings")


@step
def hover_rings():
    assert state.ui["hover"] == "header.settings", state.ui["hover"]
    drawn = prims()
    assert "header.settings.bg" in drawn and drawn["tooltip.l0"]["text"] == "Settings", drawn.get("tooltip.l0")
    move_to("input.rings")


@step
def open_model_menu():
    drawn = prims()
    assert state.ui["hover"] == "input.rings" and drawn["tooltip.l0"]["text"].startswith("Context: "), drawn.get("tooltip.l0")
    assert "input.ring.context" in drawn, "no context ring"
    click("input.model")


@step
def close_model_menu():
    assert state.ui["model_menu"], "the model button did not open its menu"
    assert "models.bg" in prims() and "models.row0.name" in prims(), "the menu is not on screen"
    key("ESC")


@step
def leave_panel():
    assert not state.ui["model_menu"], "Esc did not close the model menu"
    assert state.session()["focused"], "Esc on an open menu must not release focus"
    view = next(a for a in STATE["window"].screen.areas if a.type == "VIEW_3D" and a != STATE["panel"])
    STATE["window"].event_simulate(type="MOUSEMOVE", value="NOTHING", x=view.x + view.width // 2, y=view.y + view.height // 2)


@step
def escape():
    assert state.ui["hover"] is None, f"hover should clear when the mouse leaves the panel, got {state.ui['hover']!r}"
    key("ESC")


@step
def verify_unfocused():
    assert not state.session()["focused"], "Esc did not release focus"


bpy.context.preferences.view.show_splash = False
loopcut.register()
bpy.app.timers.register(run_next, first_interval=0.6)
