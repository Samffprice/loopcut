"""acceptance.run against real Blender: every check kind, passing and failing, on a scene made on
the spot.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/acceptance_check.py

Headless, no model or network; ~2 s. A reading nook (a rug, an armchair of parts with its origin
at the base, a table, a floating stool, two crates through each other, a bulb in a shade and one
outside it, a camera animated from frame 1 to 120, a perfume bottle in frame and a crate out of
it) and the scene settings. Each check is asserted pass or fail with the numbers in its detail;
then `unchanged` in the parent (a Spawn, a rename that is not a change, an edit that is, what a merge
replaced found through the job's baseline) and in a child (stamp_all), `file` next to a saved .blend,
and the Verified block. Last, no_overlap and
clear_of on the shared_support eval's reading corner (corner.py), where they read metres of overlap
between objects apart: now none, the same gaps from both, and a book sunk 10 mm into the table.
"""

import copy
import math
import os
import sys
import tempfile
import traceback
import types
from pathlib import Path

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout
checkout.use()
import corner  # noqa: E402
from loopcut import acceptance, contact, merge, ownership, waves  # noqa: E402

SEEN = {}  # kind -> statuses seen


def box(name, size, location, parent=None, collection=None):
    import bmesh
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x, v.co.y, v.co.z = v.co.x * size[0], v.co.y * size[1], v.co.z * size[2]
    bm.to_mesh(mesh)
    bm.free()
    ob = bpy.data.objects.new(name, mesh)
    (collection or bpy.context.scene.collection).objects.link(ob)
    ob.location = location
    if parent is not None:
        ob.parent = parent
        ob.matrix_parent_inverse = parent.matrix_world.inverted()
    return ob


def material(name, rgb):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1.0)
    return mat


def joined(name, parts):
    """One mesh of boxes, as a worker joins a prop: parts are (size, centre, material). The chain
    boxes' vertices also go in a vertex group "Chains", the board's faces get a face attribute "board"."""
    import bmesh
    mesh, bm, names = bpy.data.meshes.new(name), bmesh.new(), []
    for size, centre, mat in parts:
        names += [mat] if mat not in names else []
        verts = bmesh.ops.create_cube(bm, size=1.0)["verts"]
        for v in verts:
            v.co.x, v.co.y, v.co.z = (v.co[k] * size[k] + centre[k] for k in range(3))
        for face in {f for v in verts for f in v.link_faces}:
            face.material_index = names.index(mat)
    bm.to_mesh(mesh)
    bm.free()
    for mat in names:
        mesh.materials.append(bpy.data.materials.get(mat) or material(mat, (0.5, 0.4, 0.3)))
    ob = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(ob)
    chains, board = names.index("Sign_Chain"), names.index("Sign_Board")
    ob.vertex_groups.new(name="Chains").add(sorted({v for p in mesh.polygons if p.material_index == chains
                                                    for v in p.vertices}), 1.0, "REPLACE")
    flag = mesh.attributes.new("board", "BOOLEAN", "FACE")
    for p in mesh.polygons:
        flag.data[p.index].value = p.material_index == board
    return ob


def joined_sign(name, x, board_z):
    """A hanging sign joined into one mesh: a post, an arm, two chains down to a 0.4 m board whose
    bottom is at board_z."""
    top = board_z + 0.4
    length = 1.91 - top
    return joined(name, [((0.1, 0.1, 2.0), (x, 0, 1.0), "Sign_Wood"), ((0.8, 0.08, 0.08), (x + 0.4, 0, 1.95), "Sign_Wood"),
                         ((0.02, 0.02, length), (x + 0.2, 0, top + length / 2), "Sign_Chain"),
                         ((0.02, 0.02, length), (x + 0.6, 0, top + length / 2), "Sign_Chain"),
                         ((0.6, 0.05, 0.4), (x + 0.4, 0, board_z + 0.2), "Sign_Board")])


