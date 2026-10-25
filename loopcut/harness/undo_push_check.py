"""Undo right after Loopcut's steps, in real Blender with its window drawing.

    Blender --factory-startup --python loopcut/harness/undo_push_check.py
GUI only (the crash is on a redraw); ~30 s. Run it with BLENDER_USER_RESOURCES and TMPDIR set to fresh
folders and LOOPCUT_ENV_FILE to an empty file.

Blender 5.2.2 crashed on the redraw after Undo (BrushAssetShelf.poll, a freed context.object) when two
undo pushes in a row held the same state and an object was added and renamed after them with no push.
Every run_python step pushed "Before Loopcut: ..." and nothing after it, so a step that changed nothing
(a look, an error, a stop at the deadline), then a step that added an object, then Undo, was exactly
that (0.2.18). Each step now also pushes "Loopcut: ..." after it (tools.UNDO_AFTER).

Cases, every step through tools.execute and one event-loop turn apart, then ed.undo with a 3D viewport
override and six redraws of every area: pass, add; an error, add; a stop (LOOPCUT_RUN_TIMEOUT=3, an
endless loop), add; a single add; an add, then another (Japanese summary), where Undo takes out only the
second; pass, then frame_camera creating a camera; pass, then import_asset from an asset library made
on the spot (a .blend written with bpy.data.libraries.write, no network); pass, then the text to motion
Try it card's shipped output applied as a job's is (cloud._apply_to_scene, media/samples); pass, then
merge.py's rollback of a failed merge (its push before ed.undo, merge_wave) over an object added after
its push. Each must survive the redraws, and the Undo must leave exactly the objects there were before
the case's last step, which must have added some. Ends with the undo history
(WindowManager.print_undo_steps) and one PASS or FAIL line; exits 1 on a failure.

UNDO_CHECK_UNFIXED=1 turns the push after off, as the add-on was before: Blender then crashes in the
first case, so no PASS line is printed and the exit code is the signal's. UNDO_CHECK_CASES=keep,camera
runs only those cases.
"""

import ctypes
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ.setdefault("LOOPCUT_DATA_DIR", tempfile.mkdtemp(prefix="loopcut-undo-check-"))
if not os.environ.get("LOOPCUT_ENV_FILE"):
    empty = Path(tempfile.mkdtemp(prefix="loopcut-undo-check-env-")) / "empty.env"
    empty.write_text("")
    os.environ["LOOPCUT_ENV_FILE"] = str(empty)
from loopcut import merge, settings, tools  # noqa: E402
settings.register()
if bpy.context.preferences.addons.get(settings.PACKAGE) is None:
    bpy.context.preferences.addons.new().module = settings.PACKAGE

UNFIXED = os.environ.get("UNDO_CHECK_UNFIXED") == "1"
if UNFIXED:
    tools.UNDO_AFTER = False
REDRAWS = 6

PASS = "pass\n"
ERROR = "raise ValueError('undo check')\n"
LOOP = "while True:\n    pass\n"


def add(name: str, x: float = 3.0) -> str:
    return (f"bpy.ops.mesh.primitive_uv_sphere_add(location=({x}, 0, 0))\n"
            f"bpy.context.active_object.name = {name!r}\n")


def step(summary: str, code: str, timeout: str = "30"):
    os.environ["LOOPCUT_RUN_TIMEOUT"] = timeout
    started = time.monotonic()
    result = tools.execute("run_python", json.dumps({"summary": summary, "code": code}))
    print(f"  run_python {summary!r}: ok={result.ok} timed_out={result.timed_out} "
          f"{time.monotonic() - started:.1f} s", flush=True)


def frame_camera(name: str):
    result = tools.execute("frame_camera", json.dumps({"camera": name, "create": True, "objects": ["Cube"],
                                                       "direction": "front"}))
    print(f"  frame_camera create {name}: ok={result.ok}", flush=True)
    assert result.ok, result.text


def asset(ref: str):
    result = tools.execute("import_asset", json.dumps({"source": "library", "id": ref}))
    print(f"  import_asset {ref}: ok={result.ok}", flush=True)
    assert result.ok, result.text


def cloud_motion():
    """The text to motion Try it card's shipped output, applied the way a finished job's output is."""
    from loopcut import cloud, cloud_jobs, try_samples
    sample = try_samples.match("motion", "motion_from_text", {})
    assert sample, "media/samples has no motion sample"
    job = try_samples.job(sample, "gen", {"prompt": "a figure jumps for joy"}, {})
    descriptor = {"name": "motion_from_text", "title": "Motion from text", "apply": "apply_motion"}
    result = cloud._apply_to_scene(cloud_jobs, descriptor, job, bpy.context.scene)
    print(f"  cloud output {descriptor['name']}: ok={result.ok}", flush=True)
    assert result.ok, result.text


