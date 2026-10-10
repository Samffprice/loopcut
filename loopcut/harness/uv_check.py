"""inspect_uv, uv_unwrap and inspect_texture on meshes built on the spot, headless, as a worker's
child Blender runs them.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/uv_check.py

Every unwrap method on a crate, a bottle and a ball, each audited after: all inside 0-1, no islands
overlapping, gutters at the padding; a density target met and one that cannot fit said so; a
shared atlas. A hand-made bad layout (a sign whose UVs run past the tile, a flipped face, two
islands stacked) reported as such, and a planner for objects with and without UVs. The checker:
an image, and every slot, active map and datablock put back. inspect_texture on a painted
material behind a Mapping node and a Mix: the path to Base Color, the UV map, samples at points
and faces read through the mapping, a hash that changes when paint lands, the thumbnail, and a UV
Map node naming a map the mesh lacks. Last, the orchestrator may not unwrap what a worker built,
the change tracker and the mesh token see an unwrap and paint (and not Edit Mode), and the tools
route through tools.execute.
"""
import contextlib
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import state, texture_tools, tools, uv_tools, worker_capture
from loopcut.tools import ToolError

work = tempfile.TemporaryDirectory(prefix="loopcut-uv-check-")
os.environ["LOOPCUT_DATA_DIR"] = str(Path(work.name) / "data")
tools._editor_context = lambda editor: contextlib.nullcontext()  # Headless: no windows to borrow.
worker_capture.configure("modeling", Path(work.name) / "looks")  # The checker renders as a worker's does.
scene = bpy.context.scene
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)


def mesh(kind, name, location=(0, 0, 0), keep_uvs=False, **options):
    getattr(bpy.ops.mesh, f"primitive_{kind}_add")(location=location, **options)
    obj = bpy.context.active_object
    obj.name = obj.data.name = name
    if not keep_uvs:
        while obj.data.uv_layers:
            obj.data.uv_layers.remove(obj.data.uv_layers[0])
    return obj


def audit(obj, resolution=1024, padding=8):
    return uv_tools.audit(*uv_tools._gather(obj, uv_tools._layer(obj).name), resolution, padding)


def unwrap(names, method, **options):
    started = time.monotonic()
    text = uv_tools.uv_unwrap(names, method, **options).text
    print(f"UV unwrap {method} {names} ({time.monotonic() - started:.2f} s):\n{text}")
    return text


def clean(obj, padding=8, resolution=1024):
    """Inside 0-1, nothing overlapping or folded, no zero-area face, gutters at the padding."""
    stats = audit(obj, resolution, padding)
    assert not stats["outside"] and not stats["zero"], (obj.name, stats["outside"][:5], stats["zero"][:5])
    assert not stats["overlapping"] and not stats["folded"], (obj.name, stats["overlapping"][:5], stats["folded"])
    if stats["gutter"] is not None:
        assert stats["gutter"][0] >= padding * 0.9, (obj.name, stats["gutter"])
    assert stats["border"] is None or stats["border"] >= padding / 2 * 0.9, (obj.name, stats["border"])
    return stats


# ------------------------------------------------------------------ no UVs yet, and the planner
crate = mesh("cube", "Crate", (0, 0, 1), size=2)
bottle = mesh("cylinder", "Bottle", (3, 0, 0.3), vertices=32, radius=0.1, depth=0.3)
ball = mesh("uv_sphere", "Ball", (5, 0, 0.5), segments=24, ring_count=12, radius=0.5)
text = uv_tools.inspect_uv(["Crate", "Bottle"]).text
print("UV no maps:\n" + text)
assert text.count("no UV map; uv_unwrap makes one") == 2, text
text = uv_tools.inspect_uv(["Crate", "Bottle", "Ball"], plan=True, density=200, padding=8).text
print("UV plan, no maps:\n" + text)
assert "Crate: 24.00 m2, 6 islands assumed" in text, text
assert "Shared atlas: 2048 fits" in text, text  # 27 m2 at 200 px/m: 1.1 Mpx, more than 0.75 of a 1024.
try:
    uv_tools.uv_unwrap(["Crate"], "planar")
    raise AssertionError("an unknown method must be refused")
except ToolError as ex:
    assert "method must be one of smart" in str(ex), ex

