"""merge.py and ownership.py against real Blender: the M0 spike's 17 checks on production code,
plus what production added.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/merge_check.py [-- --keep]

Headless, no model or network; ~20 s. Workers are plain background Blenders on the wave's spawn
copy (this file again, "-- worker <jobs.json>"), one at a time, each running several briefs in
turn (the spawn copy reopened between them): stamp_all, a few lines of "model code",
write_result. No worker host.

Wave 1 is the spike: A builds a Chair and owns Lamp_Light, B builds a Table and illegally scales
the Rug, and the user renames the Rug while they run. Checked: created datablocks keep the worker's
own names, constraint, boolean and driver targets are the live datablocks, parenting and world
positions kept, one shared palette material, a genuine name collision left to Blender's own .001
and reported (not prefixed), B's Rug edit dropped and reported, the owned lamp
replaced with live references following, no staging collections, libraries or stamps left, a
linked library material and a packed image reused, one undo step. S2: the user edits the owned
lamp during the wave: a conflict, the user's edit kept.

New: the wave checkpoint (kind "wave"); the temporary files deleted; wave 2 with a created light
put in the hidden LC_Quarantine collection and then accepted (both where the scene's objects are,
not a collection per worker), a camera worker's scene.camera
applied and a lighting worker's scene.world refused because the user changed it, the shared-mesh
rule (an in-place edit of a mesh two objects use is dropped, a single-user copy is merged), a
created object in the user's collection not counting as a change to it, and an exception injected
mid-merge leaving the scene exactly as before.

Wave 3 is the perfume_ad eval's lighting and camera workers (a spot parented to an empty, a world
with a node tree, a node group, a packed image and one on disk, an emissive card) and
shared_support's lamp worker: nothing left pointing at a result.blend (weak references included,
which every later inspect_scene and render_job refused), render_job's missing-file refusal naming
the datablock and path, an object whose parent or constraint target was held back saying so,
owned objects left as they were staying where the user had them (not in LC_Quarantine), a copy of
what an earlier worker replaced going to the replacement, and accept_quarantine by name.

Wave 4 is a palette made before the wave that no object wears yet: materials, node groups and
images nothing uses (and the palette) are in the workers' copy with no fake user left live, and a
material a worker made again with a live one's name and settings merges as the live one. Created
objects go to the scene's main collection, to the brief's count-check collection, or to a collection
of their own when the user asked for collections; a worker's collection that hangs on nothing is
dropped, an instancer's source kept; the headline counts objects; a created object's own mesh
takes the object's name.

The materials wave (M4 quality): a modeling worker's own changed copy of a palette material merges
as its own and passes a material_on check naming the palette material; a copy left as it was is the
live one; its edit to the palette itself is dropped; a worker with aspect materials replaces the
palette material live.

The collections wave (GPT-6.1 Sol's asset pack, O0008): created objects keep the collections a
worker put them in on purpose; a collection the workers made that the shared notes name is made live
once; the active collection and a collection nobody asked for fall back to home as before.

The shot wave (M4 quality): a shot worker's own camera move and light animation merge with
nothing held back; a modeling worker's bulb light and fan spin, which its brief asks for, merge and
say why, and an unasked test light is held back with the reason; the shot worker continued in a
later wave, owning what it built, replaces its own work with nothing dropped or duplicated.

The split wave (M4 early return): wave 3's lighting and camera workers merged in two calls as they
finish, with an orchestrator edit in between: the spawn copy stays for the second call, the camera
worker's copy of the lamp the first call replaced still finds the replacement, the camera the
orchestrator changed in between is a conflict (its version kept), and each call is its own undo step.

The support wave (eval 20260930-154037, the Arcane chest's lost ArcaneRecipe): a worker's text blocks
and images merge: new texts with their contents (.001 when the user took the name meanwhile), a
packed atlas and an unused painted mask packed, with the pixel hash inspect_texture reads, a save
keeping them; a blank image and an edit to the user's script skipped with the reason; the manifest
checked live; owns takes a text's name; the painter continued updates its recipe and atlas (the
unsaved repaint packed), and an owned text the user edits meanwhile is a conflict named as missing.

mesh_validate_check (M4): a worker's mesh with an out-of-range loop index is fixed by Mesh.validate()
before it merges, and noted in the report; the fixed mesh survives bm.from_mesh (the crash this
guards against).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout
checkout.use()
from loopcut import checkpoints, merge, ownership  # noqa: E402
from loopcut.ownership import STAMP  # noqa: E402

DOT_NUM = re.compile(r"\.\d{3}$")
WORKER_TIMEOUT = 180
RESULTS = []


def check(name: str, ok, detail="") -> None:
    RESULTS.append((name, bool(ok), detail))


def rnd(v):
    try:
        return tuple(round(float(x), 4) for x in v)
    except TypeError:
        return round(float(v), 4)


def obj(name):
    return bpy.data.objects.get(name)


# ------------------------------------------------------------------ fixtures (the spike's, plus wave 2's)

def add_mesh_object(name, mesh_name, size, loc, collection):
    import bmesh
    me = bpy.data.meshes.new(mesh_name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x *= size[0]
        v.co.y *= size[1]
        v.co.z *= size[2]
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    collection.objects.link(ob)
    return ob


def new_material(name, rgb):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1.0)
    if mat.node_tree is None:
        mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    return mat


def add_driver(ob, path, index, target, transform_type, expr):
    fc = ob.driver_add(path, index)
    drv = fc.driver
    drv.type = "SCRIPTED"
    var = drv.variables.new()
    var.name = "v"
    var.type = "TRANSFORMS"
    var.targets[0].id = target
    var.targets[0].transform_type = transform_type
    drv.expression = expr
    return fc


def build_library(lib_path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    mat = new_material("Lib_Varnish", (0.9, 0.85, 0.7))
    bpy.data.libraries.write(lib_path, {mat}, fake_user=True)


def build_live(lib_path):
    import numpy as np
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    col = scene.collection
    wood = new_material("Palette_Wood", (0.45, 0.28, 0.12))
    grain = bpy.data.images.new("Wood_Grain", 128, 128)
    grain.generated_type = "UV_GRID"
    buf = np.empty(len(grain.pixels), dtype=np.float32)
    grain.pixels.foreach_get(buf)
    grain.pixels.foreach_set(buf)  # A generated image only packs once its buffer is dirty.
    grain.pack()
    tex = wood.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = grain
    with bpy.data.libraries.load(lib_path, link=True) as (_src, dst):
        dst.materials = ["Lib_Varnish"]
    wool = new_material("Rug_Wool", (0.6, 0.55, 0.4))
    floor = add_mesh_object("Floor", "FloorMesh", (8, 8, .02), (0, 0, -.01), col)
    floor.data.materials.append(wood)
    rug = add_mesh_object("Rug", "RugMesh", (2, 1.4, .01), (0, 0, .005), col)
    rug.data.materials.append(wool)
    lamp = bpy.data.objects.new("Lamp_Light", bpy.data.lights.new("Lamp_Light", "POINT"))
    lamp.data.energy = 100.0
    lamp.location = (1.5, 1.5, 2.0)
    col.objects.link(lamp)
    shade = add_mesh_object("Lamp_Shade", "Lamp_ShadeMesh", (.3, .3, .2), (1.5, 1.5, 2.1), col)
    shade.parent = lamp
    shade.matrix_parent_inverse = lamp.matrix_world.inverted()
    cam = bpy.data.objects.new("Cam_Main", bpy.data.cameras.new("Cam_Main"))
    cam.location = (5, -5, 3)
    col.objects.link(cam)
    scene.camera = cam
    cam.constraints.new("TRACK_TO").target = lamp  # A live reference TO the owned object.
    box = add_mesh_object("Box", "BoxMesh", (.6, .6, .6), (-2.5, 2, .3), col)
    box.data.materials.append(wood)
    box.data.materials.append(dst.materials[0])
    cutter = add_mesh_object("Cutter", "CutterMesh", (.3, .3, 1), (-2.5, 2, .5), col)
    cutter.display_type = "WIRE"
    box.modifiers.new("Cut", "BOOLEAN").object = cutter
    add_driver(box, "rotation_euler", 2, rug, "SCALE_X", "v*0.1")
    # Wave 2: two posts sharing one mesh, the user's own collection, a world.
    post_a = add_mesh_object("Post_A", "PostMesh", (.1, .1, 1), (3, 2, .5), col)
    post_b = bpy.data.objects.new("Post_B", post_a.data)
    post_b.location = (3.5, 2, .5)
    col.objects.link(post_b)
    props = bpy.data.collections.new("Props")
    col.children.link(props)
    add_mesh_object("Stool", "StoolMesh", (.4, .4, .45), (-1, -1.5, .225), props)
    scene.world = bpy.data.worlds.new("LiveWorld")
    bpy.context.view_layer.update()


# ------------------------------------------------------------------ worker (child process)

def model_A():
    scene_col, rug = bpy.context.scene.collection, bpy.data.objects["Rug"]
    root = bpy.data.objects.new("Chair_Root", None)
    root.location = (0.6, 0.0, 0.0)
    scene_col.objects.link(root)
    wood, fabric = bpy.data.materials["Palette_Wood"], new_material("Chair_Fabric", (0.2, 0.3, 0.7))
    legs = []
    for i, (x, y) in enumerate(((-.2, -.2), (.2, -.2), (-.2, .2), (.2, .2)), 1):
        leg = add_mesh_object(f"Chair_Leg_{i}", f"Chair_LegMesh_{i}", (.04, .04, .45), (0.6 + x, y, .225), scene_col)
        leg.data.materials.append(wood)
        leg.parent = root
        leg.matrix_parent_inverse = root.matrix_world.inverted()
        legs.append(leg)
    c = legs[0].constraints.new("COPY_LOCATION")
    c.target, c.use_offset, c.influence = rug, True, 0.0
    seat = add_mesh_object("Chair_Seat", "Chair_SeatMesh", (.5, .5, .06), (0.6, 0, .48), scene_col)
    seat.data.materials.append(fabric)
    seat.data.materials.append(next(m for m in bpy.data.materials if m.name == "Lib_Varnish"))
    seat.parent = root
    seat.matrix_parent_inverse = root.matrix_world.inverted()
    seat.modifiers.new("Chair_Notch", "BOOLEAN").object = bpy.data.objects["Cutter"]
    add_driver(seat, "rotation_euler", 2, bpy.data.objects["Floor"], "LOC_X", "v*0")
    lamp = bpy.data.lights["Lamp_Light"]  # Owned: colour and power.
    lamp.color, lamp.energy = (1.0, 0.8, 0.6), 250.0


def model_B():
    scene_col, rug = bpy.context.scene.collection, bpy.data.objects["Rug"]
    root = bpy.data.objects.new("Table_Root", None)
    root.location = (-1.5, 0.0, 0.0)
    scene_col.objects.link(root)
    c = root.constraints.new("COPY_ROTATION")
    c.target, c.influence = rug, 0.0
    fabric = new_material("Chair_Fabric", (0.7, 0.2, 0.2))  # Collides with worker A's.
    top = add_mesh_object("Table_Top", "Table_TopMesh", (1.0, .6, .05), (-1.5, 0, .72), scene_col)
    top.data.materials.append(fabric)
    leg = add_mesh_object("Leg", "Leg", (.05, .05, .7), (-1.9, -.25, .35), scene_col)  # Unprefixed.
    leg.data.materials.append(bpy.data.materials["Palette_Wood"])
    for ob in (top, leg):
        ob.parent = root
        ob.matrix_parent_inverse = root.matrix_world.inverted()
    rug.scale = (2.0, 2.0, 1.0)  # Not owned by B.


def model_C():
    """Modeling: a vase created into the user's Props collection under a root named for the
    deliverable, and a test light (not its role)."""
    body = add_mesh_object("Vase_Body", "Vase_BodyMesh", (.2, .2, .4), (2, -1, .2), bpy.data.collections["Props"])
    root = bpy.data.objects.new("Vase", None)
    bpy.data.collections["Props"].objects.link(root)
    body.parent = root
    light = bpy.data.objects.new("Vase_TestLight", bpy.data.lights.new("Vase_TestLight", "POINT"))
    light.location = (2, -1, 1.5)
    bpy.context.scene.collection.objects.link(light)


def model_D():
    """Camera aspect: a new camera made the scene camera, and the owned Cam_Main's lens."""
    cam = bpy.data.objects.new("Shot_Cam", bpy.data.cameras.new("Shot_Cam"))
    cam.location = (0, -6, 1.5)
    bpy.context.scene.collection.objects.link(cam)
    bpy.context.scene.camera = cam
    bpy.data.cameras["Cam_Main"].lens = 85.0


def model_E():
    """Lighting aspect: a new world made the scene world."""
    world = bpy.data.worlds.new("Light_World")
    world.color = (0.1, 0.1, 0.3)
    bpy.context.scene.world = world


def model_F():
    """Owns Post_A only: moves it, and edits the mesh it shares with Post_B in place (not owned)."""
    bpy.data.objects["Post_A"].location.x += 0.5
    for v in bpy.data.meshes["PostMesh"].vertices:
        v.co.z *= 2.0


def model_G():
    """Owns Post_B only: a single-user copy of the shared mesh, then edits that."""
    post = bpy.data.objects["Post_B"]
    post.data = post.data.copy()
    post.data.name = "PostB_Mesh"
    for v in post.data.vertices:
        v.co.z *= 3.0


