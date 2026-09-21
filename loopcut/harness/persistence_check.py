"""Conversations across Blender restarts, in two launches that share a data folder.

    export LOOPCUT_DATA_DIR=<tmp>/data
    Blender --factory-startup --python harness/persistence_check.py -- write <tmp>/project
    Blender --factory-startup --enable-event-simulate <tmp>/project/kitchen.blend \
        --python harness/persistence_check.py -- resume <tmp>/project
"""
import json
import os
import sys
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
import loopcut  # noqa: E402
from loopcut import checkpoints, conversations, state, tools  # noqa: E402
from loopcut.ui import host  # noqa: E402

PHASE, PROJECT_DIR = sys.argv[sys.argv.index("--") + 1], Path(sys.argv[sys.argv.index("--") + 2])
PROJECT, OTHER, NOTES = PROJECT_DIR / "kitchen.blend", PROJECT_DIR / "garage.blend", PROJECT_DIR / "notes.json"
STEPS: list = []


def step(phase):
    def add(fn):
        if phase == PHASE:
            STEPS.append(fn)
        return fn
    return add


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
        finish(1, f"PERSISTENCE FAILED in {PHASE}.{fn.__name__}")
    if not STEPS:
        finish(0, f"PERSISTENCE {PHASE} OK")
    return 0.5


def meshes() -> set:
    return {o.name for o in bpy.data.objects if o.type == "MESH"}


def turn(text: str, name: str, x: float, capture: bool = False) -> None:
    """What agent.send plus one approved run_python do, minus the model."""
    session = state.session()
    user = state.item_user(text)
    checkpoints.begin_turn(session, user)
    session["items"].append(user)
    session["messages"].append({"role": "user", "content": text})
    checkpoints.ensure_for_turn(session)
    code = f"bpy.ops.mesh.primitive_cube_add(size=1, location=({x}, 3, 0))\nbpy.context.active_object.name = {name!r}"
    assert tools.execute("run_python", json.dumps({"summary": text, "code": code})).ok
    if capture:
        shot = tools.execute("capture_viewport", "{}")
        session["messages"].append({"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": conversations.store_image(session, shot.image_path)}}]})
    reply = state.item_assistant()
    reply.update(text="Done.", streaming=False)
    session["items"].append(reply)
    session["messages"].append({"role": "assistant", "content": "Done."})
    conversations.save(session)


def open_panel():
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.loopcut.open()


def click(hit_id: str):
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if host.is_host(a))
    display = host._displays[area.as_pointer()]
    hit = next((h for h in display["hits"] if h["id"] == hit_id), None)
    assert hit, f"{hit_id} is not on screen; hits: {[h['id'] for h in display['hits']]}"
    region = next(r for r in area.regions if r.type == "WINDOW")
    x = region.x + int(hit["x"] + hit["w"] / 2)
    y = region.y + int(display["height"] - (hit["y"] + hit["h"] / 2))
    window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x, y=y)
    window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x, y=y)


# ------------------------------------------------------------------ launch 1

@step("write")
def untitled_chat_then_first_save():
    PROJECT_DIR.mkdir(parents=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(OTHER), copy=True)
    assert state.session()["projects"] == [], "an unsaved scene belongs to no file yet"
    turn("add A", "A", 0, capture=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(PROJECT))  # First save: the chat now belongs to this file.
    assert state.session()["projects"] == [conversations.project_key(str(PROJECT))], state.session()["projects"]


@step("write")
def second_turn_and_save():
    turn("add B", "B", 2)
    bpy.ops.wm.save_mainfile()
    session = state.session()
    stored = (conversations._folder(session["id"]) / "conversation.json").read_text()
    assert "base64" not in stored and conversations.IMAGE_SCHEME in stored
    NOTES.write_text(json.dumps({"id": session["id"], "items": len(session["items"])}))


# ------------------------------------------------------------------ launch 2

@step("resume")
def resumed_on_startup():
    notes = json.loads(NOTES.read_text())
    session = state.session()
    assert bpy.data.filepath == str(PROJECT)
    assert session["id"] == notes["id"], "the file's conversation was not resumed"
    assert len(session["items"]) == notes["items"] and meshes() >= {"A", "B"}
    assert [i.get("checkpoint") is not None for i in session["items"] if i["kind"] == "user"] == [True, True]
    wired = conversations.wire_messages(session["id"], session["messages"])
    assert any(part.get("image_url", {}).get("url", "").startswith("data:image/png")
               for m in wired if isinstance(m["content"], list) for part in m["content"]), \
        "the stored capture did not come back for the API"


@step("resume")
def other_file_gets_its_own_chat():
    bpy.ops.wm.open_mainfile(filepath=str(OTHER))
    session = state.session()
    assert session["id"] != json.loads(NOTES.read_text())["id"] and session["items"] == []
    assert "A" not in meshes()


@step("resume")
def back_to_the_project():
    bpy.ops.wm.open_mainfile(filepath=str(PROJECT))
    assert state.session()["id"] == json.loads(NOTES.read_text())["id"]
    open_panel()


@step("resume")
def new_chat_keeps_the_old_one_reachable():
    click("header.new")


@step("resume")
def open_history():
    assert state.session()["items"] == [] and state.session()["id"] != json.loads(NOTES.read_text())["id"]
    click("header.history")


@step("resume")
def pick_the_old_conversation():
    assert state.ui["view"] == "history" and [r["title"] for r in state.ui["history"]] == ["add A"], state.ui
    click("history.row0")


@step("resume")
def restore_a_checkpoint_from_a_previous_launch():
    session = state.session()
    assert session["id"] == json.loads(NOTES.read_text())["id"] and state.ui["view"] == "chat"
    index = [n for n, i in enumerate(session["items"]) if i["kind"] == "user"][1]
    state.ui["restore_index"] = index
    click(f"item{index}.checkpoint.restore")


@step("resume")
def confirm_restore():
    click(f"item{state.ui['restore_index']}.checkpoint.confirm")


@step("resume")
def restored_and_still_one_conversation():
    session = state.session()
    errors = [i["text"] for i in session["items"] if i["kind"] == "error"]
    assert not errors, errors
    assert "A" in meshes() and "B" not in meshes(), meshes()
    assert session["id"] == json.loads(NOTES.read_text())["id"], "a restore must not switch conversations"
    assert session["input"] == "add B"
    restored = conversations.project_key(bpy.data.filepath)
    if checkpoints.in_place_supported():  # The Loopcut build restores into the open file.
        assert restored == conversations.project_key(str(PROJECT)), restored
        assert session["projects"] == [restored], session["projects"]
    else:
        assert Path(restored).name.startswith("kitchen.restored-")
        assert session["projects"] == [conversations.project_key(str(PROJECT)), restored], session["projects"]
    for path in (str(PROJECT), bpy.data.filepath):
        assert conversations.resume_or_new(path)["id"] == session["id"], f"{path} lost its conversation"
    on_disk = conversations.load(session["id"])
    assert [i["kind"] for i in on_disk["items"]] == [i["kind"] for i in session["items"]], "restore was not saved"


bpy.context.preferences.view.show_splash = False
# Saving from a timer draws the file's thumbnail outside a draw context, which the assert-enabled
# dev build of the fork aborts on (release Blenders do not check). The checks need no thumbnail.
bpy.context.preferences.filepaths.file_preview_type = "NONE"
loopcut.register()
# persistent: this check opens files, and Blender drops ordinary timers when it does.
bpy.app.timers.register(run_next, first_interval=0.8, persistent=True)
