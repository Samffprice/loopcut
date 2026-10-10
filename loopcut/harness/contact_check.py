"""check_contact against real geometry: fixture scenes built on the spot, every status, depth and
correction vector asserted, each fix applied and checked again, and watched pairs that later
run_python calls break and mend.

    Blender -b --factory-startup --python-exit-code 1 --python loopcut/harness/contact_check.py

Fixtures, side by side on one floor: a table top floating 5 mm over its legs; a lamp (a root empty)
whose third foot hangs 8 mm; a chair whose legs pass through a 15 mm rug to the floor; a box resting
right; a crate (a root empty) and a collection instance; two cubes, one 100 mm into the other; boxes
in and out of a case and a ball in an open bowl; two upright bins side by side on the floor; an unknown
relation; missing and meshless objects. Last, in a scene of its own, the shared_support eval's
reading corner (corner.py): a side table with its caps wound inward, a lamp whose base's rim is level
with the chair's feet, and the gaps between them.

Eval 20260928-005053 asset_pack: a worker's hand-built HangingSign (bmesh -> to_mesh with no
validate(), a chain-link ring left open) carried a loop with an invalid (-1) vertex index once
written to a .blend and read back. bm.from_mesh on that trusted the index and segfaulted Blender
itself (BM_edge_create, from _shape in contact.py). Reproduced here with a minimal cube that has one
polygon corrupted the same way.
"""
import array
import contextlib
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).parent))
import checkout
checkout.use()
import corner
from loopcut import contact, state, tools

work = tempfile.TemporaryDirectory(prefix="loopcut-contact-check-")
os.environ["LOOPCUT_DATA_DIR"] = str(Path(work.name) / "data")
tools._editor_context = lambda editor: contextlib.nullcontext()  # Headless: no windows to borrow.
scene = bpy.context.scene
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)

_FACES = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]  # Outward.


def box(name, lo, hi, parent=None, link=True):
    """A box with these world-space corners, its origin at the world origin."""
    verts = [(x, y, z) for z in (lo[2], hi[2]) for y in (lo[1], hi[1]) for x in (lo[0], hi[0])]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], _FACES)
    obj = bpy.data.objects.new(name, mesh)
    if link:
        scene.collection.objects.link(obj)
    obj.parent = parent
    return obj


def upright(name, x, y, height):
    """A box authored Y-up and stood up by a 90-degree X rotation, as imports are: its bottom is on
    z = 0 give or take float noise (cos 90 deg is 6e-17, not 0)."""
    verts = [(vx, vy, vz) for vz in (-y[1], -y[0]) for vy in (0.0, height) for vx in x]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], _FACES)
    obj = bpy.data.objects.new(name, mesh)
    obj.rotation_euler.x = math.pi / 2
    scene.collection.objects.link(obj)
    return obj


def empty(name, location=(0, 0, 0)):
    obj = bpy.data.objects.new(name, None)
    obj.location = location
    scene.collection.objects.link(obj)
    return obj


def legs(root, centres, size, top, bottom=0.0, name="Leg"):
    for k, (x, y) in enumerate(centres, 1):
        box(f"{name}.{k:03d}", (x - size / 2, y - size / 2, bottom), (x + size / 2, y + size / 2, top), root)


box("Floor", (-10, -10, -0.1), (20, 10, 0))
# Table: the top floats 5 mm over the legs' tops (0.72).
table_legs = empty("TableLegs")
legs(table_legs, [(x, y) for x in (-0.5, 0.5) for y in (-0.3, 0.3)], 0.05, 0.72)
box("TableTop", (-0.6, -0.4, 0.725), (0.6, 0.4, 0.755))
# Lamp on a shelf: three feet under a base, the third 8 mm short of the shelf.
box("Shelf", (2.5, -0.5, 0), (3.5, 0.5, 0.5))
lamp = empty("Lamp")
box("Base", (2.85, -0.15, 0.51), (3.15, 0.15, 0.53), lamp)
for k, (x, y, z) in enumerate([(2.9, -0.1, 0.5), (3.1, -0.1, 0.5), (3.0, 0.1, 0.508)], 1):
    box(f"Foot.{k:03d}", (x - 0.02, y - 0.02, z), (x + 0.02, y + 0.02, z + 0.01), lamp)