def build() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = 1080, 1920
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 128
    scene.frame_start, scene.frame_end = 1, 120
    box("Rug", (2.0, 1.4, 0.015), (0, 0, 0.0075))
    top = 0.015
    root = bpy.data.objects.new("Armchair", None)
    scene.collection.objects.link(root)
    root.location = (0, 0, top)
    bpy.context.view_layer.update()
    wood = material("Pal_Wood", (0.4, 0.25, 0.1))
    material("Pal_Brass", (0.8, 0.6, 0.2))
    for n, (x, y) in enumerate(((-.3, -.3), (.3, -.3), (-.3, .3), (.3, .3)), 1):
        box(f"Armchair_Leg{n}", (0.05, 0.05, 0.4), (x, y, top + 0.2), root).data.materials.append(wood)
    seat = box("Armchair_Seat", (0.7, 0.7, 0.1), (0, 0, top + 0.45), root)
    seat.data.materials.append(material("Armchair_Fabric", (0.2, 0.3, 0.7)))
    box("Table", (0.6, 0.6, 0.5), (1.2, 0, top + 0.25))  # Origin at its centre, not its base.
    # A hanging sign, far off: its origin at the foot of its post, the board beside the post above.
    sign = bpy.data.objects.new("Sign", None)
    scene.collection.objects.link(sign)
    sign.location = (6.0, 4.0, 0.0)
    bpy.context.view_layer.update()  # Its matrix_world, which box() parents by.
    box("Sign_Post", (0.1, 0.1, 2.0), (6.0, 4.0, 1.0), sign)
    box("Sign_Board", (0.9, 0.05, 0.5), (6.5, 4.0, 1.6), sign)
    box("Stool", (0.4, 0.4, 0.4), (-0.7, 0, top + 0.012 + 0.2))  # Floats 12 mm.
    box("CrateA", (0.5, 0.5, 0.5), (0, 1.5, 0.25))
    box("CrateB", (0.5, 0.5, 0.5), (0.3, 1.5, 0.25))  # 0.2 m into CrateA.
    bpy.ops.mesh.primitive_cone_add(vertices=32, radius1=0.25, radius2=0.1, depth=0.3, location=(1.2, 0, 1.2))
    bpy.context.active_object.name = "Shade"
    for name, where in (("Bulb", (1.2, 0, 1.15)), ("Bulb2", (1.2, 0, 1.6))):
        light = bpy.data.objects.new(name, bpy.data.lights.new(name, "POINT"))
        light.location = where
        scene.collection.objects.link(light)
    dense = box("Dense", (0.3, 0.3, 0.3), (-1.2, 1.5, 0.15))
    dense.modifiers.new("Subdiv", "SUBSURF").levels = 1  # 6 quads -> 24 -> 48 triangles, evaluated.
    box("Bottle", (0.1, 0.1, 0.25), (0, -3, 0.125))
    box("FarCrate", (0.5, 0.5, 0.5), (40, 0, 0.25))
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scene.collection.objects.link(cam)
    cam.location, cam.rotation_euler = (0, -5, 0.4), (math.radians(90), 0, 0)
    scene.camera = cam
    cam.keyframe_insert("location", frame=1)
    cam.location.x = 0.3
    cam.keyframe_insert("location", frame=120)
    scene.frame_set(1)
    box("Static", (0.1, 0.1, 0.1), (3, 3, 0.05))
    joined_sign("HangSign", 10.0, 1.21)
    joined_sign("GroundSign", 12.0, 0.0)  # Chains and all, but its board stands on the ground.
    box("Beam", (1.0, 0.1, 0.1), (14.0, 0, 2.0))  # 1.95..2.05
    box("Lantern", (0.2, 0.2, 0.3), (14.0, 0, 1.8))  # Hangs from the beam's underside.
    box("Lantern2", (0.2, 0.2, 0.3), (14.3, 0, 1.75))  # 50 mm short of it.
    box("Crate3", (0.2, 0.2, 0.3), (13.7, 0, 2.2))  # Sits on the beam.
    books = bpy.data.collections.new("Books")
    scene.collection.children.link(books)
    for n in range(3):
        box(f"Book{n}", (0.03, 0.2, 0.25), (-2 + n * 0.04, -1, 0.125), collection=books)
    bpy.context.view_layer.update()


def expect(check: dict, status: str, *words: str, spawn=None) -> dict:
    advisory = check["kind"] in acceptance.ADVISORY_KINDS  # A brief's advisory list: no quote needed.
    result = acceptance.run([check if advisory else {"quote": "q", **check}], spawn, advisory=advisory)[0]
    assert result.get("advisory", False) == advisory, result
    SEEN.setdefault(check["kind"], set()).add(result["status"])
    assert result["status"] == status, f"{check}: {result['status']} ({result['detail']}), want {status}"
    for word in words:
        assert word in result["detail"], f"{check}: {word!r} not in {result['detail']!r}"
    print(f"ACCEPT {result['status']:<11} {acceptance.line(result)}")
    return result


