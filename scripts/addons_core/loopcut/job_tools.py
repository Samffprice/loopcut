"""Main-thread boundary for durable jobs. Only saving a copy touches the editing process."""

import json
import time
import uuid
from pathlib import Path

from . import checkpoints, jobs, state
from .tools import ToolError, ToolResult

SCHEMAS = [
    {"type": "function", "function": {
        "name": "start_render_job",
        "description": "Render a frozen scene in the background. Asks once for this job and time budget. "
                       "Creates a NEW subfolder under output_dir; never overwrites other work. PNG frames, "
                       "optionally opaque, silent MP4 from that sequence. File Output nodes are disabled. "
                       "Returns a job id, not a finished render. Inspect status later; closing chat is safe.",
        "parameters": {"type": "object", "properties": {
            "output_dir": {"type": "string"},
            "frames": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 10000},
            "camera": {"type": "string", "description": "Named camera; default the active camera"},
            "format": {"type": "string", "enum": ["PNG", "MP4"]},
            "width": {"type": "integer", "minimum": 4, "maximum": 8192},
            "height": {"type": "integer", "minimum": 4, "maximum": 8192},
            "samples": {"type": "integer", "minimum": 1, "maximum": 4096},
            "budget_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
        }, "required": ["output_dir", "frames", "budget_seconds"]}}},
    {"type": "function", "function": {
        "name": "render_job_status", "description": "List durable jobs or inspect one. preview=true attaches "
        "its latest verified frame, explicitly from the saved revision, not the current editing scene.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"},
                                                                  "preview": {"type": "boolean"}}}}},
    {"type": "function", "function": {
        "name": "cancel_render_job", "description": "Cancel this background job; keeps verified outputs for resume.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}}},
    {"type": "function", "function": {
        "name": "resume_render_job", "description": "Approve another attempt with the SAME frozen scene, output "
        "and time budget. Decodes and hashes existing frames, renders only missing/invalid frames. "
        "Does not incorporate edits since the original job; start a new job for those.",
        "parameters": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]}}},
]


def _path(job_id):
    return jobs.folder(checkpoints.data_root(), job_id)


def preview_path(row: dict) -> Path | None:
    output = Path(row["output"])
    if row.get("kind") == "inspection" and row["state"] == "complete":
        record = jobs.read_json(output / "inspection.json")
        target, expected = output / "contact_sheet.png", record["sheet_sha256"]
    else:
        records = row["progress"].get("files", [])
        if not records:
            return None
        record = records[-1]
        target, expected = output / record["file"], record["sha256"]
    if target.parent != output or jobs.digest(target) != expected:
        raise jobs.JobError("Preview changed after verification.")
    return target


