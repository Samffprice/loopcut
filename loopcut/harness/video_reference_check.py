"""Video reference v0 in a real Blender: what the unit tests (tests/test_video_reference.py) cannot run.

    Blender -b --factory-startup --python loopcut/harness/video_reference_check.py

Run it under the heavy lock: it renders two 3 s clips and starts a background Blender (the clip
worker) for each, ~15 s and two Blender processes at once. No model or network. The media are made here.

1. Makes its own clip: a field of randomly coloured cubes at several depths (trackable, with
   parallax), a handheld camera sway (yaw and a little translation, keyframed), and a 0.3 m subject
   cube that holds, moves fast across frames 24-28, and holds. Workbench, 50 mm, 640x360, 24 fps, H.264.
2. video_attach.probe reads 24 fps, 72 frames, 640x360 and leaves no movie clip behind.
3. video_attach.start trimmed to 0.5 s-end (clip frames 13-72) launches video_worker.py in a
   background Blender; the check waits on that process and calls _poll as the panel's timer would.
   - Pixels in background mode: 60 JPEGs named by clip frame, each decoding at 640x360, and frame
     26 (mid-move) closer to a direct render of frame 26 than frames 25 and 27 are: no off-by-one.
   - beats.json passes check_beats; the "fastest motion" peak is within 2 frames of the move
     (24-29); the clip starts and ends on a hold.
   - Sheets drawn with imbuf and blf: each JPEG is the planned size and became an attachment carrying
     the clip's record; the message would hold at most 6 images.
   - beats are found under the camera sway: the frame-wide shift is taken out before differencing
     (raw differencing found no hold here on 2026-10-16).
   - The tracker in background mode: detect_features/track_markers/clean_tracks/solve_camera ran (no
     "tracker failed here"); every attempt is printed, and the solve outcome, which the moving subject
     may spoil (then the rejection path ran).
4. clip_background puts the clip behind the scene camera as a MOVIE_CLIP background, sets 24 fps and
   maps clip frame 13 to the scene's start frame; look_at_reference with frame=26 returns that frame.
5. GUI only (skipped with -b): compare_with_reference with frame=26 puts the clip frame beside a
   camera capture at the matching scene frame and puts the scene back on its own frame.
6. The same sway without the subject: the solve must pass (error within videoref.MAX_SOLVE_ERROR from
   a 26 mm first guess on a 50 mm camera), camera_motion.json passes check_camera_motion, and its yaw
   follows the keyframed sway (|correlation| > 0.8, the spec's eval bar).
Prints VIDEO REFERENCE CHECK OK at the end; exits 1 on the first failure.
"""

import json
import math
import os
import random
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ["LOOPCUT_DATA_DIR"] = tempfile.mkdtemp(prefix="loopcut-harness-")

from loopcut import attachments, state, tools, video_attach, videoref  # noqa: E402

FPS, FRAMES, SIZE = 24, 72, (640, 360)
MOVE = (24, 28)  # The subject's fast move, scene frames.
# The subject's size, m, 3 m from the camera. At 0.6 m it crosses a third of the frame in front of the
# tracked field and no solve survives (2026-10-16: 3.5 px, and too few tracks once its own are deleted).
SUBJECT = 0.3


def step(text: str) -> None:
    """One line per verified step, flushed: a failure then shows how far the check got."""
    print(f"ok  {text}", flush=True)


def sway(frame: int) -> tuple[float, float, float]:
    """The camera's yaw (degrees), x and z offsets at a frame."""
    t = (frame - 1) / FPS
    return (2.5 * math.sin(2 * math.pi * t / 1.3), 0.12 * math.sin(2 * math.pi * t / 1.5),
            0.06 * math.sin(2 * math.pi * t / 1.1))


