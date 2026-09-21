"""Exercise the Poly Haven tools against the live API inside Blender (network needed):
  Blender -b --factory-startup --python harness/polyhaven_check.py
Downloads land in a throwaway LOOPCUT_DATA_DIR; the scene is never saved."""
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

from loopcut import tools  # noqa: E402


def check():
    code = 1
    try:
        found = tools.execute("search_polyhaven", json.dumps({"query": "wooden table", "type": "models", "limit": 4}))
        assert found.ok and "1. " in found.text and "wooden_table" in found.text, found.text
        assert found.image_path and found.image_path.stat().st_size > 5_000, found
        print(found.text)

        hdri = tools.execute("import_polyhaven", json.dumps({"asset_id": "lakeside_sunrise"}))
        assert hdri.ok and bpy.context.scene.world.name == "lakeside_sunrise", hdri.text
        assert bpy.context.scene.world.node_tree.nodes["Environment Texture"].image.packed_file, "HDRI packed"

        tools.execute("run_python", json.dumps({"summary": "Plane", "code": "bpy.ops.mesh.primitive_plane_add()"}))
        texture = tools.execute("import_polyhaven", json.dumps({"asset_id": "brick_wall_02", "apply_to": ["Plane"]}))
        assert texture.ok and "applied to Plane" in texture.text and "base_color" in texture.text, texture.text
        plane = bpy.data.objects["Plane"]
        assert plane.active_material.name == "brick_wall_02" and plane.active_material["polyhaven"] == "brick_wall_02"
        assert all(n.image.packed_file for n in plane.active_material.node_tree.nodes if n.type == "TEX_IMAGE")

        model = tools.execute("import_polyhaven", json.dumps({"asset_id": "wooden_table_02"}))
        assert model.ok and "Imported wooden_table_02" in model.text and '"bounds"' in model.text, model.text
        assert any(o.get("polyhaven") == "wooden_table_02" for o in bpy.data.objects), "tagged"
        print(model.text[:300])

        # The cache makes a repeat import a local affair: the same file, no second download.
        from loopcut import polyhaven
        cached = list((polyhaven.cache_root() / "wooden_table_02" / "1k").glob("*.blend"))
        assert cached, "blend cached"
        stamp = cached[0].stat().st_mtime
        again = tools.execute("import_polyhaven", json.dumps({"asset_id": "wooden_table_02"}))
        assert again.ok and cached[0].stat().st_mtime == stamp, again.text

        missing = tools.execute("import_polyhaven", json.dumps({"asset_id": "no_such_asset_xyz"}))
        assert not missing.ok and "no asset" in missing.text, missing.text
        print("polyhaven_check: OK")
        code = 0
    except Exception:
        traceback.print_exc()
    finally:
        sys.exit(code)


check()