box("Stem", (2.99, -0.01, 0.53), (3.01, 0.01, 0.8), lamp)
box("Shade", (2.85, -0.15, 0.8), (3.15, 0.15, 0.95), lamp)
# Chair: legs from the floor up through a 15 mm rug lying on it.
box("Rug", (5.4, -0.6, 0), (6.6, 0.6, 0.015))
chair = empty("Chair")
legs(chair, [(6 + x, y) for x in (-0.2, 0.2) for y in (-0.2, 0.2)], 0.04, 0.45, name="ChairLeg")
box("Seat", (5.75, -0.25, 0.45), (6.25, 0.25, 0.5), chair)
box("Back", (5.75, 0.2, 0.5), (6.25, 0.25, 0.9), chair)
# Resting right, and roots: a crate of parts (the lid rests on its sides), a collection instance 10 mm up.
box("Box", (-3.15, -0.15, 0), (-2.85, 0.15, 0.3))
crate = empty("Crate")
box("Side.001", (-6.3, -0.3, 0), (-6.2, 0.3, 0.1), crate)
box("Side.002", (-5.8, -0.3, 0), (-5.7, 0.3, 0.1), crate)
box("Lid", (-6.3, -0.3, 0.1), (-5.7, 0.3, 0.12), crate)
kit = bpy.data.collections.new("Kit")
kit.objects.link(box("KitBox", (-0.1, -0.1, 0), (0.1, 0.1, 0.2), link=False))
kit_instance = empty("KitInstance", (-9, 0, 0.01))
kit_instance.instance_type, kit_instance.instance_collection = "COLLECTION", kit
# Penetration: CubeA's bottom 100 mm into CubeB's top.
box("CubeB", (8.5, -0.5, 0), (9.5, 0.5, 1))
box("CubeA", (8.7, -0.3, 0.9), (9.3, 0.3, 1.5))
# inside: a closed case, and an open bowl (the lower half of a sphere) holding a ball.
box("Case", (11.5, -0.5, 0), (12.5, 0.5, 1))
box("Gem", (11.9, -0.1, 0.4), (12.1, 0.1, 0.6))
box("Gem2", (12.4, -0.1, 0.4), (12.55, 0.1, 0.6))
bm = bmesh.new()
bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=0.5)
bmesh.ops.delete(bm, geom=[v for v in bm.verts if v.co.z > 1e-4], context="VERTS")
bowl_mesh = bpy.data.meshes.new("Bowl")
bm.to_mesh(bowl_mesh)
bm.free()
bowl = bpy.data.objects.new("Bowl", bowl_mesh)
bowl.location = (15, 0, 0.5)
scene.collection.objects.link(bowl)
box("Ball", (14.9, -0.1, 0.1), (15.1, 0.1, 0.3))
# A mirrored slab (scale -1 turns its faces inside out) with a peg 50 mm into it, and a one-sided wall
# (a single quad facing -Y) with a cabinet standing behind it and a picture 10 mm through it.
slab = box("Slab", (-17.5, 2, 0), (-16.5, 3, 0.5))
slab.scale.x = -1
box("Peg", (16.9, 2.4, 0.45), (17.1, 2.6, 0.65))
wall_mesh = bpy.data.meshes.new("Wall")
wall_mesh.from_pydata([(0, 5, 0), (2, 5, 0), (2, 5, 2), (0, 5, 2)], [], [(0, 1, 2, 3)])
scene.collection.objects.link(bpy.data.objects.new("Wall", wall_mesh))
box("Cabinet", (0.5, 5.5, 0), (1.5, 6.0, 1))
box("Picture", (0.8, 4.98, 1), (1.2, 5.01, 1.3))
scene.collection.objects.link(bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", "SUN")))
# Two bins standing on one floor, 0.5 m apart: each meets the plane of the other's bottom face.
upright("BinA", (20.0, 20.7), (-0.35, 0.35), 0.8)
upright("BinB", (21.2, 21.8), (-0.3, 0.3), 0.5)
bpy.context.view_layer.update()


def near(value, expected, eps=2e-4):
    assert value is not None and abs(value - expected) <= eps, (value, expected)


def same(v, expected, eps=2e-4):
    assert len(v) == 3 and all(abs(a - b) <= eps for a, b in zip(v, expected)), (v, expected)


def check(*pairs, tolerance=0.002):
    started = time.monotonic()
    results = contact.evaluate([{"a": p[0], "relation": p[1], "b": p[2], **(p[3] if len(p) > 3 else {})}
                                for p in pairs], tolerance)
    for result in results:
        print(f"CONTACT {contact.pair_line(result)}")
        for detail in result.get("details", ()) if result["status"] == "fail" else ():
            print(f"CONTACT   {detail}")
    print(f"CONTACT   ({len(pairs)} pair{'s' if len(pairs) > 1 else ''} in {time.monotonic() - started:.3f} s)")
    return results


def move(name, v):
    bpy.data.objects[name].location += Vector(v)
    bpy.context.view_layer.update()


# ------------------------------------------------------------------ rests_on
top, table = check(("TableTop", "rests_on", "TableLegs"), ("TableLegs", "rests_on", "Floor"))
assert top["status"] == "fail" and len(top["supports"]) == 1, top
near(top["supports"][0]["offset"], 0.005)
assert top["fixes"] == [("TableTop", (0.0, 0.0, top["fixes"][0][1][2]))], top["fixes"]
near(top["fixes"][0][1][2], -0.005)
assert "TableLegs: union of its 4 child meshes" in top["notes"], top["notes"]
assert table["status"] == "pass" and len(table["supports"]) == 4, table
move("TableTop", top["fixes"][0][1])
assert check(("TableTop", "rests_on", "TableLegs"))[0]["status"] == "pass"

lamp_result, = check(("Lamp", "rests_on", "Shelf"))
assert lamp_result["status"] == "fail", lamp_result
parts = sorted(s["part"] for s in lamp_result["supports"])
assert parts == ["Foot.001", "Foot.002", "Foot.003"], parts  # Not the base: it rests on the feet.
offsets = {s["part"]: s["offset"] for s in lamp_result["supports"]}
near(offsets["Foot.001"], 0.0), near(offsets["Foot.002"], 0.0), near(offsets["Foot.003"], 0.008)
assert [name for name, _ in lamp_result["fixes"]] == ["Foot.003"], lamp_result["fixes"]
same(lamp_result["fixes"][0][1], (0, 0, -0.008))
assert "Lamp: union of its 6 child meshes" in lamp_result["notes"], lamp_result["notes"]
lamp_text = contact.report([lamp_result], 0.002)
assert "move Foot.003 by (0, 0, -0.008) m" in lamp_text and "Foot.003 at (3, 0.1): gap 8.0 mm" in lamp_text, lamp_text
print("CONTACT lamp result text:\n" + lamp_text)
move("Foot.003", lamp_result["fixes"][0][1])
assert check(("Lamp", "rests_on", "Shelf"))[0]["status"] == "pass"

rug, floor = check(("Chair", "rests_on", "Rug"), ("Chair", "rests_on", "Floor"))
assert rug["status"] == "fail" and len(rug["supports"]) == 4, rug
for support in rug["supports"]:
    assert support["how"] == "through", support
    near(support["offset"], -0.015)
assert rug["fixes"] == [("Chair", rug["fixes"][0][1])] and len(rug["fixes"]) == 1, rug["fixes"]
same(rug["fixes"][0][1], (0, 0, 0.015))
assert "through Rug, 15.0 mm below its top" in "\n".join(rug["details"]), rug["details"]
assert floor["status"] == "pass" and len(floor["supports"]) == 4, floor  # Right on the floor, through the rug.
move("Chair", rug["fixes"][0][1])
assert check(("Chair", "rests_on", "Rug"))[0]["status"] == "pass"

resting, crate_result, instance = check(("Box", "rests_on", "Floor"), ("Crate", "rests_on", "Floor"),
                                        ("KitInstance", "rests_on", "Floor"))
assert resting["status"] == "pass" and len(resting["supports"]) == 1 and not resting["fixes"], resting
assert crate_result["status"] == "pass" and len(crate_result["supports"]) == 2, crate_result  # The lid is not one.
assert "Crate: union of its 3 child meshes" in crate_result["notes"], crate_result["notes"]
assert instance["status"] == "fail" and "KitInstance: 1 instanced mesh" in instance["notes"], instance
near(instance["supports"][0]["offset"], 0.01)
same(instance["fixes"][0][1], (0, 0, -0.01))

# ------------------------------------------------------------------ touches, clear_of, inside
into, clear, floor_contact, far, apart = check(
    ("CubeA", "touches", "CubeB"), ("CubeA", "clear_of", "CubeB"), ("CubeB", "touches", "Floor"),
    ("Box", "clear_of", "CubeB", {"gap": 1.0}), ("Box", "clear_of", "Crate", {"gap": 3.0}))
assert into["status"] == "fail" and "100.0 mm into CubeB" in into["line"], into
same(into["fixes"][0][1], (0, 0, 0.1))
assert clear["status"] == "fail", clear
same(clear["fixes"][0][1], (0, 0, 0.102))
assert floor_contact["status"] == "pass" and far["status"] == "pass", (floor_contact, far)
assert apart["status"] == "fail" and "2.55 m apart, needs 3 m" in apart["line"], apart
same(apart["fixes"][0][1], (0.45, 0, 0))
move("CubeA", into["fixes"][0][1])
touching, still = check(("CubeA", "touches", "CubeB"), ("CubeA", "clear_of", "CubeB"))
assert touching["status"] == "pass" and still["status"] == "fail" and still["line"].startswith("touching"), (touching, still)
same(still["fixes"][0][1], (0, 0, 0.002))

# A vertex in the plane of a face is on it, not behind it (was "1.1 m into BinB").
side_by_side, bins_touch = check(("BinA", "clear_of", "BinB", {"gap": 0.1}), ("BinA", "touches", "BinB"))
assert side_by_side["status"] == "pass" and "clear by 500.0 mm" in side_by_side["line"], side_by_side
assert bins_touch["status"] == "fail" and "gap 500.0 mm" in bins_touch["line"], bins_touch
same(bins_touch["fixes"][0][1], (0.5, 0, 0))

held, poking, ball = check(("Gem", "inside", "Case"), ("Gem2", "inside", "Case"), ("Ball", "inside", "Bowl"))
assert held["status"] == "pass" and ball["status"] == "pass", (held, ball)
assert poking["status"] == "fail" and "up to 50.0 mm" in poking["line"], poking
same(poking["fixes"][0][1], (-0.05, 0, 0))

peg, peg_on, behind, through_wall = check(("Peg", "touches", "Slab"), ("Peg", "rests_on", "Slab"),
                                          ("Cabinet", "clear_of", "Wall"), ("Picture", "touches", "Wall"))
assert peg["status"] == "fail" and "50.0 mm into Slab" in peg["line"], peg
same(peg["fixes"][0][1], (0, 0, 0.05))
assert peg_on["status"] == "fail" and peg_on["supports"][0]["how"] == "into", peg_on
same(peg_on["fixes"][0][1], (0, 0, 0.05))
assert behind["status"] == "pass" and "clear by 500.0 mm" in behind["line"], behind  # Not in the wall.
assert through_wall["status"] == "fail" and "10.0 mm into Wall" in through_wall["line"], through_wall
same(through_wall["fixes"][0][1], (0, -0.01, 0))

# ------------------------------------------------------------------ what cannot be checked
text = tools.execute("check_contact", json.dumps({"pairs": [
    {"a": "Box", "relation": "on_top_of", "b": "Floor"}, {"a": "Lampp", "relation": "rests_on", "b": "Shelf"},
    {"a": "Sun", "relation": "touches", "b": "Floor"}, {"a": "Foot.001", "relation": "touches", "b": "Lamp"},
    {"a": "Box", "relation": "rests_on", "b": "Floor"}]})).text
print("CONTACT unavailable call:\n" + text)
assert text.startswith("Contact: 1 pass, 0 fail, 4 unavailable"), text
assert "unavailable: Box on_top_of Floor: unknown relation 'on_top_of': use rests_on, touches, clear_of, inside" in text
assert 'unavailable: Lampp rests_on Shelf: no object named "Lampp" in this scene (did you mean "Lamp"?)' in text
assert "unavailable: Sun touches Floor: Sun is a light with no mesh and no mesh children" in text
assert "unavailable: Foot.001 touches Lamp: Foot.001 is part of both Foot.001 and Lamp" in text
assert "pass: Box rests_on Floor: 1 support touching." in text

# ------------------------------------------------------------------ watched pairs and run_python
state.reset()
started = time.monotonic()
plain = tools.run_python("x = 1", summary="nothing watched")
unwatched = time.monotonic() - started
assert "contact" not in plain.text, plain.text
watched = tools.execute("check_contact", json.dumps({"watch": True, "pairs": [
    {"a": "Box", "relation": "rests_on", "b": "Floor"}, {"a": "Lamp", "relation": "rests_on", "b": "Shelf"}]}))
assert "Watching 2 pairs" in watched.text and len(state.session()["contact_watch"]) == 2, watched.text


def run(code, label):
    started = time.monotonic()
    result = tools.run_python(code, summary=label)
    lines = [line for line in result.text.splitlines() if "contact " in line]
    print(f"CONTACT run_python {label}: {time.monotonic() - started:.3f} s, {lines}")
    assert result.ok, result.text
    return lines


lines = run("bpy.data.objects['Box'].location.z += 0.012", "lift the box")
assert lines == ["~ contact pass -> FAIL: Box rests_on Floor: 1 support: 1 floating. "
                 "Fix: move Box by (0, 0, -0.012) m."], lines  # One line, in the Scene changes.
assert run("bpy.data.objects['Seat'].location.x += 1", "move something unwatched") == []
lines = run("for v in bpy.data.objects['Foot.003'].data.vertices:\n    v.co.z += 0.008", "raise a foot's mesh")
assert len(lines) == 1 and lines[0].startswith("~ contact pass -> FAIL: Lamp rests_on Shelf") \
    and "move Foot.003 by (0, 0, -0.008) m" in lines[0], lines
lines = run("bpy.data.objects['Box'].location.z -= 0.012", "put the box back")
assert lines == ["~ contact FAIL -> pass: Box rests_on Floor: 1 support touching."], lines
started = time.monotonic()
tools.run_python("x = 1", summary="watched, nothing changed")
print(f"CONTACT run_python cost: {unwatched:.3f} s unwatched, {time.monotonic() - started:.3f} s with 2 watched unchanged")

# ------------------------------------------------------------------ a denser mesh, for the time it takes
bm = bmesh.new()
bmesh.ops.create_uvsphere(bm, u_segments=256, v_segments=128, radius=0.5)
dense = bpy.data.meshes.new("Dense")
bm.to_mesh(dense)
bm.free()
ball_obj = bpy.data.objects.new("Dense", dense)
ball_obj.location = (18, 0, 0.5)
scene.collection.objects.link(ball_obj)
bpy.context.view_layer.update()
dense_result, dense_touch = check(("Dense", "rests_on", "Floor"), ("Dense", "touches", "Floor"))
assert dense_result["status"] == "pass" and len(dense_result["supports"]) == 1, dense_result
assert dense_touch["status"] == "pass", dense_touch
print(f"CONTACT dense sphere: {len(dense.vertices)} vertices")

# ------------------------------------------------------------------ the reading corner
# Pairs 113 mm to 1.28 m apart read as 1.06 to 1.91 m "into" each other: whether a point was inside
# came from the normal of its nearest face, which the side table's inward caps turn round, and whose
# sign is noise when the nearest point is the rim of the lamp's base, level with the chair's feet.
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
corner.build()
pairs = [(a, "clear_of", b) for a, b in corner.GAPS] + [(b, "clear_of", a) for a, b in corner.GAPS]
for result in check(*pairs):
    gap = corner.GAPS.get((result["a"], result["b"])) or corner.GAPS[(result["b"], result["a"])]
    assert result["status"] == "pass" and result["line"] == f"clear by {contact._mm(gap)}", result
standing = ("Armchair", "SideTable", "FloorLamp")
for result in check(*[(name, "touches", "Rug") for name in standing], *[(name, "rests_on", "Rug") for name in standing],
                    *[(f"Book_{n}", "rests_on", "SideTable") for n in (1, 2, 3)]):
    assert result["status"] == "pass" and "into" not in result["line"], result
# Parity finds a real overlap in a part wound inward as in any other.
move("Book_1", (0, 0, -0.01))
sunk, sunk_clear = check(("Book_1", "touches", "SideTable"), ("Book_1", "clear_of", "SideTable"))
assert sunk["status"] == "fail" and "10.0 mm into SideTable" in sunk["line"], sunk
same(sunk["fixes"][0][1], (0, 0, 0.01))
assert sunk_clear["status"] == "fail" and "10.0 mm into SideTable" in sunk_clear["line"], sunk_clear
move("Book_1", sunk["fixes"][0][1])
assert check(("Book_1", "touches", "SideTable"))[0]["status"] == "pass"

# Two boxes joined into one mesh through each other (a plus): parity over the whole mesh would
# call where they overlap outside. A nut 20 mm into the overlap, off every face centre, is found.
plus = bpy.data.meshes.new("Plus")
plus.from_pydata([(x, y, z) for lo, hi in (((5.0, 0.0, 0.0), (6.0, 0.2, 0.2)), ((5.4, -0.4, 0.0), (5.6, 0.6, 0.2)))
                  for z in (lo[2], hi[2]) for y in (lo[1], hi[1]) for x in (lo[0], hi[0])], [],
                 _FACES + [tuple(i + 8 for i in face) for face in _FACES])
scene.collection.objects.link(bpy.data.objects.new("Plus", plus))
box("Nut", (5.44, 0.09, 0.18), (5.46, 0.11, 0.28))
bpy.context.view_layer.update()
nut, = check(("Nut", "touches", "Plus"))
assert nut["status"] == "fail" and "20.0 mm into Plus" in nut["line"], nut
same(nut["fixes"][0][1], (0, 0, 0.02))
# ------------------------------------------------------------------ an open strap by a camera's lens
# A thin strap loop (a torus with a gap cut in it: open, no inside) 39 mm clear of a lens barrel by its
# bounds read as 140-150 mm into the lens parts. Open surfaces have no inside and count only where the
# two meet; wound either way, and a band looped round a lens mount 9.3 mm clear of its corners.
def strap(name, centre, major, minor, gap_segments=3, flip=False, segments=48, sides=8):
    verts, faces = [], []
    for i in range(segments):
        u = 2 * math.pi * i / segments
        normal = (math.cos(u), 0.0, math.sin(u))
        for j in range(sides):
            v = 2 * math.pi * j / sides
            verts.append((centre[0] + (major + minor * math.cos(v)) * normal[0], centre[1] + minor * math.sin(v),
                          centre[2] + (major + minor * math.cos(v)) * normal[2]))
    for i in range(segments - gap_segments):  # The last few segments left out: two open ends.
        for j in range(sides):
            face = (i * sides + j, (i + 1) % segments * sides + j, (i + 1) % segments * sides + (j + 1) % sides,
                    i * sides + (j + 1) % sides)
            faces.append(face[::-1] if flip else face)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)
    return obj