def packed_image(name, size=32):
    image = bpy.data.images.new(name, size, size)
    image.generated_type = "COLOR_GRID"
    pixels = [0.0] * len(image.pixels)
    image.pixels.foreach_get(pixels)
    image.pixels.foreach_set(pixels)  # A generated image only packs once its buffer is dirty.
    image.pack()
    return image


def model_H():
    """Lighting aspect, as perfume_ad's lighting worker: a spot parented to a pivot empty (every role
    may create empties), a light card with an emissive, image-textured material aimed at another
    empty, a world with a node tree (Mix Shader, a node group, a packed image and one on disk by a
    path relative to the spawn copy), the owned lamp brighter, and scene.world set to the new world."""
    scene = bpy.context.scene
    root = bpy.data.objects.new("Lit_Spot_root", None)
    root.location = (1.0, -1.0, 2.0)
    scene.collection.objects.link(root)
    spot = bpy.data.objects.new("Lit_Spot", bpy.data.lights.new("Lit_Spot_data", "SPOT"))
    spot.data.energy = 60.0
    spot.location = (0.0, 0.0, 0.5)
    scene.collection.objects.link(spot)
    spot.parent = root
    aim = bpy.data.objects.new("Lit_Aim", None)
    scene.collection.objects.link(aim)
    card = add_mesh_object("Lit_Card", "Lit_CardMesh", (1.0, 0.02, 0.6), (-1.5, 1.0, 1.5), scene.collection)
    card.constraints.new("TRACK_TO").target = aim
    grid = packed_image("Lit_Grid")
    ramp_path = Path(CURRENT_JOB["result"]).parent / "textures" / "lit_ramp.png"
    ramp_path.parent.mkdir(parents=True, exist_ok=True)
    ramp = bpy.data.images.new("Lit_Ramp", 8, 8)
    ramp.filepath_raw = str(ramp_path)
    ramp.file_format = "PNG"
    ramp.save()
    ramp.source = "FILE"
    ramp.filepath = bpy.path.relpath(str(ramp_path))  # Relative to the spawn copy; written absolute.
    glow = new_material("Lit_CardGlow", (1.0, 0.9, 0.8))
    nodes, links = glow.node_tree.nodes, glow.node_tree.links
    emission, texture = nodes.new("ShaderNodeEmission"), nodes.new("ShaderNodeTexImage")
    texture.image = grid
    links.new(texture.outputs["Color"], emission.inputs["Color"])
    links.new(emission.outputs["Emission"], next(n for n in nodes if n.type == "OUTPUT_MATERIAL").inputs["Surface"])
    card.data.materials.append(glow)
    tint = bpy.data.node_groups.new("Lit_Tint", "ShaderNodeTree")
    tint.interface.new_socket("Color", in_out="INPUT", socket_type="NodeSocketColor")
    tint.interface.new_socket(socket_type="NodeSocketColor", name="Color", in_out="OUTPUT")
    group_in, group_out, hue = (tint.nodes.new(t) for t in ("NodeGroupInput", "NodeGroupOutput", "ShaderNodeHueSaturation"))
    tint.links.new(group_in.outputs[0], hue.inputs["Color"])
    tint.links.new(hue.outputs["Color"], group_out.inputs[0])
    world = bpy.data.worlds.new("Lit_World")
    if world.node_tree is None:
        world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    output = next(n for n in nodes if n.type == "OUTPUT_WORLD")
    for node in [n for n in nodes if n != output]:
        nodes.remove(node)
    sky, env, group, mix, ramp_node = (nodes.new(t) for t in ("ShaderNodeBackground", "ShaderNodeBackground",
                                                              "ShaderNodeGroup", "ShaderNodeMixShader", "ShaderNodeTexImage"))
    sky.inputs["Color"].default_value = (0.02, 0.02, 0.03, 1.0)
    group.node_tree = tint
    texture = nodes.new("ShaderNodeTexEnvironment")
    texture.image = grid
    ramp_node.image = ramp
    links.new(texture.outputs["Color"], group.inputs[0])
    links.new(group.outputs[0], env.inputs["Color"])
    links.new(ramp_node.outputs["Color"], mix.inputs["Fac"])
    links.new(sky.outputs[0], mix.inputs[1])
    links.new(env.outputs[0], mix.inputs[2])
    links.new(mix.outputs[0], output.inputs["Surface"])
    scene.world = world
    bpy.data.lights["Lamp_Light"].energy = 40.0


def model_I():
    """Camera aspect, as perfume_ad's camera worker: a close-up camera focused on the live Box and
    tracking an aim-target empty on it, made the scene camera, the owned Cam_Main's lens, and a fill
    light (the camera aspect grants cameras, not lights)."""
    scene = bpy.context.scene
    cam = bpy.data.objects.new("Shot_CloseUp", bpy.data.cameras.new("Shot_CloseUp"))
    cam.location = (-1.0, 3.5, 1.0)
    scene.collection.objects.link(cam)
    cam.data.dof.use_dof = True
    cam.data.dof.focus_object = bpy.data.objects["Box"]
    target = bpy.data.objects.new("Shot_Target", None)
    target.location = bpy.data.objects["Box"].matrix_world.translation
    scene.collection.objects.link(target)
    cam.constraints.new("TRACK_TO").target = target
    scene.camera = cam
    bpy.data.cameras["Cam_Main"].lens = 35.0
    fill = bpy.data.objects.new("Shot_Fill", bpy.data.lights.new("Shot_Fill", "AREA"))
    scene.collection.objects.link(fill)


def model_J():
    """Modeling, as shared_support's lamp worker: a root named for the deliverable, a shade and a
    light parented to it (the light is outside the role: no lighting aspect), a bulb parented to the
    light and a glow that copies its location."""
    col = bpy.context.scene.collection
    root = bpy.data.objects.new("FloorLamp", None)
    root.location = (-3.0, -2.0, 0.0)
    col.objects.link(root)
    shade = add_mesh_object("FloorLamp_Shade", "FloorLamp_ShadeMesh", (.4, .4, .3), (0, 0, 1.4), col)
    light = bpy.data.objects.new("FloorLamp_Light", bpy.data.lights.new("FloorLamp_Light", "POINT"))
    light.location = (0, 0, 1.4)
    col.objects.link(light)
    shade.parent = light.parent = root
    bulb = add_mesh_object("FloorLamp_Bulb", "FloorLamp_BulbMesh", (.05, .05, .08), (0, 0, 1.4), col)
    bulb.parent = light
    glow = add_mesh_object("FloorLamp_Glow", "FloorLamp_GlowMesh", (.1, .1, .1), (0, 0, 1.4), col)
    glow.parent = root
    glow.constraints.new("COPY_LOCATION").target = light


def model_K():
    """Modeling, as a crystal worker after the orchestrator made a palette no object wore yet: it
    finds Pal_Glow in its copy and uses it; it makes Pal_Glass again exactly as the live one (it
    is Pal_Glass.001 here) and a Pal_Stone of its own, other than the live one."""
    col = bpy.context.scene.collection
    glow = bpy.data.materials.get("Pal_Glow")
    NOTES["copy"] = {"materials": sorted(m.name for m in bpy.data.materials),
                     "node_groups": sorted(g.name for g in bpy.data.node_groups),
                     "images": sorted(i.name for i in bpy.data.images)}
    crystal = add_mesh_object("Crystal", "CrystalMesh", (.2, .2, .5), (4, -3, .25), col)
    crystal.data.materials.append(glow or new_material("Pal_Glow", (0.2, 0.6, 1.0)))
    shard = add_mesh_object("Crystal_Shard", "Crystal_ShardMesh", (.1, .1, .2), (4.3, -3, .1), col)
    shard.data.materials.append(new_material("Pal_Glass", (0.9, 0.9, 0.95)))
    base = add_mesh_object("Crystal_Base", "Crystal_BaseMesh", (.4, .4, .05), (4, -3, .025), col)
    base.data.materials.append(new_material("Pal_Stone", (0.5, 0.5, 0.5)))
    add_mesh_object("Crystal_Sign", "Crystal_SignFootA", (.3, .05, .3), (4.5, -3.3, .15), col)  # Parts joined onto FootA.
    add_mesh_object("Crystal_Twin", "Crystal_Twin_Old", (.1, .1, .1), (3.5, -3.3, .05), col)  # A live mesh has that name.


def model_L():
    """Modeling, where the user asked for a collection per crate: two crates into a collection of its own."""
    crates = bpy.data.collections.new("Crate_Stack")
    bpy.context.scene.collection.children.link(crates)
    for i in (1, 2):
        add_mesh_object(f"Crate_{i}", f"Crate_{i}Mesh", (.4, .4, .4), (-4, 2 + i, .2), crates)


def model_M():
    """Owns the user's Props collection and puts a new group in it: the group arrives with Props, whose
    own members did not change, so it hangs on nothing; its brief's count check names Props."""
    group = bpy.data.collections.new("Stack_Group")
    bpy.data.collections["Props"].children.link(group)
    for i in (1, 2):
        add_mesh_object(f"Stack_Box{i}", f"Stack_Box{i}Mesh", (.3, .3, .3), (-4, -2, .15 + .3 * (i - 1)), group)


def model_P():
    """As M, but the user asked for collections: its group stays, and its box is only in it."""
    group = bpy.data.collections.new("Pile_Group")
    bpy.data.collections["Props"].children.link(group)
    add_mesh_object("Pile_Box1", "Pile_Box1Mesh", (.3, .3, .3), (-4.5, -2, .15), group)


def model_N():
    """An instancer: the leaf lives in a collection of its own that the Tree empty shows."""
    source = bpy.data.collections.new("Tree_Src")
    add_mesh_object("Tree_Leaf", "Tree_LeafMesh", (.2, .2, .6), (0, 0, .3), source)
    tree = bpy.data.objects.new("Tree", None)
    tree.instance_type, tree.instance_collection = "COLLECTION", source
    tree.location = (4, 3, 0)
    bpy.context.scene.collection.objects.link(tree)


