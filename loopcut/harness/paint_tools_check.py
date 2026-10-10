"""bake_paint_masks and stylize_material in Blender, headless, on a small UV'd scene built on the spot:
a bevelled box with two material slots (Wood sides, Brass top) beside a post that shares Wood, a key and
a rim light, a camera.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/paint_tools_check.py [-- --no-child]

- bake: every part's mask, pos and id are float Non-Color images of the asked size; id covers the part
  (about its UV area), pos spans the part's box in metres, the key faces the top, edges sit on the bevel;
  the materials, meshes and objects keep their ownership tokens; the render engine, selection, active
  object and active UV map are put back; refusals for a mesh with no UV map, a light, a bad key, a bad
  resolution; the time it took;
- stylize: a material shared with the post is copied, a palette (stamped) one too even with one user, the
  post's own one rebuilt in place; a second call rebuilds the same named nodes; a flat #rrggbb part; a
  normal image left sRGB is made Non-Color; bad image names, slots and steps refused;
- Cycles and EEVEE render it (the unlit half shows: pixels move against the PBR half alone); glTF export carries
  base colour, metallic-roughness, normal and occlusion from the painted images;
- in a worker's child Blender (worker_host.Child): both tools run, nothing is rolled back, the palette
  copy and the paint images are created, masks left behind are not, stylize on a mesh the brief does not
  own is refused.
Prints PAINT TOOLS OK, or raises.
"""

import json
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
from loopcut import ownership, paint_tools, tools, uv_tools, worker_host  # noqa: E402

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
work = Path(tempfile.mkdtemp(prefix="loopcut-paint-tools-check-"))
os.environ["LOOPCUT_DATA_DIR"] = str(work / "data")
RES = 256


def call(name, **arguments):
    return tools.execute(name, json.dumps(arguments))


def pixels(name):
    image = bpy.data.images[name]
    w, h = image.size
    values = np.empty(w * h * 4, np.float32)
    image.pixels.foreach_get(values)
    return values.reshape(h, w, 4)


def material(name, rgb):
    m = bpy.data.materials.new(name)
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (*rgb, 1)
    return m


def build():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.25))
    box = bpy.context.active_object
    box.name = box.data.name = "Box"
    box.scale = (1.0, 0.6, 0.5)
    bpy.ops.object.transform_apply(scale=True)
    bevel = box.modifiers.new("Bevel", "BEVEL")
    bevel.width, bevel.segments, bevel.limit_method = 0.02, 2, "ANGLE"
    wood, brass = material("Wood", (0.2, 0.08, 0.03)), material("Brass", (0.5, 0.35, 0.08))
    box.data.materials.append(wood)
    box.data.materials.append(brass)
    for face in box.data.polygons:
        face.material_index = 1 if face.normal.z > 0.5 else 0
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.08, depth=0.6, location=(0.8, 0, 0.3))
    post = bpy.context.active_object
    post.name = post.data.name = "Post"
    post.data.materials.append(wood)
    bpy.ops.mesh.primitive_plane_add(size=4)
    bpy.context.active_object.name = "Floor"
    for name, location, energy in (("Key", (-1.5, -2.0, 2.5), 300), ("Rim", (1.5, 2.0, 1.5), 100)):
        light = bpy.data.objects.new(name, bpy.data.lights.new(name, "AREA"))
        light.data.energy = energy
        scene.collection.objects.link(light)
        light.location = location
        light.rotation_euler = (Vector((0, 0, 0.3)) - Vector(location)).to_track_quat("-Z", "Y").to_euler()
    camera = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scene.collection.objects.link(camera)
    camera.location = (2.2, -2.6, 1.6)
    camera.rotation_euler = (Vector((0.3, 0, 0.25)) - camera.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = camera
    scene.render.resolution_x, scene.render.resolution_y = 160, 120
    world = bpy.data.worlds.new("World")
    scene.world = world
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.03, 0.03, 0.025, 1)
    assert "Unwrapped Box, Post" in uv_tools.uv_unwrap(["Box", "Post"], "smart", resolution=RES).text
    box.data.uv_layers.new(name="Junk")
    box.data.uv_layers.active = box.data.uv_layers["Junk"]  # Active, not the render map: the bake puts it back.
    return scene, box, post, wood, brass


def tokens():
    return {ownership.key_of(i): ownership.token(i) for i in (*bpy.data.materials, *bpy.data.meshes, *bpy.data.objects)}


