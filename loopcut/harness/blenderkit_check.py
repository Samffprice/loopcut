"""Exercise the BlenderKit tools against the live API inside Blender, on the free catalogue (no key):
  Blender -b --factory-startup --python harness/blenderkit_check.py
With LOOPCUT_BLENDERKIT_KEY set it also checks that plan assets are searchable."""
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout  # noqa: E402
checkout.use()
os.environ.setdefault("LOOPCUT_DATA_DIR", tempfile.mkdtemp(prefix="loopcut-harness-"))
os.environ.setdefault("LOOPCUT_CONFIG_DIR", tempfile.mkdtemp(prefix="loopcut-harness-config-"))

from loopcut import blenderkit, tools  # noqa: E402


def first_id(text: str) -> str:
    return next(line.split(" ", 1)[1].split(":")[0] for line in text.splitlines() if line.startswith("1. "))


def check():
    code = 1
    try:
        found = tools.execute("search_blenderkit", json.dumps({"query": "chair", "type": "model", "limit": 4}))
        assert found.ok and "1. " in found.text and "free" in found.text and found.image_path, found.text
        print(found.text)
        model_id = first_id(found.text)
        got = tools.execute("import_blenderkit", json.dumps({"asset_id": model_id}))
        assert got.ok and "Imported" in got.text and "From BlenderKit" in got.text and '"bounds"' in got.text, got.text
        assert any(o.get("blenderkit_id") == model_id for o in bpy.data.objects), "tagged"
        assert isinstance(bpy.context.scene.get(blenderkit.SCENE_KEY), str), "scene id kept for fair share"
        print(got.text[:240])

        found = tools.execute("search_blenderkit", json.dumps({"query": "oak wood", "type": "material", "limit": 3}))
        assert found.ok, found.text
        material_id = first_id(found.text)
        got = tools.execute("import_blenderkit", json.dumps({"asset_id": material_id, "apply_to": ["Cube"]}))
        assert got.ok and "applied to Cube" in got.text, got.text
        material = bpy.data.objects["Cube"].active_material
        assert material.get("blenderkit_id") == material_id, (material.name, material.get("blenderkit_id"), material_id)
        assert all(n.image.packed_file for n in material.node_tree.nodes if n.type == "TEX_IMAGE" and n.image), "packed"

        found = tools.execute("search_blenderkit", json.dumps({"query": "sky", "type": "hdr", "limit": 3}))
        assert found.ok, found.text
        got = tools.execute("import_blenderkit", json.dumps({"asset_id": first_id(found.text), "resolution": "0.5k"}))
        assert got.ok and "is now the scene world" in got.text, got.text
        assert bpy.context.scene.world.node_tree.nodes["Environment Texture"].image.packed_file

        again = tools.execute("import_blenderkit", json.dumps({"asset_id": model_id}))
        assert again.ok, again.text
        bad = tools.execute("import_blenderkit", json.dumps({"asset_id": "no-such-asset"}))
        assert not bad.ok and "no asset" in bad.text, bad.text

        if blenderkit.api_key():
            found = tools.execute("search_blenderkit", json.dumps({"query": "sofa", "type": "model"}))
            assert found.ok and "free and plan assets" in found.text, found.text
        else:
            print("plan part skipped (no LOOPCUT_BLENDERKIT_KEY)")
        print("blenderkit_check: OK")
        code = 0
    except Exception:
        traceback.print_exc()
    finally:
        sys.exit(code)


check()