def make_scene(with_subject: bool = True):
    scene = bpy.context.scene
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    rng = random.Random(4)
    for _ in range(170):
        bpy.ops.mesh.primitive_cube_add(size=rng.uniform(0.15, 0.35),
                                        location=(rng.uniform(-4, 4), rng.uniform(5, 10), rng.uniform(-1, 4)))
    subject = None
    if with_subject:
        bpy.ops.mesh.primitive_cube_add(size=SUBJECT, location=(-0.8, 3.0, 1.0))
        subject = bpy.context.active_object
        subject.name = "Subject"
    camera_data = bpy.data.cameras.new("Camera")
    camera = bpy.data.objects.new("Camera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    for frame in range(1, FRAMES + 1, 3):
        yaw, x, z = sway(frame)
        camera.location = (x, 0.0, 1.5 + z)
        camera.rotation_euler = (math.radians(90), 0.0, math.radians(yaw))
        camera.keyframe_insert("location", frame=frame)
        camera.keyframe_insert("rotation_euler", frame=frame)
    for frame, x in ((1, -0.8), (MOVE[0], -0.8), (MOVE[1], 0.8), (FRAMES, 0.8)) if subject else ():
        subject.location.x = x
        subject.keyframe_insert("location", frame=frame)
    r = scene.render
    r.engine = "BLENDER_WORKBENCH"
    scene.display.shading.color_type = "RANDOM"
    r.resolution_x, r.resolution_y, r.resolution_percentage = SIZE[0], SIZE[1], 100
    r.fps, r.fps_base = FPS, 1.0
    scene.frame_start, scene.frame_end = 1, FRAMES
    scene.view_settings.view_transform = "Standard"
    return scene


def render_clip(scene, work: Path, name: str = "check_clip.mp4") -> Path:
    r = scene.render
    r.image_settings.media_type = "VIDEO"
    r.image_settings.file_format = "FFMPEG"
    r.ffmpeg.format, r.ffmpeg.codec = "MPEG4", "H264"
    r.ffmpeg.constant_rate_factor = "HIGH"
    r.ffmpeg.audio_codec = "NONE"
    r.use_file_extension = False
    r.filepath = str(work / name)
    bpy.ops.render.render(animation=True)
    r.image_settings.media_type = "IMAGE"
    r.image_settings.file_format = "PNG"
    return work / name


def prepare(clip_path: Path, watch: str) -> tuple[dict, Path, dict]:
    """video_attach.start from 0.5 s, the worker waited on, and _poll run as the panel's timer would."""
    session = state.reset()
    session["attachments"] = []
    began = time.monotonic()
    clip_id = video_attach.start(session, clip_path, 0.5, None, watch, True)
    job = video_attach._running[clip_id]
    job["process"].wait(timeout=video_attach.WORKER_SECONDS)
    video_attach._poll()
    if bpy.app.timers.is_registered(video_attach._poll):
        bpy.app.timers.unregister(video_attach._poll)
    folder = job["folder"]
    print(f"worker finished in {time.monotonic() - began:.1f} s; folder {folder}", flush=True)
    result = json.loads((folder / "result.json").read_text())
    assert "error" not in result, result
    assert session["items"][-1]["kind"] == "notice", session["items"][-1]
    return session, folder, result


def render_still(scene, frame: int, path: Path) -> Path:
    scene.frame_set(frame)
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    return path


def pixels(path: Path):
    import imbuf
    import numpy as np
    image = imbuf.load(str(path))
    try:
        assert tuple(image.size) == SIZE, (path.name, tuple(image.size))
        with image.with_buffer() as buffer:
            return np.asarray(buffer)[..., :3].astype(np.float32)
    finally:
        image.free()


def yaw_correlation(folder: Path) -> float:
    """camera_motion.json checked, and its yaw (rotation about the camera's own up axis) against the sway."""
    motion = videoref.check_camera_motion(json.loads((folder / "camera_motion.json").read_text()))
    solved = {e["frame"]: e["rotation_deg"][1] for e in motion["frames"]}
    keys = sorted(solved)
    return correlation([solved[f] for f in keys], [sway(f)[0] - sway(keys[0])[0] for f in keys])


def correlation(a: list[float], b: list[float]) -> float:
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    return cov / math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b) or 1e-12)


