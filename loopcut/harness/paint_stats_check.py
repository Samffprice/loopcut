"""Paint stats in Blender, headless: inspect_texture, the facts a worker reports and its check_brief
lines on a mesh built on the spot, and the calibration blends.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/paint_stats_check.py
    Blender -b some.blend --factory-startup --python loopcut/harness/paint_stats_check.py -- --calibrate [--max-texels N]

The built mesh: three 1 x 3 m quads side by side, a material slot each, sharing one 2048 px image
through a Principled Base Color, under an orthographic camera: designed patches (three close
values, big and hard-edged, with value-matched pink accents), the same patches under fine noise,
and saturated violet accents far lighter than their brass. Each slot must read its own verdict,
facts must carry the parts and the orchestrator's summary, the worker's lines must name the problem
parts, and the call must stay fast. --calibrate prints every mesh's paint lines in the open blend
(its own scene camera) with the time each took. A 4608 px image, past texture_tools.MAX_TEXELS and
never saved, is measured on its reduced copy. --max-texels raises MAX_TEXELS (full-resolution
reads, as the first calibration of the 4.8-6K chest and shelf images did).
"""
import contextlib
import math
import os
import sys
import tempfile
import time
from pathlib import Path

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import facts, paint_stats, texture_tools, tools, worker_capture, worker_child

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []


def calibrate():
    if "--max-texels" in ARGS:
        texture_tools.MAX_TEXELS = int(ARGS[ARGS.index("--max-texels") + 1])
    scene = bpy.context.scene
    for obj in sorted((o for o in scene.objects if o.type == "MESH"), key=lambda o: o.name):
        started = time.perf_counter()
        pixels = texture_tools._Pixels()
        lines = paint_stats.object_lines(obj, pixels, scene)
        took = time.perf_counter() - started
        skipped = sorted(set(pixels.why.values()))
        if lines or skipped:
            print(f"== {obj.name} ({took:.2f} s){': ' + '; '.join(skipped) if skipped else ''}")
            print("\n".join(lines))


if "--calibrate" in ARGS:
    calibrate()
    sys.exit(0)

work = tempfile.TemporaryDirectory(prefix="loopcut-paint-check-")
os.environ["LOOPCUT_DATA_DIR"] = str(Path(work.name) / "data")
tools._editor_context = lambda editor: contextlib.nullcontext()  # Headless: no windows to borrow.
scene = bpy.context.scene
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)

SIZE = 2048
M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929], [0.2119034982, 0.6806995451, 0.1073969566],
               [0.0883024619, 0.2817188376, 0.6299787005]])
M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468], [1.9779984951, -2.4285922050, 0.4505937099],
               [0.0259040371, 0.7827717662, -0.8086757660]])


def srgb(L, hue, chroma):
    """OKLab lightness, hue (degrees) and chroma to sRGB-encoded RGB."""
    L, hue, chroma = np.broadcast_arrays(L, np.radians(hue), chroma)
    lab = np.stack([L, chroma * np.cos(hue), chroma * np.sin(hue)], axis=-1)
    rgb = np.clip(((lab @ np.linalg.inv(M2).T) ** 3) @ np.linalg.inv(M1).T, 0, 1)
    return np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * rgb ** (1 / 2.4) - 0.055)


def painting() -> np.ndarray:
    """Three thirds, bottom row first as Blender stores pixels: designed, noisy, popping accents."""
    third = SIZE // 3
    y, x = np.mgrid[:SIZE, :third].astype(np.float64)
    L = np.full(x.shape, 0.58)
    L[(x + 0.3 * y) < third * 0.6] = 0.45
    L[(x - 0.2 * y) > third * 0.5] = 0.72
    blobs = ((x % 90) < 24) & ((y % 110) < 30)
    designed = srgb(L, np.where(blobs, 350.0, 80.0), 0.08)  # Pink glaze on brass, value-matched.
    noisy = srgb(L + np.random.default_rng(0).normal(0, 0.12, L.shape), 80.0, 0.08)
    popping = srgb(np.where(blobs, np.minimum(L + 0.25, 0.97), L), np.where(blobs, 295.0, 80.0),
                   np.where(blobs, 0.2, 0.08))
    image = np.zeros((SIZE, SIZE, 4))
    image[..., 3] = 1.0
    image[:, :3 * third, :3] = np.concatenate([designed, noisy, popping], axis=1)
    return image


image = bpy.data.images.new("Shop_Paint", SIZE, SIZE)
image.pixels.foreach_set(painting().astype(np.float32).ravel())
mesh = bpy.data.meshes.new("Panel")
mesh.from_pydata([(i, y, 0) for i in range(4) for y in (0, 3)], [],
                 [(2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1) for i in range(3)])
layer = mesh.uv_layers.new(name="PaintUV")
for polygon in mesh.polygons:
    i = polygon.index
    for loop, (u, v) in zip(polygon.loop_indices, ((i / 3, 0), ((i + 1) / 3, 0), ((i + 1) / 3, 1), (i / 3, 1))):
        layer.uv[loop].vector = (u, v)
    polygon.material_index = i
