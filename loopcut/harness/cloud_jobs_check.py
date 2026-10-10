"""Real Blender verification against the local contract mock, never a paid service.

    <blender> -b --factory-startup --python loopcut/harness/cloud_jobs_check.py -- --service http://127.0.0.1:PORT

Start the mock with --live-tools image_generate,image_to_3d --job-seconds .5.
The harness also enables those routes via /mock/control. It requires no stored credentials.
"""

import argparse
import json
import os
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
    print("PASS cloud jobs check", flush=True)


try:
    main()
except Exception:
    traceback.print_exc()
    print("FAIL cloud jobs check", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)  # Blender otherwise continues after a --python exception with exit status zero.
