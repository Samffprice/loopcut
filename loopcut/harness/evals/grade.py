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
import hashlib
import sys
import time
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


def render(task, out: Path, suffix: str = "") -> tuple[list[str], list[str], list[dict]]:
    scene = bpy.context.scene
    notes, files, evidence = [], [], []
    source_revision = None
    if bpy.data.filepath and Path(bpy.data.filepath).is_file():
        with Path(bpy.data.filepath).open("rb") as handle:
            source_revision = hashlib.file_digest(handle, "sha256").hexdigest()
    saved_camera, saved_frame, saved_subframe = scene.camera, scene.frame_current, scene.frame_subframe
    r = scene.render
    properties = [(r, key) for key in ("resolution_percentage", "filepath", "use_border", "use_file_extension")]
    properties += [(r.image_settings, key) for key in ("media_type", "file_format", "color_mode", "color_depth")]
    if r.engine == "CYCLES":
        properties += [(scene.cycles, key) for key in ("samples", "use_denoising", "device")]
    elif r.engine == "BLENDER_EEVEE":
        properties.append((scene.eevee, "taa_render_samples"))
    elif r.engine != "BLENDER_WORKBENCH":
        raise ValueError(f"Unsupported evaluation engine: {r.engine}")
    saved = [(owner, key, getattr(owner, key)) for owner, key in properties]
    muted = []
    generated = None
    try:
        requested = getattr(task, "render_cameras", [])
        cameras = sorted((o for o in scene.objects if o.type == "CAMERA"), key=lambda o: o.name)
        if requested and requested != ["*"]:
            missing = [name for name in requested if name not in scene.objects or scene.objects[name].type != "CAMERA"]
            if missing:
                raise ValueError(f"Requested evaluation camera(s) missing: {', '.join(missing)}")
            cameras = [scene.objects[name] for name in requested]
        elif requested != ["*"]:
            cameras = [scene.camera] if scene.camera else []
        if not cameras:
            frame_everything()
            generated = scene.camera
            cameras = [generated]
            notes.append("no camera in the scene: rendered from a temporary camera framing everything")
        if r.engine == "CYCLES":
            scene.cycles.samples = min(scene.cycles.samples, CYCLES_SAMPLES)
            scene.cycles.use_denoising = True
            use_gpu()
            samples = scene.cycles.samples
        elif r.engine == "BLENDER_EEVEE":
            scene.eevee.taa_render_samples = min(scene.eevee.taa_render_samples, EEVEE_SAMPLES)
            samples = scene.eevee.taa_render_samples
        else:
            samples = None
        # Side outputs must not write to paths from a contestant's scene.
        trees = list(bpy.data.node_groups)
        if getattr(scene, "node_tree", None):
            trees.append(scene.node_tree)
        for tree in trees:
            for node in tree.nodes:
                if node.bl_idname == "CompositorNodeOutputFile":
                    muted.append((node, node.mute))
                    node.mute = True
        notes.append(f"rendered {len(cameras)} camera(s) with {r.engine}, {samples} samples; original scene lighting and color management")
        r.resolution_percentage = max(1, min(r.resolution_percentage, int(MAX_SIDE * 100 / max(r.resolution_x, r.resolution_y, 1))))
        r.image_settings.media_type = "IMAGE"
        r.image_settings.file_format, r.image_settings.color_mode = "PNG", "RGBA"
        r.use_border, r.use_file_extension = False, False
        for index, camera in enumerate(cameras):
            for frame in task.render_frames or [saved_frame]:
                scene.frame_set(frame)
                scene.camera = camera
                path = out / f"{task.id}{suffix}.c{index:02d}.f{frame:03d}.png"
                r.filepath = str(path)
                started = time.monotonic()
                bpy.ops.render.render(write_still=True)
                image = bpy.data.images.load(str(path), check_existing=False)
                try:
                    size = list(image.size)  # Loading is lazy; accessing size triggers decoding.
                    if min(size) <= 0 or not image.has_data or len(image.pixels[:4]) != 4:
                        raise RuntimeError(f"Evaluation image does not decode: {path.name}")
                finally:
                    bpy.data.images.remove(image)
                files.append(path.name)
                evidence.append({"file": path.name, "camera": camera.name, "frame": frame, "engine": r.engine,
                                 "scene_revision": source_revision,
                                 "samples": samples, "size": size, "view_transform": scene.view_settings.view_transform,
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                 "elapsed_seconds": round(time.monotonic() - started, 3)})
        return files, notes, evidence
    finally:
        for owner, key, value in saved:
            setattr(owner, key, value)
        for node, value in muted:
            node.mute = value
        scene.frame_set(saved_frame, subframe=saved_subframe)
        scene.camera = saved_camera
        if generated:
            data = generated.data
            bpy.data.objects.remove(generated, do_unlink=True)
            bpy.data.cameras.remove(data)


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
                result["stage_renders"], notes, result["stage_render_evidence"] = render(task, out, suffix=".s1")
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
        result["renders"], notes, result["render_evidence"] = render(task, out)
        result["notes"] += notes
    except Exception:
        result["problems"] = ["harness: " + traceback.format_exc()]
        result["met"], result["passed"] = 0, False
    (out / f"{task.id}.grade.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"GRADE {task.id}: {result['met']}/{len(result['requirements'])} "
          f"{'; '.join(result['problems'])[:300]}")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