# ------------------------------------------------------------------ every method, audited after
unwrap(["Crate"], "smart")
stats = clean(crate)
assert stats["islands"] == 6 and stats["angle"] < 1.01, stats
unwrap(["Bottle"], "cylinder")
clean(bottle)
unwrap(["Ball"], "sphere")
clean(ball)
cube = mesh("cube", "Box", (7, 0, 0.5), size=1)
unwrap(["Box"], "cube")
clean(cube)
tube = mesh("cylinder", "Tube", (9, 0, 0.5), vertices=16, radius=0.2, depth=1)
text = unwrap(["Tube"], "angle_seams", angle=45)
assert "Marked 32 seams on Tube" in text, text  # The two rims; the wall is a closed tube with no seam yet...
assert "a closed tube or ball needs a seam along it" in text, text  # ...so it unrolls as a stretched ring.
tube.data.edges[next(e.index for e in tube.data.edges if abs(tube.data.vertices[e.vertices[0]].co.z
                                                            - tube.data.vertices[e.vertices[1]].co.z) > 0.5)].use_seam = True
text = unwrap(["Tube"], "seams")
assert "no seams marked" not in text, text
stats = clean(tube)
assert stats["islands"] == 3, stats  # Wall and two caps.
plain = mesh("cube", "Plain", (11, 0, 0.5), size=1)
assert "has no seams marked" in unwrap(["Plain"], "seams")

# ------------------------------------------------------------------ density: met, and too much for the tile
text = unwrap(["Crate"], "smart", density=100)
median = audit(crate)["density"][1]
assert "Islands scaled to 100 px/m" in text and abs(median - 100) < 3, (median, text)
clean(crate)
text = unwrap(["Crate"], "smart", density=1000)
assert "1000 px/m does not fit one 1024 px tile" in text and "a 8192 texture would hold it" in text, text

# ------------------------------------------------------------------ one shared atlas
text = unwrap(["Box", "Bottle"], "smart", atlas=True)
assert "one shared 0-1 layout" in text and "Shared layout:" in text and "no overlap" in text, text
shared = uv_tools.audit(*uv_tools._combined([cube, bottle], {cube: "UVMap", bottle: "UVMap"}))
assert not shared["overlapping"] and not shared["outside"], shared["overlapping"][:5]

# ------------------------------------------------------------------ a bad layout, measured as bad
sign = mesh("plane", "Sign", (0, 3, 1), size=1, keep_uvs=True)
layer = sign.data.uv_layers.active
layer.uv.foreach_set("vector", [0.8, 0.0, 1.6, 0.0, 1.6, 0.8, 0.8, 0.8])  # Past the tile's right edge.
text = uv_tools.inspect_uv(["Sign"]).text
print("UV bad sign:\n" + text)
assert "outside 0-1: 1 faces (u 0.80..1.60" in text and "tiles 1002" in text, text
quads = bpy.data.meshes.new("Stack")  # Four loose quads side by side: four islands whatever their UVs.
quads.from_pydata([(x + dx, dy, 0) for x in range(4) for dx, dy in ((0, 0), (1, 0), (1, 1), (0, 1))], [],
                  [tuple(range(4 * q, 4 * q + 4)) for q in range(4)])
quads.uv_layers.new(name="UVMap")
stack = bpy.data.objects.new("Stack", quads)
scene.collection.objects.link(stack)
uvs = [0.1, 0.1, 0.5, 0.1, 0.5, 0.5, 0.1, 0.5] * 3  # Three stacked on one spot...
uvs += [0.6, 0.6, 0.6, 0.9, 0.9, 0.9, 0.9, 0.6]  # ...and a fourth alone, wound backwards.
quads.uv_layers.active.uv.foreach_set("vector", uvs)
stats = audit(stack)
assert stats["islands"] == 4 and stats["flipped"] == [3], stats
assert stats["overlapping"] == [(0, 1), (0, 2), (1, 2)] and 0.55 < stats["overlap"] < 0.75, stats  # 0.16 of 0.25.
text = "\n".join(uv_tools.object_lines("Stack", "UVMap", stats, 8))
print("UV stacked:\n" + text)
assert "flipped: 3" in text and "of covered cells, islands at faces 0/1, 0/2, 1/2" in text, text
assert "min 0.0 px (islands at faces 0/1)" in text, text  # Stacked outlines touch: no gutter at all.

# ------------------------------------------------------------------ the checker, and everything put back
paint = bpy.data.materials.new("Paint")
crate.data.materials.append(paint)
extra = crate.data.uv_layers.new(name="Second")
crate.data.uv_layers.active_index = 1
images = lambda: set(bpy.data.images.keys()) - {"Render Result"}  # noqa: E731  A look's render buffer goes.
before = (set(bpy.data.materials.keys()), images(), crate.data.uv_layers.active_index,
          [s.material for s in crate.material_slots], len(bottle.material_slots))
result = uv_tools.inspect_uv(["Crate", "Bottle"], uv_map="", checker=True)
print("UV checker:\n" + result.text)
assert result.image_path and Path(result.image_path).is_file(), result.image_path
after = (set(bpy.data.materials.keys()), images(), crate.data.uv_layers.active_index,
         [s.material for s in crate.material_slots], len(bottle.material_slots))