def make_kit() -> str:
    """An asset library with one object asset in a .blend of its own, for import_asset's library source. Written
    with libraries.write, so the open file stays as it is; the datablocks go again, before any case."""
    folder = Path(tempfile.mkdtemp(prefix="loopcut-undo-kit-"))
    mesh = bpy.data.meshes.new("UndoStool")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    stool = bpy.data.objects.new("UndoStool", mesh)
    stool.asset_mark()
    bpy.data.libraries.write(str(folder / "kit.blend"), {stool}, fake_user=True)
    bpy.data.objects.remove(stool)
    bpy.data.meshes.remove(mesh)
    bpy.context.preferences.filepaths.asset_libraries.new(name="Undo Kit", directory=str(folder))
    return "Undo Kit::kit.blend::OBJECT::UndoStool"


def merge_rollback(name: str):
    """merge_wave's own calls: its push before the wave, a change, and on a failure a push and ed.undo."""
    assert merge._undo("push", "Loopcut: before merge wave 9")
    window = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=window, screen=window.screen):
        obj = bpy.data.objects.new(name, bpy.data.meshes.new(name))  # What a merge links in.
        bpy.context.scene.collection.objects.link(obj)
    assert name in bpy.data.objects
    assert merge._undo("push", "Loopcut: failed merge") and merge._undo("undo")
    print(f"  merge rollback over {name}: done", flush=True)


KIT = make_kit()
# name: its steps, callables run one event-loop turn apart; Undo follows the last, except where it rolls back itself.
CASES = {
    "pass": [lambda: step("Look", PASS), lambda: step("Add sphere", add("UndoPass"))],
    "error": [lambda: step("Fail", ERROR), lambda: step("Add sphere", add("UndoError"))],
    "stop": [lambda: step("Loop", LOOP, timeout="3"), lambda: step("Add sphere", add("UndoStop"))],
    "single": [lambda: step("Add sphere", add("UndoSingle"))],
    "keep": [lambda: step("Add sphere", add("UndoKeep", -3.0)),
             lambda: step("テーブルの横に椅子を三脚並べて、それぞれの名前を付け直す", add("UndoSecond"))],
    "camera": [lambda: step("Look", PASS), lambda: frame_camera("UndoCam")],
    "import": [lambda: step("Look", PASS), lambda: asset(KIT)],
    "cloud": [lambda: step("Look", PASS), cloud_motion],
    "merge": [lambda: step("Look", PASS), lambda: merge_rollback("UndoMerged")],
}
ROLLS_BACK = {"merge"}


def undo_once():
    window = bpy.context.window_manager.windows[0]
    area = next(a for a in window.screen.areas if a.type == "VIEW_3D")
    with bpy.context.temp_override(window=window, area=area):
        bpy.ops.ed.undo()


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def cases():
    wanted = [c.strip() for c in os.environ.get("UNDO_CHECK_CASES", "").split(",") if c.strip()] or list(CASES)
    failures = []
    print(f"UNDO CHECK {'unfixed (no push after)' if UNFIXED else 'fixed'}: {', '.join(wanted)}", flush=True)
    for name in wanted:
        print(f"case {name}: start", flush=True)
        *earlier, last = CASES[name]
        for run in earlier:
            run()
            yield
        before = {o.name for o in bpy.data.objects}
        last()
        added = sorted({o.name for o in bpy.data.objects} - before)
        yield
        if name not in ROLLS_BACK:
            assert added, f"{name}: its last step added no object"
            undo_once()
            print(f"  undone after {', '.join(added)}; redrawing {REDRAWS} times", flush=True)
        for _ in range(REDRAWS):
            redraw()
            yield
        left = {o.name for o in bpy.data.objects}
        problems = ([f"{', '.join(sorted(left - before))} still there"] if left - before else []) + \
            [f"{', '.join(sorted(before - left))} taken out too"] * bool(before - left)
        print(f"case {name}: {'ok' if not problems else 'FAILED: ' + '; '.join(problems)}", flush=True)
        failures += [f"{name}: {p}" for p in problems]
    print("undo history, newest last:", flush=True)
    bpy.context.window_manager.print_undo_steps()
    ctypes.CDLL(None).fflush(None)  # Blender's printf: os._exit would drop what a redirected stdout buffered.
    if failures:
        print(f"UNDO PUSH CHECK FAIL: {'; '.join(failures)}", flush=True)
        os._exit(1)
    print(f"UNDO PUSH CHECK PASS: {len(wanted)} cases survived {REDRAWS} redraws after Undo, each Undo took out "
          f"only its last step", flush=True)
    os._exit(0)


_cases = cases()


def tick():
    try:
        next(_cases)
        return 0.2
    except StopIteration:
        os._exit(0)
    except Exception:
        traceback.print_exc()
        print("UNDO PUSH CHECK FAIL: the check raised", flush=True)
        os._exit(1)


bpy.context.preferences.view.show_splash = False
bpy.app.timers.register(tick, first_interval=2.0)
