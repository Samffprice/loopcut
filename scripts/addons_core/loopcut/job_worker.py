"""Render a frozen scene into owned, verified frames; optionally encode that sequence as MP4."""

import os
import sys
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).parent))
import jobs


def image_record(path: Path, width: int, height: int) -> dict:
    jobs.verify_png(path, width, height)
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        if tuple(image.size) != (width, height) or not image.has_data or len(image.pixels) != width * height * 4:
            raise jobs.JobError(f"Image does not decode at {width} x {height}: {path.name}")
        # Force the decoder to provide actual pixels, rather than trusting a PNG header.
        if len(image.pixels[:4]) != 4 or len(image.pixels[-4:]) != 4:
            raise jobs.JobError(f"Incomplete image: {path.name}")
    finally:
        bpy.data.images.remove(image)
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": jobs.digest(path),
            "width": width, "height": height}


def valid_frame(path: Path, record: dict, spec: dict) -> bool:
    try:
        return (record.get("scene_revision") == spec["scene_revision"]
                and record.get("sha256") == jobs.digest(path)
                and image_record(path, spec["width"], spec["height"])["sha256"] == record["sha256"])
    except (OSError, RuntimeError, jobs.JobError):
        return False


def configure(scene, spec: dict) -> None:
    r = scene.render
    r.engine = spec["engine"]
    r.resolution_x, r.resolution_y, r.resolution_percentage = spec["width"], spec["height"], 100
    r.use_border, r.use_file_extension = False, False
    r.image_settings.media_type = "IMAGE"
    r.image_settings.file_format, r.image_settings.color_mode, r.image_settings.color_depth = "PNG", "RGBA", "8"
    if r.engine == "CYCLES":
        scene.cycles.samples = spec["samples"]
        scene.cycles.device = "CPU"  # Factory child has no user GPU configuration; never silently rely on it.
    if r.engine == "BLENDER_EEVEE":
        scene.eevee.taa_render_samples = spec["samples"]
    # File Output nodes otherwise write outside this job's owned output directory.
    for tree in bpy.data.node_groups:
        for node in tree.nodes:
            if node.bl_idname == "CompositorNodeOutputFile":
                node.mute = True
    if getattr(scene, "node_tree", None):
        for node in scene.node_tree.nodes:
            if node.bl_idname == "CompositorNodeOutputFile":
                node.mute = True


