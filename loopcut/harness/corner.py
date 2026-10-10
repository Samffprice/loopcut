"""The reading corner of the shared_support eval (run 20260927-221551), rebuilt from its measured
dims and transforms for contact_check and acceptance_check.

On it check_contact's clear_of and the no_overlap check read 1.1 to 1.9 m of penetration between
objects that triangle tests put 113 mm to 1.28 m apart, for two reasons the pieces keep:
- The side table was built by hand with its caps wound inward (the top's and every leg's), so the
  nearest-face normal said "inside" for anything whose nearest point was on a cap.
- Beside a disc (the lamp's base), level with its bottom, the nearest point is the rim, and the
  bottom face's plane passes through the point: its side is float noise times the distance.

build() makes Rug, Armchair (a root empty and 11 bevelled parts), SideTable (a root empty, a top
and 3 legs), Book_1 to Book_3 and FloorLamp (a root empty, a base, a step, a pole and an open shade)
where they were, around the world origin.
"""

import math

import bpy
from mathutils import Euler, Matrix, Vector

# Measured on the eval's final scene, by exact tests (triangle crossings, vertex-face and
# edge-edge distances): no pair crosses, and these are the gaps.
GAPS = {("Armchair", "SideTable"): 0.1130, ("Armchair", "FloorLamp"): 0.2783, ("SideTable", "FloorLamp"): 1.2800}


def _link(name, mesh, location, parent=None, rotation=(0, 0, 0), scale=(1, 1, 1), bevel=None):
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = parent
    obj.matrix_world = Matrix.LocRotScale(Vector(location), Euler(rotation), Vector(scale))
    if bevel:
        modifier = obj.modifiers.new("Bevel", "BEVEL")
        modifier.width, modifier.segments, modifier.limit_method = bevel[0], bevel[1], "ANGLE"
    return obj


def _box(name, size):
    """A box of this size centred on its origin."""
    x, y, z = (s / 2 for s in size)
    verts = [(vx, vy, vz) for vz in (-z, z) for vy in (-y, y) for vx in (-x, x)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)])
    return mesh


def _cylinder(name, segments, bottom, top, z0, z1, caps="out"):
    """A ring of radius bottom at z0 and one of radius top at z1, sides facing out; caps "out",
    "in" (wound the wrong way, as the side table's were) or None (open, as a lamp shade)."""
    ring = [(math.cos(2 * math.pi * i / segments), math.sin(2 * math.pi * i / segments)) for i in range(segments)]
    verts = [(c * bottom, s * bottom, z0) for c, s in ring] + [(c * top, s * top, z1) for c, s in ring]
    faces = [(i, (i + 1) % segments, segments + (i + 1) % segments, segments + i) for i in range(segments)]
    down, up = list(range(segments))[::-1], list(range(segments, 2 * segments))
    if caps == "out":
        faces += [down, up]
    elif caps == "in":
        faces += [down[::-1], up[::-1]]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    return mesh


def _root(name, location):
    obj = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(obj)
    obj.matrix_world = Matrix.Translation(location)  # Now, not at the next update: children are placed in world space.
    return obj


def build() -> None:
    _link("Rug", _cylinder("Rug", 96, 1.3, 1.3, 0.0, 0.015), (0, 0, 0))

    chair = _root("Armchair", (0, 0, 0))
    for side, x in (("L", -0.385), ("R", 0.385)):
        _link(f"Armchair_Arm{side}", _box(f"Armchair_Arm{side}", (0.09, 0.78, 0.18)), (x, 0.18, 0.54), chair,
              scale=(1, 0.9487, 1), bevel=(0.03, 3))
    _link("Armchair_BackCushion", _box("Armchair_BackCushion", (0.76, 0.14, 0.58)), (0, 0.575, 0.66), chair,
          rotation=(math.radians(-6), 0, 0), bevel=(0.04, 3))
    _link("Armchair_BackPostL", _cylinder("Armchair_BackPostL", 24, 0.022, 0.022, -0.33, 0.33),
          (-0.345, 0.575, 0.625), chair, bevel=(0.002, 2))
    _link("Armchair_BackPostR", _cylinder("Armchair_BackPostR", 24, 0.022, 0.022, -0.33, 0.33),
          (0.345, 0.575, 0.625), chair, bevel=(0.002, 2))
    _link("Armchair_Frame", _box("Armchair_Frame", (0.78, 0.8, 0.035)), (0, 0.2, 0.3125), chair,
          scale=(1, 0.975, 1), bevel=(0.004, 3))
    for n, (x, y) in enumerate(((0.33, 0.61), (0.33, -0.15), (-0.33, 0.38), (-0.33, -0.38)), 1):
        _link(f"Armchair_Leg{n}", _cylinder(f"Armchair_Leg{n}", 24, 0.025, 0.025, -0.14, 0.14), (x, y, 0.155),
              chair, bevel=(0.002, 2))
    _link("Armchair_SeatCushion", _box("Armchair_SeatCushion", (0.8, 0.72, 0.12)), (0, 0.17, 0.39), chair,
          bevel=(0.035, 3))

    table = _root("SideTable", (0.796, 0.2, 0.015))
    _link("SideTable_Top", _cylinder("SideTable_Top", 48, 0.25, 0.25, 0.525, 0.55, caps="in"),
          (0.796, 0.2, 0.015), table, bevel=(0.003, 2))
    for n, (x, y) in enumerate(((0.796, 0.36), (0.65744, 0.12), (0.93456, 0.12)), 1):
        _link(f"SideTable_Leg{n}", _cylinder(f"SideTable_Leg{n}", 24, 0.014, 0.022, 0.0, 0.525, caps="in"),
              (x, y, 0.015), table, bevel=(0.003, 2))
    for n, (size, (x, y), turn) in enumerate((((0.13, 0.19, 0.03), (0.721, 0.23), 0),
                                              ((0.12, 0.18, 0.025), (0.871, 0.24), -6),
                                              ((0.18, 0.11, 0.022), (0.796, 0.065), 2)), 1):
        mesh = _box(f"Book_{n}", size)
        mesh.transform(Matrix.Translation((0, 0, size[2] / 2)))  # Origin at the bottom.
        _link(f"Book_{n}", mesh, (x, y, 0.565), rotation=(0, 0, math.radians(turn)))

    lamp = _root("FloorLamp", (-0.75, 0.55, 0.015))
    for name, segments, bottom, top, z0, z1, caps in (
            ("FloorLamp_Base", 48, 0.15, 0.15, 0.0, 0.035, "out"),
            ("FloorLamp_BaseStep", 48, 0.06, 0.06, 0.035, 0.055, "out"),
            ("FloorLamp_Pole", 48, 0.012, 0.012, 0.055, 1.62, "out"),
            ("FloorLamp_Shade", 48, 0.21, 0.155, 1.235, 1.585, None)):
        _link(name, _cylinder(name, segments, bottom, top, z0, z1, caps), (-0.75, 0.55, 0.015), lamp)
    bpy.context.view_layer.update()
