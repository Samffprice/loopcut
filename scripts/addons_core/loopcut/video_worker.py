"""Prepare an attached clip in a background Blender: frames, beats, an optional camera solve, sheets.

    blender --background --factory-startup --disable-autoexec --python video_worker.py -- <clip folder>

Started by video_attach.start, never registered as an add-on module. Reads <folder>/spec.json and
writes, in that folder: frames/<clip frame>.jpg (every used clip frame), beats.json, camera_motion.json
when a solve passes, sheet_<n>.jpg, and result.json last ({"error": ...} on failure).

Pixels: Blender has no ffmpeg command and a movie clip exposes no pixels, but the sequencer decodes
movies when rendering, in background mode too (job_worker.encode_movie checks its own MP4 that way).
So a scratch scene with the clip as a movie strip renders the used frames to JPEG, with the Standard
view transform and no dither so the pixels are the clip's; imbuf reads them back.

Camera solve: Blender's tracker (detect_features, track_markers, solve_camera) needs a Movie Clip
editor; the startup file's window is borrowed and its largest area switched to one. Any failure
there drops the solve with its reason; frames, beats and sheets do not depend on it.
"""

import json
import os
import sys
import time
import traceback
from pathlib import Path

import blf
import bpy
import imbuf
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import jobs  # noqa: E402
import videoref  # noqa: E402

BACKGROUND = (20, 23, 29, 255)
PEAK_COLOR = (235, 140, 45, 255)
HOLD_COLOR = (70, 170, 205, 255)


def frame_size(size, side: int) -> tuple[int, int]:
    """The clip's shape at `side` pixels on its long side (never larger than the clip), even sizes."""
    width, height = size
    scale = min(1.0, side / max(width, height))
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def extract(spec: dict, folder: Path) -> Path:
    frames = folder / "frames"
    frames.mkdir(exist_ok=True)
    scene = bpy.data.scenes.new("Loopcut clip frames")
    r = scene.render
    r.fps, r.fps_base = videoref.fps_pair(spec["fps"])  # One scene frame per clip frame.
    r.resolution_x, r.resolution_y = frame_size(spec["size"], videoref.FRAME_SIDE)
    r.resolution_percentage = 100
    r.use_sequencer, r.use_compositing = True, False
    r.dither_intensity = 0.0  # Dither noise would read as motion.
    r.image_settings.media_type = "IMAGE"
    r.image_settings.file_format, r.image_settings.color_mode = "JPEG", "RGB"
    r.image_settings.quality = 90
    r.use_file_extension = True
    r.filepath = str(frames / "######")  # videoref.frame_file
    scene.view_settings.view_transform, scene.view_settings.look = "Standard", "None"
    strips = scene.sequence_editor_create().strips
    try:
        strips.new_movie("clip", spec["source"], channel=1, frame_start=1, fit_method="FIT")
    except TypeError:
        strips.new_movie("clip", spec["source"], channel=1, frame_start=1)
    scene.frame_start, scene.frame_end = spec["in_frame"], spec["out_frame"]
    bpy.ops.render.render(animation=True, scene=scene.name)
    missing = [f for f in range(spec["in_frame"], spec["out_frame"] + 1)
               if not (frames / videoref.frame_file(f)).is_file()]
    if missing:
        raise RuntimeError(f"{len(missing)} clip frames did not decode (first {missing[0]}).")
    return frames


def grey(path: Path, size: tuple[int, int]) -> list[float]:
    image = imbuf.load(str(path))
    try:
        image.resize(size, method="BILINEAR")
        with image.with_buffer() as buffer:
            pixels = np.asarray(buffer)[..., :3].astype(np.float32)
        return (pixels.mean(axis=2) / 255.0).ravel().tolist()
    finally:
        image.free()


def beats(spec: dict, frames_dir: Path, folder: Path) -> list[dict]:
    fps, first, last = spec["fps"], spec["in_frame"], spec["out_frame"]
    analysed = videoref.analysis_frames(first, last, fps)
    size = frame_size(spec["size"], videoref.ANALYSIS_SIDE)
    motion = videoref.measure_motion([grey(frames_dir / videoref.frame_file(f), size) for f in analysed], size[0])
    energy = motion["energy"]
    found = videoref.find_beats(energy, analysed, fps)
    document = videoref.beats_document(spec["name"], fps, first, last, analysed, energy, found,
                                       motion["shift"], size)
    jobs.atomic_json(folder / "beats.json", videoref.check_beats(document))
    return found


def _clip_editor():
    """(window, screen, area, region) of a Movie Clip editor borrowed from the startup file's window."""
    windows = bpy.context.window_manager.windows
    if not len(windows):
        raise RuntimeError("this Blender has no window for the tracker to borrow")
    window = windows[0]
    screen = window.screen
    area = max(screen.areas, key=lambda a: a.width * a.height)
    area.type = "CLIP_EDITOR"
    if area.type != "CLIP_EDITOR":
        raise RuntimeError("could not open a Movie Clip editor")
    region = next(r for r in area.regions if r.type == "WINDOW")
    return window, screen, area, region