def cases(tmp: Path) -> None:
    build()
    scene = bpy.context.scene
    expect({"kind": "exists", "name": "Armchair", "type": "EMPTY"}, "pass", "empty")
    expect({"kind": "exists", "name": "Armchiar"}, "fail", "did you mean 'Armchair'")
    expect({"kind": "exists", "name": "Armchair", "type": "mesh"}, "fail", "is empty, not mesh")
    bpy.data.objects["Stool"][ownership.QUARANTINE] = "job-w1/stool"
    expect({"kind": "exists", "name": "Stool"}, "fail", "quarantine")
    del bpy.data.objects["Stool"][ownership.QUARANTINE]

    expect({"kind": "count", "prefix": "Armchair_Leg", "type": "MESH", "min": 4, "max": 4}, "pass",
           "4 mesh objects named Armchair_Leg or starting with Armchair_Leg (Armchair_Leg1, Armchair_Leg2, ")
    expect({"kind": "count", "prefix": "Rug", "max": 1}, "pass", "1 objects named Rug or starting with Rug (Rug)")
    expect({"kind": "count", "prefix": "Armchair_Leg", "min": 5}, "fail", "want at least 5")
    expect({"kind": "count", "collection": "Books", "min": 3, "max": 3}, "pass", "in collection Books")
    expect({"kind": "count", "collection": "Shelf", "min": 1}, "fail", "no collection")

    expect({"kind": "dims", "name": "Armchair", "size": [0.7, 0.7, 0.5]}, "pass", "size (0.7, 0.7, 0.5)")
    expect({"kind": "dims", "name": "Armchair", "size": [None, None, 0.45]}, "fail", "z 50.0 mm over (tolerance 9.0 mm)")
    expect({"kind": "dims", "name": "Armchair", "size": [0.71, None, None], "tol": 0.02}, "pass")
    expect({"kind": "dims", "name": "Bulb", "size": [1, 1, 1]}, "unavailable", "no geometry")

    expect({"kind": "contact", "a": "Armchair", "relation": "rests_on", "b": "Rug"}, "pass", "4 supports touching")
    expect({"kind": "contact", "a": "Stool", "relation": "rests_on", "b": "Rug"}, "fail", "gap 12.0 mm", "Fix: move Stool")
    expect({"kind": "contact", "a": "Stool", "relation": "clear_of", "b": "Armchair", "gap": 0.1}, "pass", "clear by")
    expect({"kind": "contact", "a": "Stool", "relation": "clear_of", "b": "Armchair", "gap": 0.5}, "fail", "needs 500.0 mm")
    expect({"kind": "contact", "a": "Nope", "relation": "touches", "b": "Rug"}, "fail", "no object named")

    # Spatial relations, on whole objects and on regions of one joined mesh.
    board, chains = {"a": "HangSign", "a_part": "material:Sign_Board"}, {"b": "HangSign", "b_part": "material:Sign_Chain"}
    expect({"kind": "suspended", **board, **chains}, "pass", "HangSign[material:Sign_Board]'s lowest point 1.21 m above "
           "the ground (z 0), want at least 50.0 mm; attached to HangSign[material:Sign_Chain] at z 1.61, its top half")
    expect({"kind": "suspended", "a": "HangSign", "a_part": "attribute:board", "b": "HangSign", "b_part": "group:Chains",
            "clear": 1.0}, "pass", "1.21 m above")
    expect({"kind": "suspended", "a": "GroundSign", "a_part": "material:Sign_Board", "b": "GroundSign",
            "b_part": "material:Sign_Chain"}, "fail", "lowest point 0.0 mm above the ground (z 0)",
           "Fix: raise GroundSign[material:Sign_Board] 50.0 mm within GroundSign (a remodel for its builder")
    expect({"kind": "suspended", "a": "HangSign", "a_part": "material:Nope", **chains}, "fail",
           "no faces of HangSign in material:Nope; its materials: Sign_Board, Sign_Chain, Sign_Wood")
    expect({"kind": "suspended", "a": "Lantern", "b": "Beam", "ground": "Rug"}, "pass", "above Rug's top (z 0.015)",
           "attached to Beam at z 1.95")
    expect({"kind": "suspended", "a": "Lantern2", "b": "Beam"}, "fail", "not attached to Beam: gap 50.0 mm",
           "Fix: move Lantern2 by (0, 0, 0.05) m")
    expect({"kind": "suspended", "a": "Crate3", "b": "Beam"}, "fail", "only at z 2.05, below its middle (z 2.2): it leans")
    expect({"kind": "attached_to", "a": "HangSign", "a_part": "material:Sign_Chain", "b": "HangSign",
            "b_part": "material:Sign_Wood"}, "pass", "attached to HangSign[material:Sign_Wood]")
    expect({"kind": "attached_to", "a": "Lantern2", "b": "Beam"}, "fail", "50.0 mm from Beam", "move Lantern2 by (0, 0, 0.05)")
    expect({"kind": "above", "a": "Shade", "b": "Table", "gap": 0.4}, "pass", "Shade's lowest point z 1.05")
    expect({"kind": "above", "a": "Stool", "b": "Rug", "gap": 0.5}, "fail", "12.0 mm apart, want at least 500.0 mm",
           "Fix: move Stool by (0, 0, 0.488) m")
    expect({"kind": "above", "a": "FarCrate", "b": "Rug"}, "fail", "not over Rug")
    expect({"kind": "below", "a": "Rug", "b": "Shade"}, "pass", "Rug's highest point z 0.015")
    expect({"kind": "below", "a": "Shade", "b": "Table"}, "fail", "Fix: move Shade by (0, 0, ")

    # Armchair and Table stand on the rug's top: their bottoms share a plane, which is not overlap.
    expect({"kind": "no_overlap", "names": ["Armchair", "Table", "Stool", "Rug"]}, "pass", "none of 6 pairs")
    expect({"kind": "no_overlap", "names": ["CrateA", "CrateB", "Table"]}, "fail", "1 of 3 pairs overlap",
           "CrateA 200.0 mm into CrateB. Fix: move CrateA by (-0.202, 0, 0) m")
    expect({"kind": "no_overlap", "names": ["CrateA", "Nope"]}, "fail", "no object named")

    expect({"kind": "in_frame", "camera": "Cam", "names": ["Bottle"]}, "pass", "100% inside")
    expect({"kind": "in_frame", "camera": "Cam", "names": ["Bottle", "FarCrate"]}, "fail", "0% inside", "clipped: right (want 100%)")
    expect({"kind": "in_frame", "camera": "Cam", "names": ["Bottle"], "region": [0.0, 0.0, 0.4, 1.0]}, "fail",
           "outside region (0, 0, 0.4, 1): right by")
    expect({"kind": "in_frame", "camera": "Cam", "names": ["Bottle"], "region": [0.3, 0.1, 0.7, 0.5]}, "pass")
    expect({"kind": "in_frame", "camera": "NoCam", "names": ["Bottle"]}, "fail", "No camera named")

    expect({"kind": "faces_max", "name": "Armchair", "max": 60}, "pass", "60 triangles")
    expect({"kind": "faces_max", "name": "Dense", "max": 40}, "fail", "48 triangles")

    expect({"kind": "origin_base", "name": "Armchair"}, "pass", "0.0 mm apart")
    expect({"kind": "origin_base", "name": "Table"}, "fail", "250.0 mm apart", "move the origin by (0, 0, -0.25)")
    # The middle of its lowest 1 cm (the post's foot), not of its whole box (450 mm toward the board).
    expect({"kind": "origin_base", "name": "Sign"}, "pass", "bottom centre (6, 4, 0): 0.0 mm apart")
    expect({"kind": "origin_base", "name": "Sign", "origin_xy": [6.0, 4.0]}, "pass", "the brief's base point")
    expect({"kind": "origin_base", "name": "Sign", "origin_xy": [6.2, 4.0]}, "fail", "200.0 mm apart",
           "move the origin by (0.2, 0, 0)")

    # Advisory kinds (facts.measure): the armchair's parts are plain boxes, flat shaded, no modifiers;
    # Dense keeps its Subdivision and is smooth shaded once asked.
    expect({"kind": "no_modifiers", "name": "Armchair"}, "pass", "no unapplied modifiers")
    expect({"kind": "no_modifiers", "name": "Dense"}, "fail", "1 unapplied: Subdiv")
    expect({"kind": "flat_shaded", "name": "Armchair"}, "pass", "0% of 30 faces smooth shaded")
    for poly in bpy.data.objects["Dense"].data.polygons:
        poly.use_smooth = True
    expect({"kind": "flat_shaded", "name": "Dense"}, "fail", "100% of 24 faces smooth", "shade_flat()")
    expect({"kind": "triangles", "name": "Dense", "min": 40, "max": 100}, "pass", "48 triangles", "want 40 to 100")
    expect({"kind": "triangles", "name": "Armchair", "min": 400}, "fail", "60 triangles", "want at least 400")
    planned = [[-0.4, -0.4, 0.0], [0.4, 0.4, 1.0]]
    expect({"kind": "inside_bounds", "name": "Armchair", "bounds": planned}, "pass", "inside the planned box")
    expect({"kind": "inside_bounds", "name": "Sign", "bounds": [[5.9, 3.9, 0], [6.8, 4.1, 2.0]]}, "fail",
           "outside the planned box by x+ 150.0 mm")
    expect({"kind": "inside_bounds", "name": "Armchair"}, "unavailable", "no planned bounds")
    expect({"kind": "material_on", "name": "Armchair", "material": "Pal_Wood"}, "pass", "Armchair_Leg1")
    expect({"kind": "material_on", "name": "Armchair", "material": "Pal_Brass"}, "fail", "they use Armchair_Fabric, Pal_Wood")
    expect({"kind": "material_on", "name": "Armchair", "material": "Gold"}, "fail", "no material named")

    expect({"kind": "light_inside", "light": "Bulb", "container": "Shade"}, "pass", "inside Shade's convex hull")
    expect({"kind": "light_inside", "light": "Bulb2", "container": "Shade"}, "fail", "Bulb2 outside Shade's convex hull",
           "Fix: move Bulb2")
    expect({"kind": "light_inside", "light": "Table", "container": "Shade"}, "fail", "not a light")

    expect({"kind": "setting", "path": "render.resolution_y", "equals": 1920}, "pass", "= 1920")
    expect({"kind": "setting", "path": "scene.render.resolution_x", "equals": 1920}, "fail", "want 1920")
    expect({"kind": "setting", "path": "render.engine", "equals": "cycles"}, "pass", "CYCLES")
    expect({"kind": "setting", "path": "render.engine", "equals": "EEVEE"}, "fail", "want 'EEVEE'")
    scene.render.engine = "BLENDER_EEVEE" if "BLENDER_EEVEE" in {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items} else "BLENDER_EEVEE_NEXT"
    expect({"kind": "setting", "path": "render.engine", "equals": "EEVEE"}, "pass")
    scene.render.engine = "CYCLES"
    expect({"kind": "setting", "path": "cycles.samples", "min": 64, "max": 256}, "pass", "= 128")
    expect({"kind": "setting", "path": "cycles.samples", "min": 256}, "fail", "want 256..")
    expect({"kind": "setting", "path": "camera", "equals": "Cam"}, "pass")
    expect({"kind": "setting", "path": "frame_end", "equals": 240}, "fail", "frame_end = 120")

    frame = scene.frame_current
    expect({"kind": "animated", "name": "Cam", "path": "location", "frames": [1, 120]}, "pass", "at 120: (0.3, -5, 0.4)")
    expect({"kind": "animated", "name": "Static", "path": "location", "frames": [1, 120]}, "fail", "the same")
    expect({"kind": "animated", "name": "Cam", "path": "data.lens", "frames": [1, 60]}, "fail", "the same")
    expect({"kind": "animated", "name": "Cam", "path": "loc", "frames": [1, 60]}, "fail", "no property 'loc'")
    assert scene.frame_current == frame, "animated puts the frame back"

    expect({"kind": "contact", "a": "Armchair", "relation": "sideways", "b": "Rug"}, "unavailable", "relation: one of")

    # in_frame at a frame: Cam moves from x 0 to 0.3 over 1..120, so the bottle, in the middle third
    # at frame 1, is in the left third at 120; the frame is put back.
    expect({"kind": "in_frame", "camera": "Cam", "names": ["Bottle"], "region": [0.0, 0.1, 0.4, 0.5], "frame": 120},
           "pass", "camera Cam at frame 120")
    expect({"kind": "in_frame", "camera": "Cam", "names": ["Bottle"], "region": [0.3, 0.1, 0.7, 0.5], "frame": 120},
           "fail", "at frame 120", "outside region (0.3, 0.1, 0.7, 0.5): left by 0.243")
    assert scene.frame_current == frame, "in_frame puts the frame back"

    # light: type, energy, colour within a tolerance, a spot's cone in degrees.
    key = bpy.data.objects.new("Key", bpy.data.lights.new("Key", "SPOT"))
    scene.collection.objects.link(key)
    key.data.energy, key.data.color, key.data.spot_size = 200.0, (1.0, 0.9, 0.8), math.radians(40)
    expect({"kind": "light", "name": "Key", "type": "SPOT", "energy": [100, 300], "color": [1, 0.92, 0.8],
            "spot_size": [30, 45]}, "pass", "spot, energy 200 W", "spot_size 40.0°")
    expect({"kind": "light", "name": "Key", "energy": [None, 150], "color": [0.5, 0.9, 0.8], "tol": 0.1},
           "fail", "energy 200 W, want ..150 W", "color (1, 0.9, 0.8), want (0.5, 0.9, 0.8) within 0.1")
    expect({"kind": "light", "name": "Bulb", "type": "AREA", "spot_size": [10, 20]}, "fail",
           "a point light, want area", "spot_size: a point light has no cone")
    expect({"kind": "light", "name": "Table"}, "fail", "not a light")
    bpy.data.objects.remove(key)

    # unchanged, parent: against a Spawn; a rename is not a change, an edit is.
    ids, tokens = ownership.spawn_state()
    spawn = merge.Spawn("t-w1", tmp / "spawn.blend", ids, tokens, {"camera": "OBJECT:Cam", "world": None})
    expect({"kind": "unchanged", "names": ["Armchair", "Table"]}, "pass", "datablocks as they were", spawn=spawn)
    bpy.data.objects["Table"].name = "SideTable"
    expect({"kind": "unchanged", "names": ["SideTable"]}, "pass", spawn=spawn)
    expect({"kind": "unchanged", "names": ["Table"]}, "pass", "2 datablocks", spawn=spawn)  # By its spawn name.
    bpy.data.materials["Pal_Wood"].diffuse_color = (1, 0, 0, 1)
    expect({"kind": "unchanged", "names": ["Armchair", "SideTable"]}, "fail", "Pal_Wood (material) changed", spawn=spawn)
    bpy.data.materials["Pal_Wood"].diffuse_color = (0.4, 0.25, 0.1, 1)
    bpy.data.objects.remove(bpy.data.objects["SideTable"])
    expect({"kind": "unchanged", "names": ["Table"]}, "fail", "Table is gone", spawn=spawn)
    expect({"kind": "unchanged", "names": ["Brand_New"]}, "unavailable", "existed at spawn", spawn=spawn)
    tokens = {"OBJECT:Rug": ownership.token(bpy.data.objects["Rug"])}
    expect({"kind": "unchanged", "names": ["Rug"], "tokens": tokens}, "pass")

    # unchanged after a merge replaced what it names, as an owner's merge does (user_remap, the original
    # removed): the Arcane look pass read seven painted props as gone. The job's baseline follows the
    # replacement (waves.Job.follow), so it is found, and as it was.
    ids, tokens = ownership.spawn_state()
    spawn = merge.Spawn("t-w2", tmp / "spawn2.blend", ids, tokens, {"camera": "OBJECT:Cam", "world": None})
    old = bpy.data.objects["Rug"]
    new = old.copy()
    new.data = old.data.copy()
    scene.collection.objects.link(new)
    old_mesh = old.data
    old.user_remap(new)
    old_mesh.user_remap(new.data)
    bpy.data.batch_remove([old, old_mesh])
    new.name = new.data.name = "Rug"
    spawn.moved = {"OBJECT:Rug": new.session_uid, "MESH:Rug": new.data.session_uid}
    expect({"kind": "unchanged", "names": ["Rug"]}, "fail", "Rug is gone", spawn=spawn)
    job = types.SimpleNamespace(baseline=copy.copy(spawn))
    job.baseline.ids = dict(spawn.ids)
    waves.Job.follow(job, spawn)
    expect({"kind": "unchanged", "names": ["Rug"]}, "pass", "2 datablocks as they were", spawn=job.baseline)

    # unchanged, child: its own stamp_all tokens; created objects don't count.
    ownership.stamp_all()
    box("Armchair_Cushion", (0.5, 0.5, 0.1), (0, 0, 0.6), bpy.data.objects["Armchair"])
    expect({"kind": "unchanged", "names": ["Rug", "Armchair"]}, "pass")
    bpy.data.meshes["Rug"].vertices[0].co.z += 0.1
    expect({"kind": "unchanged", "names": ["Rug", "Armchair"]}, "fail", "Rug (mesh) changed")

    # file: relative to the saved .blend.
    expect({"kind": "file", "path": "renders/shot.png"}, "unavailable", "not saved")
    bpy.ops.wm.save_as_mainfile(filepath=str(tmp / "nook.blend"))
    (tmp / "renders").mkdir()
    (tmp / "renders" / "shot.png").write_bytes(b"\x89PNG" + b"0" * 100)
    (tmp / "renders" / "empty.png").write_bytes(b"")
    (tmp / "renders" / "frame_0001.png").write_bytes(b"x")
    expect({"kind": "file", "path": "renders/shot.png"}, "pass", "104 bytes")
    expect({"kind": "file", "path": "//renders/frame_####.png"}, "pass", "1 file matching")
    expect({"kind": "file", "path": "renders/empty.png"}, "fail", "is empty")
    expect({"kind": "file", "path": "renders/missing.png"}, "fail", "does not exist")

    # A worker's copy: a check on another worker's object is deferred, not failed; the worker's own
    # missing objects (its deliverable, its prefix, what it owns) still fail, and so does a check whose
    # objects are all here.
    lamp = {"prefix": "Lamp", "deliverable": "FloorLamp", "owned": ["OBJECT:Rug"]}

    def scoped(check: dict, status: str, *words: str, scope=lamp) -> None:
        result = acceptance.run([{"quote": "q", **check}], scope=scope)[0]
        assert result["status"] == status and all(w in result["detail"] for w in words), (check, result)
        print(f"ACCEPT {result['status']:<11} {acceptance.line(result)}")
    scoped({"kind": "no_overlap", "names": ["Lamp_Shade", "Sofa"]}, "deferred",
           "Sofa is not in this copy (another worker's): runs on the live scene after the merge")
    scoped({"kind": "in_frame", "camera": "HeroCam", "names": ["Sofa", "Rug"]}, "deferred", "HeroCam, Sofa are not")
    scoped({"kind": "no_overlap", "names": ["Sofa", "Rug"]}, "fail", "no object named", "Sofa", scope=None)
    scoped({"kind": "exists", "name": "FloorLamp"}, "fail", "no object named", "FloorLamp")
    scoped({"kind": "contact", "a": "Lamp_Base", "relation": "rests_on", "b": "Rug"}, "fail", "no object named", "Lamp_Base")
    scoped({"kind": "contact", "a": "Stool", "relation": "rests_on", "b": "Rug"}, "fail", "Fix: move Stool")

    results = acceptance.run([{"kind": "exists", "quote": "q", "name": "Rug"},
                              {"kind": "contact", "quote": "q", "a": "Stool", "relation": "rests_on", "b": "Rug"}])
    block = acceptance.verified_block(results)
    assert block.startswith("Verified by Loopcut on this scene: 1 of 2 checks.\n- c2 contact Stool rests_on Rug: FAIL"), block
    print(f"ACCEPT Verified block:\n{block}")


