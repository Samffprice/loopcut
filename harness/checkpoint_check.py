"""Checkpoints against a real Blender, driven through the real panel buttons. No model needed.

    LOOPCUT_DATA_DIR=<tmp>/data Blender --factory-startup --enable-event-simulate \
        --python harness/checkpoint_check.py -- <tmp>/project
"""
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension"))
import loopcut  # noqa: E402
from loopcut import checkpoints, state, tools  # noqa: E402
from loopcut.ui import host  # noqa: E402

PROJECT_DIR = Path(sys.argv[sys.argv.index("--") + 1])
PROJECT = PROJECT_DIR / "kitchen.blend"
DATA_DIR = Path(os.environ["LOOPCUT_DATA_DIR"])
STEPS: list = []
SEEN: dict = {}


def step(fn):
    STEPS.append(fn)
    return fn


def finish(code: int, message: str):
    print(message, file=sys.stderr if code else sys.stdout)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def run_next():
    fn = STEPS.pop(0)
    try:
        fn()
    except Exception:
        traceback.print_exc()
        finish(1, f"CHECKPOINT FAILED in {fn.__name__}")
    if not STEPS:
        finish(0, "CHECKPOINT OK")
    return 0.5


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def meshes() -> set:
    return {o.name for o in bpy.data.objects if o.type == "MESH"}


def turn(text: str, code: str) -> dict:
    """What agent.send + one approved run_python do, minus the model."""
    session = state.session()
    user = state.item_user(text)
    checkpoints.begin_turn(session, user)
    session["items"].append(user)
    session["messages"].append({"role": "user", "content": text})
    checkpoints.ensure_for_turn(session)
    result = tools.execute("run_python", json.dumps({"summary": text, "code": code}))
    assert result.ok, result.text
    reply = state.item_assistant()
    reply.update(text="Done.", streaming=False)
    session["items"].append(reply)
    session["messages"].append({"role": "assistant", "content": "Done."})
    return user


def panel():
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if host.is_host(a))
    return window, area


def click(hit_id: str):
    window, area = panel()
    display = host._displays[area.as_pointer()]
    hit = next((h for h in display["hits"] if h["id"] == hit_id), None)
    assert hit, f"{hit_id} is not on screen; hits: {[h['id'] for h in display['hits']]}"
    region = next(r for r in area.regions if r.type == "WINDOW")
    x = region.x + int(hit["x"] + hit["w"] / 2)
    y = region.y + int(display["height"] - (hit["y"] + hit["h"] / 2))
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


def add_cube(name: str, x: float) -> str:
    return f"bpy.ops.mesh.primitive_cube_add(size=1, location=({x}, 3, 0))\nbpy.context.active_object.name = {name!r}"


@step
def save_project_and_split():
    PROJECT_DIR.mkdir(parents=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(PROJECT))
    SEEN["project_sha"] = sha(PROJECT)
    window = bpy.context.window_manager.windows[0]
    view = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    region = next(r for r in view.regions if r.type == "WINDOW")
    with bpy.context.temp_override(window=window, area=view, region=region):
        bpy.ops.screen.area_split(direction="VERTICAL", factor=0.62)


@step
def open_panel_and_work():
    window = bpy.context.window_manager.windows[0]
    area = max((a for a in window.screen.areas if a.type == "VIEW_3D"), key=lambda a: a.x)
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.loopcut.open()
    state.reset()
    assert not checkpoints._store(state.session()).entries

    turn("add A", add_cube("A", 0))
    # The user's own work between turns: never checkpointed on its own, must never be lost.
    bpy.ops.mesh.primitive_cone_add(location=(0, -4, 0))
    bpy.context.active_object.name = "Manual"
    bpy.data.objects["Cube"].location.z = 2.5
    SEEN["turn2"] = turn("add B", add_cube("B", 2))
    turn("add C", add_cube("C", 4))

    assert meshes() == {"Cube", "A", "Manual", "B", "C"}, meshes()
    assert bpy.data.filepath == str(PROJECT), "taking checkpoints must not move the open file"
    assert sha(PROJECT) == SEEN["project_sha"], "taking checkpoints must not write the user's file"
    assert len(checkpoints._store(state.session()).entries) == 3