def solve(spec: dict, folder: Path) -> dict:
    fps, first, last = spec["fps"], spec["in_frame"], spec["out_frame"]
    total = last - first + 1
    report = {"asked": True, "solved": False}
    clip = bpy.data.movieclips.load(spec["source"], check_existing=False)
    clip.frame_start = 1  # Clip frame f at scene frame f.
    scene = bpy.context.scene
    scene.render.fps, scene.render.fps_base = videoref.fps_pair(fps)
    scene.frame_start, scene.frame_end = first, last
    scene.frame_set(first)
    tracking = clip.tracking
    settings = tracking.settings
    settings.default_pattern_size, settings.default_search_size = 21, 121
    settings.default_motion_model = "Loc"
    settings.use_keyframe_selection = True
    tracking.camera.sensor_width = 36.0  # Focal lengths below are full-frame equivalents.
    window, screen, area, region = _clip_editor()
    space = area.spaces.active
    space.clip = clip
    space.mode = "TRACKING"
    with bpy.context.temp_override(window=window, screen=screen, area=area, region=region, scene=scene):
        bpy.ops.clip.detect_features(placement="FRAME", margin=16, threshold=0.1, min_distance=40)
        report["tracks"] = len(tracking.tracks)
        if report["tracks"] < 8:
            report["reason"] = f"only {report['tracks']} trackable features on the first frame"
            return report
        bpy.ops.clip.select_all(action="SELECT")
        bpy.ops.clip.track_markers(backwards=False, sequence=True)
        # The lens is unknown (a phone's main camera is about 26 mm full-frame equivalent, others are not).
        # Measured on the harness clips (2026-10-16): a full solve from 26 mm can refine to the true 50 mm,
        # or run off to 40 px; deleting the tracks a sound solve cannot explain and solving again on its
        # keyframes takes ~1 px to ~0.2 px; rotation-only footage solves as a tripod at the right lens
        # (0.1 px; 1.7 px at a wrong one). A large subject crossing the tracked background defeats it.
        attempts = []

        def attempt(mode: str, focal: float) -> dict:
            settings.use_tripod_solver = mode == "tripod"
            if hasattr(settings, "refine_intrinsics_focal_length"):
                settings.refine_intrinsics_focal_length = mode != "tripod"
            tracking.camera.focal_length = focal
            try:
                bpy.ops.clip.solve_camera()
                attempts.append(_solved(tracking, first, last, total, mode, focal))
            except RuntimeError as ex:  # "no good keyframes", "at least 8 common tracks"...
                attempts.append({"mode": mode, "guess": focal, "cameras": [], "error": 0.0, "ok": False,
                                 "valid": False, "focal": focal, "reason": str(ex).strip().splitlines()[0]})
            return attempts[-1]

        # Full solves from the likeliest lens out; the first whose errors mean something (under 10x the
        # limit: one from a wrong lens can be 40 px) decides which tracks to delete, and the solve is redone
        # on its keyframes. Rotation only, per lens, when nothing else passes.
        for lens in videoref.FULL_GUESSES:
            full = attempt("full", lens)
            if full["ok"]:
                break
            if full["valid"] and full["error"] < 10 * videoref.MAX_SOLVE_ERROR:
                settings.use_keyframe_selection = False  # Keep the keyframes this solve found.
                bpy.ops.clip.clean_tracks(frames=0, error=2 * videoref.MAX_SOLVE_ERROR, action="DELETE_TRACK")
                if len(tracking.tracks) >= 8:
                    attempt("full", full["focal"] if 8 <= full["focal"] <= 300 else lens)
                settings.use_keyframe_selection = True
                break
        if not any(a["ok"] for a in attempts) and len(tracking.tracks) >= 8:
            for focal in videoref.FOCAL_GUESSES:
                attempt("tripod", focal)
    # A passing full solve (rotation and translation) over a passing tripod one; then the smallest error.
    best = min(attempts, key=lambda a: (not a["ok"], a["mode"] == "tripod", not a["valid"], a["error"]))
    report.update(mode=best["mode"], error_px=round(best["error"], 4), frames=len(best["cameras"]),
                  focal_mm=round(best["focal"], 2), reason=best["reason"],
                  attempts=[{"mode": a["mode"], "lens_mm": a["guess"], "error_px": round(a["error"], 4),
                             "frames": len(a["cameras"])} for a in attempts])
    if best["ok"]:
        document = videoref.camera_motion_document(spec["name"], fps, first, last, best["error"], best["focal"],
                                                   tracking.camera.sensor_width, best["cameras"],
                                                   "tripod" if best["mode"] == "tripod" else "full")
        jobs.atomic_json(folder / "camera_motion.json", videoref.check_camera_motion(document))
        report["solved"] = True
    return report


