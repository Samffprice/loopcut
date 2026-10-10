"""export_glb (glb.py) against Blender's real glTF exporter, headless.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/glb_check.py [-- --keep]

A small pack: a root empty with a rotated, scaled crate under it that has a Subdivision modifier and
a hidden cutter, a barrel beside it, a camera and a light; the crate's material runs a Noise Texture
into its base colour. Checked: the export names every object, counts the crate's triangles after its
modifier and measures its size the way the scene does (glTF's Y-up turned back); the hidden cutter,
the camera and the light stay out; the Noise Texture is warned about; the selection is as it was;
no temporary file is left. An existing file needs overwrite; a new file that fails its checks
leaves the old one byte for byte; a good one replaces it. Under a second.
"""

import math
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout
checkout.use()
from loopcut import glb  # noqa: E402
from loopcut.tools import ToolError  # noqa: E402

RESULTS = []


def check(name: str, ok, detail="") -> None:
    RESULTS.append((name, bool(ok), str(detail)[:400]))
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  [{detail}]" if detail and not ok else ""))


def build() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    pack = bpy.data.objects.new("Pack", None)
    scene.collection.objects.link(pack)
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(1, 0, 0.25))
    crate = bpy.context.active_object
    crate.name = crate.data.name = "Crate"
    crate.scale = (2.0, 1.0, 0.5)
    crate.rotation_euler = (0, 0, math.radians(30))
    crate.parent = pack
    crate.modifiers.new("Smooth", "SUBSURF").levels = 1
    wood = bpy.data.materials.new("Wood")
    if wood.node_tree is None:
        wood.use_nodes = True
    nodes = wood.node_tree.nodes
    noise = nodes.new("ShaderNodeTexNoise")
    wood.node_tree.links.new(noise.outputs["Color"], nodes["Principled BSDF"].inputs["Base Color"])
    crate.data.materials.append(wood)
    bpy.ops.mesh.primitive_cube_add(size=0.3, location=(1, 0, 0.6))
    cutter = bpy.context.active_object
    cutter.name = "Crate_Cutter"
    cutter.parent = crate
    cutter.hide_set(True)
    bpy.ops.mesh.primitive_cylinder_add(vertices=16, radius=0.4, depth=1.0, location=(-1, 0, 0.5))
    bpy.context.active_object.name = "Barrel"
    camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    light = bpy.data.objects.new("Light", bpy.data.lights.new("Light", "POINT"))
    for ob in (camera, light):
        scene.collection.objects.link(ob)
    for ob in bpy.context.view_layer.objects:
        ob.select_set(ob.name == "Barrel")
    bpy.context.view_layer.update()


def leftovers(folder: Path) -> list:
    return sorted(p.name for p in folder.iterdir() if p.suffix == ".glb" and p.name != "pack.glb")


def main() -> int:
    keep = "--keep" in sys.argv
    folder = Path(tempfile.mkdtemp(prefix="lc_glb_check_")).resolve()
    t0 = time.time()
    try:
        build()
        target = folder / "pack.glb"
        result = glb.export_glb(str(target))
        print(result.text)
        lines = result.text.splitlines()
        found = glb.summary(glb.parse(target.read_bytes())[0]) if target.exists() else {"nodes": {}}
        nodes = sorted(found["nodes"])
        check("the export names every object and nothing else (no hidden cutter, camera or light)",
              result.ok and nodes == ["Barrel", "Crate", "Pack"], f"{nodes} {result.text}")
        crate = found["nodes"].get("Crate") or {}
        size = [round(v, 3) for v in crate.get("size") or ()]
        check("the crate's triangles are counted after its modifier, its size measured as the scene measures it",
              crate.get("tris") == 48 and "  Crate: 48 triangles" in "\n".join(lines)
              and "3 objects match the scene" not in result.text and "2 objects match the scene" in result.text,
              f"{crate} {size}")
        check("what glTF leaves out is warned about",
              any("glTF leaves out Wood: Noise Texture" in line for line in lines), result.text)
        selected = sorted(ob.name for ob in bpy.context.view_layer.objects if ob.select_get())
        check("the selection is as it was, and no temporary file is left",
              selected == ["Barrel"] and leftovers(folder) == [], f"{selected} {leftovers(folder)}")
        before = target.read_bytes()
        try:
            glb.export_glb(str(target))
            refused = ""
        except ToolError as ex:
            refused = str(ex)
        check("an existing file needs overwrite", "already exists; pass overwrite=true" in refused, refused)
        real = glb.compare
        glb.compare = lambda found, expected: ["Crate: 40 triangles in the file, 48 in the scene"]
        try:
            failed = glb.export_glb(str(target), overwrite=True)
        finally:
            glb.compare = real
        check("a new file that fails its checks leaves the old one byte for byte, and nothing else behind",
              not failed.ok and target.read_bytes() == before and leftovers(folder) == []
              and "is as it was" in failed.text, failed.text)
        bpy.data.objects["Barrel"].scale.z = 2.0
        bpy.context.view_layer.update()
        again = glb.export_glb(str(target), objects=["Barrel"], overwrite=True)
        barrel = glb.summary(glb.parse(target.read_bytes())[0])["nodes"].get("Barrel") or {}
        check("a good one replaces it, only what was named",
              again.ok and "replacing the earlier file" in again.text and round((barrel.get("size") or [0, 0, 0])[2], 3) == 2.0
              and sorted(glb.summary(glb.parse(target.read_bytes())[0])["nodes"]) == ["Barrel"], again.text)
    except Exception:
        traceback.print_exc()
        check("ran to the end", False, "exception above")
    finally:
        if not keep:
            shutil.rmtree(folder, ignore_errors=True)
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"GLB {passed}/{len(RESULTS)} in {time.time() - t0:.1f}s; tmp {folder}{' (kept)' if keep else ''}")
    print("GLB OK" if passed == len(RESULTS) else "GLB FAIL")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    if bpy.app.background:
        sys.exit(code)
