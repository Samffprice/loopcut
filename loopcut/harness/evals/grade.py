"""Score a finished scene, whatever tool made it, and render the frames the judges look at:
    Blender -b --factory-startup --python harness/evals/grade.py -- <task_id> <final.blend> <out_dir> [<stage1.blend>]
The task's start scene is rebuilt first so the check has the same `before` snapshot as a live
run; then the file is opened and checked. Two-turn tasks take the scene saved after the first
brief as well: it is checked against the first checklist and remembered, then the final file is
checked against the follow-up checklist. Writes <out_dir>/<task>.grade.json and one PNG per
frame in the task's render_frames (the current frame when it has none), at most 640 px wide,
with the engine the scene itself uses: a tool that set up Cycles is rendered with Cycles (capped
at 64 samples, denoised, on the GPU), one that left EEVEE gets EEVEE. From the scene's camera,
or from a camera added to frame everything when there is none."""

import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace

import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tasks  # noqa: E402

MAX_SIDE = 640
EEVEE_SAMPLES = 64
CYCLES_SAMPLES = 64


def frame_everything() -> None:
    """A camera on a three-quarter view of every mesh, for scenes that have none."""
    boxes = [tasks.bounds(o) for o in tasks.meshes()]
    if boxes:
        low = Vector(min(b[0][i] for b in boxes) for i in range(3))
        high = Vector(max(b[1][i] for b in boxes) for i in range(3))
    else:
        low, high = Vector((-1, -1, -1)), Vector((1, 1, 1))
    center, radius = (low + high) / 2, max((high - low).length / 2, 0.1)
    camera = bpy.data.objects.new("GradeCamera", bpy.data.cameras.new("GradeCamera"))
    bpy.context.scene.collection.objects.link(camera)
    direction = Vector((1.0, -1.2, 0.8)).normalized()
    camera.location = center + direction * radius * 2.6
    camera.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = camera


def use_gpu() -> None:
    prefs = bpy.context.preferences.addons.get("cycles")
    if prefs is None:
        return
    prefs = prefs.preferences
    for kind in ("METAL", "CUDA", "OPTIX", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = kind
            prefs.refresh_devices()
        except TypeError:
            continue
        if any(d.type == kind for d in prefs.devices):
            for device in prefs.devices:
                device.use = device.type == kind
            bpy.context.scene.cycles.device = "GPU"
            return


def render(task, out: Path, suffix: str = "") -> tuple[list[str], list[str]]:
    scene = bpy.context.scene
    notes, files = [], []
    if scene.camera is None:
        frame_everything()
        notes.append("no camera in the scene: rendered from a camera added to frame everything")
    r = scene.render
    if scene.render.engine == "CYCLES":
        scene.cycles.samples = min(scene.cycles.samples, CYCLES_SAMPLES)
        scene.cycles.use_denoising = True
        use_gpu()
        notes.append(f"rendered with Cycles, {scene.cycles.samples} samples")
    else:
        scene.render.engine = "BLENDER_EEVEE"
        scene.eevee.taa_render_samples = EEVEE_SAMPLES
        notes.append("rendered with EEVEE, the engine the scene was left on")
    r.resolution_percentage = max(1, min(100, int(MAX_SIDE * 100 / max(r.resolution_x, r.resolution_y, 1))))
    r.film_transparent = False
    r.image_settings.file_format, r.image_settings.color_mode = "PNG", "RGB"
    r.use_border = False
    for frame in task.render_frames or [scene.frame_current]:
        scene.frame_set(frame)
        path = out / f"{task.id}{suffix}.f{frame:03d}.png"
        r.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        files.append(path.name)
    return files, notes


def open_file(path: Path) -> None:
    bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False)
    bpy.context.view_layer.update()


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1:]
    task, final, out = tasks.BY_ID[args[0]], Path(args[1]).resolve(), Path(args[2]).resolve()
    stage1 = Path(args[3]).resolve() if len(args) > 3 else None
    out.mkdir(parents=True, exist_ok=True)
    first = getattr(task.check, "requirements", [task.id])
    second = [f"then: {r}" for r in getattr(task.follow_up_check, "requirements", [])] if task.follow_up else []
    result = {"id": task.id, "file": str(final), "prompt": task.prompt, "follow_up": task.follow_up,
              "requirements": first + second, "notes": []}
    try:
        bpy.ops.wm.read_factory_settings(use_empty=False)
        if task.setup:
            task.setup()
        ctx = SimpleNamespace(before=tasks.snapshot(), session=None, stage1=None, memory=None)
        problems = []
        if task.follow_up:
            if stage1 is not None and stage1.is_file():
                open_file(stage1)
                problems += task.check(ctx)
                ctx.stage1 = tasks.snapshot()
                ctx.memory = task.remember(ctx) if task.remember else {}
                result["stage_renders"], notes = render(task, out, suffix=".s1")
                result["notes"] += [f"stage 1: {n}" for n in notes]
            else:
                problems.append("stage 1: no <task>.stage1.blend was saved after the first brief, so it was not graded")
                problems += [f"{r}: not graded" for r in first]
            open_file(final)
            problems += [f"then: {p}" for p in task.follow_up_check(ctx)]
        else:
            open_file(final)
            problems += task.check(ctx)
        result["problems"] = problems
        result["met"] = len(result["requirements"]) - len([p for p in problems if not p.startswith("stage 1:")])
        result["passed"] = not problems
        result["renders"], notes = render(task, out)
        result["notes"] += notes
    except Exception:
        result["problems"] = ["harness: " + traceback.format_exc()]
        result["met"], result["passed"] = 0, False
    (out / f"{task.id}.grade.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"GRADE {task.id}: {result['met']}/{len(result['requirements'])} "
          f"{'; '.join(result['problems'])[:300]}")


main()
sys.stdout.flush()