panel = bpy.data.objects.new("Panel", mesh)
scene.collection.objects.link(panel)
for name in ("Panel_Designed", "Panel_Noisy", "Panel_Accents"):
    material = bpy.data.materials.new(name)
    tree = material.node_tree
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    tree.links.new(node.outputs["Color"], tree.nodes["Principled BSDF"].inputs["Base Color"])
    panel.data.materials.append(material)
camera = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
camera.data.type, camera.data.ortho_scale = "ORTHO", 3.4
camera.location = (1.5, 1.5, 5)
scene.collection.objects.link(camera)
scene.camera = camera
scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1920, 1080, 100

# ------------------------------------------------------------------ inspect_texture
started = time.perf_counter()
text = tools.execute("inspect_texture", '{"target": "Panel"}').text
took = time.perf_counter() - started
print(f"inspect_texture Panel ({took:.2f} s, one 2048 px image, three parts):\n{text}")
lines = {line.split(":")[0].strip(): line for line in text.splitlines() if line.startswith("  Panel_")}
assert "Paint stats per material slot (advisory; OKLab lightness over its UV area; sizes in screen px" in text, text
assert lines["Panel_Designed"].endswith("- reads designed"), lines
assert "3 values 1:1.6" in lines["Panel_Designed"], lines
assert "noise-like" in lines["Panel_Noisy"], lines
assert "accents pop" in lines["Panel_Accents"] and "noise-like" not in lines["Panel_Accents"], lines
px = 1920 / 3.4 / (SIZE / 3)  # Screen px a metre over texels a metre.
assert took < 3.0, f"inspect_texture took {took:.2f} s on a 2K image"
material_text = tools.execute("inspect_texture", '{"target": "Panel_Noisy"}').text
assert "Paint stats" not in material_text, "only an object's slots are measured: a material has no UV area of its own"

# ------------------------------------------------------------------ what a worker reports
measured = facts.deliverables([panel])
paint = measured["Panel"]["paint"]
assert [p["part"] for p in paint] == ["Panel_Designed", "Panel_Noisy", "Panel_Accents"], paint
assert all(abs(p["px"] - px) < 0.02 for p in paint), (px, paint)
summary = paint_stats.text_of("Panel", paint)
print(f"orchestrator: {summary}")
assert summary.startswith("Panel paint (3 parts, advisory): "), summary
worker = paint_stats.brief_lines(paint_stats.measured([panel], scene))
print("worker check_brief:\n" + "\n".join(worker))
assert worker[-1].startswith("- reads designed: Panel_Designed"), worker
assert any(line.startswith("- Panel_Noisy:") for line in worker), worker
worker_capture._role.update(deliverable="Panel", owned=set())  # What a worker's child holds for its brief.
brief = worker_child._paint_text()
assert brief.startswith("\nPaint stats (measured, advisory") and "- Panel_Accents:" in brief, brief

# ------------------------------------------------------------------ past MAX_TEXELS: a reduced copy, unsaved paint read
BIG = 4608
big = bpy.data.images.new("Crate_Paint", BIG, BIG)  # Generated and never saved: only its pixels hold the paint.
y, x = np.mgrid[:BIG, :BIG]
big_pixels = srgb(np.where((x // 600 + y // 600) % 2, 0.62, 0.5), 80.0, 0.08)
big_pixels = np.concatenate([big_pixels, np.ones((BIG, BIG, 1))], axis=-1).astype(np.float32)
big.pixels.foreach_set(big_pixels.ravel())
del big_pixels, y, x
crate = bpy.data.objects.new("Crate", panel.data.copy())
crate.location.x = 4
scene.collection.objects.link(crate)
material = bpy.data.materials.new("Crate_Wood")
node = material.node_tree.nodes.new("ShaderNodeTexImage")
node.image = big
material.node_tree.links.new(node.outputs["Color"], material.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
crate.data.materials.clear()
crate.data.materials.append(material)
for polygon in crate.data.polygons:
    polygon.material_index = 0
started = time.perf_counter()
text = tools.execute("inspect_texture", '{"target": "Crate"}').text
took = time.perf_counter() - started
print(f"inspect_texture Crate ({took:.2f} s, one {BIG} px image past MAX_TEXELS):\n" + text.split("Paint stats")[-1])
assert f"too big to read here ({BIG}x{BIG}); samples and hash skipped" in text, text
assert "Crate_Wood: 2 values 1:1.2" in text and "restrained" in text, text
assert took < 5.0, f"{took:.2f} s"

# ------------------------------------------------------------------ edit mode and no camera
scene.camera = None
text = tools.execute("inspect_texture", '{"target": "Panel"}').text
assert "sizes in image texels (no camera)" in text and "texels, noise" in text, text
bpy.context.view_layer.objects.active = panel
bpy.ops.object.mode_set(mode="EDIT")
assert paint_stats.object_lines(panel, texture_tools._Pixels(), scene) == []
bpy.ops.object.mode_set(mode="OBJECT")
print("paint_stats_check: all passed")