def encode_movie(spec: dict, output: Path, frames: list[dict]) -> dict:
    if not bpy.app.ffmpeg.supported:
        raise jobs.JobError("This Blender build cannot encode MP4 (FFmpeg is unavailable). PNG frames are retained.")
    scene = bpy.data.scenes.new("Loopcut encode")
    r = scene.render
    r.resolution_x, r.resolution_y, r.resolution_percentage = spec["width"], spec["height"], 100
    r.fps, r.fps_base = spec["fps"], spec["fps_base"]
    r.image_settings.media_type = "VIDEO"
    r.image_settings.file_format = "FFMPEG"
    r.ffmpeg.format, r.ffmpeg.codec = "MPEG4", "H264"
    r.ffmpeg.constant_rate_factor, r.ffmpeg.ffmpeg_preset = "MEDIUM", "GOOD"
    r.ffmpeg.audio_codec = "NONE"
    r.use_file_extension = False
    temporary, final = output / "movie.partial.mp4", output / "movie.mp4"
    r.filepath = str(temporary)
    scene.frame_start, scene.frame_end = 1, len(frames)
    # PNGs already contain the original scene's display transform. Standard avoids applying AgX twice.
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    editor = scene.sequence_editor_create()
    strip = editor.strips.new_image("Verified PNG sequence", str(output / frames[0]["file"]), channel=1, frame_start=1)
    for record in frames[1:]:
        strip.elements.append(record["file"])
    strip.frame_final_duration = len(frames)
    bpy.ops.render.render(animation=True, scene=scene.name)
    movie = bpy.data.movieclips.load(str(temporary), check_existing=False)
    try:
        fps = spec["fps"] / spec["fps_base"]
        if tuple(movie.size) != (spec["width"], spec["height"]) or movie.frame_duration != len(frames):
            raise jobs.JobError("Encoded movie has the wrong dimensions or frame count.")
        if abs(movie.fps - fps) > 0.02:
            raise jobs.JobError("Encoded movie has the wrong frame rate.")
        record = {"file": final.name, "frames": movie.frame_duration, "fps": movie.fps,
                  "duration_seconds": movie.frame_duration / movie.fps,
                  "width": movie.size[0], "height": movie.size[1], "audio": "none"}
    finally:
        bpy.data.movieclips.remove(movie)
    # Exercise the decoder too. Container metadata alone would accept an unreadable video stream.
    editor.strips.remove(strip)
    editor.strips.new_movie("Verify encoded movie", str(temporary), channel=1, frame_start=1)
    r.image_settings.media_type = "IMAGE"
    r.image_settings.file_format, r.image_settings.color_mode, r.image_settings.color_depth = "PNG", "RGBA", "8"
    probe = output / "movie-decode-check.png"
    checked = sorted({1, (len(frames) + 1) // 2, len(frames)})
    for frame in checked:
        scene.frame_set(frame)
        r.filepath = str(probe)
        bpy.ops.render.render(write_still=True, scene=scene.name)
        image_record(probe, spec["width"], spec["height"])
    probe.unlink()
    record["decode_checked_frames"] = checked
    os.replace(temporary, final)
    return {**record, "bytes": final.stat().st_size, "sha256": jobs.digest(final)}


def render_job(path: Path) -> None:
    started = time.monotonic()
    spec = jobs.spec_at(path)
    if spec.get("blender_build") != bpy.app.build_hash.decode():
        raise jobs.JobError("Blender changed since this job was created; start a new job for this build.")
    if spec.get("renderer_revision") != jobs.digest(Path(__file__)):
        raise jobs.JobError("The renderer changed since this job was created; start a new job.")
    snapshot = path / "scene.blend"
    if jobs.digest(snapshot) != spec["scene_revision"]:
        raise jobs.JobError("The saved scene changed; create a new job instead of resuming this one.")
    output = Path(spec["output"])
    if jobs.read_json(output / "loopcut-job.json").get("id") != spec["id"]:
        raise jobs.JobError("Output folder no longer belongs to this job.")
    for asset in spec.get("assets", []):
        if jobs.digest(Path(asset["path"])) != asset["sha256"]:
            raise jobs.JobError("An external scene asset changed; create a new job for the new revision.")
    bpy.ops.wm.open_mainfile(filepath=str(snapshot), load_ui=False, use_scripts=False)
    scene = bpy.data.scenes.get(spec["scene"])
    if scene is None:
        raise jobs.JobError("Saved scene is missing.")
    bpy.context.window.scene = scene
    scene.camera = bpy.data.objects.get(spec["camera"])
    if scene.camera is None or scene.camera.type != "CAMERA":
        raise jobs.JobError("Saved camera is missing.")
    configure(scene, spec)
    previous = jobs.read_json(path / "progress.json") if (path / "progress.json").exists() else {}
    owned = {r["file"]: r for r in previous.get("files", [])}
    progress = {"phase": "rendering", "completed": 0, "total": len(spec["frames"]), "files": [],
                "reused": 0, "rendered": 0, "verified_complete": False}

    def report():
        progress["elapsed_seconds"] = round(time.monotonic() - started, 2)
        jobs.atomic_json(path / "progress.json", progress)

    # Keep old valid records available across a cancellation during resume verification.
    progress["files"] = list(owned.values())
    report()
    completed = []
    for frame in spec["frames"]:
        filename = f"frame_{frame:07d}.png"
        target = output / filename
        old = owned.get(filename, {})
        if valid_frame(target, old, spec):
            record = old
            progress["reused"] += 1
        else:
            scene.frame_set(frame)
            # Camera markers may switch the active camera at frame_set. The requested camera wins.
            scene.camera = bpy.data.objects[spec["camera"]]
            temporary = output / (filename + ".partial.png")
            scene.render.filepath = str(temporary)
            bpy.ops.render.render(write_still=True, scene=scene.name)
            record = image_record(temporary, spec["width"], spec["height"])
            os.replace(temporary, target)
            record.update(file=filename, frame=frame, scene_revision=spec["scene_revision"])
            progress["rendered"] += 1
        completed.append(record)
        owned[filename] = record
        progress["files"], progress["completed"] = list(owned.values()), len(completed)
        report()
    progress["files"] = completed
    if spec["format"] == "MP4":
        progress["phase"] = "encoding"
        report()
        progress["movie"] = encode_movie(spec, output, completed)
    for asset in spec.get("assets", []):
        if jobs.digest(Path(asset["path"])) != asset["sha256"]:
            raise jobs.JobError("An external asset changed during rendering; the job is not verified.")
    progress.update(phase="verified", verified_complete=True)
    report()
    jobs.atomic_json(output / "manifest.json", {"job": spec, "result": progress})


if __name__ == "__main__":
    folder = Path(sys.argv[sys.argv.index("--") + 1])
    try:
        render_job(folder)
    except Exception as ex:
        traceback.print_exc()
        progress = jobs.read_json(folder / "progress.json") if (folder / "progress.json").exists() else {}
        jobs.atomic_json(folder / "progress.json", {**progress, "verified_complete": False,
                                                  "error": f"{type(ex).__name__}: {ex}"})
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    os._exit(0)
