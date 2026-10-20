"""Real Blender verification against the local contract mock, never a paid service.

    <blender> -b --factory-startup --python loopcut/harness/cloud_jobs_check.py -- --service http://127.0.0.1:PORT

Start the mock with --live-tools image_generate,image_to_3d --job-seconds .5.
The harness also enables those routes via /mock/control. It requires no stored credentials.
The motion part (contract 0.7) needs ffmpeg on PATH for its short test video, else it skips that call.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ["LOOPCUT_DATA_DIR"] = tempfile.mkdtemp(prefix="loopcut-cloud-check-")
os.environ["LOOPCUT_CONFIG_DIR"] = tempfile.mkdtemp(prefix="loopcut-cloud-config-")
from loopcut import cloud, cloud_http, cloud_jobs, conversations, state, tools  # noqa: E402


def check(condition, message):
    if not condition:
        print("FAIL " + message, flush=True)
        raise AssertionError(message)
    print("PASS " + message, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", required=True)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    parts = urllib.parse.urlsplit(args.service)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("--service must be the local HTTP mock")
    client = cloud_http.Client(base_url=args.service, api_key="lc_demo", client_headers=cloud_http.contract_headers())
    client.request("POST", "/mock/control", {"live_tools": ["image_generate", "image_to_3d"], "fail_jobs": False,
                                             "force_error": None, "settings": {"no_generation": False, "private_mode": False}})
    descriptors = {row["name"]: row for row in client.request("GET", "/v1/manifest")["tools"]}
    state.reset()
    original = cloud_jobs.datablock_snapshot(bpy.data)
    with patch.object(cloud_jobs, "_client", return_value=client):
        image_job = cloud_jobs.run(descriptors["image_generate"], {"prompt": "A tiny prop reference"}, {},
                                   project={"id": "cloud-harness", "no_generation": False},
                                   on_progress=lambda row: print(f"image_generate {row['pct']}% {row['message']}", flush=True))
        print(cloud_jobs.apply_result(descriptors["image_generate"], image_job), flush=True)
        images = [image for image in bpy.data.images if "loopcut_provenance" in image
                  and json.loads(image["loopcut_provenance"])["job_id"] == image_job["id"]]
        check(bool(images), "generated image landed with provenance")
        check(any(ob.type == "EMPTY" and ob.empty_display_type == "IMAGE" and ob.data in images for ob in bpy.data.objects),
              "image reference empty is visible in the scene")
        reference = state.session()["references"][0]
        check(conversations.image_path(state.session()["id"], reference["full"]).is_file(), "model can read the pinned reference")
        files = cloud_jobs.resolve_files(descriptors["image_to_3d"], {"image": images[0].name},
                                         Path(tempfile.mkdtemp(prefix="loopcut-cloud-input-")))
        check(files["image"].is_file(), "image datablock exported to PNG")
        model_job = cloud_jobs.run(descriptors["image_to_3d"], {"prompt": "A simple prop", "image": images[0].name}, files,
                                   project={"id": "cloud-harness", "no_generation": False},
                                   on_progress=lambda row: print(f"image_to_3d {row['pct']}% {row['message']}", flush=True))
        print(cloud_jobs.apply_result(descriptors["image_to_3d"], model_job), flush=True)
    imported = [ob for ob in bpy.data.objects if ob.type == "MESH" and "loopcut_provenance" in ob
                and json.loads(ob["loopcut_provenance"])["job_id"] == model_job["id"]]
    check(bool(imported), "GLB landed as mesh objects with provenance")
    check(all("loopcut_provenance" in ob.data for ob in imported), "imported meshes carry provenance")
    check(all(ob.select_get() for ob in imported), "imported objects are selected")
    check(all(any(collection.name.startswith("Loopcut ") for collection in ob.users_collection) for ob in imported),
          "imported objects are grouped")
    materials = [slot.material for ob in imported for slot in ob.material_slots if slot.material]
    check(all("loopcut_provenance" in material for material in materials), "imported materials carry provenance")
    created = cloud_jobs.created_datablocks(original, cloud_jobs.datablock_snapshot(bpy.data))
    check(all("loopcut_provenance" in block for block in created), "every created datablock carries provenance")
    check(all("loopcut_provenance" not in block for block in original.values()), "original scene provenance remains untouched")
    origin = bpy.context.scene
    other = bpy.data.scenes.new('Cloud check other scene')
    try:
        with bpy.context.temp_override(scene=other):
            with patch.object(cloud, '_apply', return_value=tools.ToolResult('unexpected import')) as apply:
                result = cloud._apply_to_scene(cloud_jobs, descriptors['image_generate'], image_job, origin)
        check(not result.ok and not apply.called, 'a completed job cannot import into a different scene')
    finally:
        bpy.data.scenes.remove(other)
    check(cloud._same_scene(origin), 'scene identity survives Blender RNA wrapper access')
    with tempfile.TemporaryDirectory(prefix='loopcut-variants-check-') as directory:
        path = Path(directory) / 'variants.gltf'
        path.write_text(json.dumps({'asset': {'version': '2.0'},
                                    'extensions': {'KHR_materials_variants': {'variants': []}}}))
        try:
            cloud_jobs._check_gltf(path)
        except cloud_http.CloudError as ex:
            check(ex.code == 'apply_not_available', 'destructive glTF variants are refused before import')
        else:
            raise AssertionError('glTF variants reached the importer')
    motion(client)
    print("PASS cloud jobs check", flush=True)


def motion(client):
    """apply_motion against the mock, which serves a real Kimodo SOMA77 walk (contract 0.7): the apply_args
    target_armature and in_place stay local, and a video reference is measured, quoted and applied."""
    from loopcut import motion_retarget
    fixture = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "motion" / "kimodo_walk_60.bvh").read_text()
    client.request("POST", "/mock/control", {"live_tools": ["motion_from_text", "motion_from_video"]})
    descriptors = {row["name"]: row for row in client.request("GET", "/v1/manifest")["tools"]}
    text, video = descriptors["motion_from_text"], descriptors["motion_from_video"]
    check(cloud._descriptor(text) and cloud._descriptor(video), "motion descriptors are callable in this build")
    check("target_armature" in cloud.schema({**text, "backend": "managed"})["function"]["parameters"]["properties"],
          "target_armature is offered to the model")
    scene = bpy.context.scene
    hero, body = motion_retarget.build_skeleton(motion_retarget.Source(motion_retarget.parse_bvh(fixture)), scene, (2, 0, 0))
    hero.name, body.name = "Hero", "Hero body"
    project = {"id": "cloud-harness", "no_generation": False}
    check("in_place" in cloud.apply_args(text) and "in_place" in cloud.apply_args(video), "in_place is an apply argument")
    prepared = cloud_jobs.prepare_inputs(text, {"prompt": "walk forward and wave", "target_armature": "Hero body",
                                                "in_place": True}, {})
    check(prepared["target_armature"] == "Hero", "a deformed mesh names its armature")
    with patch.object(cloud_jobs, "_client", return_value=client):
        job = cloud_jobs.run(text, prepared, {}, project=project)  # The mock refuses unknown inputs with 422.
        check(cloud_jobs._kept(job, "apply_args") == {"target_armature": "Hero", "in_place": True},
              "target_armature and in_place kept for the handler, not sent")
        check(motion_retarget.parse_bvh(Path(next(o["path"] for o in job["outputs"] if o["name"] == "motion.bvh"))
                                        .read_text()).by_name.get("LeftShin") is not None, "the mock serves a SOMA77 BVH")
        line = cloud_jobs.apply_result(text, job)
        print(line, flush=True)
        action = hero.animation_data.action if hero.animation_data else None
        check(action is not None and action.name.startswith("Motion: walk forward"), "motion applied to the named armature")
        check("root motion in place" in line and "fingers not animated" in line, "in_place and the sidecar reach the handler")
        check("loopcut_provenance" in action, "the motion's action carries provenance")
        xs = []
        for frame in range(scene.frame_start, scene.frame_start + 48):
            scene.frame_set(frame)
            xs.append((hero.matrix_world @ hero.pose.bones["Hips"].head).to_2d())
        check(max((x - xs[0]).length for x in xs) < 0.2, "in place: the hips stay over their start")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            print("SKIP motion_from_video: no ffmpeg for the test video", flush=True)
            return
        folder = Path(tempfile.mkdtemp(prefix="loopcut-motion-video-"))
        clip_path = folder / "take.mp4"
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=96x64:rate=30",
                        "-pix_fmt", "yuv420p", str(clip_path)], check=True)
        clip = bpy.data.movieclips.load(str(clip_path))
        for ob in bpy.context.selected_objects:
            ob.select_set(False)
        bpy.context.view_layer.objects.active = None
        arguments = {"video": clip.name}
        files = cloud_jobs.resolve_files(video, arguments, folder / "inputs")
        prepared = cloud_jobs.prepare_inputs(video, arguments, files)
        check(abs(prepared["video_seconds"] - 2.0) < 0.1 and "target_armature" not in prepared,
              f"the clip is measured ({prepared['video_seconds']} s) and nothing is targeted")
        try:
            cloud_jobs.run(video, prepared, files, project=project)
            check(False, "motion_from_video asks for a quote first")
        except cloud_http.CloudError as ex:
            check(ex.code == "quote_required" and ex.extra.get("credits") == 6, f"2 s quoted at {ex.extra.get('credits')} credits")
            quote = ex.extra["quote_id"]
        job = cloud_jobs.run(video, {**prepared, "quote_id": quote}, files, project=project)
        line = cloud_jobs.apply_result(video, job)
        print(line, flush=True)
        check("new skeleton" in line and "Motion: take" in line and "root motion in world" in line,
              "no target: the motion lands on a new skeleton named from the clip, travelling")


try:
    main()
except Exception:
    traceback.print_exc()
    print("FAIL cloud jobs check", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)  # Blender otherwise continues after a --python exception with exit status zero.
