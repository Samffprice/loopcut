"""Video reference v0, on Blender's main thread: a clip the user attaches becomes timed contact sheets
the model sees like attached images, plus beats.json and, when the tracker manages, camera_motion.json.

1. probe: Blender reads the clip's size, frame rate and length (a movie clip loaded and removed).
2. start: the trimmed part goes to video_worker.py in a background Blender (the render jobs' command
   line: --background --factory-startup --disable-autoexec), so the panel never waits on decoding,
   tracking or drawing. Its sheets are reserved in the message's image budget meanwhile.
3. _poll (a bpy.app.timers timer) picks the result up, checks it, stores the sheets as conversation
   images and adds them to the next message's attachments, each carrying the clip's record under
   "video" (persisted with the references, so the tools find the clip after a restart).

Local only: the clip is read where it is and never copied or uploaded; its frames and sheets live in
the conversation's folder (video/<id>/), like attached images. Only the sheets go to the model.

Tools (offered only while the conversation has a clip; with_clip_tools): clip_background, and a
`frame` argument on compare_with_reference and look_at_reference (tools.py calls frame_for).
"""

import json
import os
import subprocess
import time
import uuid
from copy import deepcopy
from pathlib import Path

import bpy

from . import attachments, conversations, state, videoref
from .tools import ToolError, ToolResult
from .videoref import ClipError

WORKER = Path(__file__).with_name("video_worker.py")
WORKER_SECONDS = 300   # Extraction, beats and sheets take seconds; a long track and solve can take minutes.
POLL_SECONDS = 0.5
_running: dict[str, dict] = {}  # Clip id -> the worker preparing it; never saved.

FRAME_PARAMETER = {"type": "integer", "description": "For an attached clip: a clip frame (as in beats.json); the "
                                                     "scene is captured at the scene frame that shows it"}
LOOK_FRAME_PARAMETER = {"type": "integer", "description": "For an attached clip: a clip frame (as in beats.json)"}
SCHEMAS = [{"type": "function", "function": {  # Offered only while a clip is attached: see with_clip_tools.
    "name": "clip_background",
    "title": "Adding a video reference",
    "description": (
        "Show an attached clip in a camera's view as a movie-clip background image, at the clip's frame rate "
        "(the scene's frame rate is set to it), the clip's first used frame at start_frame. Returns which "
        "scene frames show which clip frames."),
    "parameters": {"type": "object", "properties": {
        "name": {"type": "string", "description": "The clip's file name"},
        "camera": {"type": "string", "description": "Default the scene camera"},
        "start_frame": {"type": "integer", "description": "Scene frame showing the first used clip frame. "
                                                          "Default the scene's start frame"},
        "opacity": {"type": "number", "description": "0..1, default 0.5"},
        "depth": {"type": "string", "enum": ["front", "back"], "description": "front (default) over the scene, "
                                                                              "back behind it"},
        "remove": {"type": "boolean", "description": "Take this clip off the camera"},
    }, "required": ["name"]},
}}]


# ------------------------------------------------------------------ attaching

def probe(path: Path) -> dict:
    """Size, frame rate and frame count, as Blender reads them. Leaves no datablock behind."""
    attachments.check_clip(path)
    try:
        clip = bpy.data.movieclips.load(str(path), check_existing=False)
    except RuntimeError as ex:
        raise ClipError(f"{path.name} is not a video Blender can read.") from ex
    try:
        info = {"fps": float(clip.fps), "frames": int(clip.frame_duration), "size": [int(v) for v in clip.size]}
    finally:
        bpy.data.movieclips.remove(clip)
    if info["fps"] <= 0 or info["frames"] < 1 or 0 in info["size"]:
        raise ClipError(f"{path.name} has no frames or frame rate Blender can read.")
    return info


def worker_command(folder: Path) -> list[str]:
    return [bpy.app.binary_path, "--background", "--factory-startup", "--disable-autoexec",
            "--python-exit-code", "1", "--python", str(WORKER), "--", str(folder)]


def start(session: dict, path: Path, start_seconds: float, end_seconds: float | None, watch: str = "",
          solve: bool = True) -> str:
    """Begin preparing `path` trimmed to start..end seconds; returns the clip id."""
    path = Path(path)
    info = probe(path)
    first, last = videoref.trim(info["frames"], info["fps"], start_seconds, end_seconds)
    room = attachments.room(session)
    if room < 1:
        raise ClipError(f"{attachments.MAX_PER_MESSAGE} images are already attached; send them or remove one.")
    clip_id = uuid.uuid4().hex[:16]
    folder = conversations._folder(session["id"]) / "video" / clip_id
    folder.mkdir(parents=True)
    spec = {"id": clip_id, "name": path.name, "source": str(path.resolve()), **info, "in_frame": first,
            "out_frame": last, "watch": " ".join(str(watch).split())[:300], "solve": bool(solve),
            "max_sheets": min(videoref.MAX_IMAGES, room)}
    (folder / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPCUT_")}
    with (folder / "worker.log").open("ab") as log:
        process = subprocess.Popen(worker_command(folder), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=os.name != "nt", env=env)
    session["video_reserved"] = session.get("video_reserved", 0) + spec["max_sheets"]
    _running[clip_id] = {"process": process, "session": session, "folder": folder, "name": path.name,
                         "reserved": spec["max_sheets"], "started": time.monotonic()}
    fps = info["fps"]
    session["items"].append(state.item_notice(
        f"Preparing {path.name}, {videoref.time_of(first, fps):.2f}-{videoref.time_of(last, fps):.2f} s: frames, "
        f"beats{' and a camera solve' if solve else ''}. It stays on this computer."))
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=POLL_SECONDS)
    return clip_id


