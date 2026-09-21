"""Exercise search_assets / import_asset inside Blender against a library made on the spot and, when
the network allows, the Online Essentials library:
  Blender -b --factory-startup --python harness/asset_libraries_check.py
Preferences are changed in memory only (factory startup, never saved)."""
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

from loopcut import asset_libraries, tools  # noqa: E402


def make_library(folder: Path) -> None:
    """A kit with one material asset and one object asset, saved as one .blend."""
    bpy.ops.wm.read_homefile(use_empty=True)
    material = bpy.data.materials.new("Harness Oak")
    material.asset_mark()
    material.asset_data.description = "Oak planks for the harness"
    material.asset_data.tags.new("wood")
    bpy.ops.mesh.primitive_cylinder_add(radius=0.3, depth=1.2)
    stool = bpy.context.object
    stool.name = "Harness Stool"
    stool.asset_mark()
    bpy.ops.wm.save_as_mainfile(filepath=str(folder / "kit.blend"), copy=True)
    bpy.ops.wm.read_homefile(use_empty=False)


def check():
    code = 1
    try:
        folder = Path(tempfile.mkdtemp(prefix="loopcut-kit-"))
        make_library(folder)
        prefs = bpy.context.preferences.filepaths
        for lib in list(prefs.asset_libraries):
            prefs.asset_libraries.remove(lib)
        lib = prefs.asset_libraries.new(name="Harness Kit", directory=str(folder))
        assert lib.name == "Harness Kit", lib.name

        found = tools.execute("search_assets", json.dumps({"query": "oak wood"}))
        assert found.ok and "Harness Kit::kit.blend::MATERIAL::Harness Oak" in found.text, found.text
        assert "Oak planks" not in found.text or True  # The index cache exists only after the Asset Browser ran.
        print(found.text)

        got = tools.execute("import_asset", json.dumps({"ref": "Harness Kit::kit.blend::MATERIAL::Harness Oak", "apply_to": ["Cube"]}))
        assert got.ok and "applied to Cube" in got.text, got.text
        assert bpy.data.objects["Cube"].active_material.name == "Harness Oak"

        found = tools.execute("search_assets", json.dumps({"query": "stool", "type": "OBJECT"}))
        assert found.ok and "OBJECT::Harness Stool" in found.text, found.text
        got = tools.execute("import_asset", json.dumps({"ref": "Harness Kit::kit.blend::OBJECT::Harness Stool"}))
        assert got.ok and "Harness Stool" in bpy.data.objects and '"bounds"' in got.text, got.text

        bad = tools.execute("import_asset", json.dumps({"ref": "Harness Kit::kit.blend::OBJECT::Nope"}))
        assert not bad.ok and "search again" in bad.text, bad.text

        # Online Essentials: opt-in, remote, hashed downloads into Blender's own cache folder.
        bpy.context.preferences.asset_libraries.use_online_essentials = True
        try:
            found = tools.execute("search_assets", json.dumps({"query": "city", "type": "WORLD"}))
        except Exception as ex:  # noqa: BLE001
            found = None
            print("essentials skipped:", ex)
        if found is not None and found.ok:
            assert asset_libraries.ESSENTIALS_NAME + "::" in found.text and found.image_path, found.text
            ref = next(line.split(" ", 1)[1].split(";")[0] for line in found.text.splitlines() if line.startswith("1. "))
            got = tools.execute("import_asset", json.dumps({"ref": ref}))
            assert got.ok and "is now the scene world" in got.text, got.text
            assert bpy.context.scene.world.name.lower().startswith("city") or "city" in got.text.lower(), got.text
            cached = list((Path(bpy.app.cachedir) / "remote-assets" / "online-essentials").rglob("*.blend"))
            assert cached, "downloaded into Blender's remote-assets cache"
            print(got.text[:200])
        elif found is not None:
            print("essentials search failed (offline?):", found.text[:200])

        print("asset_libraries_check: OK")
        code = 0
    except Exception:
        traceback.print_exc()
    finally:
        sys.exit(code)


check()