def preserved_cases() -> None:
    """A snapshot (acceptance.snapshot, what the checks tool records) and preserved against it: a look
    pass's new material fails until materials are excluded, a move and a vertex edit say by how much,
    and an owner's merge (the object replaced by its copy) is still the object as recorded."""
    build()
    names = ["Armchair", "HangSign", "Bulb", "Rug"]
    records = acceptance.snapshot(names)
    assert records["Armchair"]["verts"] == 5 * 8 and records["Bulb"]["dims"] is None, records
    check = {"kind": "preserved", "names": names, "snapshot": "s1", "baseline": records}
    expect(check, "pass", "4 objects as snapshot s1 recorded (origin, transform, dims, bounds, topology, vertices, "
           "materials)")
    sign_mesh = bpy.data.objects["HangSign"].data
    sign_mesh.materials[sign_mesh.materials.find("Sign_Board")] = material("Arcane_Board", (0.2, 0.5, 0.6))
    expect(check, "fail", "HangSign: materials HangSign: Sign_Wood, Sign_Chain, Sign_Board -> Sign_Wood, Sign_Chain, "
           "Arcane_Board")
    expect({**check, "exclude": ["materials"]}, "pass", "(origin, transform, dims, bounds, topology, vertices)")
    bpy.data.objects["Armchair"].location.x += 0.1
    bpy.context.view_layer.update()
    expect({**check, "exclude": ["materials"]}, "fail", "Armchair: origin (0, 0, 0.015) -> (0.1, 0, 0.015) (100.0 mm); "
           "bounds (-0.35, -0.35, 0.015)..(0.35, 0.35, 0.515) -> (-0.25, -0.35, 0.015)..(0.45, 0.35, 0.515) (up to 100.0 mm)")
    bpy.data.objects["Armchair"].location.x -= 0.1
    sign_mesh.vertices[0].co.z -= 0.03
    bpy.context.view_layer.update()
    expect({**check, "exclude": ["materials"]}, "fail", "HangSign: size (0.85, 0.1, 2) -> (0.85, 0.1, 2.03) m (z +30.0 mm)",
           "vertices moved, up to 30.0 mm at the sampled ones")
    expect({**check, "exclude": ["materials", "vertices", "dims", "bounds"]}, "pass")
    sign_mesh.vertices[0].co.z += 0.03
    old = bpy.data.objects["Rug"]  # An owner's merge: the live object replaced by its copy.
    new = old.copy()
    new.data = old.data.copy()
    bpy.context.scene.collection.objects.link(new)
    old_mesh = old.data
    old.user_remap(new)
    old_mesh.user_remap(new.data)
    bpy.data.batch_remove([old, old_mesh])
    new.name = new.data.name = "Rug"
    bpy.context.view_layer.update()
    expect({**check, "exclude": ["materials"]}, "pass", "4 objects as snapshot s1 recorded")
    bpy.data.objects.remove(bpy.data.objects["Bulb"])
    expect({**check, "exclude": ["materials"]}, "fail", "Bulb: is gone")


