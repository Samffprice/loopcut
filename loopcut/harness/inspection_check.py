"""Inspect real cameras/animation phases, validate visible signal and reject stale evidence.
Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/inspection_check.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import bpy
import imbuf
import numpy as np
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import agent, context, inspection, jobs, llm, mainthread, tools
from types import SimpleNamespace

temporary = tempfile.TemporaryDirectory(prefix="loopcut-inspection-check-")
root = Path(os.environ.get("LOOPCUT_INSPECTION_CHECK_DIR", temporary.name))
root.mkdir(parents=True, exist_ok=True)
os.environ["LOOPCUT_DATA_DIR"] = str(root / "data")


def wait(path):
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        row = jobs.status(path)
        if row["state"] not in jobs.ACTIVE:
            assert row["state"] == "complete", row
            return row
        time.sleep(.1)
    raise AssertionError("inspection did not finish")


def preserved():
    s = bpy.context.scene
    return (bpy.data.filepath, s.camera.name if s.camera else None, s.frame_current, s.render.engine,
            s.render.resolution_x, s.render.resolution_y, s.render.resolution_percentage, s.render.filepath,
            s.render.image_settings.media_type, s.render.image_settings.file_format,
            tuple(o.name for o in bpy.context.selected_objects),
            tuple((a.type, a.ui_type) for w in bpy.context.window_manager.windows for a in w.screen.areas))


scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.render.resolution_x = scene.render.resolution_y = 256
material = bpy.data.materials.new("Bright red signal")
material.use_nodes = True
material.node_tree.nodes.clear()
emission = material.node_tree.nodes.new("ShaderNodeEmission")
emission.inputs["Color"].default_value = (1, .015, .005, 1)
output = material.node_tree.nodes.new("ShaderNodeOutputMaterial")
material.node_tree.links.new(emission.outputs[0], output.inputs["Surface"])
cube = bpy.data.objects["Cube"]
cube.data.materials.clear()
cube.data.materials.append(material)
for frame, x in ((1, -.5), (2, .5), (3, 0)):
    cube.location.x = x
    cube.keyframe_insert("location", frame=frame)
alternate = bpy.data.objects.new("Side Camera", bpy.data.cameras.new("Side Camera"))
scene.collection.objects.link(alternate)
alternate.location = (6, -6, 4)
alternate.rotation_euler = (-alternate.location).to_track_quat("-Z", "Y").to_euler()
scene.frame_set(2)
inspection.register()
first = inspection.live_token()
assert first == inspection.live_token(), "freshness check changes its own revision"
before = preserved()
started = time.monotonic()
request = inspection.inspect_scene("final_lighting", cameras=["Camera", "Side Camera"], frames=[1, 2, 3],
                                     focus=["Cube"], max_side=128, samples=1)
row = wait(request.pending_job)
result = inspection.finish(request.pending_job)
assert result.ok and result.image_path.is_file(), result.text
assert preserved() == before, "inspection leaked camera/frame/render/editor state"
metadata = jobs.read_json(Path(row["output"]) / "inspection.json")
assert len(metadata["views"]) == 6
assert {(v["camera"], v["frame"]) for v in metadata["views"]} == {
    (camera, frame) for camera in ("Camera", "Side Camera") for frame in (1, 2, 3)}
for view in metadata["views"]:
    image = imbuf.load(str(Path(row["output"]) / view["file"]))
    try:
        with image.with_buffer() as buffer:
            rgb = np.asarray(buffer)[..., :3].astype(np.float32) / 255
            red = np.mean((rgb[..., 0] > .2) & (rgb[..., 0] > rgb[..., 1] * 1.5))
            assert red > .05, f"red object missing in {view['camera']} frame {view['frame']}: {red}"
    finally:
        image.free()
assert all(v["signal"]["fraction_above_002"] > .1 for v in metadata["views"])
assert not any(v["signal"]["near_black"] for v in metadata["views"])
estimate = context.estimate_tokens([{"role": "tool", "content": result.text},
                                   {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "image"}}]}])
report = {"wall_seconds": round(time.monotonic() - started, 3), "render_seconds": metadata["elapsed_seconds"],
          "views": 6, "image_bytes": result.image_path.stat().st_size,
          "context_token_estimate_including_fixed_overhead": estimate,
          "provider_billed_tokens": None, "contact_sheet": str(result.image_path)}
(root / "metrics.json").write_text(json.dumps(report, indent=1))
print("INSPECTION Cycles: six visible camera/frame views, state preserved", flush=True)

cube.scale.x = 1.2
stale = inspection.finish(request.pending_job)
assert not stale.ok and stale.image_path is None and "stale" in stale.text, stale
print("INSPECTION rejects stale scene revisions", flush=True)

for purpose in ("geometry", "materials"):
    # No user camera: create a focus-framed camera only in the render copy.
    before = preserved()
    request = inspection.inspect_scene(purpose, cameras=[], frames=[2], focus=["Cube"], max_side=128, samples=1)
    row = wait(request.pending_job)
    result = inspection.finish(request.pending_job)
    assert result.ok and result.image_path.is_file(), result.text
    assert before == preserved()
    meta = jobs.read_json(Path(row["output"]) / "inspection.json")
    assert meta["engine"] == ("BLENDER_WORKBENCH" if purpose == "geometry" else "BLENDER_EEVEE")
    assert not meta["views"][0]["signal"]["near_black"]
    print(f"INSPECTION {purpose}: generated camera, explicit engine, visible signal", flush=True)

for args in ({"cameras": ["Missing"]}, {"focus": ["Missing"]}, {"frames": [True]}, {"max_side": 9999}):
    result = tools.execute("inspect_scene", json.dumps({"purpose": "final_lighting", **args}))
    assert not result.ok and result.image_path is None, result

for mutate in (lambda: setattr(scene.render, "resolution_x", 258),
               lambda: setattr(scene.view_settings, "exposure", .5),
               lambda: setattr(scene.camera.data, "lens", 52),
               lambda: setattr(emission.inputs["Color"], "default_value", (.1, 1, .1, 1))):
    token = inspection.live_token()
    mutate()
    assert token != inspection.live_token(), "a render-relevant edit did not invalidate the inspection"
print("INSPECTION tracks output, color-management, camera and shader edits", flush=True)

sys.path.insert(0, str(Path(__file__).parent / "evals"))
import grade
grade.MAX_SIDE, grade.CYCLES_SAMPLES = 128, 1
scene.render.image_settings.media_type = "VIDEO"
scene.render.image_settings.file_format = "FFMPEG"
before = preserved()
task = SimpleNamespace(id="coverage", render_frames=[1, 2, 3], render_cameras=["*"])
grade_out = root / "grade"
grade_out.mkdir(exist_ok=True)
files, notes, evidence = grade.render(task, grade_out)
assert len(files) == len(evidence) == 6
assert {(e["camera"], e["frame"]) for e in evidence} == {
    (camera, frame) for camera in ("Camera", "Side Camera") for frame in (1, 2, 3)}
assert preserved() == before, "grading leaked camera/frame/video settings"
task.render_cameras = ["Missing"]
try:
    grade.render(task, grade_out)
    raise AssertionError("missing camera was silently substituted")
except ValueError as error:
    assert "missing" in str(error)
assert preserved() == before
print("INSPECTION grader covers all cameras/frames and restores video settings on success/failure", flush=True)

cancel = threading.Event()
finished = []
def run_async():
    try:
        finished.append(agent._run_tool_on_main("inspect_scene", '{"purpose":"geometry","max_side":128}', cancel.is_set))
    except llm.Cancelled:
        finished.append("cancelled")
thread = threading.Thread(target=run_async)
thread.start()
deadline = time.monotonic() + 10
while mainthread._jobs.empty() and time.monotonic() < deadline:
    time.sleep(.01)
mainthread._pump()  # Starts the worker and returns; no native rendering in this pump.
probe = mainthread.run_on_main(lambda: "UI remains responsive")
mainthread._pump()
assert probe.result() == "UI remains responsive" and thread.is_alive()
cancel.set()
while thread.is_alive() and time.monotonic() < deadline:
    mainthread._pump()
    time.sleep(.02)
thread.join(.5)
assert finished == ["cancelled"], finished
active = [row for row in jobs.list_jobs(root / "data") if row["state"] in jobs.ACTIVE]
while active and time.monotonic() < deadline:
    time.sleep(.1)
    active = [row for row in jobs.list_jobs(root / "data") if row["state"] in jobs.ACTIVE]
assert not active, "cancelled inspection worker is still running"
print("INSPECTION agent wait leaves the UI pump responsive and cancellation stops the child", flush=True)
inspection.unregister()
jobs.reap()
print("INSPECTION OK", flush=True)
