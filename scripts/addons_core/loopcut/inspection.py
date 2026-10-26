"""Bounded, camera/frame-aware visual evidence from a saved scene, with stale-result rejection.

Saving and freshness checks run on Blender's main thread. Waiting runs on the agent thread;
rendering and contact-sheet composition run in a supervised child process.
"""

import json
import time
import uuid
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from . import checkpoints, cost_guard, job_tools, jobs
from .tools import ToolError, ToolResult

PURPOSES = {"geometry", "materials", "final_lighting"}
MAX_TILES = 12
MAX_FRAMES = 5
STALE = ("The inspection went stale: the scene changed while it rendered (or Blender restarted), so no image is "
         "attached. Inspect the current scene again.")
RETRIED_NOTE = "\nThe first try went stale as the scene changed while it rendered; this is a second try."
PAUSED_NOTE = ("\nThe animation was playing, which changes the scene every frame, so playback was paused while "
               "this rendered; it plays again after this step.")
_paused = None  # What pause_playback paused for a look, until resume_paused: where, its frame, _playback_events.
_playing = {}  # Where playback last started (animation_playback_pre): its window, its scene, a sequencer's area.
_playback_events = 0  # Each start and stop of playback, the user's or Loopcut's.
_epoch = uuid.uuid4().hex
_generation = 0

SCHEMAS = [{"type": "function", "function": {
    "name": "inspect_scene",
    "title": "Checking the scene",
    "description": "Inspect named cameras and frames in one annotated contact sheet (max 12 views), each "
    "view's exposure and colour in numbers. geometry uses Workbench; materials an EEVEE studio-light rig; "
    "final_lighting the scene's own engine and lighting, capped in resolution and samples. Renders a "
    "background copy; changes nothing. A scene edit while rendering rejects the stale image. Focus names "
    "are labeled; without a camera, a temporary camera frames them.",
    "parameters": {"type": "object", "properties": {
        "purpose": {"type": "string", "enum": sorted(PURPOSES)},
        "cameras": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4},
        "frames": {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 5,
                   "description": "Defaults to the current frame. For animation inspect first/middle/last or meaningful phases."},
        "focus": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        "max_side": {"type": "integer", "minimum": 128, "maximum": 640, "description": "Per view; default 384"},
        "samples": {"type": "integer", "minimum": 1, "maximum": 64, "description": "Default 16"},
        "budget_seconds": {"type": "integer", "minimum": 5, "maximum": 180, "description": "Default 90"},
        "view_layer": {"type": "string"},
    }, "required": ["purpose"]}}}]


def _changed(*args):
    global _generation
    if len(args) > 1 and hasattr(args[1], "updates") and not any(args[1].updates):
        return  # An explicit view-layer update with no changed IDs is not a new revision.
    _generation += 1


_changed._bpy_persistent = None


def _playback_started(scene, *args):
    """animation_playback_pre: where it plays. The handler runs in the context of the window (and the
    area: a sequencer plays its own scene) where playback started; Python can't see a screen's timer."""
    global _playback_events
    import bpy
    _playback_events += 1
    area = bpy.context.area
    _playing.clear()
    _playing.update(window=bpy.context.window.as_pointer() if bpy.context.window else None, scene=scene.as_pointer(),
                    area=area.as_pointer() if area is not None and area.type == "SEQUENCE_EDITOR" else None)


def _playback_stopped(*args):
    global _playback_events
    _playback_events += 1


_playback_started._bpy_persistent = _playback_stopped._bpy_persistent = None
HANDLERS = [(name, _changed) for name in ("depsgraph_update_post", "frame_change_post", "load_post", "undo_post",
                                         "redo_post")] + [("animation_playback_pre", _playback_started),
                                                          ("animation_playback_post", _playback_stopped)]


def register():
    import bpy
    for name, handler in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler not in handlers:
            handlers.append(handler)