def corner_cases() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    corner.build()
    # Was "4 of 6 pairs overlap": Armchair 1.059 m into SideTable, 1.157 m into FloorLamp, ...
    expect({"kind": "no_overlap", "names": ["Rug", "Armchair", "SideTable", "FloorLamp"]}, "pass", "none of 6 pairs")
    # Was "SideTable 30.0 mm into Book_1": the books lie on a top whose caps are wound inward.
    expect({"kind": "no_overlap", "names": ["SideTable", "Book_1", "Book_2", "Book_3"]}, "pass", "none of 6 pairs")
    for (a, b), gap in corner.GAPS.items():
        expect({"kind": "contact", "a": a, "relation": "clear_of", "b": b}, "pass", f"clear by {contact._mm(gap)}")
    bpy.data.objects["Book_1"].location.z -= 0.01
    bpy.context.view_layer.update()
    expect({"kind": "no_overlap", "names": ["SideTable", "Book_1"]}, "fail", "1 of 1 pairs overlap",
           "SideTable 10.0 mm into Book_1. Fix: move SideTable by (0, 0, -0.012) m")
    expect({"kind": "contact", "a": "Book_1", "relation": "clear_of", "b": "SideTable"}, "fail",
           "10.0 mm into SideTable. Fix: move Book_1 by (0, 0, 0.012) m")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lc_acceptance_check_"))
    try:
        cases(tmp)
        preserved_cases()
        missing = [kind for kind in (*acceptance.KINDS, *acceptance.ADVISORY_KINDS)
                   if not {"pass", "fail"} <= SEEN.get(kind, set())]
        assert not missing, f"kinds not seen both passing and failing: {missing}"
        corner_cases()
    except Exception:
        traceback.print_exc()
        print("ACCEPT FAIL")
        return 1
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"ACCEPT OK: {sum(len(s) for s in SEEN.values())} kind/status pairs over {len(SEEN)} kinds")
    return 0


code = main()
sys.stdout.flush()
if bpy.app.background:
    sys.exit(code)