# ------------------------------------------------------------------ bake_paint_masks
scene, box, post, wood, brass = build()
scene.render.engine = "BLENDER_EEVEE"
for obj in scene.objects:
    obj.select_set(obj.name == "Floor")
scene.view_layers[0].objects.active = scene.objects["Floor"]
before = tokens()
started = time.perf_counter()
baked = call("bake_paint_masks", objects=["Box", "Post"], key="Key", resolution=RES)
took = time.perf_counter() - started
assert baked.ok, baked.text
print(baked.text)
assert tokens() == before, "the bake changed a material, mesh or object"
assert scene.render.engine == "BLENDER_EEVEE" and [o.name for o in scene.objects if o.select_get()] == ["Floor"]
assert scene.view_layers[0].objects.active.name == "Floor" and box.data.uv_layers.active.name == "Junk"
assert [s.material for s in box.material_slots] == [wood, brass] and not [m for m in bpy.data.materials
                                                                           if m.name.startswith("lc_paint_bake")]
assert "Baked 3 parts at 256 px" in baked.text and "area light Key" in baked.text, baked.text
assert "Cam: " in baked.text and "texels a screen px" in baked.text and "Box_1 (slot 1, Brass)" in baked.text
assert took < 30, took
for label in ("Box_0", "Box_1", "Post"):
    for kind in ("mask", "pos", "id"):
        image = bpy.data.images[f"{label}_{kind}"]
        assert image.is_float and image.colorspace_settings.name == "Non-Color" and tuple(image.size) == (RES, RES)
mask, pos, ids = pixels("Box_1_mask"), pixels("Box_1_pos"), pixels("Box_1_id")
top = ids[..., 0] > 0
assert 0.03 < top.mean() < 0.5, top.mean()
assert np.allclose(pos[top][:, :2].max(axis=0), (1.0, 0.6), atol=0.03), pos[top].max(axis=0)
assert mask[..., 2][top].mean() > 0.7, "the key is above: the top faces it"
side_mask = pixels("Box_0_mask")
assert np.median(side_mask[..., 0][pixels("Box_0_id")[..., 0] > 0]) < 0.2, "edges only near the bevel"
assert side_mask[..., 0].max() > 0.6, "and there"
assert len(np.unique(np.round(pixels("Post_id")[..., 0][pixels("Post_id")[..., 0] > 0], 5))) == 1
for bad, words in (({"objects": ["Key"]}, "Key is not a mesh"), ({"objects": ["Box"], "key": "Nope"}, "lights: Key, Rim"),
                   ({"objects": ["Box"], "key": [0, 0, 0]}, "not [0, 0, 0]"),
                   ({"objects": ["Box"], "resolution": 64}, "128 to 4096")):
    refused = call("bake_paint_masks", **bad)
    assert not refused.ok and words in refused.text, (bad, refused.text)
bare = bpy.data.objects.new("Bare", bpy.data.meshes.new("Bare"))
bare.data.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
scene.collection.objects.link(bare)
assert "Bare has no UV map: uv_unwrap it first" in call("bake_paint_masks", objects=["Bare"]).text
again = call("bake_paint_masks", objects=["Post"], key=[0, 0, 1], resolution=RES)
assert again.ok and "direction (0.00, 0.00, 1.00)" in again.text and "Post_mask," in again.text, again.text
assert "Post_mask.001" not in [i.name for i in bpy.data.images], "a second bake reuses its images"
print(f"PAINT TOOLS bake: 3 parts at {RES} px in {took:.1f} s; tokens, slots, engine, selection, UV map kept")

# ------------------------------------------------------------------ paint (a stand-in for the worker's) and stylize_material
painted = {}
for label in ("Box_0", "Box_1", "Post"):
    m, ids = pixels(f"{label}_mask"), pixels(f"{label}_id")
    rgb = np.dstack([0.2 + 0.5 * m[..., 2]] * 3) * (0.6, 0.4, 0.25)
    for kind, data, space in (("paint", rgb, "sRGB"), ("normal", np.dstack([np.full(rgb.shape[:2], v) for v in
                                                                             (0.5, 0.55, 1.0)]), "sRGB"),
                              ("orm", np.dstack([m[..., 1], np.full(rgb.shape[:2], 0.6), np.zeros(rgb.shape[:2])]),
                               "Non-Color")):
        image = bpy.data.images.new(f"{label}_{kind}", RES, RES)
        image.colorspace_settings.name = space
        image.pixels.foreach_set(np.dstack([data, np.ones(rgb.shape[:2])]).astype(np.float32).ravel())
        image.pack()
        painted[label, kind] = image.name
