"""Main-thread boundary for durable jobs. Only saving a copy touches the editing process."""

import functools
import json
import os
import time
import uuid
from pathlib import Path

from . import checkpoints, cost_guard, jobs, state, tools
from .tools import ToolError, ToolResult

WAIT_CAP = 240   # Seconds one status call waits at most; the schema says so too.
WAIT_READ = 1.0  # Seconds between reads of the job's state file while waiting.
WAIT_TICK = 0.1  # Seconds between checks for a stop while waiting.
WAITING = {"queued", "running"}  # A wait goes on through these; cancelling (the user's Cancel) ends it.
# After start and resume. An eval turn spent 38 of its 60 steps polling a job, sleeping in
# run_python in between (product_reveal, GLM-5.3-Flash, 2026-09-27).
STARTED_NOTE = ("\nThe job renders in the background and goes on after this turn ends: do not poll it. If you "
                "need its output this turn, call status with its job_id and wait=<seconds>; otherwise finish "
                "the turn.")

SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "render_job",
        "title": "Managing a background render",
        "description": "Durable background renders of a frozen scene copy; closing chat is safe. start: PNG "
                       "frames, or a silent MP4 from them, into folder (by the .blend; new or empty) or a NEW "
                       "subfolder of output_dir, never overwriting; File Output nodes are disabled. Needs "
                       "budget_seconds; frames, or start/end (default the scene's range); asks once; returns a job "
                       "id, not a finished render. status: list jobs, or inspect job_id; preview=true attaches its "
                       "latest verified frame (the saved revision). cancel: stop job_id; keeps verified frames. "
                       "resume: another attempt for job_id with the SAME frozen scene, output and time budget; "
                       "renders only missing/invalid frames. Edits since need a new job.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["start", "status", "cancel", "resume"]},
            "job_id": {"type": "string"},
            "preview": {"type": "boolean"},
            "output_dir": {"type": "string"},
            "folder": {"type": "string"},
            "frames": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 10000},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "camera": {"type": "string", "description": "Named camera; default the active camera"},
            "format": {"type": "string", "enum": ["PNG", "MP4"]},
            "width": {"type": "integer", "minimum": 4, "maximum": 8192},
            "height": {"type": "integer", "minimum": 4, "maximum": 8192},
            "samples": {"type": "integer", "minimum": 1, "maximum": 4096},
            "budget_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
            "wait": {"type": "integer", "maximum": WAIT_CAP,
                     "description": "status: seconds to wait for job_id to end"},
        }, "required": ["action"]}}},
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


def create_spec(output_dir: str | None, frames: list[int], budget_seconds: int, camera=None, format="PNG",
                width=None, height=None, samples=None, _allow_no_camera=False, _engine=None,
                folder: str | None = None) -> tuple[dict, Path]:
    """The job's record. The frames go into a new loopcut-<id> subfolder of output_dir, or straight
    into folder (relative to the .blend), which must be new or empty and not another running job's."""
    import bpy
    from . import files
    scene, r = bpy.context.scene, bpy.context.scene.render
    selected = bpy.data.objects.get(camera) if camera is not None else scene.camera
    if (selected is None and not _allow_no_camera) or (selected is not None and
            (selected.type != "CAMERA" or selected.name not in scene.objects)):
        raise jobs.JobError("Choose a camera in this scene before starting a render job.")
    job_id = uuid.uuid4().hex
    if (output_dir is None) == (folder is None):
        raise jobs.JobError("Give output_dir (a new subfolder is made in it) or folder (the frames go straight "
                            "into it), not both." if folder is not None else "render_job start needs output_dir or folder.")
    if folder is not None:
        output = files.check(files.resolve(folder))
        jobs.ensure_empty(output)
        busy = [row["id"] for row in jobs.list_jobs(checkpoints.data_root())
                if row["state"] in jobs.ACTIVE and Path(row["output"]) == output]
        if busy:
            raise jobs.JobError(f"Job {busy[0]} is rendering into {output}; name another folder.")
    else:
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


def _label(idb) -> str:
    kind = "library" if idb.id_type == "LIBRARY" else idb.id_type.lower()
    return f'{kind} "{idb.name}"' + (f" (linked from {Path(idb.library.filepath).name})" if idb.library else "")


def external_assets() -> list[dict]:
    """The files outside the .blend that the scene reads (images, fonts, sounds, volumes, caches,
    libraries), each with its hash. One that is missing, or a sequence/tile pattern, raises
    JobError naming it in full and the datablocks that use it. Packed data is inside the file;
    weak references (the file an appended datablock came from, which Blender keeps only to reuse
    it on a later append) are never read, so neither counts."""
    import bpy
    users: dict[str, set] = {}
    for idb, paths in bpy.data.file_path_map().items():  # Skips packed data and weak references.
        for raw in paths:
            users.setdefault(os.path.normpath(bpy.path.abspath(raw, library=idb.library)), set()).add(_label(idb))
    missing = [path for path in sorted(users) if not Path(path).is_file()]
    if missing:
        named = [f"{path} (used by {', '.join(sorted(users[path])[:3])}{' and more' if len(users[path]) > 3 else ''})"
                 for path in missing[:3]]
        raise jobs.JobError("External asset is missing or uses a sequence/tile pattern: " + "; ".join(named)
                            + (f"; and {len(missing) - 3} more" if len(missing) > 3 else "")
                            + ". Pack/resolve it before creating a durable job.")
    return [{"path": str(Path(path).resolve()), "sha256": jobs.digest(Path(path))} for path in sorted(users)]