def band(name, centre, radius, width, segments=64):
    verts = [(centre[0] + radius * math.cos(2 * math.pi * i / segments), centre[1] + y,
              centre[2] + radius * math.sin(2 * math.pi * i / segments)) for i in range(segments) for y in (-width / 2, width / 2)]
    faces = [(2 * i, 2 * ((i + 1) % segments), 2 * ((i + 1) % segments) + 1, 2 * i + 1) for i in range(segments)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    scene.collection.objects.link(obj)
    return obj


for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
box("Lens_Barrel", (-0.2, -0.1, -0.1), (0.161, 0.1, 0.1))
box("Lens_Ring", (-0.24, -0.12, -0.12), (-0.2, 0.12, 0.12))
strap("Strap", (0.324, 0.0, 0.0), 0.12, 0.004)
strap("Strap_Flipped", (0.324, 0.5, 0.0), 0.12, 0.004, flip=True)
box("Lens_Barrel_2", (-0.2, 0.4, -0.1), (0.161, 0.6, 0.1))
box("Lens_Mount", (1.95, -0.05, -0.05), (2.05, 0.05, 0.05))
band("Band", (2.0, 0.0, 0.0), 0.08, 0.02)
bpy.context.view_layer.update()
assert not bpy.data.objects["Strap"].data.edges[0].is_loose
results = check(("Strap", "clear_of", "Lens_Barrel"), ("Lens_Barrel", "clear_of", "Strap"),
                ("Strap", "clear_of", "Lens_Ring"), ("Strap_Flipped", "clear_of", "Lens_Barrel_2"),
                ("Lens_Barrel_2", "clear_of", "Strap_Flipped"), ("Strap", "touches", "Lens_Barrel"),
                ("Band", "clear_of", "Lens_Mount"), ("Lens_Mount", "clear_of", "Band"))
for result in results[:5]:
    assert "into" not in result["line"], result
for result in (results[0], results[1], results[3], results[4]):
    assert result["status"] == "pass" and result["line"] == "clear by 39.0 mm", result
assert results[2]["status"] == "pass" and results[2]["line"] == "clear by 400.0 mm", results[2]
assert results[5]["status"] == "fail" and "39.0 mm" in results[5]["line"] and "into" not in results[5]["line"], results[5]
for result in results[6:]:
    gap = float(result["line"].removeprefix("clear by ").removesuffix(" mm"))
    assert result["status"] == "pass" and 9.0 < gap < 9.5, result
# A real overlap reads as it is, wound either way: moved 49 mm in, the strap is 10 mm into the barrel.
# Wound inward (Strap), it read as 375 mm: once they met anywhere, the barrel's far corner, in front
# of the strap's near side, counted as straight behind that face.
move("Strap", (-0.049, 0, 0))
move("Strap_Flipped", (-0.049, 0, 0))
for result in check(("Strap", "touches", "Lens_Barrel"), ("Lens_Barrel", "touches", "Strap"),
                    ("Strap_Flipped", "touches", "Lens_Barrel_2"), ("Lens_Barrel_2", "clear_of", "Strap_Flipped")):
    assert result["status"] == "fail" and f"10.0 mm into {result['b']}" in result["line"], result

# ------------------------------------------------------------------ a corrupted polygon loop (crash regression)
# A box with one polygon's vertex_index poked to -1, the way a bmesh.to_mesh() with no validate()
# call can leave a .blend after a botched hand-built mesh (an open ring whose last face collapsed).
# bm.from_mesh trusted that index and crashed Blender itself; _shape now calls mesh.validate() first,
# so the bad polygon is dropped and the other five still measure.
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
box("Floor", (-1, -1, -0.1), (5, 1, 0))
broken = box("Broken", (0, 0, 0), (1, 1, 1))
loop_verts = array.array("i", [0]) * len(broken.data.loops)
broken.data.loops.foreach_get("vertex_index", loop_verts)
loop_verts[1] = -1  # One loop of one face now points nowhere.
broken.data.loops.foreach_set("vertex_index", loop_verts)
broken.data.update()
box("Neighbour", (1.5, 0, 0), (2.5, 1, 1))
bpy.context.view_layer.update()
broken_result, neighbour_result = check(("Broken", "rests_on", "Floor"), ("Broken", "touches", "Neighbour"))
assert broken_result["status"] == "pass", broken_result  # Five good faces are enough to rest on the floor.
assert neighbour_result["status"] == "fail" and "into" not in neighbour_result["line"], neighbour_result
print("CONTACT OK")
