"""transfer_textures (C40) on a textured prop built on the spot, headless.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/uv_transfer_check.py

A subdivided cube painted for its own UV map gets a new layout (Smart UV Project into a second map),
then transfer_textures moves its images across. The same surface points (barycentric points on every
triangle) sample the same colour and alpha on the new layout as on the old; the tangent-space normal
map gives the same shading normals (object-space normal bakes of both, compared); the materials read
the new images through the new map; a tiling texture is left alone; the scene's engine, selection,
slots and maps are put back, with no temporary datablock left. Refusals: a material shared with an
object not in the call, a missing map. uv_unwrap over a painted map keeps the old layout, and the
transfer from it puts the paint back. The op note fires for an unwrap in Edit Mode on a painted map.
"""
import contextlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import op_notes, tools, uv_tools
from loopcut.tools import ToolError

work = tempfile.TemporaryDirectory(prefix="loopcut-uv-transfer-")
os.environ["LOOPCUT_DATA_DIR"] = str(Path(work.name) / "data")
tools._editor_context = lambda editor: contextlib.nullcontext()  # Headless: no windows to borrow.
scene = bpy.context.scene
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
scene.render.engine = "BLENDER_EEVEE"
SIZE = 256


def image(name, fill, colorspace="sRGB", alpha=False):
    """An image whose pixels are fill(u, v) -> (h, w, 4), u and v at pixel centres."""
    img = bpy.data.images.new(name, SIZE, SIZE, alpha=alpha)
    img.colorspace_settings.name = colorspace
    centres = (np.arange(SIZE) + 0.5) / SIZE
    u, v = np.meshgrid(centres, centres)
    img.pixels.foreach_set(fill(u, v).astype(np.float32).ravel())
    img.pack()
    return img


def rgba(*channels):
    return np.stack([np.broadcast_to(c, channels[0].shape) for c in channels], axis=-1)


def node(tree, kind, **values):
    n = tree.nodes.new(kind)
    for key, value in values.items():
        setattr(n, key, value)
    return n


def prop(name, location):
    bpy.ops.mesh.primitive_cube_add(location=location)
    obj = bpy.context.active_object
    obj.name = obj.data.name = name
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.subdivide(number_cuts=3)
    bpy.ops.object.mode_set(mode="OBJECT")
    obj.data.uv_layers[0].name = "UVMap"
    return obj


paint = image("Paint", lambda u, v: rgba(0.15 + 0.7 * u, 0.15 + 0.7 * v, 0.5 + 0.3 * np.sin(6 * u), np.ones_like(u)))
normal = image("Nor", lambda u, v: rgba(0.5 + 0.25 * np.sin(2 * math.pi * u), 0.5 + 0.25 * np.cos(2 * math.pi * v),
                                        np.full_like(u, 0.85), np.ones_like(u)), "Non-Color")
decal = image("Decal", lambda u, v: rgba(np.full_like(u, 0.8), 0.2 + 0.6 * v, np.full_like(u, 0.3), 0.2 + 0.7 * u),
              alpha=True)
tile = image("Tile", lambda u, v: rgba(u, u, v, np.ones_like(u)))

obj = prop("Prop", (0, 0, 0))
mat = bpy.data.materials.new("PropMat")
mat.use_nodes = True
tree = mat.node_tree
bsdf = tree.nodes["Principled BSDF"]
paint_node = node(tree, "ShaderNodeTexImage", image=paint)
tree.links.new(paint_node.outputs["Color"], bsdf.inputs["Base Color"])
nor_node = node(tree, "ShaderNodeTexImage", image=normal)
normal_map = node(tree, "ShaderNodeNormalMap", space="TANGENT", uv_map="UVMap")
tree.links.new(nor_node.outputs["Color"], normal_map.inputs["Color"])
tree.links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])
decal_node = node(tree, "ShaderNodeTexImage", image=decal)
coords = node(tree, "ShaderNodeUVMap", uv_map="UVMap")
tree.links.new(coords.outputs["UV"], decal_node.inputs["Vector"])
tree.links.new(decal_node.outputs["Color"], bsdf.inputs["Emission Color"])
tree.links.new(decal_node.outputs["Alpha"], bsdf.inputs["Alpha"])
tile_node = node(tree, "ShaderNodeTexImage", image=tile, extension="REPEAT")
mapping = node(tree, "ShaderNodeMapping")
mapping.inputs["Scale"].default_value = (4, 4, 4)
tree.links.new(node(tree, "ShaderNodeTexCoord").outputs["UV"], mapping.inputs["Vector"])
tree.links.new(mapping.outputs["Vector"], tile_node.inputs["Vector"])
tree.links.new(tile_node.outputs["Color"], bsdf.inputs["Specular Tint"])
obj.data.materials.append(mat)
reference = mat.copy()  # The material as painted, to compare shading normals against: it reads UVMap by name.
reference.name = "PropMat as painted"
for held in [n for n in reference.node_tree.nodes if n.type == "TEX_IMAGE" and not n.inputs["Vector"].is_linked]:
    reference.node_tree.links.new(node(reference.node_tree, "ShaderNodeUVMap", uv_map="UVMap").outputs["UV"],
                                  held.inputs["Vector"])

