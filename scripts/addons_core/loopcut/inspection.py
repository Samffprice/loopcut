"""Bounded, camera/frame-aware visual evidence from a saved scene, with stale-result rejection.

Saving and freshness checks run on Blender's main thread. Waiting runs on the agent thread;
rendering and contact-sheet composition run in a supervised child process.
"""

import json
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from . import checkpoints, job_tools, jobs
from .tools import ToolError, ToolResult

PURPOSES = {"geometry", "materials", "final_lighting"}
MAX_TILES = 12
_epoch = uuid.uuid4().hex
_generation = 0

SCHEMAS = [{"type": "function", "function": {
    "name": "inspect_scene",
    "description": "Inspect named cameras and frames in one annotated contact sheet (max 12 views). "
    "geometry uses Workbench; materials uses an EEVEE studio-light rig; final_lighting uses the scene's "
    "actual engine and lighting, capped in resolution and samples. Renders in a background copy; "
    "leaves camera, frame, selection and editors untouched. Asks once for this bounded inspection. "
    "A scene edit while rendering rejects the stale image. Focus names are labeled; without a camera, "
    "a temporary camera frames them. Named cameras keep their original composition.",
    "parameters": {"type": "object", "properties": {
        "purpose": {"type": "string", "enum": sorted(PURPOSES)},
        "cameras": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
        "frames": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 5,
                   "description": "Defaults to the current frame. For animation inspect first/middle/last or meaningful phases."},
        "focus": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "max_side": {"type": "integer", "minimum": 128, "maximum": 640, "description": "Per-view limit; default 384. Sheet stays within 1536px."},
        "samples": {"type": "integer", "minimum": 1, "maximum": 64, "description": "Default 16; never final production quality."},
        "budget_seconds": {"type": "integer", "minimum": 5, "maximum": 180, "description": "Worker render budget; scene-copy preparation is extra. Default 90."},
    }, "required": ["purpose"]}}}]


def _changed(*args):
    global _generation
    if len(args) > 1 and hasattr(args[1], "updates") and not any(args[1].updates):
        return  # An explicit view-layer update with no changed IDs is not a new revision.
    _generation += 1


_changed._bpy_persistent = None


def register():
    import bpy
    for name in ("depsgraph_update_post", "frame_change_post", "load_post", "undo_post", "redo_post"):
        handlers = getattr(bpy.app.handlers, name)
        if _changed not in handlers:
            handlers.append(_changed)


def unregister():
    import bpy
    for name in ("depsgraph_update_post", "frame_change_post", "load_post", "undo_post", "redo_post"):
        handlers = getattr(bpy.app.handlers, name)
        if _changed in handlers:
            handlers.remove(_changed)


def live_token() -> dict:
    import bpy
    from . import scene_diff
    # Flush pending RNA/edit-mode updates before reading the dependency-graph generation.
    for obj in bpy.context.objects_in_mode:
        obj.update_from_editmode()
    bpy.context.view_layer.update()
    scene = bpy.context.scene
    token = {"epoch": _epoch, "generation": _generation, "scene": scene.as_pointer(),
            "frame": scene.frame_current, "subframe": scene.frame_subframe,
            "camera": scene.camera.name if scene.camera else "", "file": bpy.data.filepath,
            "render": scene_diff.rna_values(scene.render), "view": scene_diff.rna_values(scene.view_settings),
            "display": scene_diff.rna_values(scene.display_settings),
            "world": scene.world.name if scene.world else ""}
    return json.loads(json.dumps(token))  # Match the persisted JSON representation of RNA tuples.


def validate_request(purpose, cameras, frames, focus, max_side, samples, budget_seconds):
    if purpose not in PURPOSES:
        raise jobs.JobError("purpose must be geometry, materials or final_lighting.")
    for value, name, low, high in ((max_side, "max_side", 128, 640), (samples, "samples", 1, 64),
                                  (budget_seconds, "budget_seconds", 5, 180)):
        jobs.integer(value, name, low, high)
    for names, label, limit in ((cameras, "cameras", 4), (focus, "focus", 12)):
        if not isinstance(names, list) or len(names) > limit or any(not isinstance(n, str) or not n for n in names):
            raise jobs.JobError(f"{label} must be a list of up to {limit} names.")
        if len(set(names)) != len(names):
            raise jobs.JobError(f"{label} contains duplicate names.")
    if not isinstance(frames, list) or not 1 <= len(frames) <= 5:
        raise jobs.JobError("Choose 1 to 5 frames.")
    for frame in frames:
        jobs.integer(frame, "frame", -1048574, 1048574)
    if len(set(frames)) != len(frames):
        raise jobs.JobError("Frames must be unique.")
    if max(1, len(cameras)) * len(frames) > MAX_TILES:
        raise jobs.JobError("At most 12 camera/frame combinations per inspection.")