def save_snapshot(spec: dict, path: Path) -> None:
    import bpy
    for obj in bpy.context.objects_in_mode:
        obj.update_from_editmode()
    bpy.context.view_layer.update()
    # Keep external dependencies evidence-bound. A changed asset invalidates resume rather than
    # silently mixing two versions of a texture or linked scene into one sequence.
    assets = external_assets()
    # The output holds only renders: the job knows its folder by identity, and its records stay in path.
    output = Path(spec["output"])
    jobs.ensure_empty(output)
    path.mkdir(parents=True)
    output.mkdir(parents=True, exist_ok=True)  # An inspection's is inside path.
    snapshot = path / "scene.blend"
    result = bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True, compress=True, relative_remap=True)
    if result != {"FINISHED"}:
        raise jobs.JobError("Blender could not save the job's scene copy.")
    spec.update(scene_revision=jobs.digest(snapshot), assets=assets, output_identity=jobs.identity(output))
    jobs.atomic_json(path / "spec.json", spec)


def _summary(row):
    p = row.get("progress", {})
    return {key: row.get(key) for key in ("id", "kind", "purpose", "state", "scene_revision", "source", "camera", "engine",
             "width", "height", "samples", "budget_seconds", "output", "error")} | {
        "frame_count": len(row["frames"]), "first_frame": row["frames"][0], "last_frame": row["frames"][-1],
        "completed": p.get("completed", 0), "total": p.get("total", len(row["frames"])),
        "reused": p.get("reused", 0), "rendered": p.get("rendered", 0), "phase": p.get("phase"),
        "movie": p.get("movie"), "verified_complete": row["state"] == "complete" and p.get("verified_complete", False)}


def frame_range(frames=None, start=None, end=None) -> list:
    """frames as given, or every frame from start to end, each defaulting to the scene's."""
    import bpy
    if frames is not None:
        if start is not None or end is not None:
            raise jobs.JobError("Give frames, or start/end, not both.")
        return frames
    scene = bpy.context.scene
    first, last = scene.frame_start if start is None else start, scene.frame_end if end is None else end
    jobs.integer(first, "start", -1048574, 1048574)
    jobs.integer(last, "end", -1048574, 1048574)
    if not 1 <= last - first + 1 <= 10000:
        raise jobs.JobError(f"start {first} to end {last}: request 1 to 10000 frames, end not before start.")
    return list(range(first, last + 1))


def start_render_job(budget_seconds, output_dir=None, frames=None, camera=None, format="PNG", width=None,
                     height=None, samples=None, folder=None, start=None, end=None) -> ToolResult:
    try:
        frames = frame_range(frames, start, end)
        spec, path = create_spec(output_dir, frames, budget_seconds, camera, format, width, height, samples,
                                 folder=folder)
        # A resume renders this saved copy, so the check here covers it too.
        refusal = cost_guard.render_refusal()
        if refusal:
            raise ToolError(refusal)
        save_snapshot(spec, path)
        row = jobs.launch(path)
    except (jobs.JobError, OSError, ValueError, RuntimeError) as ex:
        raise ToolError(str(ex)) from ex
    from . import files
    return ToolResult(json.dumps(_summary(row)) + STARTED_NOTE
                      + files.relative_note(*(raw for raw in (output_dir, folder) if raw is not None)))


def _duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60:02d}s"


def progress_note(row: dict, waited: float | None = None) -> str:
    """Where a job stands, in a line: frames done, time so far, a rough time left from the frames
    rendered so far, and, while it runs, how to wait for it rather than poll."""
    p = row.get("progress", {})
    done, total = p.get("completed", 0), p.get("total", len(row["frames"]))
    head = f"Waited {_duration(waited)}; " if waited is not None else ""
    if row["state"] not in WAITING:
        took = f" in {_duration(row['elapsed_seconds'])}" if row.get("elapsed_seconds") else ""
        return f"\n{head}the job is now {row['state']}: {done}/{total} frames{took}." if head else ""
    line = f"{done}/{total} frames" + (", encoding the MP4" if p.get("phase") == "encoding" else "")
    line += f", {_duration(time.time() - row.get('updated', time.time()))} into this attempt"
    rendered, spent = p.get("rendered", 0), p.get("elapsed_seconds")
    if rendered and spent and total > done:
        line += f", about {_duration(spent / rendered * (total - done))} left"
    if head:
        return (f"\n{head}still {row['state']}: {line}. Wait again only if you need its output this turn; "
                f"otherwise finish the turn: it goes on without you.")
    return (f"\nStill {row['state']}: {line}. To wait for it, call status with wait=<seconds> (up to "
            f"{WAIT_CAP}) rather than polling.")