for image in [i for i in bpy.data.images if i.get(paint_tools.MASK)]:
    bpy.data.images.remove(image)
style = {"unlit": 0.65, "key": "Key", "step": [0, 0.3, 0.72], "rim": [0.19, 0.24, 0.35, 0.15], "ao": 0.5}
first = call("stylize_material", object="Box", slot=0, paint="Box_0_paint", normal="Box_0_normal", orm="Box_0_orm",
             **style)
assert first.ok, first.text
print(first.text)
copy = box.material_slots[0].material
assert copy.name == "Box_Wood" and copy != wood and post.material_slots[0].material == wood, copy.name
assert "a copy of Wood (shared)" in first.text and "Box_0_normal was sRGB: now Non-Color" in first.text, first.text
names = sorted(n.name for n in copy.node_tree.nodes)
assert all(n.startswith(paint_tools.NODE) for n in names) and abs(copy.node_tree.nodes["Paint Mix"].inputs["Fac"].default_value - 0.65) < 1e-6
assert copy.node_tree.nodes["Paint Normal Map"].uv_map == "UVMap" and copy.node_tree.nodes["Paint Normal Map"].space == "TANGENT"
assert "Paint Key Position" in names, "an area key is followed per point"
count = len(bpy.data.materials)
second = call("stylize_material", object="Box", slot=0, paint="Box_0_paint", normal="Box_0_normal", orm="Box_0_orm",
              **{**style, "unlit": 0.5, "rim": []})
assert second.ok and "rebuilt in place" in second.text and len(bpy.data.materials) == count, second.text
assert sorted(n.name for n in copy.node_tree.nodes) == sorted(set(names) - {"Paint Rim Weight", "Paint Rim Band",
                                                                              "Paint Rim", "Paint Rim Add"})
assert abs(copy.node_tree.nodes["Paint Mix"].inputs["Fac"].default_value - 0.5) < 1e-6
third = call("stylize_material", object="Box", slot=0, paint="Box_0_paint", normal="Box_0_normal", orm="Box_0_orm",
             **style)
assert sorted(n.name for n in copy.node_tree.nodes) == names, "the same named nodes again"
flat = call("stylize_material", object="Box", slot=1, paint="#c8a050", step=[0.25, 0.03, 0.35])
assert flat.ok and box.material_slots[1].material == brass and "flat" in flat.text, flat.text
assert brass.node_tree.nodes["Paint Base"].bl_idname == "ShaderNodeRGB"
own = call("stylize_material", object="Post", paint="Post_paint", normal="Post_normal", orm="Post_orm", **style)
assert own.ok and post.material_slots[0].material == wood and "rebuilt in place" in own.text, own.text
for bad, words in (({"object": "Box", "paint": "Missing"}, "no image 'Missing'"),
                   ({"object": "Box", "paint": "Box_0_paint", "slot": 2}, "slot: 0 to 1"),
                   ({"object": "Box", "paint": "Box_0_paint", "step": [0, 0]}, "softness"),
                   ({"object": "Key", "paint": "#ffffff"}, "no materials"),
                   ({"object": "Box", "paint": "Box_0_paint", "key": "Cam"}, "not a light")):
    refused = call("stylize_material", **bad)
    assert not refused.ok and words in refused.text, (bad, refused.text)
print("PAINT TOOLS stylize: shared Wood copied, Brass and the post's Wood rebuilt in place, the same nodes on a "
      "second call, refusals")

# ------------------------------------------------------------------ Cycles, EEVEE, glTF


def render(engine, unlit=None):
    scene.render.engine = engine
    if engine == "CYCLES":
        scene.cycles.samples = 8
    if unlit is not None:
        for m in (copy, wood):
            m.node_tree.nodes["Paint Mix"].inputs["Fac"].default_value = unlit
    path = work / f"{engine.lower()}_{unlit}.png"
    scene.render.filepath = str(path)
    began = time.perf_counter()
    bpy.ops.render.render(write_still=True)
    shot = bpy.data.images.load(str(path))
    values = np.empty(shot.size[0] * shot.size[1] * 4, np.float32)
    shot.pixels.foreach_get(values)
    bpy.data.images.remove(shot)
    return values.reshape(-1, 4)[:, :3], time.perf_counter() - began