def inspect_scene(purpose: str, cameras=None, frames=None, focus=None, max_side=384, samples=16,
                  budget_seconds=90) -> ToolResult:
    import bpy
    scene = bpy.context.scene
    cameras = ([scene.camera.name] if scene.camera else []) if cameras is None else cameras
    frames = [scene.frame_current] if frames is None else frames
    focus = [] if focus is None else focus
    try:
        validate_request(purpose, cameras, frames, focus, max_side, samples, budget_seconds)
        for name in cameras:
            if name not in scene.objects or scene.objects[name].type != "CAMERA":
                raise jobs.JobError(f"Camera {name!r} is not in this scene.")
        for name in focus:
            if name not in scene.objects:
                raise jobs.JobError(f"Focus object {name!r} is not in this scene.")
        register()
        # Reserve the footer before sizing renders, keeping text readable without scaling the sheet.
        scale = min(max_side / max(scene.render.resolution_x, scene.render.resolution_y),
                    1536 / (len(frames) * scene.render.resolution_x),
                    (1536 / max(1, len(cameras)) - 52) / scene.render.resolution_y)
        width, height = max(4, int(scene.render.resolution_x * scale)), max(4, int(scene.render.resolution_y * scale))
        engine = {"geometry": "BLENDER_WORKBENCH", "materials": "BLENDER_EEVEE"}.get(purpose, scene.render.engine)
        # Internal inspection outputs are owned job data, not a model-selected path through the file gate.
        spec, path = job_tools.create_spec(str(Path(bpy.app.tempdir).resolve()), sorted(frames), budget_seconds,
                                           camera=cameras[0] if cameras else None, width=width, height=height,
                                           samples=samples, _allow_no_camera=True, _engine=engine)
        spec.update(kind="inspection", purpose=purpose, cameras=cameras, focus=focus,
                    camera=cameras[0] if cameras else "Automatic focus view",
                    engine=engine,
                    source_engine=scene.render.engine, output=str(path / "output"),
                    inspection_revision=jobs.digest(Path(__file__).with_name("inspection_worker.py")))
        job_tools.save_snapshot(spec, path)
        spec["live_token"] = live_token()
        jobs.atomic_json(path / "spec.json", spec)
        jobs.launch(path)
        return ToolResult(f"Inspection {spec['id']} is running from saved revision {spec['scene_revision']}.",
                          pending_job=path)
    except (jobs.JobError, OSError, ValueError, RuntimeError) as ex:
        raise ToolError(str(ex)) from ex


def finish(path: Path) -> ToolResult:
    """Main thread, immediately before attaching evidence. Old revisions are never labeled current."""
    try:
        row = jobs.status(path)
        if row["state"] != "complete":
            return ToolResult(f"Inspection {row['id']} {row['state']}: {row.get('error', '')}", ok=False)
        if row.get("live_token") != live_token():
            return ToolResult(f"Inspection {row['id']} is stale: the scene changed while it rendered or Blender "
                              "was restarted. No image attached. Inspect the current scene again.", ok=False)
        sheet = Path(row["output"]) / "contact_sheet.png"
        metadata = jobs.read_json(Path(row["output"]) / "inspection.json")
        if jobs.digest(sheet) != metadata["sheet_sha256"] or metadata["scene_revision"] != row["scene_revision"]:
            raise jobs.JobError("Inspection evidence changed after verification.")
        brief = {key: value for key, value in metadata.items() if key not in {"sheet_sha256", "views"}}
        brief["views"] = [{key: view[key] for key in ("camera", "frame", "focus", "signal")} for view in metadata["views"]]
        return ToolResult("Image attached: scene inspection from saved revision " + row["scene_revision"][:12] +
                          ".\n" + json.dumps(brief), image_path=sheet, evidence_token=row["live_token"])
    except (jobs.JobError, OSError, ValueError) as ex:
        return ToolResult(f"Inspection unavailable: {ex}", ok=False)


def wait_result(path: Path, is_cancelled, run_on_main) -> ToolResult:
    """Agent thread: the UI pump remains free while native rendering runs elsewhere."""
    from . import llm
    while True:
        if is_cancelled():
            jobs.cancel(path)
            raise llm.Cancelled()
        row = jobs.status(path)
        if row["state"] not in jobs.ACTIVE:
            future = run_on_main(lambda: finish(path) if not is_cancelled() else ToolResult("Inspection cancelled.", ok=False))
            while True:
                try:
                    return future.result(timeout=.1)
                except FutureTimeout:
                    if future.done():
                        return future.result()
                    if is_cancelled() and future.cancel():
                        raise llm.Cancelled()
        time.sleep(.1)


def describe(arguments):
    return (f"Inspect {arguments.get('purpose', '?')} · {arguments.get('budget_seconds', 90)}s render budget",
            json.dumps(arguments, indent=2))


DISPATCH = {"inspect_scene": inspect_scene}