def _wait_seconds(wait) -> float:
    try:
        seconds = 0.0 if wait is None else float("nan") if isinstance(wait, bool) else float(wait)
    except (TypeError, ValueError):
        seconds = float("nan")
    if not seconds >= 0:
        raise ToolError(f"wait must be a number of seconds, up to {WAIT_CAP}.")
    return min(seconds, WAIT_CAP)


def _status(row: dict, preview: bool, waited: float | None = None) -> ToolResult:
    image = None
    if preview:
        from . import attachments, files, scratch
        source = preview_path(row)
        if source:
            # Existing helper bounds the image the model sees.
            image = scratch.folder() / f"job-{row['id']}-{jobs.digest(source)[:12]}.png"
            attachments._to_png(source, image, files.IMAGE_SIDE)
    return ToolResult(json.dumps(_summary(row)) + "\nAny preview is from this job's SAVED revision, not the live scene."
                      + progress_note(row, waited), image_path=image, file_image=True)


def render_job_status(job_id: str | None = None, preview: bool = False, wait: int = 0) -> ToolResult:
    try:
        seconds = _wait_seconds(wait)
        if job_id is None:
            if seconds:
                raise ToolError("wait needs job_id.")
            return ToolResult(json.dumps([_summary(row) for row in jobs.list_jobs(checkpoints.data_root())[:30]]))
        path = _path(job_id)  # On this thread: finding the data folder may call bpy.
        row = jobs.status(path)
        if seconds and row["state"] in WAITING:
            # Not here: this is the main thread. The agent thread waits, then reads the job again.
            return ToolResult(json.dumps(_summary(row)), then=functools.partial(wait_status, path, preview, seconds))
        return _status(row, preview)
    except (jobs.JobError, OSError, ValueError) as ex:
        raise ToolError(str(ex)) from ex


def wait_status(path: Path, preview: bool, seconds: float, is_cancelled, on_main) -> ToolResult:
    """The agent thread, between main-thread calls, so Blender and its UI stay free: reads the job's
    state file every WAIT_READ seconds until the job is no longer queued or running (finished,
    failed, cancelled, interrupted), `seconds` pass, or the turn is stopped. A stop ends the wait,
    never the job. Then the status is read on the main thread as without wait (a preview needs
    Blender), saying how long it waited."""
    from . import llm
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            if jobs.current(path)["state"] not in WAITING:
                break
        except (jobs.JobError, OSError, ValueError):
            break  # The read below reports it.
        until = min(time.monotonic() + WAIT_READ, started + seconds)
        while time.monotonic() < until:
            if is_cancelled():
                raise llm.Cancelled()
            time.sleep(WAIT_TICK)
    waited = time.monotonic() - started

    def read():
        try:
            return _status(jobs.status(path), preview, waited)
        except (jobs.JobError, OSError, ValueError) as ex:
            return ToolResult(str(ex), ok=False)

    return on_main(read)


def cancel_render_job(job_id: str) -> ToolResult:
    try:
        jobs.cancel(_path(job_id))
        return render_job_status(job_id)
    except (jobs.JobError, OSError, ValueError) as ex:
        raise ToolError(str(ex)) from ex


def resume_render_job(job_id: str) -> ToolResult:
    try:
        return ToolResult(json.dumps(_summary(jobs.launch(_path(job_id)))) + STARTED_NOTE)
    except (jobs.JobError, OSError, ValueError) as ex:
        raise ToolError(str(ex)) from ex


ACTIONS = {"start": start_render_job, "status": render_job_status, "cancel": cancel_render_job,
           "resume": resume_render_job}


def render_job(action: str = "", **arguments) -> ToolResult:
    if not isinstance(action, str) or action not in ACTIONS:
        raise ToolError(f"action must be one of {', '.join(ACTIONS)}")
    return tools.forward(ACTIONS[action], arguments, f"render_job {action}")


DISPATCH = {"render_job": render_job}


def describe(name: str, arguments: dict):
    if name != "render_job":
        return None
    action, settings = arguments.get("action"), {k: v for k, v in arguments.items() if k != "action"}
    if action == "start":
        frames, start, end = arguments.get("frames"), arguments.get("start"), arguments.get("end")
        if frames is not None:
            count = f"{len(frames) if isinstance(frames, list) else '?'} frames"
        elif start is None and end is None:
            count = "the scene's frames"
        else:
            count = f"frames {start if start is not None else 'start'}-{end if end is not None else 'end'}"
        return (f"Background {arguments.get('format', 'PNG')} render · {count} · "
                f"{arguments.get('budget_seconds', '?')}s budget", json.dumps(settings, indent=2))
    if action == "resume":
        return "Resume saved render job (same settings and time budget)", json.dumps(settings, indent=2)
    if action == "cancel":
        return f"Cancel render job {arguments.get('job_id', '?')}", ""
    if action == "status":
        try:
            wait = _wait_seconds(arguments.get("wait"))
        except ToolError:
            wait = 0
        waits = f" · waiting up to {wait:g}s" if wait else ""
        return f"Render job {arguments['job_id']}{waits}" if arguments.get("job_id") else "Render jobs", ""
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