assert before == after, (before, after)
assert "Image attached: Crate, Bottle wearing a temporary checker on UVMap" in result.text, result.text
look = bpy.data.images.load(str(result.image_path), check_existing=False)
import numpy as np
pixels = np.empty(look.size[0] * look.size[1] * 4, dtype=np.float32)
look.pixels.foreach_get(pixels)
bpy.data.images.remove(look)
colours = len({tuple(p) for p in (pixels.reshape(-1, 4)[:, :3] * 8).astype(int).tolist()})
assert colours > 12, f"a checker shows many colours; the image has {colours}"
crate.data.uv_layers.remove(extra)

# ------------------------------------------------------------------ inspect_texture
plane = mesh("plane", "Label", (0, 6, 1), size=1, keep_uvs=True)  # UVs 0-1 over the plane.
image = bpy.data.images.new("LabelPaint", 64, 32)
halves = np.zeros((32, 64, 4), dtype=np.float32)
halves[:, :32] = (1.0, 0.0, 0.0, 1.0)
halves[:, 32:] = (0.0, 1.0, 0.0, 1.0)
image.pixels.foreach_set(halves.ravel())
label = bpy.data.materials.new("LabelMat")
tree = label.node_tree
shader = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
node = tree.nodes.new("ShaderNodeTexImage")
node.name, node.image, node.interpolation = "Paint", image, "Closest"
coords = tree.nodes.new("ShaderNodeUVMap")
coords.uv_map = "UVMap"
shift = tree.nodes.new("ShaderNodeMapping")
shift.name = "Shift"
shift.inputs["Location"].default_value = (0.5, 0.0, 0.0)  # POINT: u + 0.5, so the left half reads green.
mix = tree.nodes.new("ShaderNodeMix")
mix.data_type, mix.name = "RGBA", "Tint"
tree.links.new(coords.outputs["UV"], shift.inputs["Vector"])
tree.links.new(shift.outputs["Vector"], node.inputs["Vector"])
tree.links.new(node.outputs["Color"], next(s for s in mix.inputs if s.name == "A" and s.enabled))
tree.links.new(next(s for s in mix.outputs if s.enabled), shader.inputs["Base Color"])
plane.data.materials.append(label)
result = texture_tools.inspect_texture("Label", faces=[0], uv=[[0.25, 0.5], [0.75, 0.5]])
print("TEXTURE label:\n" + result.text)
text = result.text
assert "Material LabelMat (on 1 faces):" in text and "- Paint: Color -> Tint.A -> Principled BSDF.Base Color" in text, text
assert "image LabelPaint: 64x32, sRGB" in text and "generated in memory, not packed or saved" in text, text
assert "UV map: UVMap via Mapping Shift (point: location 0.5, 0, 0); Closest, repeat" in text, text
assert "uv 0.25, 0.5: Paint 0.000 1.000 0.000 1.000; " not in text  # One binding: no trailing separator.
assert "uv 0.25, 0.5: Paint 0.000 1.000 0.000 1.000" in text and "uv 0.75, 0.5: Paint 1.000 0.000 0.000 1.000" in text, text
assert "face 0 (LabelMat, uv 0.500, 0.500): Paint centre" in text, text
digest = text.split("hash ")[1].split()[0].rstrip(",;")
halves[:4, :4] = (0.0, 0.0, 1.0, 1.0)  # A dab of paint in a corner.
image.pixels.foreach_set(halves.ravel())
again = texture_tools.inspect_texture("LabelPaint", thumbnail=True)
print("TEXTURE after paint:\n" + again.text)
assert f"hash {digest}" not in again.text and "hash " in again.text, (digest, again.text)
assert "used by LabelMat" in again.text and "unsaved pixel edits" in again.text, again.text
assert again.image_path and Path(again.image_path).is_file(), again.image_path
coords.uv_map = "Painted"
text = texture_tools.inspect_texture("Label").text
assert "UV map: Painted via Mapping Shift" in text and "Label has no such map: it renders with none" in text, text

# ------------------------------------------------------------------ the orchestrator's own objects only
session = state.session()
saved = session.get("turn"), session.get("worker_threads")
session["turn"] = types.SimpleNamespace(roles=types.SimpleNamespace(orchestrate=True), job=None)
session["worker_threads"] = {"crate-1": {"created": ["Crate"]}}
try:
    uv_tools.uv_unwrap(["Crate"], "smart")
    raise AssertionError("an orchestrator must not unwrap what a worker built")
except ToolError as ex:
    assert "worker crate-1 built Crate: send the unwrap back to it (continues crate-1)" in str(ex), ex