def model_Q():
    """A modeling worker without aspect materials: its own copy of Palette_Wood, changed (noise into
    the roughness), on its chair's leg; a copy of Rug_Wool renamed but left as it was, on the seat;
    and an edit to the palette's Palette_Wood itself, which it does not own."""
    scene = bpy.context.scene
    seat = add_mesh_object("Chair", "ChairMesh", (.45, .45, .05), (2.5, -2, .45), scene.collection)
    leg = add_mesh_object("Chair_Leg", "Chair_LegMesh", (.04, .04, .43), (0, 0, -.235), scene.collection)
    leg.parent = seat
    wood = bpy.data.materials["Palette_Wood"].copy()
    wood.name = "Chair_Wood"
    nodes, links = wood.node_tree.nodes, wood.node_tree.links
    noise = nodes.new("ShaderNodeTexNoise")
    links.new(noise.outputs["Fac"], next(n for n in nodes if n.type == "BSDF_PRINCIPLED").inputs["Roughness"])
    leg.data.materials.append(wood)
    wool = bpy.data.materials["Rug_Wool"].copy()
    wool.name = "Chair_Wool"
    seat.data.materials.append(wool)
    seat.data.materials.append(bpy.data.materials["Palette_Wood"])  # So the edited palette is in the result.
    palette = next(n for n in bpy.data.materials["Palette_Wood"].node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    palette.inputs["Roughness"].default_value = 0.1


def model_R():
    """A materials worker with aspect materials: re-finishes the palette's Rug_Wool in place (every
    object wearing it follows) and leaves Palette_Wood, which it also owns, as it was."""
    bsdf = next(n for n in bpy.data.materials["Rug_Wool"].node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Roughness"].default_value = 0.95
    bsdf.inputs["Sheen Weight"].default_value = 0.4


def model_V():
    """A recolour worker that owns the Box and has no aspect materials (20260929-230207 O0015): the
    palette's Rug_Wool into the Box's first slot and one face onto its second slot."""
    box = bpy.data.objects["Box"]
    box.material_slots[0].material = bpy.data.materials["Rug_Wool"]
    box.data.polygons[0].material_index = 1


def model_W():
    """GPT's asset pack (20260929-230207 O0008): the orchestrator made ShopAssetPack live before the wave
    and the brief said to put each prop in it; a potion linked there on purpose, and a cork made where
    new objects land by default (the Work collection was active)."""
    potion = add_mesh_object("PotionOne", "PotionOneMesh", (.2, .2, .4), (5, 0, .2), bpy.data.collections["ShopAssetPack"])
    bpy.ops.mesh.primitive_cube_add(size=0.05, location=(5, 0, .45))
    bpy.context.active_object.name = "PotionOne_Cork"
    bpy.context.active_object.parent = potion


def model_X():
    """A pack collection the worker made, named in the wave's shared notes, as was ShopAssetPack, and
    one it made that nobody asked for."""
    pack = bpy.data.collections.get("PackTwo") or bpy.data.collections.new("PackTwo")
    if pack.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(pack)
    add_mesh_object("PotionTwo", "PotionTwoMesh", (.2, .2, .4), (5.5, 0, .2), pack)
    extra = bpy.data.collections.new("TwoParts")
    bpy.context.scene.collection.children.link(extra)
    add_mesh_object("PotionTwo_Stand", "PotionTwo_StandMesh", (.3, .3, .02), (5.5, 0, .01), extra)


def model_Y():
    """Another worker making the same pack collection in its own copy: one PackTwo live, both potions in it."""
    pack = bpy.data.collections.new("PackTwo")
    bpy.context.scene.collection.children.link(pack)
    add_mesh_object("PotionThree", "PotionThreeMesh", (.2, .2, .4), (6, 0, .2), pack)


def _key(idb, path, frames_values, index=-1, linear=False):
    for frame, value in frames_values:
        if index >= 0:
            getattr(idb, path)[index] = value
        else:
            setattr(idb, path, value)
        idb.keyframe_insert(path, index=index, frame=frame)
    if linear:
        anim = idb.animation_data
        from bpy_extras.anim_utils import action_get_channelbag_for_slot
        channels = action_get_channelbag_for_slot(anim.action, anim.action_slot)
        for fc in channels.fcurves:
            for point in fc.keyframe_points:
                point.interpolation = "LINEAR"


def model_S():
    """A shot worker (lighting with aspects lighting and camera): an orbiting camera on a keyed pivot
    empty, a key light whose energy flickers, a rim that sweeps (object rotation keys), the camera's
    lens keyed too, and scene.camera set to its camera. All of it is the shot's; nothing held back."""
    import math
    scene = bpy.context.scene
    pivot = bpy.data.objects.new("Shot_Pivot", None)
    scene.collection.objects.link(pivot)
    _key(pivot, "rotation_euler", [(1, 0.0), (96, math.tau)], index=2, linear=True)
    camera = bpy.data.objects.new("Shot_Cam", bpy.data.cameras.new("Shot_Cam"))
    camera.location = (0, -4, 1.5)
    camera.parent = pivot
    scene.collection.objects.link(camera)
    _key(camera.data, "lens", [(1, 50.0), (96, 85.0)])
    key = bpy.data.objects.new("Shot_Key", bpy.data.lights.new("Shot_Key", "AREA"))
    key.location = (2, -2, 3)
    scene.collection.objects.link(key)
    _key(key.data, "energy", [(1, 200.0), (48, 120.0), (96, 200.0)])
    rim = bpy.data.objects.new("Shot_Rim", bpy.data.lights.new("Shot_Rim", "SPOT"))
    rim.location = (-2, 2, 3)
    scene.collection.objects.link(rim)
    _key(rim, "rotation_euler", [(1, 0.0), (96, 1.2)], index=2)
    scene.camera = camera


def model_S2():
    """The same shot worker continued in a later wave, owning what it built: re-keys the flicker,
    slows the orbit, turns on depth of field and adds a fill."""
    import math
    scene = bpy.context.scene
    key = bpy.data.objects["Shot_Key"]
    _key(key.data, "energy", [(48, 60.0)])
    _key(bpy.data.objects["Shot_Pivot"], "rotation_euler", [(96, math.pi)], index=2)
    bpy.data.objects["Shot_Cam"].data.dof.use_dof = True
    fill = bpy.data.objects.new("Shot_Fill2", bpy.data.lights.new("Shot_Fill2", "AREA"))
    scene.collection.objects.link(fill)


def model_T():
    """A modeling worker whose brief asks for a floor lamp that lights the corner: its bulb light,
    parented under the lamp, is asked for; a test light in the middle of the room is not."""
    scene = bpy.context.scene
    lamp = add_mesh_object("ReadingLamp", "ReadingLampMesh", (.3, .3, 1.6), (3, 3, .8), scene.collection)
    bulb = bpy.data.objects.new("ReadingLamp_Bulb", bpy.data.lights.new("ReadingLamp_Bulb", "POINT"))
    bulb.parent = lamp
    bulb.location = (0, 0, .7)
    scene.collection.objects.link(bulb)
    test = bpy.data.objects.new("ReadingLamp_Test", bpy.data.lights.new("ReadingLamp_Test", "POINT"))
    scene.collection.objects.link(test)


def model_U():
    """A modeling worker whose brief asks for a fan that spins: the blades' rotation keys (an action
    on its own object) are asked for; a key on the user's Rug would not be its own."""
    import math
    scene = bpy.context.scene
    add_mesh_object("Fan", "FanMesh", (.2, .2, .3), (-3, 3, 2.5), scene.collection)
    blades = add_mesh_object("Fan_Blades", "Fan_BladesMesh", (1.1, .12, .02), (-3, 3, 2.3), scene.collection)
    _key(blades, "rotation_euler", [(1, 0.0), (48, math.tau)], index=2, linear=True)


def model_bad_mesh():
    """A worker's own mistake: a mesh with a loop index past the vertex count, the kind a bad
    boolean or a hand-rolled bmesh op can leave. merge._validate_meshes must catch this before the
    live scene ever sees it: production once segfaulted bm.from_mesh on a mesh like this, downstream
    in a later check, far from where the bad mesh was made."""
    ob = add_mesh_object("Bad_Block", "Bad_BlockMesh", (.2, .2, .2), (3, 3, .1), bpy.context.scene.collection)
    ob.data.loops[0].vertex_index = len(ob.data.vertices) + 5


RECIPE = "# Arcane chest paint recipe\nPALETTE = {'wood': (0.42, 0.25, 0.12)}\ndef paint(pixels):\n    return pixels\n"


def _paint(image, seed):
    """Pixels from a seeded generator, as a painter's numpy pass writes them (floats into an 8-bit buffer)."""
    import numpy as np
    image.pixels.foreach_set(np.random.default_rng(seed).random(len(image.pixels), dtype=np.float32))


def model_TX():
    """The Arcane chest painter (eval 20260930-154037): its method in a text block, its executable
    painter source in another (Painter: the user makes one so named during the wave), an atlas painted and packed by
    its own code on its chest's material, a mask painted and left unpacked and unused, a blank image it
    never used, and an edit to the user's own script, which it does not own."""
    bpy.data.texts.new("ArcaneRecipe").from_string(RECIPE)
    bpy.data.texts.new("Painter").from_string("painter source v1\n")
    chest = add_mesh_object("Chest", "ChestMesh", (.4, .3, .3), (6, 0, .3), bpy.context.scene.collection)
    paint = new_material("Chest_Paint", (0.5, 0.3, 0.2))
    atlas = bpy.data.images.new("Chest_Atlas", 128, 128)
    _paint(atlas, 1)
    atlas.pack()
    node = paint.node_tree.nodes.new("ShaderNodeTexImage")
    node.image = atlas
    bsdf = next(n for n in paint.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    paint.node_tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    chest.data.materials.append(paint)
    _paint(bpy.data.images.new("Chest_Mask", 64, 64), 2)  # Unused, unsaved paint: packed by write_result.
    bpy.data.images.new("Chest_Scratch", 32, 32)          # Unused, never painted: skipped.
    script = bpy.data.texts["UserScript"]
    script.from_string(script.as_string() + "# the painter was here\n")


def model_TY():
    """The painter continued, owning its recipe and atlas: appends to the recipe, repaints the atlas
    and leaves the paint unsaved (write_result packs it)."""
    recipe = bpy.data.texts["ArcaneRecipe"]
    recipe.from_string(recipe.as_string() + "BRASS = (0.8, 0.6, 0.2)\n")
    _paint(bpy.data.images["Chest_Atlas"], 3)
    NOTES["atlas_dirty"] = bpy.data.images["Chest_Atlas"].is_dirty


CURRENT_JOB: dict = {}
NOTES: dict = {}  # What a model function saw in its copy, into its manifest.


def worker_main(jobs_path):
    jobs = json.loads(Path(jobs_path).read_text())
    for n, job in enumerate(jobs["jobs"]):
        if n:
            bpy.ops.wm.open_mainfile(filepath=jobs["spawn"])
        CURRENT_JOB.clear()
        CURRENT_JOB.update(job)
        NOTES.clear()
        t0 = time.time()
        ownership.stamp_all()
        stamp_s = time.time() - t0
        globals()[f"model_{job['worker']}"]()
        bpy.context.view_layer.update()
        manifest = ownership.write_result(job["result"], set(job["owned"]), job["prefix"], job["role"],
                                          job.get("aspects", []))
        manifest["world_locations"] = {ob.name: rnd(ob.matrix_world.translation)
                                       for ob in bpy.data.objects if not ob.get(STAMP)}
        manifest["stamp_s"] = round(stamp_s, 4)
        manifest["notes"] = dict(NOTES)
        Path(job["manifest"]).write_text(json.dumps(manifest, indent=1))
        print(f"[worker {job['worker']}] done: created={len(manifest['created'])} "
              f"advisory={manifest['advisory']}")


# ------------------------------------------------------------------ parent

def run_worker(folder: Path, spawn: merge.Spawn, jobs: list, while_running=None) -> dict:
    """One background Blender on the spawn copy, running the jobs in turn; returns the manifests."""
    for job in jobs:
        job.update(result=str(folder / job["worker"] / "result.blend"), manifest=str(folder / f"manifest_{job['worker']}.json"))
    jobs_path = folder / "jobs.json"
    jobs_path.write_text(json.dumps({"spawn": str(spawn.path), "jobs": jobs}))
    log = open(folder / "worker.log", "w")
    t0 = time.time()
    process = subprocess.Popen([bpy.app.binary_path, "-b", "--factory-startup", str(spawn.path), "--python-exit-code", "1",
                                "--python", __file__, "--", "worker", str(jobs_path)], stdout=log, stderr=subprocess.STDOUT)
    try:
        if while_running:
            while_running()  # The user keeps working during the wave.
        process.wait(timeout=WORKER_TIMEOUT)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        log.close()
    manifests = {}
    for job in jobs:
        path = Path(job["manifest"])
        if process.returncode != 0 or not path.exists():
            tail = "".join((folder / "worker.log").read_text().splitlines(True)[-20:])
            raise RuntimeError(f"worker run failed rc={process.returncode}:\n{tail}")
        manifests[job["worker"]] = json.loads(path.read_text())
    print(f"[parent] {len(jobs)} workers in one child: {time.time() - t0:.2f}s")
    return manifests


def results_of(jobs: list, manifests: dict) -> list:
    return [{"brief": {"id": j["worker"], "prefix": j["prefix"], "role": j["role"], "aspects": j.get("aspects", []),
                       "deliverable": j.get("deliverable", j["prefix"]), "requirements": j.get("requirements", []),
                       "checks": j.get("checks", [])},
             "manifest": manifests[j["worker"]]} for j in jobs]


def backup(paths: list, folder: Path) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    pairs = [(Path(p), folder / f"{n}_{Path(p).name}") for n, p in enumerate(paths)]
    for source, copy in pairs:
        shutil.copy2(source, copy)
    return pairs


def restore(pairs: list) -> None:
    for source, copy in pairs:
        shutil.copy2(copy, source)


def fingerprint() -> dict:
    """Everything a merge could change, by name: tokens, collection members, pointers, libraries."""
    scene = bpy.context.scene
    _ids, tokens = ownership.spawn_state()
    return {"tokens": tokens, "libraries": sorted(lib.name for lib in bpy.data.libraries),
            "members": {c.name: sorted(o.name for o in c.objects) for c in bpy.data.collections},
            "top": sorted(o.name for o in scene.collection.objects) + sorted(c.name for c in scene.collection.children),
            "camera": scene.camera.name if scene.camera else None, "world": scene.world.name if scene.world else None,
            "stamped": sorted(ownership.key_of(i) for i in ownership.all_ids() if i.get(STAMP))}


def external_refs() -> dict:
    """What the scene still points at outside itself, the strict way: every path Blender walks,
    weak references included (bpy.utils.blend_paths, what render_job and inspect_scene used to
    read), that is missing or names a result.blend; datablocks carrying a weak reference; and
    what render_job's own check says."""
    from loopcut import job_tools, jobs
    paths = bpy.utils.blend_paths(absolute=True, packed=False, local=False)
    weak = [f"{i.id_type}:{i.name}" for i in merge._datablocks() if i.library_weak_reference is not None]
    try:
        job_tools.external_assets()
        refused = ""
    except jobs.JobError as ex:
        refused = str(ex)
    return {k: v for k, v in {"missing": sorted({p for p in paths if not os.path.isfile(p)}),
                              "result_paths": sorted({p for p in paths if "result.blend" in p}),
                              "weak": sorted(weak), "refused": refused}.items() if v}


def session(tmp: Path) -> dict:
    """A conversation where the orchestrator's delegate call is message 2 and the wave card item 3."""
    from loopcut import state
    sess = state.new_session()
    sess.update(id="ab" * 16, input="half-typed")
    sess["messages"] += [{"role": "user", "content": "go"}, {"role": "assistant", "content": "plan"},
                         {"role": "assistant", "content": "delegate"}, {"role": "tool", "content": "report"}]
    sess["items"] += [state.item_user("go"), {"kind": "assistant", "text": "plan"}, {"kind": "notice", "text": "x"},
                      {"kind": "wave", "text": "Wave 1"}, {"kind": "merge", "text": "Merged"}]
    return sess


def spike_checks(spawn: merge.Spawn, report: merge.MergeReport, manifests: dict) -> None:
    """The spike's S1 checks, on what merge_wave did."""
    by_uid = {i.session_uid: i for i in ownership.all_ids(local=False)}
    # B's Chair_Fabric (different colour, same name as A's) is the deliberate collision: Blender's own
    # append gives it .001, and that is the only .001 name the merge should have let through.
    dots = [f"{i.id_type}:{i.name}" for i in ownership.all_ids(local=False)
            if DOT_NUM.search(i.name) and f"{i.id_type}:{i.name}" != "MATERIAL:Chair_Fabric.001"]
    check("no unexpected .001 datablocks", not dots, ", ".join(dots))
    check("B's Chair_Fabric collided with A's and got Blender's own .001, reported in the merge",
          bpy.data.materials.get("Chair_Fabric.001") is not None
          and any(r == "B: MATERIAL Chair_Fabric -> Chair_Fabric.001" for r in report.renamed),
          report.renamed)
    live_rug = by_uid.get(spawn.ids["OBJECT:Rug"])
    leg1, troot = obj("Chair_Leg_1"), obj("Table_Root")
    t1 = leg1.constraints[0].target if leg1 and leg1.constraints else None
    t2 = troot.constraints[0].target if troot and troot.constraints else None
    check("constraint target is the live Rug (renamed by user mid-wave)",
          live_rug is not None and t1 == live_rug and t2 == live_rug,
          f"leg1->{t1.name if t1 else None}, table_root->{t2.name if t2 else None}")
    box, cutter, seat = obj("Box"), by_uid.get(spawn.ids["OBJECT:Cutter"]), obj("Chair_Seat")
    check("Boolean cutters are the live Cutter (live Box + merged Chair_Seat)",
          box.modifiers["Cut"].object == cutter and seat and seat.modifiers[0].object == cutter,
          f"box->{box.modifiers['Cut'].object.name}, seat->{seat.modifiers[0].object.name if seat else None}")
    dvar_box = box.animation_data.drivers[0].driver.variables[0].targets[0].id
    dvar_seat = seat.animation_data.drivers[0].driver.variables[0].targets[0].id if seat else None
    floor = by_uid.get(spawn.ids["OBJECT:Floor"])
    check("driver targets unchanged (Box->live Rug, Chair_Seat->live Floor)", dvar_box == live_rug and dvar_seat == floor,
          f"box->{dvar_box.name if dvar_box else None}, seat->{dvar_seat.name if dvar_seat else None}")
    root = obj("Chair_Root")
    legs_ok = all(obj(f"Chair_Leg_{i}") and obj(f"Chair_Leg_{i}").parent == root for i in range(1, 5))
    tb, lamp, shade = obj("Leg"), obj("Lamp_Light"), obj("Lamp_Shade")
    bpy.context.view_layer.update()
    want = {k: v for k, v in manifests["A"]["world_locations"].items() if k.startswith("Chair")}
    want.update({k: v for k, v in manifests["B"]["world_locations"].items() if k.startswith("Table") or k == "Leg"})
    moved = [k for k, v in want.items() if obj(k) is None or rnd(obj(k).matrix_world.translation) != tuple(v)]
    check("parenting intact (legs->roots, Lamp_Shade->new Lamp_Light, world positions kept)",
          legs_ok and tb and tb.parent == troot and shade.parent == lamp and not moved,
          f"legs_ok={legs_ok} shade_parent={shade.parent.name if shade.parent else None} moved={moved}")
    woods = [m for m in bpy.data.materials if m.name.startswith("Palette_Wood")]
    wood_live = by_uid.get(spawn.ids["MATERIAL:Palette_Wood"])
    leg_mats = {obj(f"Chair_Leg_{i}").data.materials[0] for i in range(1, 5)} | {tb.data.materials[0]}
    check("Palette_Wood is one datablock (the live one), used by merged legs",
          len(woods) == 1 and woods[0] == wood_live and leg_mats == {wood_live}, f"{[m.name for m in woods]}")
    cf, tf = bpy.data.materials.get("Chair_Fabric"), bpy.data.materials.get("Chair_Fabric.001")
    check("both fabric materials exist with distinct names and the right users",
          cf and tf and cf != tf and seat.data.materials[0] == cf and obj("Table_Top").data.materials[0] == tf,
          f"{cf.name if cf else None} / {tf.name if tf else None}")
    check("B's unprefixed 'Leg' kept its own name (no collision to resolve)", tb is not None and tb.name == "Leg")
    check("Rug unchanged (scale 1,1,1; only the user's rename)",
          live_rug and rnd(live_rug.scale) == (1.0, 1.0, 1.0) and live_rug.name == "Rug_Persian",
          f"scale={rnd(live_rug.scale) if live_rug else None}")
    dropped = [d for d in report.dropped_edits if d["key"] == "OBJECT:Rug" and d["worker"] == "B"]
    check("B's out-of-ownership Rug change dropped + reported (not merged)",
          dropped and dropped[0]["name"] == "Rug_Persian" and dropped[0]["worker_value"]["scale"] == [2.0, 2.0, 1.0],
          json.dumps(report.dropped_edits))
    lamps = [o for o in bpy.data.objects if o.name.startswith("Lamp_Light")]
    cam = obj("Cam_Main")
    check("owned Lamp_Light replaced (colour+power), live refs follow",
          len(lamps) == 1 and lamp.data.name == "Lamp_Light" and rnd(lamp.data.energy) == 250.0
          and rnd(lamp.data.color) == (1.0, 0.8, 0.6) and cam.constraints[0].target == lamp
          and list(lamp.users_collection) == [bpy.context.scene.collection],
          f"energy={lamp.data.energy} collections={[c.name for c in lamp.users_collection]}")
    leftovers = [c.name for c in bpy.data.collections if c.name.startswith(merge.STAGING)]
    libs = [lib.name for lib in bpy.data.libraries]
    stamped = [ownership.key_of(i) for i in ownership.all_ids() if i.get(STAMP)]
    check("no staging collections / stray libraries / stamps left",
          not leftovers and libs == ["lib_assets.blend"] and not stamped, f"staging={leftovers} libs={libs} stamps={stamped}")
    varnish = [m for m in bpy.data.materials if m.name.startswith("Lib_Varnish")]
    check("linked Lib_Varnish reused (one linked datablock, used by merged seat)",
          len(varnish) == 1 and varnish[0].library is not None and seat.data.materials[1] == varnish[0],
          f"{[(m.name, m.library.name if m.library else None) for m in varnish]}")
    imgs = [i.name for i in bpy.data.images]
    grain_live = by_uid.get(spawn.ids["IMAGE:Wood_Grain"])
    check("packed image Wood_Grain is one datablock, still packed, still in Palette_Wood",
          imgs == ["Wood_Grain"] and grain_live is not None and grain_live.packed_file is not None
          and any(getattr(n, "image", None) == grain_live for n in wood_live.node_tree.nodes), f"images={imgs}")
    refs = external_refs()
    check("nothing merged points at a result.blend: no weak references, every path found, render_job's check passes",
          not refs, json.dumps(refs))


def wave_1(tmp: Path, live_path: Path, data: dict) -> tuple:
    folder = tmp / "job1" / "w1"
    spawn = merge.save_spawn(folder)
    (folder.parent / "job.json").write_text("{}")
    owns_a = ownership.expand_owns(["Lamp_Light"], [])
    check("expand_owns: an object with its single-user data", owns_a == {"OBJECT:Lamp_Light", "LIGHT:Lamp_Light"},
          sorted(owns_a))
    jobs = [{"worker": "A", "prefix": "Chair", "role": "modeling", "owned": sorted(owns_a)},
            {"worker": "B", "prefix": "Table", "role": "modeling", "owned": []}]

    def rename():
        bpy.data.objects["Rug"].name = "Rug_Persian"
    manifests = run_worker(folder, spawn, jobs, rename)
    advisory = manifests["B"]["advisory"]
    check("child: B's Rug edit found by changed_keys; 'unprefixed' is no longer advised (names are the "
          "worker's own; only a real collision is a merge concern)",
          advisory["changed_not_owned"] == ["OBJECT:Rug"] and "unprefixed" not in advisory
          and not manifests["A"]["advisory"]["changed_not_owned"], json.dumps(advisory))
    kept = backup([manifests["A"]["result"]], tmp / "backup1")
    results = results_of(jobs, manifests)
    sess = session(tmp)
    report = merge.merge_wave(spawn, results, lambda: checkpoints.ensure_for_wave(sess, "Wave 1", 2, 3))
    print(f"[parent] wave 1 merge {report.seconds}s: {report.headline()}")
    spike_checks(spawn, report, manifests)
    entry = checkpoints.Store(checkpoints.data_root(), sess["id"]).get(report.checkpoint)
    check("wave checkpoint written first (kind wave, delegate message and card indices)",
          entry["kind"] == "wave" and entry["message_index"] == 2 and entry["item_index"] == 3
          and checkpoints.Store(checkpoints.data_root(), sess["id"]).snapshot_path(report.checkpoint).is_file(), entry)
    gone = [p for p in [spawn.path] + [m["result"] for m in manifests.values()] if Path(p).exists()]
    check("temporary files gone (spawn.blend, every result.blend); job.json kept",
          not gone and (folder.parent / "job.json").exists(), f"left: {gone}")
    undo_ok = merge._undo("undo")
    undone = obj("Chair_Root") is None and obj("Table_Root") is None and \
        rnd(bpy.data.lights["Lamp_Light"].energy) == 100.0 and obj("Rug_Persian") is not None
    redo_ok = merge._undo("redo")
    redone = obj("Chair_Root") is not None and rnd(bpy.data.lights["Lamp_Light"].energy) == 250.0
    check("one undo step spans the merge (undo -> pre-merge, redo -> merged)", undo_ok and undone and redo_ok and redone,
          f"undo={undo_ok} undone={undone} redo={redo_ok} redone={redone}")
    destination = checkpoints.restore(sess, report.checkpoint)
    kinds = [i["kind"] for i in sess["items"]]
    check("restoring the wave checkpoint: scene before the merge, conversation cut at the delegate call",
          obj("Chair_Root") is None and obj("Rug_Persian") is not None and rnd(bpy.data.lights["Lamp_Light"].energy) == 100.0
          and len(sess["messages"]) == 2 and kinds == ["user", "assistant", "notice", "notice"]
          and sess["input"] == "half-typed" and destination is not None, f"{kinds} {len(sess['messages'])} {destination}")
    return jobs, manifests, kept


def stale(tmp: Path, live_path: Path, jobs: list, manifests: dict, kept: list) -> None:
    """S2: the user edits the owned Lamp_Light during the wave."""
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    ids, tokens = ownership.spawn_state()
    dummy = tmp / "job1" / "s2" / "spawn.blend"
    dummy.parent.mkdir(parents=True, exist_ok=True)
    dummy.write_bytes(b"")
    spawn = merge.Spawn("job1-s2", dummy, ids, tokens, {"camera": "OBJECT:Cam_Main", "world": "WORLD:LiveWorld"})
    bpy.data.lights["Lamp_Light"].energy = 40.0
    restore(kept)
    report = merge.merge_wave(spawn, results_of(jobs[:1], manifests), lambda: "")
    lamp = bpy.data.lights.get("Lamp_Light")
    check("staleness conflict detected; user's Lamp edit kept, worker's not applied",
          report.conflicts and not report.replaced and lamp and rnd(lamp.energy) == 40.0
          and len([o for o in bpy.data.objects if o.name.startswith("Lamp_Light")]) == 1,
          json.dumps(report.conflicts[0]) if report.conflicts else "no conflict reported")
    dots = [f"{i.id_type}:{i.name}" for i in ownership.all_ids(local=False) if DOT_NUM.search(i.name)]
    check("S2 also leaves no .001 datablocks", not dots, ", ".join(dots))


def wave_2(tmp: Path, live_path: Path) -> None:
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    folder = tmp / "job2" / "w2"
    spawn = merge.save_spawn(folder)
    own = {"C": ownership.expand_owns([], []), "D": ownership.expand_owns([], ["camera"]),
           "E": ownership.expand_owns([], ["lighting"]), "F": ownership.expand_owns(["Post_A"], []),
           "G": ownership.expand_owns(["Post_B"], [])}
    check("expand_owns: shared data stays unowned; aspects own every camera / light and the world",
          own["F"] == {"OBJECT:Post_A"} and own["G"] == {"OBJECT:Post_B"}
          and own["D"] == {"OBJECT:Cam_Main", "CAMERA:Cam_Main"}
          and own["E"] == {"OBJECT:Lamp_Light", "LIGHT:Lamp_Light", "WORLD:LiveWorld"}
          and "MESH:BoxMesh" in ownership.expand_owns(["Box"], []), {k: sorted(v) for k, v in own.items()})
    try:
        ownership.expand_owns(["Nope"], [])
        refused = False
    except ValueError as ex:
        refused = "Nope" in str(ex)
    check("expand_owns refuses a name that isn't there", refused)
    jobs = [{"worker": "C", "prefix": "Vase", "role": "modeling", "deliverable": "Vase"},
            {"worker": "D", "prefix": "Shot", "role": "camera", "aspects": ["camera"]},
            {"worker": "E", "prefix": "Light", "role": "lighting", "aspects": ["lighting"]},
            {"worker": "F", "prefix": "PostA", "role": "modeling"},
            {"worker": "G", "prefix": "PostB", "role": "modeling"}]
    for job in jobs:
        job["owned"] = sorted(own[job["worker"]])

    def user_changes_world():
        bpy.context.scene.world = bpy.data.worlds.new("UserWorld")
    manifests = run_worker(folder, spawn, jobs, user_changes_world)
    advisory = {w: m["advisory"] for w, m in manifests.items()}
    check("child: creating into the user's Props collection is not a change to it",
          "Vase_Body" in bpy.data.collections["Props"].name + " " + " ".join(manifests["C"]["created"]) and
          advisory["C"]["changed_not_owned"] == [] and advisory["G"]["changed_not_owned"] == []
          and "MESH:PostB_Mesh" in manifests["G"]["created"]
          and advisory["F"]["changed_not_owned"] == ["MESH:PostMesh"] and "OBJECT:Vase_TestLight" in advisory["C"]["outside_role"],
          json.dumps(advisory))
    results = results_of(jobs, manifests)
    pairs = backup([spawn.path] + [m["result"] for m in manifests.values()], tmp / "backup2")

    # An exception mid-merge (the third worker, after two have merged): the scene is as before.
    before = fingerprint()
    real, calls = merge._link_created, []

    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("injected")
        return real(*args, **kwargs)
    merge._link_created = failing
    try:
        merge.merge_wave(spawn, results, lambda: "")
        raised = ""
    except merge.MergeError as ex:
        raised = str(ex)
    finally:
        merge._link_created = real
    after = fingerprint()
    diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    if "tokens" in diff:
        b, a = diff["tokens"]
        diff["tokens"] = {k: (b.get(k), a.get(k)) for k in set(b) | set(a) if b.get(k) != a.get(k)}
    check("an exception mid-merge undoes it: MergeError, scene identical to before",
          "injected" in raised and "back as it was" in raised and not diff and len(calls) == 3, f"{raised} {diff}")
    restore(pairs)

    sess = session(tmp)
    report = merge.merge_wave(spawn, results, lambda: checkpoints.ensure_for_wave(sess, "Wave 2", 5, 6))
    print(f"[parent] wave 2 merge {report.seconds}s: {report.headline()}")
    for line in report.lines():
        print(f"[parent]   {line}")
    gone = [p for p in [spawn.path] + [m["result"] for m in manifests.values()] if Path(p).exists()]
    undo_ok = merge._undo("undo")
    undone = obj("Shot_Cam") is None and bpy.context.scene.camera == obj("Cam_Main") and obj("Vase_Body") is None
    redo_ok = merge._undo("redo")
    redone = obj("Shot_Cam") is not None and bpy.context.scene.camera == obj("Shot_Cam")
    check("wave 2: one undo step, temporary files gone", not gone and undo_ok and undone and redo_ok and redone,
          f"left={gone} undo={undo_ok} undone={undone} redo={redo_ok} redone={redone}")
    scene = bpy.context.scene
    vase, light, props = obj("Vase_Body"), obj("Vase_TestLight"), bpy.data.collections["Props"]
    quarantine = bpy.data.collections.get(merge.QUARANTINE_COLLECTION)
    layer = next((lc for lc in scene.view_layers[0].layer_collection.children if lc.collection == quarantine), None)
    check("created objects stay in the user's collection the worker put them in on purpose, and no collection "
          "per worker",
          vase and list(vase.users_collection) == [props] and bpy.data.collections.get("Vase") is None
          and sorted(o.name for o in props.objects) == ["Stool", "Vase", "Vase_Body"] and not vase.get(ownership.COLLECTIONS),
          [c.name for c in vase.users_collection] if vase else None)
    check("a root named for the deliverable keeps its name (the brief's checks name it)",
          obj("Vase") is not None and obj("Vase_Vase") is None and vase and vase.parent == obj("Vase"),
          sorted(o.name for o in bpy.data.objects if o.name.startswith("Vase")))
    check("out-of-role light quarantined: hidden collection, excluded from the view layer, tagged, reported",
          light and quarantine and list(light.users_collection) == [quarantine] and layer and layer.exclude
          and quarantine.hide_render and light.get(ownership.QUARANTINE) == f"{spawn.wave}/C"
          and [(q["name"], q["type"], q["hidden"]) for q in report.quarantined] == [("Vase_TestLight", "LIGHT", True)]
          and light.data.get(ownership.QUARANTINE),
          json.dumps(report.quarantined))
    text = merge.accept_quarantine(spawn.wave)
    light = obj("Vase_TestLight")
    check("accept_quarantine moves it where the rest of its worker's work went and clears the tags",
          light and list(light.users_collection) == [scene.collection] and not light.get(ownership.HOME)
          and not light.get(ownership.QUARANTINE) and not light.data.get(ownership.QUARANTINE)
          and bpy.data.collections.get(merge.QUARANTINE_COLLECTION) is None and "Vase_TestLight" in text, text)
    check("accept_quarantine again: nothing left", "Nothing from wave" in merge.accept_quarantine(spawn.wave))
    cam_main = obj("Cam_Main")
    check("camera aspect: scene.camera is the merged Shot_Cam; owned Cam_Main replaced with its new lens",
          scene.camera == obj("Shot_Cam") and cam_main and rnd(cam_main.data.lens) == 85.0
          and len([c for c in bpy.data.cameras if c.name.startswith("Cam_Main")]) == 1,
          f"camera={scene.camera.name if scene.camera else None} lens={cam_main.data.lens if cam_main else None}")
    pointer = [c for c in report.conflicts if c.get("pointer") == "scene.world"]
    check("lighting aspect: the user's mid-wave world change kept, the worker's reported as a conflict",
          scene.world and scene.world.name == "UserWorld" and pointer and pointer[0]["worker"] == "E"
          and bpy.data.worlds.get("Light_World") is not None, json.dumps(report.conflicts))
    post_a, post_b, shared = obj("Post_A"), obj("Post_B"), bpy.data.meshes.get("PostMesh")
    heights = lambda me: round(max(v.co.z for v in me.vertices) - min(v.co.z for v in me.vertices), 4)  # noqa: E731
    dropped = [d for d in report.dropped_edits if d["key"] == "MESH:PostMesh"]
    check("shared mesh: an in-place edit by the owner of one user is dropped, a single-user copy is merged",
          post_a.data == shared and heights(shared) == 1.0 and rnd(post_a.location) == (3.5, 2.0, 0.5)
          and post_b.data.name == "PostB_Mesh" and heights(post_b.data) == 3.0 and dropped and dropped[0]["worker"] == "F",
          f"post_a={post_a.data.name} {heights(post_a.data)} post_b={post_b.data.name} {heights(post_b.data)} "
          f"dropped={report.dropped_edits}")
    stamped = [ownership.key_of(i) for i in ownership.all_ids() if i.get(STAMP) or i.get(ownership.POINTER)]
    check("wave 2 leaves no stamps, pointer tags or staging collections",
          not stamped and not [c for c in bpy.data.collections if c.name.startswith(merge.STAGING)], stamped)
    refs = external_refs()
    check("wave 2 leaves nothing pointing at a result.blend", not refs, json.dumps(refs))


def wave_3(tmp: Path, live_path: Path) -> None:
    """perfume_ad's wave (a lighting and a camera worker) and shared_support's lamp: after the merge
    nothing points at a result.blend (every inspect_scene and render_job refused one), an object
    whose parent or constraint target was held back says so, and accepting one object by name
    takes it out of LC_Quarantine. The lamp worker owns a light and a mesh and leaves them as they
    were (shared_support's owned the scene's Light, which the merge put into LC_Quarantine for good);
    the camera worker's copy of Lamp_Light, which Cam_Main tracks, meets the lighting worker's
    replacement of it."""
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    lighting = sorted(ownership.expand_owns([], ["lighting"]))
    desk = bpy.data.objects.new("Desk_Light", bpy.data.lights.new("Desk_Light", "POINT"))
    bpy.context.scene.collection.objects.link(desk)
    folder = tmp / "job3" / "w3"
    spawn = merge.save_spawn(folder)
    jobs = [{"worker": "H", "prefix": "Lit", "role": "lighting", "aspects": ["lighting"], "owned": lighting},
            {"worker": "I", "prefix": "Shot", "role": "camera", "aspects": ["camera"],
             "owned": sorted(ownership.expand_owns([], ["camera"]))},
            {"worker": "J", "prefix": "FloorLamp", "role": "modeling", "deliverable": "FloorLamp",
             "owned": sorted(ownership.expand_owns(["Desk_Light", "Stool"], []))}]
    manifests = run_worker(folder, spawn, jobs)
    before = external_refs()
    report = merge.merge_wave(spawn, results_of(jobs, manifests), lambda: "")
    print(f"[parent] wave 3 merge {report.seconds}s: {report.headline()}")
    for line in report.lines():
        print(f"[parent]   {line}")
    scene = bpy.context.scene
    refs = external_refs()
    check("perfume_ad: nothing merged points at a result.blend (weak references, paths); render_job's check passes",
          not before and not refs and [lib.name for lib in bpy.data.libraries] == ["lib_assets.blend"],
          json.dumps({"before": before, "after": refs}))
    world, tint, grid = scene.world, bpy.data.node_groups.get("Lit_Tint"), bpy.data.images.get("Lit_Grid")
    ramp = bpy.data.images.get("Lit_Ramp")
    kinds = sorted(n.bl_idname for n in world.node_tree.nodes) if world else []
    glow = bpy.data.materials.get("Lit_CardGlow")
    check("perfume_ad: the merged world, its node group, images and the emissive card are whole and local",
          world and world.name == "Lit_World" and "ShaderNodeMixShader" in kinds and tint and grid and ramp
          and any(getattr(n, "node_tree", None) == tint for n in world.node_tree.nodes)
          and grid.packed_file is not None and tuple(grid.size) == (32, 32)
          and os.path.isabs(ramp.filepath) and os.path.isfile(ramp.filepath)
          and any(getattr(n, "image", None) == grid for n in glow.node_tree.nodes)
          and obj("Lit_Card").data.materials[0] == glow
          and all(i.library is None for i in (world, tint, grid, ramp, glow, obj("Lit_Spot").data))
          and not [i.name for i in merge._datablocks() if DOT_NUM.search(i.name) and i.library is None],
          f"world={world.name if world else None} kinds={kinds} ramp={ramp.filepath if ramp else None}")
    check("perfume_ad: scene.world and scene.camera from the aspect workers; owned lamp and Cam_Main replaced",
          scene.camera == obj("Shot_CloseUp") and rnd(bpy.data.lights["Lamp_Light"].energy) == 40.0
          and rnd(obj("Cam_Main").data.lens) == 35.0 and obj("Shot_CloseUp").data.dof.focus_object == obj("Box"),
          f"camera={scene.camera.name if scene.camera else None}")
    lamps = sorted(o.name for o in bpy.data.objects if o.name.startswith("Lamp_Light"))
    check("a worker's copy of what an earlier worker of the wave replaced goes to the replacement",
          not report.errors and lamps == ["Lamp_Light"] and len([d for d in bpy.data.lights if d.name.startswith("Lamp_Light")]) == 1
          and obj("Cam_Main").constraints[0].target == obj("Lamp_Light") and obj("Lamp_Shade").parent == obj("Lamp_Light"),
          f"errors={report.errors} lamps={lamps}")
    stool, desk = obj("Stool"), obj("Desk_Light")
    check("owned objects a worker left as they were stay where the user had them (not quarantined, not moved)",
          desk and [c.name for c in desk.users_collection] == ["Scene Collection"]
          and stool and [c.name for c in stool.users_collection] == ["Props"] and not desk.get(ownership.QUARANTINE),
          f"desk={[c.name for c in desk.users_collection] if desk else None} "
          f"stool={[c.name for c in stool.users_collection] if stool else None}")
    created = {c["name"]: c for c in report.created if c["id_type"] == "OBJECT"}
    lines = report.lines()
    helpers = {name: [c.name for c in obj(name).users_collection] if obj(name) else None
               for name in ("Lit_Spot_root", "Lit_Aim", "Shot_Target")}
    check("roles create their helpers: the lighting pivot and aim empties and the camera's aim target merge",
          helpers == {"Lit_Spot_root": ["Scene Collection"], "Lit_Aim": ["Scene Collection"], "Shot_Target": ["Scene Collection"]}
          and not any(obj(n).get(ownership.QUARANTINE) for n in helpers if obj(n))
          and obj("Shot_CloseUp").constraints[0].target == obj("Shot_Target"), json.dumps(helpers))
    check("an object whose parent or constraint target was held back says so in its report line",
          created["FloorLamp_Bulb"].get("held") == ["parent FloorLamp_Light"]
          and created["FloorLamp_Glow"].get("held") == ["Copy Location target FloorLamp_Light"]
          and "J: FloorLamp_Bulb (parent FloorLamp_Light held back)" in lines
          and "J: FloorLamp_Glow (Copy Location target FloorLamp_Light held back)" in lines
          and not created["Lit_Spot"].get("held") and not created["Lit_Card"].get("held")
          and not created["FloorLamp_Shade"].get("held") and not created["Shot_CloseUp"].get("held"),
          json.dumps({"created": list(created.values()), "lines": lines}))
    quarantine = bpy.data.collections.get(merge.QUARANTINE_COLLECTION)
    held = sorted(o.name for o in quarantine.objects) if quarantine else []
    text = merge.accept_quarantine(spawn.wave, ["FloorLamp_Light"])
    light, quarantine = obj("FloorLamp_Light"), bpy.data.collections.get(merge.QUARANTINE_COLLECTION)
    check("accept_quarantine by name: the object leaves LC_Quarantine for its worker's collection; the rest stay",
          held == ["FloorLamp_Light", "Shot_Fill"] and light
          and [c.name for c in light.users_collection] == ["Scene Collection"] and not light.get(ownership.QUARANTINE)
          and quarantine and sorted(o.name for o in quarantine.objects) == ["Shot_Fill"],
          f"{text} held={held} light_in={[c.name for c in light.users_collection] if light else None} "
          f"left={sorted(o.name for o in quarantine.objects) if quarantine else None}")
    text = merge.accept_quarantine(spawn.wave)
    fill = obj("Shot_Fill")
    check("accept_quarantine for the rest empties and removes LC_Quarantine; the fill light is with the rest",
          bpy.data.collections.get(merge.QUARANTINE_COLLECTION) is None and fill
          and [c.name for c in fill.users_collection] == ["Scene Collection"] and obj("FloorLamp_Bulb").parent == obj("FloorLamp_Light"),
          text)
    missing = bpy.data.images.new("Lost_Texture", 4, 4)
    missing.source = "FILE"
    missing.filepath = str(tmp / "gone" / "lost.png")
    from loopcut import job_tools, jobs as job_module
    try:
        job_tools.external_assets()
        refused = ""
    except job_module.JobError as ex:
        refused = str(ex)
    bpy.data.images.remove(missing)
    check("the missing-file refusal names the datablock and the full path",
          'image "Lost_Texture"' in refused and str(tmp / "gone" / "lost.png") in refused, refused)


def wave_split(tmp: Path, live_path: Path) -> None:
    """Two merges from one spawn (waves.py merges workers as they finish): H alone first, keep_spawn;
    then the orchestrator changes Cam_Main, which I owns; then I."""
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    folder = tmp / "job_split" / "w1"
    spawn = merge.save_spawn(folder)
    jobs = [{"worker": "H", "prefix": "Lit", "role": "lighting", "aspects": ["lighting"],
             "owned": sorted(ownership.expand_owns([], ["lighting"]))},
            {"worker": "I", "prefix": "Shot", "role": "camera", "aspects": ["camera"],
             "owned": sorted(ownership.expand_owns([], ["camera"]))}]
    manifests = run_worker(folder, spawn, jobs)
    results = results_of(jobs, manifests)
    first = merge.merge_wave(spawn, results[:1], lambda: "", keep_spawn=True)
    kept = Path(spawn.path).is_file()
    bpy.data.cameras["Cam_Main"].lens = 85.0  # The orchestrator's own edit between the two merges.
    second = merge.merge_wave(spawn, results[1:], lambda: "")
    lamps = sorted(o.name for o in bpy.data.objects if o.name.startswith("Lamp_Light"))
    check("split merge: the spawn copy stays for the second call and goes after it",
          kept and not Path(spawn.path).exists() and not first.errors, f"kept={kept} errors={first.errors}")
    check("split merge: a later call's copy of what an earlier call replaced goes to the replacement",
          not second.errors and lamps == ["Lamp_Light"] and rnd(bpy.data.lights["Lamp_Light"].energy) == 40.0
          and obj("Cam_Main").constraints[0].target == obj("Lamp_Light"), f"errors={second.errors} lamps={lamps}")
    stale_ = [c for c in second.conflicts if "CAMERA:Cam_Main" in c.get("keys", ())]
    check("split merge: what the orchestrator changed between the calls is a conflict, its version kept",
          stale_ and rnd(bpy.data.cameras["Cam_Main"].lens) == 85.0 and bpy.context.scene.camera == obj("Shot_CloseUp"),
          json.dumps(second.conflicts, default=str))
    undo_ok = merge._undo("undo")
    undone = obj("Shot_CloseUp") is None and bpy.context.scene.world and bpy.context.scene.world.name == "Lit_World"
    redo_ok = merge._undo("redo")
    check("split merge: each call is its own undo step (undo takes out the second, keeps the first)",
          undo_ok and undone and redo_ok and obj("Shot_CloseUp") is not None,
          f"undo={undo_ok} undone={undone} redo={redo_ok}")


def wave_4(tmp: Path, live_path: Path) -> None:
    """A palette the orchestrator made before the wave, that no object wore yet: every worker's copy
    lacked it, the workers remade it, and the merge prefixed the duplicates (Crystal_Glow_Blue) so
    the live material_on check failed. The copy keeps what nothing uses; a material made again the
    same is the live one."""
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    scene = bpy.context.scene
    main = bpy.data.collections.new("Main")  # Where the scene's own objects are, as the startup file's "Collection".
    scene.collection.children.link(main)
    for ob in list(scene.collection.objects):
        main.objects.link(ob)
        scene.collection.objects.unlink(ob)
    glow, glass = new_material("Pal_Glow", (0.2, 0.6, 1.0)), new_material("Pal_Glass", (0.9, 0.9, 0.95))
    new_material("Pal_Stone", (0.3, 0.3, 0.3))
    orphan = bpy.data.meshes.new("Orphan_Mesh")  # Nothing uses the mesh, so nothing writes Pal_Moss.
    orphan.materials.append(new_material("Pal_Moss", (0.2, 0.4, 0.1)))
    bpy.data.node_groups.new("Pal_Tint", "ShaderNodeTree")
    bpy.data.images.new("Pal_Noise", 8, 8)
    bpy.data.meshes.new("Crystal_Twin").use_fake_user = True  # Another mesh's name: Crystal_Twin's mesh keeps its own.
    folder = tmp / "job4" / "w4"
    spawn = merge.save_spawn(folder, ["Pal_Glow", "Pal_Moss"])
    with bpy.data.libraries.load(str(spawn.path)) as (copy, _target):
        in_copy = {"materials": set(copy.materials), "node_groups": set(copy.node_groups), "images": set(copy.images)}
    fake = [i.name for i in (glow, glass, bpy.data.materials["Pal_Moss"], bpy.data.node_groups["Pal_Tint"],
                             bpy.data.images["Pal_Noise"]) if i.use_fake_user]
    check("the workers' copy keeps what nothing uses yet (materials, node groups, images) and the palette; "
          "the live file gets no fake user",
          {"Pal_Glow", "Pal_Glass", "Pal_Stone", "Pal_Moss"} <= in_copy["materials"] and "Pal_Tint" in in_copy["node_groups"]
          and "Pal_Noise" in in_copy["images"] and not fake, json.dumps({k: sorted(v) for k, v in in_copy.items()} | {"fake": fake}))
    count = {"kind": "count", "collection": "Props", "min": 2, "quote": "two boxes in Props"}
    jobs = [{"worker": "K", "prefix": "Crystal", "role": "modeling", "deliverable": "Crystal", "owned": [],
             "requirements": [{"quote": "a crystal", "do": "a crystal"}]},
            {"worker": "L", "prefix": "Crate", "role": "modeling", "owned": [],
             "requirements": [{"quote": "each crate set in its own collection", "do": "two crates"}]},
            {"worker": "M", "prefix": "Stack", "role": "modeling", "owned": sorted(ownership.expand_owns(["Props"], [])),
             "requirements": [{"quote": "two boxes in Props", "do": "two stacked boxes"}], "checks": [count]},
            {"worker": "N", "prefix": "Tree", "role": "modeling", "owned": [],
             "requirements": [{"quote": "a tree", "do": "an instanced tree"}]},
            {"worker": "P", "prefix": "Pile", "role": "modeling", "owned": sorted(ownership.expand_owns(["Props"], [])),
             "requirements": [{"quote": "a pile in a collection of its own", "do": "a pile"}]}]
    manifests = run_worker(folder, spawn, jobs)
    report = merge.merge_wave(spawn, results_of(jobs, manifests), lambda: "")
    print(f"[parent] wave 4 merge {report.seconds}s: {report.headline()}")
    for line in report.lines():
        print(f"[parent]   {line}")
    seen = manifests["K"]["notes"]["copy"]
    crystal, shard, base = obj("Crystal"), obj("Crystal_Shard"), obj("Crystal_Base")
    glows = sorted(m.name for m in bpy.data.materials if "Pal_Glow" in m.name)
    check("a palette material no object wore is in the worker's copy, and the merged object wears the live one",
          "Pal_Glow" in seen["materials"] and "Pal_Tint" in seen["node_groups"] and "Pal_Noise" in seen["images"]
          and crystal and crystal.data.materials[0] == glow and glows == ["Pal_Glow"], json.dumps({"copy": seen, "glows": glows}))
    glasses = sorted(m.name for m in bpy.data.materials if "Pal_Glass" in m.name)
    lines = report.lines()
    check("a material made again with the live one's name and settings is the live one, said in the merge lines",
          shard and shard.data.materials[0] == glass and glasses == ["Pal_Glass"]
          and [(r["worker"], r["name"]) for r in report.reused] == [("K", "Pal_Glass")]
          and any(line.startswith("K: material Pal_Glass.001 is the live Pal_Glass made again") for line in lines)
          and not [c for c in report.created if c["id_type"] == "MATERIAL" and "Pal_Glass" in c["name"]],
          json.dumps({"glasses": glasses, "reused": report.reused, "lines": lines}))
    # K's own new_material("Pal_Stone", ...) already collided with the live palette material inside K's own
    # Blender (bpy.data.materials.new gave it .001 there); the merge keeps that name and reports the collision.
    stone = bpy.data.materials.get("Pal_Stone.001")
    check("the same name with other settings keeps its own .001, not merged under a prefix",
          base and stone and base.data.materials[0] == stone and bpy.data.materials["Pal_Stone"] != stone
          and not report.errors and "K: MATERIAL Pal_Stone -> Pal_Stone.001" in report.renamed,
          f"{[m.name for m in bpy.data.materials]} errors={report.errors} renamed={report.renamed}")
    homes = {name: sorted(c.name for c in obj(name).users_collection) if obj(name) else None
             for name in ("Crystal", "Crystal_Base", "Crate_1", "Stack_Box1", "Tree", "Tree_Leaf", "Pile_Box1")}
    collections = sorted(c.name for c in bpy.data.collections)
    check("created objects go where the scene's objects are, not a collection per worker; the user who asked "
          "for collections gets the worker's own (made live); a brief's count check names its collection",
          homes["Crystal"] == homes["Crystal_Base"] == ["Main"] and homes["Crate_1"] == ["Crate_Stack"]
          and homes["Stack_Box1"] == ["Props"] and "Crystal" not in collections and "Stack" not in collections
          and "Crate" not in collections and bpy.data.collections["Crate_Stack"] in scene.collection.children[:],
          json.dumps({"homes": homes, "all": collections}))
    tree_src = bpy.data.collections.get("Tree_Src")
    check("a collection the worker made that hangs on nothing is dropped; an instancer's source stays, shown through it",
          "Stack_Group" not in collections and "Stack_Stack_Group" not in collections and tree_src
          and obj("Tree").instance_collection == tree_src and homes["Tree"] == ["Main"] and homes["Tree_Leaf"] == ["Tree_Src"]
          and tree_src not in scene.collection.children_recursive[:], json.dumps({"homes": homes, "all": collections}))
    check("a collection the worker made stays when the user asked for collections, its objects shown through it",
          homes["Pile_Box1"] == ["Pile_Group"] and "Pile" not in collections
          and bpy.data.collections["Pile_Group"] in scene.collection.children_recursive[:],
          json.dumps({"homes": homes, "all": collections}))
    objects = sum(1 for c in report.created if c["id_type"] == "OBJECT")
    check("the merge headline counts objects, the rest of what came in apart",
          objects == 12 and report.headline().startswith(f"Merged: 12 objects and {len(report.created) - 12} other datablocks created"),
          report.headline())
    sign, twin = obj("Crystal_Sign"), obj("Crystal_Twin")
    check("a created object's own mesh takes the object's name (Sign held Sign_FootA), unless another mesh has it",
          sign and sign.data.name == "Crystal_Sign" and twin and twin.data.name == "Crystal_Twin_Old"
          and obj("Crystal").data.name == "Crystal" and obj("Post_B") is not None,
          f"sign={sign.data.name if sign else None} twin={twin.data.name if twin else None}")


def wave_materials(tmp: Path, live_path: Path) -> None:
    """Workers may make materials worth looking at (m4_quality_audit §7.4): a modeling worker's own
    changed copy of a palette material merges as its own and still counts as the palette material in
    a material_on check; a copy left as it was is the live one; its edit to the palette itself is
    dropped. A worker with aspect materials owns every material and its edit replaces the live one,
    so everything wearing it follows. A recolour worker that owns an object and has no aspect puts a
    palette material in its slot and a face on another slot (object-local, no conflict)."""
    from loopcut import acceptance
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    aspect = sorted(ownership.expand_owns([], ["materials"]))
    check("aspect materials owns every local material (not a linked library's)",
          {"MATERIAL:Palette_Wood", "MATERIAL:Rug_Wool"} <= set(aspect) and not any("Lib_Varnish" in k for k in aspect)
          and "materials" in ownership.ASPECTS, json.dumps(aspect))
    folder = tmp / "job5" / "w5"
    spawn = merge.save_spawn(folder)
    jobs = [{"worker": "Q", "prefix": "Chair", "role": "modeling", "deliverable": "Chair", "owned": []},
            {"worker": "R", "prefix": "Finish", "role": "materials", "aspects": ["materials"], "owned": aspect},
            {"worker": "V", "prefix": "Ice", "role": "materials", "owned": sorted(ownership.expand_owns(["Box"], []))}]
    manifests = run_worker(folder, spawn, jobs)
    report = merge.merge_wave(spawn, results_of(jobs, manifests), lambda: "")
    print(f"[parent] materials wave merge {report.seconds}s: {report.headline()}")
    for line in report.lines():
        print(f"[parent]   {line}")
    lines = report.lines()
    wood, leg, seat = bpy.data.materials.get("Chair_Wood"), obj("Chair_Leg"), obj("Chair")
    entry = next((c for c in report.created if c["name"] == "Chair_Wood"), {})
    check("a worker's changed copy of a palette material merges as its own, tagged with what it was copied from",
          wood and leg and leg.data.materials[0] == wood and wood.get(ownership.VARIANT) == "Palette_Wood"
          and any(n.bl_idname == "ShaderNodeTexNoise" for n in wood.node_tree.nodes)
          and entry.get("variant_of") == "Palette_Wood"
          and "Q: material Chair_Wood is its own variant of Palette_Wood (on its own parts)" in lines,
          json.dumps({"entry": entry, "lines": lines}))
    rug_wool = bpy.data.materials.get("Rug_Wool")
    reused = [r for r in report.reused if r["worker"] == "Q"]
    check("a copy left as it was is the live material (even renamed), and follows a later worker's edit of it",
          "Chair_Wool" not in bpy.data.materials and seat and seat.data.materials[0] == rug_wool
          and reused == [{"worker": "Q", "name": "Rug_Wool", "made": "Chair_Wool", "id_type": "MATERIAL", "copy": True}]
          and "Q: material Chair_Wool is an unchanged copy of the live Rug_Wool: the live one is used, no copy merged"
          in lines, json.dumps({"reused": report.reused, "seat": [m.name for m in seat.data.materials] if seat else None}))
    bsdf = next(n for n in bpy.data.materials["Palette_Wood"].node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    dropped = [d["key"] for d in report.dropped_edits if d["worker"] == "Q"]
    advisory = manifests["Q"]["advisory"]["changed_not_owned"]
    check("an edit to the palette without aspect materials is dropped and said so (the child's rollback's own list too)",
          dropped == ["MATERIAL:Palette_Wood"] and "MATERIAL:Palette_Wood" in advisory and round(bsdf.inputs["Roughness"].default_value, 3) == 0.5
          and any(line.startswith("Q: edit to Palette_Wood dropped (not owned; a palette material changes only with "
                                  "aspect materials") for line in lines), json.dumps({"dropped": dropped, "lines": lines}))
    wool = next(n for n in rug_wool.node_tree.nodes if n.type == "BSDF_PRINCIPLED") if rug_wool else None
    replaced = [r["key"] for r in report.replaced if r["worker"] == "R"]
    check("with aspect materials a worker's edit replaces the palette material; what wears it follows",
          replaced == ["MATERIAL:Rug_Wool"] and wool and round(wool.inputs["Roughness"].default_value, 3) == 0.95
          and obj("Rug").data.materials[0] == rug_wool and not report.conflicts and not report.errors
          and len([m for m in bpy.data.materials if m.name.startswith(("Rug_Wool", "Palette_Wood"))]) == 2,
          json.dumps({"replaced": report.replaced, "errors": report.errors, "conflicts": report.conflicts}))
    box = obj("Box")
    check("an owner recolours its own object with no aspect: slot and face material index merge, nothing dropped",
          box and box.data.materials[0] == rug_wool and box.data.polygons[0].material_index == 1
          and "MESH:BoxMesh" in [r["key"] for r in report.replaced if r["worker"] == "V"]
          and not [d for d in report.dropped_edits if d["worker"] == "V"] and not manifests["V"]["advisory"]["changed_not_owned"],
          json.dumps({"replaced": report.replaced, "dropped": report.dropped_edits,
                      "slots": [m.name if m else None for m in box.data.materials] if box else None}))
    results = acceptance.run([{"id": "c1", "kind": "material_on", "name": "Chair", "material": "Palette_Wood",
                               "quote": "a wooden chair"},
                              {"id": "c2", "kind": "material_on", "name": "Chair", "material": "Rug_Wool",
                               "quote": "a wool seat"}])
    check("material_on accepts the worker's own copy of the palette material, and says so",
          [r["status"] for r in results] == ["pass", "pass"]
          and "Palette_Wood on Chair, Chair_Leg (as its copy Chair_Wood)" in str(results[0].get("detail")),
          json.dumps(results))


def wave_collections(tmp: Path, live_path: Path) -> None:
    """Collection membership survives the merge (20260929-230207 O0008: seven props the workers linked
    into the live ShopAssetPack came back in the main collection, and the orchestrator re-linked them).
    A spawn collection a worker linked into on purpose keeps the object; the active one, where new
    objects land by default, does not (home, as before); a collection workers made that the shared
    notes name is made live once and holds all of theirs; one nobody asked for is not made."""
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    scene = bpy.context.scene
    for name in ("ShopAssetPack", "Work"):
        scene.collection.children.link(bpy.data.collections.new(name))
    layer = next(lc for lc in bpy.context.view_layer.layer_collection.children if lc.collection.name == "Work")
    bpy.context.view_layer.active_layer_collection = layer
    folder = tmp / "job7" / "w7"
    spawn = merge.save_spawn(folder)
    jobs = [{"worker": "W", "prefix": "PotionOne", "role": "modeling", "deliverable": "PotionOne", "owned": []},
            {"worker": "X", "prefix": "PotionTwo", "role": "modeling", "deliverable": "PotionTwo", "owned": []},
            {"worker": "Y", "prefix": "PotionThree", "role": "modeling", "deliverable": "PotionThree", "owned": []}]
    manifests = run_worker(folder, spawn, jobs)
    shared = {"notes": "Put each final mesh in ShopAssetPack; the second pack goes in PackTwo."}
    results = [dict(r, shared=shared) for r in results_of(jobs, manifests)]
    report = merge.merge_wave(spawn, results, lambda: "")
    print(f"[parent] collections wave merge {report.seconds}s: {report.headline()}")
    homes = {name: sorted(c.name for c in obj(name).users_collection) if obj(name) else None
             for name in ("PotionOne", "PotionOne_Cork", "PotionTwo", "PotionTwo_Stand", "PotionThree")}
    packs = sorted(c.name for c in bpy.data.collections if c.name.startswith(("PackTwo", "TwoParts")))
    check("a prop linked into a live collection on purpose stays in it after the merge; one made where new objects "
          "land by default goes where the scene's objects are",
          homes["PotionOne"] == ["ShopAssetPack"] and homes["PotionOne_Cork"] == ["Scene Collection"]
          and not bpy.data.collections["Work"].objects, json.dumps(homes))
    check("a collection workers made that the shared notes name is made live once and holds each worker's prop; "
          "one nobody asked for is not made",
          homes["PotionTwo"] == homes["PotionThree"] == ["PackTwo"] and packs == ["PackTwo"]
          and homes["PotionTwo_Stand"] == ["Scene Collection"]
          and bpy.data.collections["PackTwo"] in scene.collection.children[:]
          and [c["name"] for c in report.created if c["id_type"] == "COLLECTION"] == ["PackTwo"]
          and not any(ob.get(ownership.COLLECTIONS) for ob in bpy.data.objects),
          json.dumps({"homes": homes, "packs": packs, "created": report.created}))


IMPLIED = {"MESH", "CURVE", "FONT", "LIGHT", "CAMERA"}  # An object's own data: waves._created_names leaves it out.


def wave_shot(tmp: Path, live_path: Path) -> None:
    """A shot worker animates its own camera and lights (actions on objects and on their data) and
    nothing is held back; a modeling worker's lamp light and fan spin, which its brief asks for,
    merge and say why, while its unasked test light is held back with the reason. Then the same shot
    worker is continued in a later wave (orchestrator continues): owning what it built, by the names
    its merge reported, its changes replace the live ones with nothing dropped, held or duplicated."""
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    folder = tmp / "job6" / "w6"
    spawn = merge.save_spawn(folder)
    aspects = ["lighting", "camera"]
    jobs = [{"worker": "S", "prefix": "Shot", "role": "lighting", "aspects": aspects, "deliverable": "Shot",
             "owned": sorted(ownership.expand_owns([], aspects)),
             "requirements": [{"quote": "the camera slowly orbits the product", "do": "orbit"}]},
            {"worker": "T", "prefix": "ReadingLamp", "role": "modeling", "deliverable": "ReadingLamp", "owned": [],
             "requirements": [{"quote": "a floor lamp that lights the reading corner", "do": "floor lamp, warm bulb"}]},
            {"worker": "U", "prefix": "Fan", "role": "modeling", "deliverable": "Fan", "owned": [],
             "requirements": [{"quote": "a ceiling fan that spins slowly", "do": "ceiling fan"}]}]
    manifests = run_worker(folder, spawn, jobs)
    report = merge.merge_wave(spawn, results_of(jobs, manifests), lambda: "")
    print(f"[parent] shot wave merge {report.seconds}s: {report.headline()}")
    for line in report.lines():
        print(f"[parent]   {line}")
    scene, lines = bpy.context.scene, report.lines()
    shot = [q["name"] for q in report.quarantined if q["worker"] == "S"]
    actions = {name: getattr(getattr(idb, "animation_data", None), "action", None) for name, idb in (
        ("pivot", obj("Shot_Pivot")), ("lens", obj("Shot_Cam").data if obj("Shot_Cam") else None),
        ("flicker", obj("Shot_Key").data if obj("Shot_Key") else None), ("rim", obj("Shot_Rim")))}
    check("a shot worker's camera move and light animation merge: nothing held back, every action live and local",
          not shot and all(a is not None and a.library is None and not a.get(ownership.QUARANTINE) for a in actions.values())
          and scene.camera == obj("Shot_Cam") and obj("Shot_Cam").parent == obj("Shot_Pivot")
          and not manifests["S"]["advisory"]["outside_role"] and not [a for a in report.admitted if a["worker"] == "S"],
          json.dumps({"quarantined": report.quarantined, "actions": {k: v.name if v else None for k, v in actions.items()},
                      "advisory": manifests["S"]["advisory"]}))
    scene.frame_set(48)
    energy = round(obj("Shot_Key").data.energy, 1) if obj("Shot_Key") else None
    scene.frame_set(1)
    check("the merged flicker plays (energy at frame 48 is the keyed 120 W)", energy == 120.0, str(energy))
    bulb, test = obj("ReadingLamp_Bulb"), obj("ReadingLamp_Test")
    admitted = {a["name"]: a["reason"] for a in report.admitted}
    held = {q["name"]: q.get("reason", "") for q in report.quarantined}
    check("a lamp's bulb light the brief asks for merges with the lamp and says why; an unasked test light is held back "
          "with the reason",
          bulb and not bulb.get(ownership.QUARANTINE) and merge.QUARANTINE_COLLECTION not in [c.name for c in bulb.users_collection]
          and admitted.get("ReadingLamp_Bulb", "").startswith('part of ReadingLamp, and the brief asks for it: "a floor lamp that lights')
          and test and test.get(ownership.QUARANTINE)
          and held.get("ReadingLamp_Test", "").startswith("outside its role: a modeling worker makes ")
          and "lights need aspect lighting, or a brief that asks for light from the worker's own object" in held["ReadingLamp_Test"]
          and any(line.startswith("T: ReadingLamp_Bulb (light) merged though outside its role: part of ReadingLamp") for line in lines)
          and any(line.startswith("T: ReadingLamp_Test (light) quarantined: outside its role") for line in lines),
          json.dumps({"admitted": report.admitted, "quarantined": report.quarantined}))
    blades = obj("Fan_Blades")
    spin = blades.animation_data.action if blades and blades.animation_data else None
    check("a spin the brief asks for, on the worker's own blades, merges and says why",
          spin is not None and not spin.get(ownership.QUARANTINE)
          and admitted.get(spin.name, "").startswith('animates only its own Fan_Blades, and the brief asks for motion: "a ceiling fan that spins'),
          json.dumps({"admitted": report.admitted, "spin": spin.name if spin else None}))
    live2 = tmp / "live_shot.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(live2))
    # The same worker again, as orchestrator continues does it: owns what its merge reported it made.
    made = [c["name"] for c in report.created if c["worker"] == "S" and c["id_type"] not in IMPLIED]
    owned = sorted(ownership.expand_owns(made, aspects))
    folder = tmp / "job6" / "w7"
    spawn = merge.save_spawn(folder)
    jobs = [{"worker": "S2", "prefix": "Shot", "role": "lighting", "aspects": aspects, "deliverable": "Shot",
             "owned": owned, "continues": "S", "requirements": [{"quote": "slower orbit, dimmer flicker", "do": "x"}]}]
    manifests = run_worker(folder, spawn, jobs)
    counts = {kind: len(getattr(bpy.data, kind)) for kind in ("objects", "actions", "lights", "cameras")}
    again = merge.merge_wave(spawn, results_of(jobs, manifests), lambda: "")
    print(f"[parent] continued shot merge {again.seconds}s: {again.headline()}")
    for line in again.lines():
        print(f"[parent]   {line}")
    replaced = sorted(r["key"] for r in again.replaced if r.get("key", "").startswith("ACTION:"))
    after = {kind: len(getattr(bpy.data, kind)) for kind in ("objects", "actions", "lights", "cameras")}
    scene.frame_set(48)
    energy = round(obj("Shot_Key").data.energy, 1)
    scene.frame_set(96)
    turned = round(obj("Shot_Pivot").rotation_euler[2], 3)
    scene.frame_set(1)
    check("a worker continued in a later wave owns what it built (its actions too): its edits replace the live ones, "
          "nothing dropped, held back, in conflict or duplicated",
          not again.dropped_edits and not again.quarantined and not again.conflicts and not again.errors
          and any(k.startswith("ACTION:") for k in owned) and len(replaced) >= 2
          and energy == 60.0 and turned == round(3.14159265, 3) and obj("Shot_Cam").data.dof.use_dof
          and "CAMERA:Shot_Cam" in [r.get("key") for r in again.replaced]
          and after == {**counts, "objects": counts["objects"] + 1, "lights": counts["lights"] + 1}
          and not [i.name for i in merge._datablocks() if i.name.startswith("Shot_") and DOT_NUM.search(i.name)],
          json.dumps({"owned": owned, "replaced": [r["key"] for r in again.replaced], "dropped": again.dropped_edits,
                      "quarantined": again.quarantined, "conflicts": again.conflicts, "errors": again.errors,
                      "energy": energy, "turned": turned, "counts": [counts, after]}))


def wave_support(tmp: Path, live_path: Path) -> None:
    """Text blocks and images merge, and the worker's report says what arrived (eval 20260930-154037:
    the painter's ArcaneRecipe and its painter sources were missing after two merges, and owns refused
    the name). A worker's new text blocks merge (one whose name the user took meanwhile gets .001), its
    packed atlas and an unused mask it painted arrive packed with the pixels inspect_texture hashes,
    a blank image it never used and its edit to the user's script are skipped with the reason; the
    manifest names each, checked live, and a save keeps them. Owns accepts a text's name; the painter
    continued owns its recipe and atlas and both update live, the unsaved repaint packed; an owned text
    the user edits during the wave is a conflict the report names as missing."""
    from loopcut import orchestrator, waves
    bpy.ops.wm.open_mainfile(filepath=str(live_path))
    bpy.data.texts.new("UserScript").from_string("print('mine')\n")
    folder = tmp / "job_support" / "w1"
    spawn = merge.save_spawn(folder)
    jobs = [{"worker": "TX", "prefix": "Chest", "role": "modeling", "deliverable": "Chest", "owned": []}]
    manifests = run_worker(folder, spawn, jobs,
                           while_running=lambda: bpy.data.texts.new("Painter").from_string("the user's painter\n"))
    support = {e.get("key"): e for e in manifests["TX"].get("support") or ()}
    report = merge.merge_wave(spawn, results_of(jobs, manifests), lambda: "")
    print(f"[parent] support wave merge {report.seconds}s: {report.headline()}")
    states = {e["key"]: e["state"] for e in support.values()}
    check("the child's manifest names each text block and image: new ones, an unused blank image and an edit "
          "it does not own skipped with the reason",
          states == {"TEXT:ArcaneRecipe": "new", "TEXT:Painter": "new", "IMAGE:Chest_Atlas": "new",
                     "IMAGE:Chest_Mask": "new", "IMAGE:Chest_Scratch": "skipped", "TEXT:UserScript": "skipped"}
          and support["IMAGE:Chest_Scratch"]["why"] == "unused and never painted"
          and support["TEXT:UserScript"]["why"].startswith("not owned"), json.dumps(support))
    recipe, painter = bpy.data.texts.get("ArcaneRecipe"), bpy.data.texts.get("Painter.001")
    check("a worker's new text block merges with its contents and a fake user; one whose name the user took "
          "during the wave is Painter.001, the user's Painter untouched",
          recipe is not None and recipe.as_string() == RECIPE and recipe.use_fake_user and painter is not None
          and painter.as_string() == "painter source v1\n"
          and bpy.data.texts["Painter"].as_string() == "the user's painter\n",
          json.dumps({t.name: t.as_string()[:40] for t in bpy.data.texts}))
    check("its edit to the user's script stays in its copy, and the manifest says so",
          bpy.data.texts["UserScript"].as_string() == "print('mine')\n"
          and "TEXT:UserScript" in manifests["TX"]["advisory"]["changed_not_owned"]
          and any(e.get("key") == "TEXT:UserScript" and e["state"] == "skipped" for e in report.support),
          json.dumps(report.support))
    atlas, mask = bpy.data.images.get("Chest_Atlas"), bpy.data.images.get("Chest_Mask")
    chest = obj("Chest")
    node = next((n for n in chest.data.materials[0].node_tree.nodes if n.type == "TEX_IMAGE"), None) if chest else None
    check("the packed atlas arrives packed on its material, its pixels hashing as the manifest (and inspect_texture) says",
          atlas is not None and atlas.packed_file is not None and node is not None and node.image == atlas
          and ownership.pixel_hash(atlas) == support["IMAGE:Chest_Atlas"]["hash"] != "",
          json.dumps({"live": ownership.pixel_hash(atlas) if atlas else None, "manifest": support.get("IMAGE:Chest_Atlas")}))
    check("the unused mask's unsaved paint arrives packed, with a fake user so a save keeps it",
          mask is not None and mask.packed_file is not None and mask.use_fake_user
          and ownership.pixel_hash(mask) == support["IMAGE:Chest_Mask"]["hash"], json.dumps(support.get("IMAGE:Chest_Mask")))
    merged = {e.get("key"): e for e in report.support}
    lines = [waves.support_text(e) for e in report.support]
    check("the merge checks each one live: new under its live name (Painter.001 says why), skipped as the child "
          "said, nothing missing; the orchestrator's line names them",
          {k: e["state"] for k, e in merged.items()} == states and merged["TEXT:Painter"]["name"] == "Painter.001"
          and "Painter was taken" in merged["TEXT:Painter"].get("why", "")
          and f"text ArcaneRecipe new #{support['TEXT:ArcaneRecipe']['hash'][:6]} (5 lines)" in lines
          and any(line.startswith("image Chest_Atlas new #") and "128x128 packed" in line for line in lines)
          and "image Chest_Scratch not merged: unused and never painted" in lines
          and not [line for line in report.lines() if "missing after the merge" in line], json.dumps(lines))
    names = orchestrator.live_facts({"shared": {"keep": {}, "supports": []}, "workers": []})["names"]
    owns = sorted(ownership.expand_owns(["ArcaneRecipe", "Chest_Atlas"], []))
    check("a brief may own a text block or an image by name (the orchestrator's live names have texts)",
          "ArcaneRecipe" in names and owns == ["IMAGE:Chest_Atlas", "TEXT:ArcaneRecipe"], json.dumps(owns))
    saved = tmp / "support_saved.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(saved))
    bpy.ops.wm.open_mainfile(filepath=str(saved))
    check("a save keeps the text blocks and both images (packed)",
          "ArcaneRecipe" in bpy.data.texts and "Painter.001" in bpy.data.texts
          and all(bpy.data.images.get(n) is not None and bpy.data.images[n].packed_file is not None
                  for n in ("Chest_Atlas", "Chest_Mask")),
          json.dumps({"texts": [t.name for t in bpy.data.texts], "images": [i.name for i in bpy.data.images]}))

    # The painter continued: owns its recipe and atlas.
    folder = tmp / "job_support" / "w2"
    spawn = merge.save_spawn(folder)
    jobs = [{"worker": "TY", "prefix": "Chest", "role": "modeling", "deliverable": "Chest", "owned": owns}]
    manifests = run_worker(folder, spawn, jobs)
    support = {e.get("key"): e for e in manifests["TY"].get("support") or ()}
    report = merge.merge_wave(spawn, results_of(jobs, manifests), lambda: "")
    atlas = bpy.data.images.get("Chest_Atlas")
    check("the owner's edits update the live text and image; the unsaved repaint arrives packed",
          bpy.data.texts["ArcaneRecipe"].as_string() == RECIPE + "BRASS = (0.8, 0.6, 0.2)\n"
          and manifests["TY"]["notes"].get("atlas_dirty") and atlas is not None and atlas.packed_file is not None
          and ownership.pixel_hash(atlas) == support["IMAGE:Chest_Atlas"]["hash"]
          and sorted(r["key"] for r in report.replaced) == ["IMAGE:Chest_Atlas", "TEXT:ArcaneRecipe"]
          and [e["state"] for e in report.support] == ["updated", "updated"] and not report.conflicts
          and len([i for i in bpy.data.images if i.name.startswith("Chest_Atlas")]) == 1,
          json.dumps({"support": report.support, "replaced": report.replaced, "conflicts": report.conflicts}))

    # Again, while the user edits the recipe: a conflict, and the report says it is not the worker's.
    folder = tmp / "job_support" / "w3"
    spawn = merge.save_spawn(folder)
    jobs = [{"worker": "TY", "prefix": "Chest", "role": "modeling", "deliverable": "Chest", "owned": owns}]
    recipe = bpy.data.texts["ArcaneRecipe"]
    manifests = run_worker(folder, spawn, jobs, while_running=lambda: recipe.from_string(recipe.as_string() + "# user\n"))
    report = merge.merge_wave(spawn, results_of(jobs, manifests), lambda: "")
    text = next((e for e in report.support if e.get("key") == "TEXT:ArcaneRecipe"), {})
    check("an owned text the user edited during the wave keeps the user's version, and the report says the "
          "worker's is missing and why",
          text.get("state") == "missing" and "changed in the live scene" in text.get("why", "")
          and bpy.data.texts["ArcaneRecipe"].as_string().endswith("# user\n")
          and any(line.startswith("TY: TEXT:ArcaneRecipe not merged: changed in the live scene") for line in report.lines())
          and "text ArcaneRecipe MISSING: changed in the live scene during the wave; the live version was kept"
          in [waves.support_text(e) for e in report.support],
          json.dumps({"support": report.support, "lines": report.lines()}))


def mesh_validate_check(tmp: Path) -> None:
    """M4: a worker's own mesh with a bad loop index must be fixed before it reaches the live scene,
    not after (bm.from_mesh on a mesh like this has segfaulted, far from where it was built)."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    live_path = tmp / "mesh_validate_live.blend"
    bpy.ops.wm.save_as_mainfile(filepath=str(live_path))
    folder = tmp / "job_mesh" / "w1"
    spawn = merge.save_spawn(folder)
    (folder.parent / "job.json").write_text("{}")
    jobs = [{"worker": "bad_mesh", "prefix": "Bad", "role": "modeling", "owned": []}]
    manifests = run_worker(folder, spawn, jobs)
    results = results_of(jobs, manifests)
    sess = session(tmp)
    report = merge.merge_wave(spawn, results, lambda: checkpoints.ensure_for_wave(sess, "Mesh validate", 2, 3))
    fixed = [f["name"] for f in report.fixed]
    check("a created mesh with an out-of-range loop index is fixed (Mesh.validate) before it merges",
          any("Block" in n for n in fixed), fixed)
    check("the merge report says so", any("had invalid geometry" in line and "fixed before merging" in line
                                          for line in report.lines()), report.lines())
    block = obj("Bad_Block")
    ok = block is not None
    if ok:
        import bmesh
        bm = bmesh.new()
        try:
            bm.from_mesh(block.data)  # Would have segfaulted on the unfixed mesh.
        finally:
            bm.free()
    check("the merged mesh is safe for bm.from_mesh: the crash this validate() call prevents", ok)


def prefix_rule_check() -> None:
    """dungeon eval: a worker with prefix "DungeonModules" built modules named Floor, Wall, Doorway,
    Pillar, clear names as the brief asked for; the old rule (Prefix_Part always) renamed them to
    DungeonModules_Wall.010 etc, and the grader found no object named after the modules (4/15
    requirements, down from 14/15). The merge now keeps a worker's own names outright: _rename_created
    never touches .name any more. The one thing it still notices is a name Blender's own append (in
    _merge_worker, before this function runs) already suffixed with .001 because it collided with
    something that existed before this worker's result arrived - a live datablock, or an earlier
    worker's created one this wave - and that goes in the report, not renamed again."""
    bottle = bpy.data.objects.new("PotionBottleRed", None)
    sign = bpy.data.objects.new("ShopSign", None)
    wall = bpy.data.objects.new("Wall", None)              # As if append found "Wall" already live/taken.
    dup_wall = bpy.data.objects.new("Wall.001", None)
    own_numbering = bpy.data.objects.new("Gizmo.002", None)  # The worker's own choice, not a collision.
    created = [bottle, sign, wall, dup_wall, own_numbering]
    before_names = {("OBJECT", "Wall")}
    report = merge.MergeReport()
    try:
        merge._rename_created(created, before_names, "dungeon", report)
        names = [ob.name for ob in created]
        check("no name is forced under a prefix any more",
              names == ["PotionBottleRed", "ShopSign", "Wall", "Wall.001", "Gizmo.002"], names)
        check("only the genuine collision (Wall, already taken) is reported, and nothing is renamed again",
              report.renamed == ["dungeon: OBJECT Wall -> Wall.001"], report.renamed)
    finally:
        for ob in created:
            bpy.data.objects.remove(ob)


def main() -> int:
    keep = "--keep" in sys.argv
    tmp = Path(tempfile.mkdtemp(prefix="lc_merge_check_"))
    os.environ["LOOPCUT_DATA_DIR"] = str(tmp / "data")
    t0 = time.time()
    try:
        lib_path = tmp / "lib_assets.blend"
        build_library(str(lib_path))
        build_live(str(lib_path))
        live_path = tmp / "live.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(live_path))
        jobs, manifests, kept = wave_1(tmp, live_path, {})
        stale(tmp, live_path, jobs, manifests, kept)
        wave_2(tmp, live_path)
        wave_3(tmp, live_path)
        wave_split(tmp, live_path)
        wave_4(tmp, live_path)
        wave_materials(tmp, live_path)
        wave_collections(tmp, live_path)
        wave_shot(tmp, live_path)
        wave_support(tmp, live_path)
        mesh_validate_check(tmp)
        prefix_rule_check()
    except Exception:
        traceback.print_exc()
        check("ran to the end", False, "exception above")
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\n==== merge check")
    for name, ok, detail in RESULTS:
        print(f"MERGE {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    passed = sum(ok for _, ok, _ in RESULTS)
    print(f"MERGE {passed}/{len(RESULTS)} in {time.time() - t0:.1f}s; tmp {tmp}{' (kept)' if keep else ''}")
    print("MERGE OK" if passed == len(RESULTS) else "MERGE FAIL")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if argv[:1] == ["worker"]:
        worker_main(argv[1])
    else:
        code = main()
        sys.stdout.flush()
        if bpy.app.background:
            sys.exit(code)