def preparing(session: dict) -> list[str]:
    """Names of the clips this conversation is still preparing."""
    return [job["name"] for job in _running.values() if job["session"] is session]


def _poll():
    for clip_id, job in list(_running.items()):
        code = job["process"].poll()
        late = code is None and time.monotonic() - job["started"] > WORKER_SECONDS
        if code is None and not late:
            continue
        if late:
            job["process"].kill()  # This worker's own process, by its handle.
            job["process"].wait()
        del _running[clip_id]
        session = job["session"]
        session["video_reserved"] = max(0, session.get("video_reserved", 0) - job["reserved"])
        try:
            if late:
                raise ClipError(f"preparing took longer than {WORKER_SECONDS} s")
            session["items"].append(state.item_notice(finish(session, job["folder"])))
        except (ClipError, OSError, ValueError, KeyError, TypeError) as ex:
            session["items"].append(state.item_error(f"Could not prepare {job['name']}: {ex}"))
        try:
            conversations.save(session)
        except OSError:
            pass
        _redraw()
    return POLL_SECONDS if _running else None


def _redraw() -> None:
    try:
        from .ui import host
        host.tag_redraw_all()
    except (ImportError, AttributeError, RuntimeError):
        pass


def finish(session: dict, folder: Path) -> str:
    """Main thread: the worker's result, checked, into the next message's attachments. Returns the
    panel's line about it."""
    spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    if result.get("error"):
        raise ClipError(result["error"])
    beats = videoref.check_beats(json.loads((folder / "beats.json").read_text(encoding="utf-8")))
    camera = result.get("camera") or {"asked": False}
    if camera.get("solved"):
        videoref.check_camera_motion(json.loads((folder / "camera_motion.json").read_text(encoding="utf-8")))
    sheets = result.get("sheets") or []
    if not sheets or len(sheets) > spec["max_sheets"]:
        raise ClipError("the worker made no usable sheets")
    record = {"id": spec["id"], "name": spec["name"], "source": spec["source"], "folder": str(folder),
              "fps": spec["fps"], "frames": spec["frames"], "size": spec["size"], "in_frame": spec["in_frame"],
              "out_frame": spec["out_frame"], "watch": spec["watch"],
              "beats_line": videoref.beats_line(beats["beats"]),
              "camera": {k: camera[k] for k in ("asked", "solved", "mode", "error_px", "frames", "reason")
                         if k in camera}}
    added = []
    for sheet in sheets:
        image = folder / Path(sheet["file"]).name  # Only the clip's own folder.
        ref = conversations.store_image(session, image)
        name = videoref.sheet_name(spec["name"], sheet["time"])
        session["attachments"].append({"ref": ref, "full": ref, "name": name,
                                       "video": {**record, "range": sheet["range"]}})
        added.append(name)
    solve = ""
    if camera.get("solved"):
        solve = f"; camera solved, error {camera['error_px']:.2f} px"
    elif camera.get("asked"):
        solve = f"; camera not used: {camera.get('reason') or 'no solve'}"
    return (f"{spec['name']} is ready: {len(added)} sheet{'s' * (len(added) != 1)}, "
            f"{len(beats['beats'])} beat{'s' * (len(beats['beats']) != 1)}{solve}.")


# ------------------------------------------------------------------ tools

# What with_clip_tools offers or extends; the workspace agent (agent._workspace_schemas) gets them too.
CLIP_TOOLS = ("clip_background", "compare_with_reference", "look_at_reference")


def has_clip(session: dict) -> bool:
    return any(r.get("video") for r in session.get("references") or [])


def with_clip_tools(schemas: list[dict]) -> list[dict]:
    """The tool list, with clip_background and `frame` on the reference tools while this conversation
    has a clip. Without one nothing changes: every word of a schema is paid for on every step."""
    if not has_clip(state.session()):
        return schemas
    out = []
    for schema in schemas:
        name = schema["function"]["name"]
        if name in ("compare_with_reference", "look_at_reference"):
            schema = deepcopy(schema)
            properties = schema["function"]["parameters"]["properties"]
            properties["name"]["description"] = "File name of the attached image or clip"
            properties["frame"] = FRAME_PARAMETER if name == "compare_with_reference" else LOOK_FRAME_PARAMETER
        out.append(schema)
    return out + SCHEMAS


def _record(found: dict) -> dict:
    record = found.get("video")
    if not isinstance(record, dict):
        raise ToolError(f"{found['name']} is an image; frame and clip_background are for an attached clip.")
    return record


