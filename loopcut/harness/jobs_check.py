"""Real render/resume/export check. Blender -b --factory-startup --python-exit-code 1 --python this.py.
No provider calls. Artifacts are temporary unless LOOPCUT_JOB_CHECK_DIR is set.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import job_tools, jobs, state, tools

temporary = tempfile.TemporaryDirectory(prefix="loopcut-job-check-")
root = Path(os.environ.get("LOOPCUT_JOB_CHECK_DIR", temporary.name))
root.mkdir(parents=True, exist_ok=True)
os.environ["LOOPCUT_DATA_DIR"] = str(root / "data")


def wait(job_id, predicate=None, timeout=180):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        row = jobs.status(job_tools._path(job_id))
        if predicate(row) if predicate else row["state"] not in jobs.ACTIVE:
            return row
        time.sleep(.2)
    raise AssertionError(f"job timeout: {row}")


def start(**kwargs):
    result = job_tools.start_render_job(output_dir=str(root / "outputs"), width=64, height=64,
                                       samples=1, budget_seconds=150, **kwargs)
    return json.loads(result.text.partition("\n")[0])["id"]  # Guidance for the model follows the JSON.


scene = bpy.context.scene
scene.render.engine = "BLENDER_EEVEE"
before = (bpy.data.filepath, scene.camera.name, scene.frame_current, scene.render.filepath,
          scene.render.resolution_x, scene.render.resolution_y, scene.render.image_settings.media_type)
job_id = start(frames=list(range(1, 121)))
# The editing process can change the live scene immediately while the job runs from its copy.
bpy.data.objects["Cube"].location.x = 9
assert before == (bpy.data.filepath, scene.camera.name, scene.frame_current, scene.render.filepath,
                  scene.render.resolution_x, scene.render.resolution_y, scene.render.image_settings.media_type)
wait(job_id, lambda row: row["progress"].get("completed", 0) >= 3 or row["state"] in jobs.TERMINAL)
job_tools.cancel_render_job(job_id)
cancelled = wait(job_id)
assert cancelled["state"] == "cancelled", cancelled
assert not cancelled["progress"].get("verified_complete"), cancelled
records = cancelled["progress"]["files"]
assert len(records) >= 3
output = Path(cancelled["output"])
good = output / records[0]["file"]
unchanged = good.stat().st_mtime_ns
bad = output / records[1]["file"]
bad.write_bytes(bad.read_bytes()[:60])
missing = output / records[2]["file"]
missing.unlink()
state.reset()  # No conversation owns the worker or its continuation.
job_tools.resume_render_job(job_id)
done = wait(job_id)
assert done["state"] == "complete", done
assert done["progress"]["completed"] == 120
assert done["progress"]["reused"] == len(records) - 2, done["progress"]
assert done["progress"]["rendered"] == 120 - len(records) + 2
assert good.stat().st_mtime_ns == unchanged
for record in done["progress"]["files"]:
    jobs.verify_png(output / record["file"], 64, 64)
assert job_tools.render_job_status(job_id, preview=True).image_path.is_file()
assert sorted(p.name for p in output.iterdir()) == [f"frame_{f:07d}.png" for f in range(1, 121)], "renders only"
assert (job_tools._path(job_id) / "manifest.json").is_file(), "the manifest sits with the job's records"
print("JOBS resumed 120 frames; only missing/corrupt frames re-rendered", flush=True)

movie_id = start(frames=[1, 2, 3], format="MP4")
movie = wait(movie_id)
assert movie["state"] == "complete", movie
assert movie["progress"]["movie"]["frames"] == 3
assert abs(movie["progress"]["movie"]["duration_seconds"] - 3 / scene.render.fps) < .01
assert sorted(p.name for p in Path(movie["output"]).iterdir()) == [
    "frame_0000001.png", "frame_0000002.png", "frame_0000003.png", "movie.mp4"], list(Path(movie["output"]).iterdir())
print("JOBS MP4 dimensions/frame count/fps verified", flush=True)

# status with wait: the main-thread call only hands back the rest, which the agent thread runs.
waited_id = start(frames=[1, 2])
first = job_tools.render_job_status(waited_id, wait=120)
assert first.then, f"a job just started should still be queued or running: {first.text}"
waited = first.then(lambda: False, lambda fn: fn())
assert jobs.status(job_tools._path(waited_id))["state"] == "complete", waited.text
assert "Waited" in waited.text and "now complete: 2/2 frames" in waited.text, waited.text
print("JOBS status with wait returned when the job ended:", waited.text.splitlines()[-1], flush=True)

spec, path = job_tools.create_spec(str(root / "outputs"), [1], 1, width=64, height=64, samples=1)
job_tools.save_snapshot(spec, path)
jobs.launch(path)
timeout = wait(spec["id"])
assert timeout["state"] == "failed" and "budget" in timeout["error"], timeout
print("JOBS independent time budget enforced", flush=True)

launcher_result = root / "detached.json"
launcher_code = (f"import sys; sys.path.insert(0, {str(Path(job_tools.__file__).parents[1])!r}); "
                 "from loopcut import job_tools; from pathlib import Path; "
                 f"result = job_tools.start_render_job(output_dir={str(root / 'outputs')!r}, "
                 "frames=[1,2,3], width=32, height=32, samples=1, budget_seconds=90); "
                 f"Path({str(launcher_result)!r}).write_text(result.text)")
subprocess.run([str(Path(bpy.app.binary_path).resolve()), "-b", "--factory-startup", "--python-exit-code", "1",
                "--python-expr", launcher_code], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
detached_id = json.loads(launcher_result.read_text().partition("\n")[0])["id"]
detached = wait(detached_id)
assert detached["state"] == "complete", detached
print("JOBS completed after the launching Blender exited", flush=True)

# folder: straight into a folder next to the .blend, start/end instead of a list; never into one with files.
project = root / "project"
project.mkdir()
bpy.ops.wm.save_as_mainfile(filepath=str(project / "shot.blend"))
(project / "renders" / "spin").mkdir(parents=True)
(project / "renders" / "spin" / ".DS_Store").write_bytes(b"")
result = tools.execute("render_job", json.dumps({"action": "start", "folder": "renders/spin", "start": 2, "end": 4,
                                                 "width": 32, "height": 32, "samples": 1, "budget_seconds": 90}))
assert result.ok, result.text
named_id = json.loads(result.text.partition("\n")[0])["id"]
refused = tools.execute("render_job", json.dumps({"action": "start", "folder": "renders/spin", "frames": [1],
                                                  "budget_seconds": 90}))
assert not refused.ok and ("is rendering into" in refused.text or "not empty" in refused.text), refused.text
named = wait(named_id)
assert named["state"] == "complete" and named["frames"] == [2, 3, 4], named
assert Path(named["output"]) == (project / "renders" / "spin").resolve(), named["output"]
assert sorted(p.name for p in Path(named["output"]).iterdir()) == [
    ".DS_Store", "frame_0000002.png", "frame_0000003.png", "frame_0000004.png"], list(Path(named["output"]).iterdir())
refused = tools.execute("render_job", json.dumps({"action": "start", "folder": "renders/spin", "frames": [1],
                                                  "budget_seconds": 90}))
assert not refused.ok and "not empty (frame_0000002.png, frame_0000003.png, frame_0000004.png)" in refused.text, refused.text
refused = tools.execute("render_job", json.dumps({"action": "start", "folder": "a", "output_dir": "b", "frames": [1],
                                                  "budget_seconds": 90}))
assert not refused.ok and "not both" in refused.text, refused.text
print("JOBS folder next to the .blend, frames 2-4, only the renders in it; a folder with files refused", flush=True)
jobs.reap()
print("JOBS OK", flush=True)