# A new layout in a second map: Smart UV Project rotates and rearranges the islands.
obj.data.uv_layers.new(name="New", do_init=False)
obj.data.uv_layers.active = obj.data.uv_layers["New"]
bpy.ops.object.select_all(action="DESELECT")
obj.select_set(True)
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.select_all(action="SELECT")
bpy.ops.uv.smart_project(angle_limit=math.radians(40), island_margin=0.03)
assert op_notes._painted_map({}) is False, "the note fires for a map no image reads (New is not read yet)"
bpy.ops.object.mode_set(mode="OBJECT")
obj.data.uv_layers.active = obj.data.uv_layers["UVMap"]
obj.data.uv_layers["UVMap"].active_render = True


def corners(mesh, name):
    uv = np.empty(len(mesh.loops) * 2, np.float32)
    mesh.uv_layers[name].uv.foreach_get("vector", uv)
    return uv.reshape(-1, 2)


def points(mesh, old, new):
    """The same surface points on two maps: barycentric points on every loop triangle."""
    mesh.calc_loop_triangles()
    loops = np.array([t.loops[:] for t in mesh.loop_triangles])
    weights = np.array([(1 / 3, 1 / 3, 1 / 3), (0.6, 0.2, 0.2), (0.2, 0.6, 0.2), (0.2, 0.2, 0.6)])
    return (np.einsum("pk,tkc->tpc", weights, old[loops]).reshape(-1, 2),
            np.einsum("pk,tkc->tpc", weights, new[loops]).reshape(-1, 2))