def create_spec(output_dir: str, frames: list[int], budget_seconds: int, camera=None, format="PNG",
                width=None, height=None, samples=None, _allow_no_camera=False, _engine=None) -> tuple[dict, Path]:
    import bpy
    from . import files
    scene, r = bpy.context.scene, bpy.context.scene.render
    selected = bpy.data.objects.get(camera) if camera is not None else scene.camera
    if (selected is None and not _allow_no_camera) or (selected is not None and
            (selected.type != "CAMERA" or selected.name not in scene.objects)):
        raise jobs.JobError("Choose a camera in this scene before starting a render job.")
    job_id = uuid.uuid4().hex
    output = files.check(files.resolve(output_dir)) / f"loopcut-{job_id}"
    spec = {"version": 1, "id": job_id, "kind": "render", "created": time.time(),
            "session_id": state.session()["id"], "source": bpy.data.filepath, "scene": scene.name,
            "camera": selected.name if selected else "", "engine": _engine or r.engine, "frames": frames, "format": format,
            "width": width if width is not None else max(4, r.resolution_x * r.resolution_percentage // 100),
            "height": height if height is not None else max(4, r.resolution_y * r.resolution_percentage // 100),
            "samples": samples if samples is not None else (scene.cycles.samples if r.engine == "CYCLES" else
                       scene.eevee.taa_render_samples if r.engine == "BLENDER_EEVEE" else 32),
            "fps": r.fps, "fps_base": r.fps_base, "budget_seconds": budget_seconds,
            "output": str(output), "binary": str(Path(bpy.app.binary_path).resolve()),
            "existing_files": "verify_owned_frames", "side_outputs": "disabled", "device": "CPU",
            "blender_build": bpy.app.build_hash.decode(), "blender_version": bpy.app.version_string,
            "renderer_revision": jobs.digest(Path(__file__).with_name("job_worker.py"))}
    jobs.validate_spec(spec)
    if format == "MP4" and not bpy.app.ffmpeg.supported:
        raise jobs.JobError("This Blender build cannot encode MP4. Use PNG.")
    return spec, _path(job_id)


def save_snapshot(spec: dict, path: Path) -> None:
    import bpy
    for obj in bpy.context.objects_in_mode:
        obj.update_from_editmode()
    bpy.context.view_layer.update()
    # Keep external dependencies evidence-bound. A changed asset invalidates resume rather than
    # silently mixing two versions of a texture or linked scene into one sequence.
    assets = []
    for raw in sorted(set(bpy.utils.blend_paths(absolute=True, packed=False, local=False))):
        asset = Path(raw)
        if not asset.is_file():
            raise jobs.JobError(f"External asset is missing or uses a sequence/tile pattern: {asset.name}. "
                                "Pack/resolve it before creating a durable job.")
        assets.append({"path": str(asset.resolve()), "sha256": jobs.digest(asset)})
    path.mkdir(parents=True)
    snapshot = path / "scene.blend"
    result = bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True, compress=True, relative_remap=True)
    if result != {"FINISHED"}:
        raise jobs.JobError("Blender could not save the job's scene copy.")
    spec.update(scene_revision=jobs.digest(snapshot), assets=assets)
    output = Path(spec["output"])
    output.mkdir(parents=True, exist_ok=False)
    jobs.atomic_json(output / "loopcut-job.json", {"id": spec["id"]})
    jobs.atomic_json(path / "spec.json", spec)


def _summary(row):
    p = row.get("progress", {})
    return {key: row.get(key) for key in ("id", "kind", "purpose", "state", "scene_revision", "source", "camera", "engine",
             "width", "height", "samples", "budget_seconds", "output", "error")} | {
        "frame_count": len(row["frames"]), "first_frame": row["frames"][0], "last_frame": row["frames"][-1],
        "completed": p.get("completed", 0), "total": p.get("total", len(row["frames"])),
        "reused": p.get("reused", 0), "rendered": p.get("rendered", 0), "phase": p.get("phase"),
        "movie": p.get("movie"), "verified_complete": row["state"] == "complete" and p.get("verified_complete", False)}


def start_render_job(output_dir, frames, budget_seconds, camera=None, format="PNG", width=None,
                     height=None, samples=None) -> ToolResult:
    try:
        spec, path = create_spec(output_dir, frames, budget_seconds, camera, format, width, height, samples)
        save_snapshot(spec, path)
        row = jobs.launch(path)
    except (jobs.JobError, OSError, ValueError, RuntimeError) as ex:
        raise ToolError(str(ex)) from ex
    return ToolResult(json.dumps(_summary(row)))


def render_job_status(job_id: str | None = None, preview: bool = False) -> ToolResult:
    try:
        if job_id is None:
            return ToolResult(json.dumps([_summary(row) for row in jobs.list_jobs(checkpoints.data_root())[:30]]))
        row = jobs.status(_path(job_id))
        image = None
        if preview:
            from . import attachments, files, scratch
            source = preview_path(row)
            if source:
                # Existing helper bounds the image the model sees.
                image = scratch.folder() / f"job-{job_id}-{jobs.digest(source)[:12]}.png"
                attachments._to_png(source, image, files.IMAGE_SIDE)
        return ToolResult(json.dumps(_summary(row)) + "\nAny preview is from this job's SAVED revision, not the live scene.",
                          image_path=image)
    except (jobs.JobError, OSError, ValueError) as ex:
        raise ToolError(str(ex)) from ex


def cancel_render_job(job_id: str) -> ToolResult:
    try:
        jobs.cancel(_path(job_id))
        return render_job_status(job_id)
    except (jobs.JobError, OSError, ValueError) as ex:
        raise ToolError(str(ex)) from ex


def resume_render_job(job_id: str) -> ToolResult:
    try:
        return ToolResult(json.dumps(_summary(jobs.launch(_path(job_id)))))
    except (jobs.JobError, OSError, ValueError) as ex:
        raise ToolError(str(ex)) from ex


DISPATCH = {name: globals()[name] for name in (
    "start_render_job", "render_job_status", "cancel_render_job", "resume_render_job")}


def describe(name: str, arguments: dict):
    if name == "start_render_job":
        frames = arguments.get("frames", [])
        return (f"Background {arguments.get('format', 'PNG')} render · "
                f"{len(frames) if isinstance(frames, list) else '?'} frames · "
                f"{arguments.get('budget_seconds', '?')}s budget", json.dumps(arguments, indent=2))
    if name == "resume_render_job":
        return "Resume saved render job (same settings and time budget)", json.dumps(arguments, indent=2)
    return None


def refresh_ui():
    jobs.reap()
    state.ui["jobs"] = jobs.list_jobs(checkpoints.data_root())


def ui_action(kind: str, job_id: str):
    import bpy
    try:
        path = _path(job_id)
        if kind == "job_cancel":
            jobs.cancel(path)
        elif kind == "job_resume":
            jobs.launch(path)
        else:
            row = jobs.status(path)
            target = Path(row["output"])
            if kind == "job_preview":
                target = preview_path(row)
                if target is None:
                    raise jobs.JobError("No verified frame yet.")
            bpy.ops.wm.path_open(filepath=str(target))
        state.ui["job_error"] = ""
    except (jobs.JobError, OSError, ValueError, RuntimeError) as ex:
        state.ui["job_error"] = str(ex)
    refresh_ui()
