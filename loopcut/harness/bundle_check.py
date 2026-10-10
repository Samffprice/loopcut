"""A relative texture survives an indexed snapshot, isolated collection and deduplicated upload.

    <blender> -b --factory-startup --python loopcut/harness/bundle_check.py -- <tmpdir>

All scene, checkpoint and account-test data live in a new folder under tmpdir. The fake
service uses no sockets. PASS/FAIL lines are flushed and the process exits nonzero on any
failure, even without --python-exit-code. The child Blender used by collect is intentional.
"""

import hashlib
import os
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
from fake_blobs import FakeBlobs  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print("PASS " + message, flush=True)


def run(tmpdir):
    tmpdir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="loopcut-bundle-check-", dir=tmpdir)).resolve()
    os.environ["LOOPCUT_DATA_DIR"] = str(root / "data")
    os.environ["LOOPCUT_CONFIG_DIR"] = str(root / "config")
    os.environ["LOOPCUT_CHECKPOINT_BUDGET_MB"] = "32"
    checkout.use()
    from loopcut import blobs, bundles, checkpoints

    project = root / "project" / "scene.blend"
    texture = project.parent / "textures" / "source.png"
    texture.parent.mkdir(parents=True)
    generated = bpy.data.images.new("BundlePixels", width=2, height=2)
    generated.pixels[:] = [0.8, 0.3, 0.1, 1.0] * 4
    generated.filepath_raw = str(texture)
    generated.file_format = "PNG"
    generated.save()
    bpy.data.images.remove(generated)
    image = bpy.data.images.load(str(texture))
    image.name = "BundleTexture"
    image.filepath = "//textures/source.png"
    material = bpy.data.materials.new("BundleMaterial")
    if material.node_tree is None:
        material.use_nodes = True
    node = material.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    bsdf = next(node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED")
    material.node_tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    bpy.data.objects["Cube"].data.materials.append(material)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end, scene.render.fps = 1, 5, 24
    scene.render.fps_base = 1
    saved = bpy.ops.wm.save_as_mainfile(filepath=str(project), compress=True, relative_remap=False)
    check(saved == {"FINISHED"}, "saved source scene with an unpacked relative texture")
    original_hash = blobs.sha256(project)
    session = {"id": uuid.uuid4().hex}
    store = checkpoints._store(session)
    entry = checkpoints._write_snapshot(store, "turn", "Bundle check", message_index=0, item_index=0)
    snapshot = store.snapshot_path(entry["id"])
    snapshot_hash = blobs.sha256(snapshot)
    check(snapshot.is_file(), "wrote indexed " + ("recovery snapshot" if entry["in_place"] else "stock copy-save"))

    stage = root / "bundle"
    result = checkpoints.bundle_from_checkpoint(session, entry["id"], stage)
    manifest = result["manifest"]
    check(not result["missing"] and not result["unsupported"], "collector reports all inputs supported and present")
    check(bpy.data.filepath == str(project) and image.filepath == "//textures/source.png",
          "isolated collection preserved the live scene and its paths")
    check(blobs.sha256(project) == original_hash and blobs.sha256(snapshot) == snapshot_hash,
          "user scene and checkpoint bytes stayed unchanged")
    paths = {row["path"] for row in manifest["files"]}
    check(len(paths) == 2 and "scene.blend" in paths and any(path.startswith("textures/") for path in paths),
          "manifest lists scene.blend and its texture")
    for row in manifest["files"]:
        path = stage / row["path"]
        check(row["sha256"] == blobs.sha256(path) and row["size"] == path.stat().st_size,
              "correct digest and length for " + row["path"])
    check(manifest["source"] == {"kind": "checkpoint", "session": session["id"], "checkpoint": entry["id"]},
          "manifest identifies the indexed checkpoint")

    service = FakeBlobs()
    bundle_id = bundles.upload(stage, manifest, client=service.client())
    check(len(service.puts) == 3, "first upload PUT three blobs (scene, texture, manifest)")
    first_put_count = len(service.puts)
    second_id = bundles.upload(stage, manifest, client=service.client())
    check(second_id == bundle_id and len(service.puts) == first_put_count, "second upload PUT zero blobs")
    check(bundle_id == hashlib.sha256(bundles.canonical_json(manifest)).hexdigest(), "bundle id hashes canonical JSON")
    check(service.stored[bundle_id] == bundles.canonical_json(manifest), "uploaded canonical manifest bytes")

    loaded = bpy.ops.wm.open_mainfile(filepath=str(stage / "scene.blend"), load_ui=False, use_scripts=False)
    check(loaded == {"FINISHED"}, "opened the collected scene.blend")
    bundled_image = bpy.data.images["BundleTexture"]
    check(bundled_image.filepath.startswith("//textures/") and ".." not in bundled_image.filepath.split("/"),
          "saved scene references its texture with a safe relative path")
    resolved = Path(bpy.path.abspath(bundled_image.filepath)).resolve()
    check(resolved.is_relative_to(stage) and resolved.is_file(), "relative texture resolves inside the bundle")
    check(blobs.sha256(resolved) == blobs.sha256(texture), "bundled texture bytes match the source")
    bundled_image.reload()
    check(tuple(bundled_image.size) == (2, 2) and bundled_image.has_data, "Blender decodes the bundled texture")
    print("PASS bundle check; artifacts: " + str(root), flush=True)


if __name__ == "__main__":
    try:
        run(Path(sys.argv[sys.argv.index("--") + 1]))
    except Exception:
        traceback.print_exc()
        print("FAIL bundle check", flush=True)
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