def _solved(tracking, first: int, last: int, total: int, mode: str, guess: float) -> dict:
    """The solve just made, copied out before the next one replaces it."""
    reconstruction = (tracking.objects.active.reconstruction if hasattr(tracking, "objects")
                      and tracking.objects.active else tracking.reconstruction)
    cameras = sorted(((c.frame, [list(row) for row in c.matrix]) for c in reconstruction.cameras
                      if first <= c.frame <= last), key=lambda c: c[0])
    valid, error = bool(reconstruction.is_valid), float(reconstruction.average_error)
    ok, reason = videoref.accept_solve(valid, error, len(cameras), total)
    return {"mode": mode, "guess": guess, "cameras": cameras, "error": error, "ok": ok, "valid": valid,
            "reason": reason,
            "focal": float(tracking.camera.focal_length)}


def draw_sheet(sheet: dict, frames_dir: Path, target: Path) -> None:
    width, height = sheet["size"]
    canvas = imbuf.new((width, height))
    try:
        with canvas.with_buffer(write=True) as buffer:
            np.asarray(buffer)[:] = BACKGROUND
        for tile in sheet["tiles"]:
            x, y, w, h = tile["box"]
            bottom = height - y - h  # Boxes are from the top-left; image rows from the bottom.
            image = imbuf.load(str(frames_dir / videoref.frame_file(tile["frame"])))
            try:
                image.resize((w, h), method="BILINEAR")
                with image.with_buffer() as source, canvas.with_buffer(write=True) as dest:
                    np.asarray(dest)[bottom:bottom + h, x:x + w] = np.asarray(source)[..., :4]
            finally:
                image.free()
            if tile["beat"]:
                color = PEAK_COLOR if tile["beat"].startswith("PEAK") else HOLD_COLOR
                with canvas.with_buffer(write=True) as buffer:
                    pixels = np.asarray(buffer)
                    pixels[bottom + h - 18:bottom + h, x:x + w] = color  # A band across the tile's top.
                    for rows, cols in ((slice(bottom, bottom + 3), slice(x, x + w)),
                                       (slice(bottom, bottom + h), slice(x, x + 3)),
                                       (slice(bottom, bottom + h), slice(x + w - 3, x + w))):
                        pixels[rows, cols] = color
        with blf.bind_imbuf(0, canvas):
            blf.size(0, 15)
            blf.color(0, 1, 1, 1, 1)
            blf.position(0, videoref.GAP, height - 18, 0)
            blf.draw_buffer(0, sheet["title"])
            blf.size(0, 13)
            for tile in sheet["tiles"]:
                x, y, w, h = tile["box"]
                bottom = height - y - h
                blf.color(0, 1, 1, 1, 1)
                blf.position(0, x + 2, bottom - 14, 0)
                blf.draw_buffer(0, _fit(tile["label"], w - 4))
                if tile["beat"]:
                    blf.color(0, 0.05, 0.05, 0.06, 1)
                    blf.position(0, x + 5, bottom + h - 14, 0)
                    blf.draw_buffer(0, _fit(tile["beat"], w - 10))
        canvas.file_type, canvas.quality = "JPEG", videoref.SHEET_QUALITY
        imbuf.write(canvas, filepath=str(target))
    finally:
        canvas.free()


def _fit(text: str, width: int) -> str:
    while text and blf.dimensions(0, text)[0] > width:
        text = text[:-1]
    return text


def prepare(folder: Path, spec: dict) -> dict:
    started = time.monotonic()
    frames_dir = extract(spec, folder)
    found = beats(spec, frames_dir, folder)
    camera = {"asked": False}
    if spec["solve"]:
        try:
            camera = solve(spec, folder)
        except Exception as ex:  # The solve is optional: everything else still stands.
            traceback.print_exc()
            camera = {"asked": True, "solved": False, "reason": f"the tracker failed here: {ex}"}
    sheets = videoref.plan_sheets(spec["name"], spec["fps"], spec["in_frame"], spec["out_frame"], found,
                                  spec["size"], spec["max_sheets"])
    for sheet in sheets:
        draw_sheet(sheet, frames_dir, folder / sheet["file"])
    return {"sheets": [{"file": s["file"], "range": s["range"], "time": s["time"], "tiles": len(s["tiles"])}
                       for s in sheets],
            "beats": len(found), "camera": camera, "seconds": round(time.monotonic() - started, 2)}


def main() -> int:
    folder = Path(sys.argv[sys.argv.index("--") + 1])
    try:
        spec = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
        result = prepare(folder, spec)
    except Exception as ex:
        traceback.print_exc()
        result = {"error": f"{type(ex).__name__}: {ex}"}
    jobs.atomic_json(folder / "result.json", result)
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # No UI shutdown or handlers; result.json is already written.