def unregister():
    import bpy
    for name, handler in HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler in handlers:
            handlers.remove(handler)


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
    if not isinstance(frames, list):
        raise jobs.JobError(f"Choose 1 to {MAX_FRAMES} frames.")
    for frame in frames:
        jobs.integer(frame, "frame", -1048574, 1048574)
    if len(set(frames)) != len(frames):
        raise jobs.JobError("Frames must be unique.")
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise jobs.JobError(f"Choose 1 to {MAX_FRAMES} frames.")
    if max(1, len(cameras)) * len(frames) > MAX_TILES:
        raise jobs.JobError("At most 12 camera/frame combinations per inspection.")


def frame_limit(cameras) -> int:
    """The frames one inspection takes with these cameras: MAX_FRAMES, fewer where that many views
    would be more than MAX_TILES (3 cameras: 4 frames; 4 cameras: 3)."""
    return max(1, min(MAX_FRAMES, MAX_TILES // max(1, len(cameras) if isinstance(cameras, list) else 1)))


def pick_frames(frames, cameras=()) -> tuple[list, list]:
    """(the frames to inspect, the ones left out): frames as asked, or, when more unique whole frame
    numbers are asked than frame_limit(cameras), that many spread over them, the first and the last
    always among them. A walk's [1, 4, 7, 10, 13, 238] failed red as too many (GPT-6 Luna, auto-rig
    round 7); its first five would have left out the end. Anything else is as asked, for
    validate_request to name."""
    limit = frame_limit(cameras)
    if not isinstance(frames, list) or len(frames) <= limit:
        return frames, []
    for frame in frames:
        jobs.integer(frame, "frame", -1048574, 1048574)
    if len(set(frames)) != len(frames):
        return frames, []
    ordered = sorted(frames)
    kept = [ordered[round(i * (len(ordered) - 1) / max(1, limit - 1))] for i in range(limit)]
    return kept, [frame for frame in ordered if frame not in kept]


def frames_note(row: dict) -> str:
    """For an inspection's result: the frames asked that it left out, if any (pick_frames)."""
    left_out = row.get("frames_left_out") or []
    if not left_out:
        return ""
    kept, cameras = len(row["frames"]), len(row.get("cameras") or [])
    return (f"\nInspected {kept} of the {kept + len(left_out)} frames asked (at most {kept} at a time"
            + (f" with {cameras} cameras" if cameras > 1 else "") + "), spread from first to last; left out: "
            + ", ".join(map(str, left_out)) + ".")


def inspect_scene(purpose: str, cameras=None, frames=None, focus=None, max_side=384, samples=16,
                  budget_seconds=90, view_layer="") -> ToolResult:
    """view_layer: the one rendered and measured in (view_layers.production: else the active one
    when it renders, else the first that renders). Its name goes to the job, whose own Blender has
    no idea which layer the user had active."""
    import bpy
    from . import view_layers
    scene = bpy.context.scene
    cameras = ([scene.camera.name] if scene.camera else []) if cameras is None else cameras
    frames = [scene.frame_current] if frames is None else frames
    focus = [] if focus is None else focus
    # What retry asks again: resolved now, so a retry inspects the same frame however playback moved on.
    request = dict(purpose=purpose, cameras=cameras, frames=frames, focus=focus, max_side=max_side, samples=samples,
                   budget_seconds=budget_seconds, view_layer=view_layer)
    try:
        frames, left_out = pick_frames(frames, cameras)
        validate_request(purpose, cameras, frames, focus, max_side, samples, budget_seconds)
        refusal = cost_guard.render_refusal()  # The worker renders at render levels, whatever the engine.
        if refusal:
            raise ToolError(refusal)
        for name in cameras:
            if name not in scene.objects or scene.objects[name].type != "CAMERA":
                raise jobs.JobError(f"Camera {name!r} is not in this scene.")
        for name in focus:
            if name not in scene.objects:
                raise jobs.JobError(f"Focus object {name!r} is not in this scene.")
        if not isinstance(view_layer, str):
            raise jobs.JobError("view_layer is a view layer's name.")
        layer = view_layers.production(scene, view_layer)
        register()
        # Before the copy: each frame played is a scene change, so a look during playback went stale.
        paused_now = pause_playback()
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
        spec.update(kind="inspection", purpose=purpose, cameras=cameras, focus=focus, view_layer=layer.name,
                    view_layer_asked=bool(view_layer),
                    camera=cameras[0] if cameras else "Automatic focus view",
                    engine=engine,
                    source_engine=scene.render.engine, output=str(path / "output"), frames_left_out=left_out,
                    request=request, paused_playback=paused_now,
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
    return _finish(path)[0]


def _finish(path: Path) -> tuple[ToolResult, bool]:
    """finish's result, and whether it went stale. The job id is for the model only (model_note): a
    red card with "Inspection d404909579fc… is stale" told the user nothing (auto-rig round 7)."""
    try:
        row = jobs.status(path)
        job = f"Inspection job {row['id']}."
        if row["state"] != "complete":
            return ToolResult(f"The inspection {row['state']}: {row.get('error', '')}", ok=False, model_note=job), False
        if row.get("live_token") != live_token():
            return ToolResult(STALE, ok=False, model_note=job), True
        sheet = Path(row["output"]) / "contact_sheet.png"
        metadata = jobs.read_json(Path(row["output"]) / "inspection.json")
        if jobs.digest(sheet) != metadata["sheet_sha256"] or metadata["scene_revision"] != row["scene_revision"]:
            raise jobs.JobError("Inspection evidence changed after verification.")
        brief = {key: value for key, value in metadata.items() if key not in {"sheet_sha256", "views"}}
        brief["views"] = [{key: view[key] for key in ("camera", "frame", "focus", "signal")} for view in metadata["views"]]
        return ToolResult("Image attached: scene inspection from saved revision " + row["scene_revision"][:12] +
                          ".\n" + json.dumps(brief) + frames_note(row) + job_tools.missing_note(row)
                          + (PAUSED_NOTE if row.get("paused_playback") else ""),
                          image_path=sheet, evidence_token=row["live_token"], model_note=_unseen_bones()), False
    except (jobs.JobError, OSError, ValueError) as ex:
        return ToolResult(f"Inspection unavailable: {ex}", ok=False), False


def retry(path: Path) -> ToolResult:
    """Main thread: the request of the inspection at path again, from a new copy of the scene as it is
    now. Nothing of the scene changes, so this is where a stale look is tried again."""
    try:
        return inspect_scene(**jobs.spec_at(path)["request"])
    except (ToolError, jobs.JobError, OSError, ValueError, KeyError) as ex:
        return ToolResult(f"Inspection unavailable: {ex}", ok=False)


def playing() -> bool:
    """Whether an animation plays on its timer. Screen.is_animation_playing is Blender-wide and also true
    while someone drags the playhead (ED_screen_animation_playing: a timer or a scrub)."""
    import bpy
    windows = list(bpy.context.window_manager.windows)
    return bool(windows) and windows[0].screen.is_animation_playing and not any(w.screen.is_scrubbing for w in windows)


def _window(pointer):
    import bpy
    return next((w for w in bpy.context.window_manager.windows if w.as_pointer() == pointer), None)


def _scene(pointer):
    import bpy
    return next((s for s in bpy.data.scenes if s.as_pointer() == pointer), None)


def pause_playback() -> bool:
    """Main thread: Blender's own Pause, at the frame it is on, for an animation playing on its timer
    (not a scrub), until resume_paused; where it played is _playing's (the first window when playback
    began before Loopcut's handler). Whether it paused. Never raises: a look goes on without it."""
    global _paused
    import bpy
    try:
        if _paused is not None or not playing():
            return False
        window = _window(_playing.get("window")) or bpy.context.window_manager.windows[0]
        with bpy.context.temp_override(window=window, screen=window.screen):
            bpy.ops.screen.animation_cancel(restore_frame=False)
        scene = _scene(_playing.get("scene")) or window.scene
        _paused = {"window": window.as_pointer(), "scene": scene.as_pointer(), "area": _playing.get("area"),
                   "frame": scene.frame_current, "events": _playback_events}
        return True
    except Exception as ex:  # An operator refused: the look goes on, and may go stale.
        print(f"Loopcut: could not pause playback for a look: {ex!r}")
        return False


def paused() -> bool:
    return _paused is not None


def resume_paused() -> None:
    """Main thread: play again what pause_playback paused, from its frame, in the window it played in
    (and the sequencer, which plays its own scene), forward: Blender doesn't say whether it played in
    reverse. Not when the user took over meanwhile: started or stopped playback (_playback_events),
    scrubbed or moved the frame, or gave the window another scene. The agent calls this once a step
    batch's looks were checked against the scene (agent._fresh_images), and when its turn ends:
    playing before the check would make the look stale after all. Never raises."""
    global _paused
    import bpy
    pause, _paused = _paused, None
    try:
        window, scene = (_window(pause["window"]), _scene(pause["scene"])) if pause else (None, None)
        if window is None or scene is None or _playback_events != pause["events"] or \
                scene.frame_current != pause["frame"] or window.screen.is_animation_playing:
            return
        area = next((a for a in window.screen.areas if a.as_pointer() == pause["area"]), None)
        if pause["area"] is not None and area is None:
            return  # The sequencer it played in is gone.
        if pause["area"] is None and window.scene != scene:
            return
        with bpy.context.temp_override(window=window, screen=window.screen, **({"area": area} if area else {})):
            bpy.ops.screen.animation_play()
    except Exception as ex:  # The user can press play.
        print(f"Loopcut: could not resume playback: {ex!r}")


def _on_main(run_on_main, fn, is_cancelled):
    from . import llm
    future = run_on_main(fn)
    while True:
        try:
            return future.result(timeout=.1)
        except FutureTimeout:
            if future.done():
                return future.result()
            if is_cancelled() and future.cancel():
                raise llm.Cancelled()


def _waited(path: Path, is_cancelled, run_on_main) -> tuple[ToolResult, bool]:
    from . import llm
    while True:
        if is_cancelled():
            jobs.cancel(path)
            raise llm.Cancelled()
        row = jobs.status(path)
        if row["state"] not in jobs.ACTIVE:
            return _on_main(run_on_main, lambda: _finish(path) if not is_cancelled() else
                            (ToolResult("Inspection cancelled.", ok=False), False), is_cancelled)
        time.sleep(.1)


def wait_result(path: Path, is_cancelled, run_on_main) -> ToolResult:
    """Agent thread: the UI pump remains free while native rendering runs elsewhere. A look that went
    stale is tried once more from a new copy of the scene (and with playback paused, if someone pressed
    play while the first rendered: inspect_scene pauses it). During auto-rig round 7's walk every look
    went stale, a red card the user could do nothing with."""
    result, stale = _waited(path, is_cancelled, run_on_main)
    if not stale:
        return result
    again = _on_main(run_on_main, lambda: retry(path), is_cancelled)
    if again.pending_job is None:
        return again
    result, stale = _waited(again.pending_job, is_cancelled, run_on_main)
    if not stale:
        result.text += RETRIED_NOTE
    return result


def describe(arguments):
    return (f"Inspect {arguments.get('purpose', '?')} · {arguments.get('budget_seconds', 90)}s render budget",
            json.dumps(arguments, indent=2))


DISPATCH = {"inspect_scene": inspect_scene}


def _unseen_bones() -> str:
    """R7-D1: a rendered inspection never shows a Loopcut rig's bones, which the user's viewport draws."""
    from . import autorig
    return autorig.unseen_bones()