def main() -> None:
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="loopcut-clip-"))
    scene = make_scene()
    clip_path = render_clip(scene, work)
    step(f"clip rendered: {clip_path.name} in {time.monotonic() - started:.1f} s")

    before = len(bpy.data.movieclips)
    info = video_attach.probe(clip_path)
    assert len(bpy.data.movieclips) == before, "probe left a movie clip behind"
    assert (round(info["fps"]), info["frames"], tuple(info["size"])) == (FPS, FRAMES, SIZE), info
    step(f"probe: {info}, no movie clip left behind")

    session, folder, result = prepare(clip_path, "the cube")
    step(f"worker result, panel says: {session['items'][-1]['text']}")

    first, last = 13, FRAMES
    frames = sorted((folder / "frames").iterdir())
    assert [p.name for p in frames] == [videoref.frame_file(f) for f in range(first, last + 1)], \
        [p.name for p in frames][:5]
    for path in frames:
        pixels(path)
    step(f"background-mode decode: {len(frames)} JPEGs, clip frames {first}-{last}, each {SIZE[0]}x{SIZE[1]}")
    import numpy as np
    truth = pixels(render_still(scene, 26, work / "truth_26.png"))
    distance = {f: float(np.abs(pixels(folder / "frames" / videoref.frame_file(f)) - truth).mean())
                for f in (25, 26, 27)}
    assert distance[26] < min(distance[25], distance[27]), f"decoded frames are off by one: {distance}"
    step(f"no off-by-one: mean distance to a direct render of frame 26 by decoded frame {distance}")

    beats = videoref.check_beats(json.loads((folder / "beats.json").read_text()))
    print("beats:", videoref.beats_line(beats["beats"]), flush=True)
    print("curve:", beats["motion_curve"]["values"], flush=True)
    fastest = [b for b in beats["beats"] if b["label"] == "fastest motion"]
    assert fastest and MOVE[0] <= fastest[0]["frame"] <= MOVE[1] + 1, beats["beats"]
    step(f"fastest motion at clip frame {fastest[0]['frame']} (the move is frames {MOVE[0]}-{MOVE[1]})")
    holds = [b for b in beats["beats"] if b["kind"] == "hold"]
    assert holds and holds[0]["start_frame"] == first and holds[-1]["end_frame"] == last, holds
    step(f"holds under the camera sway: {[(h['start_frame'], h['end_frame'], h['label']) for h in holds]}")
    path = beats["motion_curve"]["image_shift"]
    curve_frames = beats["motion_curve"]["frames"]
    pan = correlation([p[0] for p in path], [-(sway(f)[0] - sway(first)[0]) for f in curve_frames])
    print(f"image_shift x vs the keyframed yaw: correlation {pan:.3f} (the sway also translates)", flush=True)

    sheets = [a for a in session["attachments"] if a.get("video")]
    assert sheets and len(sheets) == len(result["sheets"]) <= attachments.MAX_PER_MESSAGE, result["sheets"]
    plan = {s["file"]: s for s in videoref.plan_sheets("check_clip.mp4", info["fps"], first, last, beats["beats"],
                                                       info["size"], len(result["sheets"]))}
    import imbuf
    for sheet in result["sheets"]:
        image = imbuf.load(str(folder / sheet["file"]))
        try:
            assert list(image.size) == plan[sheet["file"]]["size"], (sheet["file"], tuple(image.size))
            assert image.file_type == "JPEG", (sheet["file"], image.file_type)
        finally:
            image.free()
    content = videoref.user_content("copy the cube", session["attachments"])
    assert sum(p["type"] == "image_url" for p in content) <= attachments.MAX_PER_MESSAGE
    assert "<video_reference clip=\"check_clip.mp4\">" in content[0]["text"]
    step(f"{len(result['sheets'])} sheet(s) drawn at the planned sizes and attached, within the 6-image budget")

    camera = result["camera"]
    assert camera.get("asked") and "tracker failed here" not in camera.get("reason", ""), camera
    step(f"tracker ran in background mode: {camera.get('tracks')} tracks, attempts {camera.get('attempts')}")
    if camera.get("solved"):
        step(f"with the subject: camera solved ({camera['mode']}, error {camera['error_px']} px), "
             f"yaw correlation {yaw_correlation(folder):.3f}")
    else:
        print("with the subject: camera solve dropped (the rejection path ran):", camera.get("reason"), flush=True)

    session["references"] = [dict(a, pinned=True) for a in session["attachments"]]
    session["attachments"] = []
    scene.render.fps = 30
    placed = video_attach.clip_background("check_clip.mp4")
    print("clip_background:", placed.text, flush=True)
    backgrounds = [b for b in scene.camera.data.background_images if b.source == "MOVIE_CLIP"]
    assert len(backgrounds) == 1 and scene.camera.data.show_background_images
    clip = backgrounds[0].clip
    assert Path(bpy.path.abspath(clip.filepath)).resolve() == clip_path.resolve(), clip.filepath
    assert (scene.render.fps, round(scene.render.fps_base, 4)) == (FPS, 1.0), (scene.render.fps, scene.render.fps_base)
    assert clip.frame_start + first - 1 == scene.frame_start, clip.frame_start
    assert video_attach.scene_frame(sheets[0]["video"], 26) == scene.frame_start + 26 - first
    looked = tools.look_at_reference("check_clip.mp4", frame=26)
    assert looked.image_path and looked.image_path.is_file(), looked.text
    assert "clip frame 26" in looked.text, looked.text
    step("clip_background placed the clip at 24 fps with clip frame 13 on the start frame; look_at_reference frame=26")

    if not bpy.app.background:
        scene.frame_set(5)
        compared = tools.compare_with_reference("check_clip.mp4", angle="camera", frame=26)
        assert compared.image_path and compared.image_path.is_file(), compared.text
        assert "clip frame 26" in compared.text and scene.frame_current == 5, (compared.text, scene.frame_current)
        step("compare: " + compared.text)
    else:
        print("compare_with_reference with frame: GUI only, skipped in background mode", flush=True)

    steady = render_clip(make_scene(with_subject=False), work, "check_steady.mp4")
    session, folder, result = prepare(steady, "")
    camera = result["camera"]
    print("without the subject:", {k: v for k, v in camera.items() if k != "attempts"}, flush=True)
    for attempt in camera.get("attempts", []):
        print("   attempt", attempt, flush=True)
    assert camera.get("solved"), camera.get("reason")
    r = yaw_correlation(folder)
    assert abs(r) > 0.8, r
    step(f"without the subject: camera solved ({camera['mode']}, lens {camera.get('focal_mm')} mm from a 26 mm guess, "
         f"error {camera['error_px']} px over {camera['frames']} frames), yaw vs keyframed sway correlation {r:.3f}")
    print(f"VIDEO REFERENCE CHECK OK in {time.monotonic() - started:.1f} s", flush=True)


try:
    main()
except Exception:
    traceback.print_exc()
    sys.stderr.flush()
    os._exit(1)