assert "Unwrapped Ball" in uv_tools.uv_unwrap(["Ball"], "sphere").text  # Nobody else's: its own to unwrap.
session["turn"], session["worker_threads"] = saved

# ------------------------------------------------------------------ the user in Edit Mode, and a worker's meshes
for obj in bpy.context.view_layer.objects:
    obj.select_set(obj == crate)
bpy.context.view_layer.objects.active = crate
bpy.ops.object.mode_set(mode="EDIT")  # Its data's UV layers read empty now; the edit mesh has them.
assert "Crate (UVMap): 6 faces, 6 islands" in uv_tools.inspect_uv(["Crate"]).text
assert "Unwrapped Crate, Ball" in unwrap(["Crate", "Ball"], "cube")
assert bpy.context.mode == "EDIT_MESH" and bpy.context.view_layer.objects.active == crate, bpy.context.mode
bpy.ops.object.mode_set(mode="OBJECT")
from loopcut import ownership, worker_child  # noqa: E402
crate.data[ownership.STAMP] = crate.data.name  # In the worker's copy since spawn, and not owned.
worker = types.SimpleNamespace(owned=set())
refusal = worker_child.Worker._not_mine(worker, {"objects": ["Crate", "Ball"]})
assert "the mesh of Crate is not yours" in refusal and "Ball" not in refusal, refusal
worker.owned = {ownership.key_of(crate.data)}
assert worker_child.Worker._not_mine(worker, {"objects": ["Crate"]}) == ""
del crate.data[ownership.STAMP]

# ------------------------------------------------------------------ the change tracker and the tokens see UVs and paint
from loopcut import scene_diff  # noqa: E402
before_token = ownership.token(crate.data)
result = uv_tools.uv_unwrap(["Crate"], "smart")
changes = scene_diff.diff(result.scene_before, result.scene_after)
print("UV scene changes after an unwrap:", changes["changed"])
assert "Crate: UVs edited (map UVMap)" in changes["changed"], changes
assert ownership.token(crate.data) != before_token, "an unwrap alone changes the mesh's token"
before_uvs, before = ownership.uv_maps(crate.data), scene_diff.snapshot()
bpy.ops.object.mode_set(mode="EDIT")  # The same UVs, read from the edit mesh: no change, the same hash.
assert ownership.uv_maps(crate.data) == before_uvs, (ownership.uv_maps(crate.data), before_uvs)
changes = scene_diff.diff(before, scene_diff.snapshot())
assert changes["changed"] == [] and changes["other"] == ["mode OBJECT -> EDIT_MESH"], changes
bpy.ops.object.mode_set(mode="OBJECT")
before = scene_diff.snapshot()
halves[4:8, :4] = (1.0, 1.0, 0.0, 1.0)  # Paint, as run_python would: no node changes.
image.pixels.foreach_set(halves.ravel())
changes = scene_diff.diff(before, scene_diff.snapshot())
print("UV scene changes after paint:", changes["changed"])
assert changes["changed"] == ["image LabelPaint: pixels edited (in LabelMat)"], changes
assert scene_diff.touched(before, scene_diff.snapshot()) == ["Label"], scene_diff.touched(before, scene_diff.snapshot())

# ------------------------------------------------------------------ the deliverable's measured UV line (facts.py)
from loopcut import facts  # noqa: E402
line = facts.uv_text("Crate", facts.deliverables([crate])["Crate"]["uv"])
print("UV facts:", line)
assert line.startswith("Crate UVs: all meshes mapped, 6 islands, all inside 0-1, no overlap, stretch 1.00") and "px/m" in line, line
bare = mesh("cube", "Bare", (0, -9, 1))
assert facts.uv_facts(bare) is None, "no map and no image: nothing to say"
bare.data.materials.append(label)
assert facts.uv_text("Bare", facts.uv_facts(bare)) == "Bare UVs: none on its 1 mesh, and its materials read images"
bpy.data.objects.remove(bare)

# ------------------------------------------------------------------ through tools.execute, and the cost on a dense mesh
result = tools.execute("inspect_uv", json.dumps({"objects": ["Crate"], "resolution": 2048}))
assert result.ok and "UV audit at 2048 px" in result.text, result.text
result = tools.execute("inspect_texture", json.dumps({"target": "Nothing"}))
assert not result.ok and "No object, material or image named 'Nothing'" in result.text, result.text
dense = mesh("uv_sphere", "Dense", (0, 9, 1), segments=128, ring_count=64, radius=0.5)
unwrap(["Dense"], "smart")
started = time.monotonic()
stats = audit(dense)
seconds = time.monotonic() - started
print(f"UV dense audit: {stats['faces']:,} faces in {seconds:.2f} s")
assert seconds < 5, seconds
print("UV OK")