def _clip_folder(record: dict) -> Path:
    """The clip's folder, only inside this conversation's own video folder (the record was read from disk)."""
    root = (conversations._folder(state.session()["id"]) / "video").resolve()
    folder = Path(str(record.get("folder", ""))).resolve()
    if folder.parent != root:
        raise ToolError("This clip's frames are not in this conversation's folder.")
    return folder


def _loaded_clip(source: str):
    wanted = os.path.normcase(os.path.abspath(source))
    for clip in bpy.data.movieclips:
        if os.path.normcase(os.path.abspath(bpy.path.abspath(clip.filepath))) == wanted:
            return clip
    return None


def scene_frame(record: dict, frame: int) -> int:
    """The scene frame showing clip frame `frame`: per the clip_background placement when there is one,
    else the scene's start frame showing the first used frame."""
    clip = _loaded_clip(record["source"])
    start = clip.frame_start if clip else bpy.context.scene.frame_start - record["in_frame"] + 1
    return start + frame - 1


def frame_for(found: dict, frame) -> tuple[Path, int, str]:
    """(stored frame image, scene frame showing it, what it is) for clip frame `frame` of a clip reference."""
    record = _record(found)
    if isinstance(frame, bool) or not isinstance(frame, (int, float)) or int(frame) != frame:
        raise ToolError("frame is a clip frame number, as in beats.json.")
    frame = int(frame)
    if not record["in_frame"] <= frame <= record["out_frame"]:
        raise ToolError(f"Clip frame {frame} is outside the part attached: {record['in_frame']}-{record['out_frame']}.")
    path = _clip_folder(record) / "frames" / videoref.frame_file(frame)
    if not path.is_file():
        raise ToolError(f"Clip frame {frame} is no longer on disk.")
    what = f"{record['name']} clip frame {frame} ({videoref.time_of(frame, record['fps']):.2f} s)"
    return path, scene_frame(record, frame), what


def clip_background(name: str, camera: str = "", start_frame: int | None = None, opacity: float = 0.5,
                    depth: str = "front", remove: bool = False) -> ToolResult:
    from . import tools
    found, _ = tools._reference(name)
    record = _record(found)
    scene = bpy.context.scene
    target = scene.objects.get(camera) if camera else scene.camera
    if target is None or target.type != "CAMERA":
        raise ToolError(f"No camera {camera!r} in this scene." if camera else
                        "The scene has no camera; name one, or make one first (frame_camera with create).")
    clip = _loaded_clip(record["source"])
    backgrounds = target.data.background_images
    if str(remove).lower() == "true":
        gone = [b for b in backgrounds if clip is not None and b.source == "MOVIE_CLIP" and b.clip == clip]
        for background in gone:
            backgrounds.remove(background)
        return ToolResult(f"Removed {len(gone)} background{'s' * (len(gone) != 1)} of {record['name']} "
                          f"from {target.name}.")
    if depth not in ("front", "back"):
        raise ToolError("depth is front or back.")
    try:
        opacity = min(1.0, max(0.0, float(opacity)))
        start = scene.frame_start if start_frame is None else int(start_frame)
    except (TypeError, ValueError) as ex:
        raise ToolError("opacity is a number from 0 to 1 and start_frame a frame number.") from ex
    if clip is None:
        if not Path(record["source"]).is_file():
            raise ToolError(f"The clip is no longer at {record['source']}.")
        try:
            clip = bpy.data.movieclips.load(record["source"], check_existing=True)
        except RuntimeError as ex:
            raise ToolError(f"Blender could not open {record['source']}: {ex}") from ex
    clip.frame_start = start - record["in_frame"] + 1  # Clip frame f shows at scene frame frame_start + f - 1.
    background = next((b for b in backgrounds if b.source == "MOVIE_CLIP" and b.clip == clip), None) \
        or backgrounds.new()
    background.source = "MOVIE_CLIP"
    background.clip = clip
    background.use_camera_clip = False
    background.alpha = opacity
    background.display_depth = depth.upper()
    background.frame_method = "FIT"
    background.show_background_image = True
    target.data.show_background_images = True
    render = scene.render
    before = render.fps / render.fps_base
    render.fps, render.fps_base = videoref.fps_pair(record["fps"])
    first, last = record["in_frame"], record["out_frame"]
    fps = record["fps"]
    lines = [f"{record['name']} is the background of {target.name} ({depth}, opacity {opacity:g}): clip frames "
             f"{first}-{last} ({videoref.time_of(first, fps):.2f}-{videoref.time_of(last, fps):.2f} s) show at "
             f"scene frames {start}-{start + last - first}.",
             f"Scene frame rate {before:.3f} -> {fps:.3f} fps." if abs(before - fps) > 1e-3
             else f"Scene frame rate already {fps:.3f} fps."]
    width, height = record["size"]
    shape = render.resolution_x / max(1, render.resolution_y)
    if abs(shape - width / height) > 0.01:
        lines.append(f"The render is {render.resolution_x}x{render.resolution_y} and the clip {width}x{height}: "
                     "the clip is fitted inside the camera frame.")
    return ToolResult(" ".join(lines))


DISPATCH = {"clip_background": clip_background}