def sample(img, uv):
    """Bilinear, pixel centres at (i + 0.5) / size, edges clamped; stored values (no colour transform)."""
    w, h = img.size
    pixels = np.empty(w * h * 4, np.float32)
    img.pixels.foreach_get(pixels)
    pixels = pixels.reshape(h, w, 4)
    x, y = np.clip(uv[:, 0] * w - 0.5, 0, w - 1), np.clip(uv[:, 1] * h - 0.5, 0, h - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    top = pixels[y0, x0] * (1 - fx) + pixels[y0, x1] * fx
    bottom = pixels[y1, x0] * (1 - fx) + pixels[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


def same_paint(label, old_image, new_image, mesh, old_map, new_map, channels=slice(0, 3), limit=0.03):
    old_uv, new_uv = points(mesh, corners(mesh, old_map), corners(mesh, new_map))
    difference = np.abs(sample(old_image, old_uv)[:, channels] - sample(new_image, new_uv)[:, channels])
    mean, p95 = float(difference.mean()), float(np.percentile(difference, 95))
    print(f"{label}: mean difference {mean:.4f}, 95th percentile {p95:.4f} over {len(old_uv)} points")
    assert mean < limit and p95 < 3 * limit, f"{label}: the artwork moved (mean {mean:.4f}, p95 {p95:.4f})"


# ------------------------------------------------------------------ the transfer
scene.render.bake.normal_g = "NEG_Y"  # A game-engine scene: the tool must still bake Blender's convention.
bpy.ops.object.select_all(action="DESELECT")
selected_before, active_before = ["Prop"], obj
obj.select_set(True)
obj.hide_select = True               # The bake has to lift it, and put it back.
bpy.context.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode="EDIT")  # Called from Edit Mode, where the mesh's UV maps are out of reach.
materials_before = sorted(m.name for m in bpy.data.materials)
images_before = sorted(i.name for i in bpy.data.images)
assert tools.is_heavy("transfer_textures", {}) and "transfer_textures" in tools.CHANGES_SCENE
result = tools.execute("transfer_textures", json.dumps({"objects": ["Prop"], "from_uv": "UVMap", "to_uv": "New"}))
print(result.text)
assert result.ok, result.text
assert "Moved 3 images" in result.text and "Tile in PropMat repeats" in result.text, result.text
assert bpy.context.object.mode == "EDIT", "Edit Mode was not put back"
bpy.ops.object.mode_set(mode="OBJECT")
assert obj.hide_select and scene.render.bake.normal_g == "NEG_Y", "hide_select or the scene's swizzle was not put back"
obj.hide_select = False
assert bpy.data.images["Decal New"].alpha_mode == "STRAIGHT"
new = {n: bpy.data.images[f"{n} New"] for n in ("Paint", "Nor", "Decal")}
assert all(i.packed_file is not None for i in new.values()), "the new images are not packed"
assert new["Paint"].colorspace_settings.name == "sRGB" and new["Nor"].colorspace_settings.name == "Non-Color"
same_paint("Base colour", paint, new["Paint"], obj.data, "UVMap", "New")
same_paint("Decal colour", decal, new["Decal"], obj.data, "UVMap", "New")
same_paint("Decal alpha", decal, new["Decal"], obj.data, "UVMap", "New", channels=slice(3, 4))

# The materials read the new images through the new map; the tiling texture and its Mapping are untouched.
for n, image_node in (("Paint", paint_node), ("Nor", nor_node), ("Decal", decal_node)):
    assert image_node.image == new[n], f"{n}'s node still reads {image_node.image.name}"
    source = image_node.inputs["Vector"].links[0].from_node
    assert source.type == "UVMAP" and source.uv_map == "New", f"{n}'s node reads through {source.name}"
assert normal_map.uv_map == "New", normal_map.uv_map
assert tile_node.image == tile and tile_node.inputs["Vector"].links[0].from_node == mapping
layers = obj.data.uv_layers
assert layers.active.name == "New" and layers["New"].active_render and layers.get("UVMap") is not None

# Put back: engine, selection, active object, slots; nothing temporary left.
assert scene.render.engine == "BLENDER_EEVEE", scene.render.engine
obj.select_set(True)  # hide_select dropped the selection flag's effect while set; compare as the user left it.
assert sorted(o.name for o in bpy.context.view_layer.objects if o.select_get()) == selected_before
assert bpy.context.view_layer.objects.active == active_before
assert [s.material.name for s in obj.material_slots] == ["PropMat"]
assert sorted(m.name for m in bpy.data.materials) == materials_before, "a temporary material was left"
assert sorted(i.name for i in bpy.data.images) == sorted(images_before + [i.name for i in new.values()])


# ------------------------------------------------------------------ the same shading normals
def bake_object_normals(material, target_map):
    out = bpy.data.images.new(f"check {material.name}", 128, 128, float_buffer=True)
    out.colorspace_settings.name = "Non-Color"
    check = material.copy()
    for link in [l for l in check.node_tree.links if l.to_socket.name == "Alpha"]:
        check.node_tree.links.remove(link)  # Shading normals only.
    held = check.node_tree.nodes.new("ShaderNodeTexImage")
    held.image = out
    check.node_tree.nodes.active = held
    obj.material_slots[0].material = check
    obj.data.uv_layers.active = obj.data.uv_layers[target_map]
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 4
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.bake(type="NORMAL", normal_space="OBJECT", margin=0, use_clear=True, target="IMAGE_TEXTURES")
    pixels = np.empty(128 * 128 * 4, np.float32)
    out.pixels.foreach_get(pixels)
    obj.material_slots[0].material = mat
    return pixels.reshape(-1, 4)[:, :3] * 2 - 1


painted = bake_object_normals(reference, "New")   # The original normal map on the old layout ...
moved = bake_object_normals(mat, "New")           # ... and the moved one on the new, both baked on New.
covered = np.linalg.norm(painted, axis=1) > 0.5
dots = np.sum(painted[covered] * moved[covered], axis=1) / (
    np.linalg.norm(painted[covered], axis=1) * np.linalg.norm(moved[covered], axis=1))
plain = bpy.data.materials.new("flat")
plain.use_nodes = True
flat = np.sum(painted[covered] * bake_object_normals(plain, "New")[covered], axis=1)
print(f"Shading normals: mean cosine {dots.mean():.4f} (5th percentile {np.percentile(dots, 5):.4f}) over "
      f"{covered.sum()} texels; the normal map tilts them by {math.degrees(math.acos(min(1, np.median(np.abs(flat))))):.1f} deg")
assert dots.mean() > 0.995 and np.percentile(dots, 5) > 0.98, "the normal map no longer gives the same normals"
scene.render.engine = "BLENDER_EEVEE"

# ------------------------------------------------------------------ refusals
other = prop("Other", (4, 0, 0))
other.data.materials.append(mat)
other.data.uv_layers.new(name="New")
try:
    uv_tools.transfer_textures(["Prop"], "UVMap", "New")
    raise AssertionError("a material shared with an object not in the call was not refused")
except ToolError as ex:
    assert "also on Other" in str(ex), ex
    print("Refused:", ex)
try:
    uv_tools.transfer_textures(["Prop"], "UVMap", "Missing")
    raise AssertionError("a missing map was not refused")
except ToolError as ex:
    assert "no UV map Missing" in str(ex), ex
bpy.data.objects.remove(other)

# ------------------------------------------------------------------ uv_unwrap over a painted map keeps the old layout
box = prop("Box", (0, 4, 0))
box_mat = bpy.data.materials.new("BoxMat")
box_mat.use_nodes = True
box_paint = node(box_mat.node_tree, "ShaderNodeTexImage", image=paint)
box_mat.node_tree.links.new(box_paint.outputs["Color"], box_mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
box.data.materials.append(box_mat)
text = uv_tools.uv_unwrap(["Box"], "smart").text
print(text)
assert "kept as UVMap before unwrap" in text, text
layers = box.data.uv_layers
assert layers.get("UVMap before unwrap") is not None and layers["UVMap"].active_render
assert not np.allclose(corners(box.data, "UVMap"), corners(box.data, "UVMap before unwrap")), "nothing was unwrapped"
text = uv_tools.transfer_textures(["Box"], "UVMap before unwrap", "UVMap").text
print(text)
assert "Moved 1 image" in text and "renamed UVMap painted" in text, text
same_paint("Box after unwrap and transfer", paint, box_paint.image, box.data, "UVMap painted", "UVMap")
moved = box_paint.image
first_layout = corners(box.data, "UVMap").copy()
text = uv_tools.uv_unwrap(["Box"], "angle_seams", angle=20).text
assert "kept as UVMap before unwrap" in text, text  # A fresh copy: the layout the moved images were baked for.
assert not np.allclose(corners(box.data, "UVMap"), first_layout), "the second unwrap made the same layout"
text = uv_tools.transfer_textures(["Box"], "UVMap before unwrap", "UVMap").text
assert "Moved 1 image" in text and "overlapping" not in text and "renamed UVMap painted 2" in text, text
assert [l.name for l in box.data.uv_layers] == ["UVMap", "UVMap painted", "UVMap painted 2"], \
    [l.name for l in box.data.uv_layers]  # No map lost to a rename.
same_paint("Box after a second unwrap and transfer", paint, box_paint.image, box.data, "UVMap painted", "UVMap")
assert box_paint.image != moved
stacked = box.data.uv_layers.new(name="Stacked", do_init=False).name  # Every face on the whole tile, stacked.
for polygon in box.data.polygons:
    for corner, loop in zip(((0, 0), (1, 0), (1, 1), (0, 1)), polygon.loop_indices):
        box.data.uv_layers[stacked].uv[loop].vector = corner
text = uv_tools.transfer_textures(["Box"], "UVMap", "Stacked").text
assert "overlapping islands" in text, text  # The transfer says what it can't keep.
print("Stacked layout:", text.splitlines()[-1][:160])

# ------------------------------------------------------------------ two meshes, one image, overlapping new layouts
pair_mat = bpy.data.materials.new("PairMat")
pair_mat.use_nodes = True
pair_node = node(pair_mat.node_tree, "ShaderNodeTexImage", image=paint)
pair_mat.node_tree.links.new(pair_node.outputs["Color"], pair_mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
pair = [prop(name, (0, 8 + 3 * k, 0)) for k, name in enumerate(("Left", "Right"))]
for piece in pair:
    piece.data.materials.append(pair_mat)
    piece.data.uv_layers.new(name="Fresh", do_init=True)  # Both the cube's own layout: the same place in 0-1.
try:
    uv_tools.transfer_textures(["Left", "Right"], "UVMap", "Fresh")
    raise AssertionError("two meshes over each other on the new layout were not refused")
except ToolError as ex:
    assert "overlap" in str(ex), ex
    print("Refused:", ex)

# ------------------------------------------------------------------ the op note in Edit Mode on a painted map
bpy.ops.object.select_all(action="DESELECT")
box.select_set(True)
bpy.context.view_layer.objects.active = box
bpy.ops.object.mode_set(mode="EDIT")
assert op_notes._painted_map({}) is True, "no note for an unwrap of a painted map"
bpy.ops.object.mode_set(mode="OBJECT")
print("UV TRANSFER CHECK OK")