@step
def click_restore_on_turn_2():
    index = state.session()["items"].index(SEEN["turn2"])
    SEEN["turn2_index"] = index
    click(f"item{index}.checkpoint.restore")


@step
def confirm():
    session = state.session()
    assert session["confirm_restore"] == SEEN["turn2_index"], "first click must only ask"
    assert meshes() == {"Cube", "A", "Manual", "B", "C"}, "nothing may change before confirming"
    click(f"item{SEEN['turn2_index']}.checkpoint.confirm")


@step
def verify_restored():
    session = state.session()
    errors = [i["text"] for i in session["items"] if i["kind"] == "error"]
    assert not errors, errors
    assert meshes() == {"Cube", "A", "Manual"}, meshes()
    assert round(bpy.data.objects["Cube"].location.z, 2) == 2.5, "manual edit before the checkpoint is kept"

    now = Path(bpy.data.filepath)
    assert now.parent == PROJECT_DIR and now.name.startswith("kitchen.restored-"), now
    assert DATA_DIR not in now.parents, "Ctrl+S must never write into the checkpoint store"
    assert sha(PROJECT) == SEEN["project_sha"], "the user's original file was modified"

    assert [i["kind"] for i in session["items"]] == ["user", "assistant", "notice"], session["items"]
    assert len(session["messages"]) == 2
    assert session["input"] == "add B", "the cut message goes back in the input box"
    SEEN["first_restored_file"] = now
    _, area = panel()  # Raises if the panel did not survive the file load.
    area.tag_redraw()


@step
def click_undo_restore():
    click("item2.notice.action")


@step
def verify_undone():
    session = state.session()
    errors = [i["text"] for i in session["items"] if i["kind"] == "error"]
    assert not errors, errors
    assert meshes() == {"Cube", "A", "Manual", "B", "C"}, meshes()
    kinds = [i["kind"] for i in session["items"]]
    assert kinds == ["user", "assistant"] * 3 + ["notice"], kinds
    assert len(session["messages"]) == 6 and session["input"] == ""
    now = Path(bpy.data.filepath)
    assert now != SEEN["first_restored_file"] and now.name.startswith("kitchen.restored-"), now
    assert SEEN["first_restored_file"].is_file(), "an earlier restored file must not be overwritten"
    assert sha(PROJECT) == SEEN["project_sha"]


@step
def budget_expiry_and_refusals():
    session = state.session()
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=7, location=(0, 8, 0))  # ~2 MB per snapshot
    os.environ["LOOPCUT_CHECKPOINT_BUDGET_MB"] = "8"
    for n in range(9):
        turn(f"nudge {n}", f"bpy.data.objects['A'].location.z = {n}")
    entries = checkpoints._store(session).entries
    live = [e for e in entries if not e["expired"]]
    expired = [e for e in entries if e["expired"]]
    assert expired, "the budget never kicked in"
    assert not entries[0]["expired"], "the very first checkpoint is protected"
    assert all(not e["expired"] for e in entries[-checkpoints.KEEP_NEWEST:])
    on_disk = {p.stem for p in checkpoints._store(session).folder.glob("*.blend")}
    assert on_disk == {e["id"] for e in live}, "expired snapshots must leave the disk, live ones must stay"
    print(f"CHECKPOINT store: {len(live)} live, {len(expired)} expired, "
          f"{sum(e['size'] for e in live) / 1e6:.1f} MB")

    before = (bpy.data.filepath, meshes(), len(session["items"]))
    for checkpoint_id, busy, expect in ((expired[0]["id"], False, "expired"), (live[-1]["id"], True, "Stop")):
        session["busy"] = busy
        try:
            checkpoints.restore(session, checkpoint_id)
            raise AssertionError(f"restore should have been refused ({expect})")
        except checkpoints.CheckpointError as ex:
            assert expect in str(ex), ex
        finally:
            session["busy"] = False
    assert before == (bpy.data.filepath, meshes(), len(session["items"])), "a refused restore changed something"


bpy.context.preferences.view.show_splash = False
loopcut.register()
# persistent: a restore loads a file, and Blender drops ordinary timers when it does.
bpy.app.timers.register(run_next, first_interval=0.6, persistent=True)