for engine in ("CYCLES", "BLENDER_EEVEE"):
    pbr, _ = render(engine, 0.0)
    styled, seconds = render(engine, 0.65)
    moved = np.abs(styled - pbr).max(axis=1)
    assert styled.max() > 0.1 and (moved > 0.05).mean() > 0.02, (engine, styled.max(), (moved > 0.05).mean())
    print(f"PAINT TOOLS {engine}: the unlit half moves {(moved > 0.05).mean() * 100:.0f} % of the pixels by over 0.05 "
          f"against PBR alone, {seconds:.1f} s")
glb = work / "box.glb"
for obj in scene.objects:
    obj.select_set(obj in (box, post))
bpy.ops.export_scene.gltf(filepath=str(glb), use_selection=True, export_format="GLB")
with open(glb, "rb") as f:
    f.read(12)
    size = struct.unpack("<I", f.read(4))[0]
    f.read(4)
    doc = json.loads(f.read(size))
images = [i.get("name") for i in doc["images"]]
exported = {}
for m in doc["materials"]:
    pbr = m.get("pbrMetallicRoughness", {})
    source = lambda t: images[doc["textures"][t["index"]]["source"]] if t else None  # noqa: E731
    exported[m["name"]] = (source(pbr.get("baseColorTexture")), source(pbr.get("metallicRoughnessTexture")),
                           source(m.get("normalTexture")), source(m.get("occlusionTexture")))
assert exported["Box_Wood"] == ("Box_0_paint", "Box_0_orm", "Box_0_normal", "Box_0_orm"), exported
assert exported["Wood"] == ("Post_paint", "Post_orm", "Post_normal", "Post_orm"), exported
print("PAINT TOOLS glTF:", json.dumps(exported))

# ------------------------------------------------------------------ in a worker's child Blender
if "--no-child" not in ARGS:
    scene, box, post, wood, brass = build()
    job = work / "job"
    (job / "w1").mkdir(parents=True)
    spawn = job / "w1" / "spawn.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(spawn), copy=True)
    child = worker_host.Child(job, "box", spawn, "modeling", binary=bpy.app.binary_path, run_timeout=60).start()
    try:
        assert child.call("init", {"warm": False, "prefix": "Box", "owned": ["OBJECT:Box", "MESH:Box"]})["ok"]
        runtime = worker_host.WorkerRuntime(child, {})
        began = time.perf_counter()
        bake = runtime.run_tool("bake_paint_masks", json.dumps({"objects": ["Box", "Post"], "key": "Key",
                                                                "resolution": RES}))
        assert bake.ok and bake.rolled_back is None and "Baked 3 parts" in bake.text, (bake.rolled_back, bake.text)
        child_bake = time.perf_counter() - began
        code = ("import bpy, numpy as np\n"
                "for kind in ('paint', 'normal', 'orm'):\n"
                "    i = bpy.data.images.new('Box_0_' + kind, 256, 256)\n"
                "    i.colorspace_settings.name = 'sRGB' if kind == 'paint' else 'Non-Color'\n"
                "    i.pixels.foreach_set(np.full(256 * 256 * 4, 0.5, np.float32))\n"
                "    i.pack()\n"
                "for i in [i for i in bpy.data.images if i.get('lc_paint_mask') and i.name.startswith('Box')]:\n"
                "    bpy.data.images.remove(i)\n")  # The post's are left: temporary, never in the result.
        painted = runtime.run_tool("run_python", json.dumps({"code": code, "summary": "paint"}))
        assert painted.ok and painted.rolled_back is None, painted.text
        styled = runtime.run_tool("stylize_material", json.dumps({"object": "Box", "paint": "Box_0_paint",
                                                                  "normal": "Box_0_normal", "orm": "Box_0_orm", **style}))
        assert styled.ok and styled.rolled_back is None, (styled.rolled_back, styled.text)
        assert "a copy of Wood (a palette material)" in styled.text, styled.text
        theirs = runtime.run_tool("stylize_material", json.dumps({"object": "Post", "paint": "#808080"}))
        assert not theirs.ok and "stylize_material refused: the mesh of Post is not yours" in theirs.text, theirs.text
        manifest = child.call("finish")["manifest"]
        assert "MATERIAL:Box_Wood" in manifest["created"] and "IMAGE:Box_0_paint" in manifest["created"], manifest
        assert not [k for k in manifest["created"] if k.endswith(("_mask", "_pos", "_id"))], manifest["created"]
        assert not manifest["advisory"]["changed_not_owned"] and not manifest["rollbacks"], manifest
        print(f"PAINT TOOLS child: bake {child_bake:.1f} s and stylize ran, nothing rolled back, Wood copied as "
              f"Box_Wood, Post refused")
    finally:
        child.exit()
print("PAINT TOOLS OK")
