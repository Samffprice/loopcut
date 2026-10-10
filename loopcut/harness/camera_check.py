"""frame_camera against real Blender, checked the way a render would see it.

    Blender -b --factory-startup --python loopcut/harness/camera_check.py
Headless, small scenes made on the spot, no model or network; ~1 s. Every case goes through
tools.execute, then projects every evaluated vertex of the subject with
bpy_extras.object_utils.world_to_camera_view (this script's own reading, through to_mesh, not the
tool's) and asserts: all inside, the tightest margin is the requested one on both sides of the
limiting axis, the subject centred, and the report's numbers are those. Cases: a single cube on a
camera the tool creates, a wide row with an Array modifier, a tall box at 9:16 and with a VERTICAL
sensor fit, a root empty with nested children, a collection instance, an orthographic camera, a
camera in QUATERNION rotation mode aimed by a vector, lens given vs kept, lens shift, a sample
smaller than the mesh, the clip range following a big subject, keep, report_only on a clipped
view changing nothing, and one undo step. Close shots: fill 2 on a bottle's cap puts the cap in the
middle and the subject at 2x its whole-fit size with the crop reported, fill 3 on a point at 9:16,
aim with fill 1, orthographic fill; camera.coverage giving report_only's numbers, exact on
given corners; and report_only at a frame, then at several, of an animated shot (a camera tracking
an animated empty), measured there with the Track To evaluated and the scene back on its frame
after. Regions: a product fitted into the right 60% (the binding margins named and exact, the rest
inside the region), with a lens shift kept, orthographic, and aim with fill 1 naming the one margin
that binds, measured.
"""

import contextlib
import json
import math
import re
import sys
import traceback
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checkout
checkout.use()
from loopcut import camera, scene_diff, tools

tools._editor_context = lambda editor: contextlib.nullcontext()  # Headless: no windows to borrow.
EXACT = 1e-4    # Margins on the limiting axis, and bounds against the report (printed to 3 decimals: 5e-4).
CENTRED = 2e-3  # The looser axis: centred on a sample when the mesh is bigger than the sample.
ROW = re.compile(r"- (?P<name>.+?): x (-?\d+\.\d+)\.\.(-?\d+\.\d+) y (-?\d+\.\d+)\.\.(-?\d+\.\d+), "
                 r"(?P<share>[\d.]+)% inside, margins L (-?\d+\.\d+) R (-?\d+\.\d+) B (-?\d+\.\d+) T (-?\d+\.\d+)")


def fresh(resolution=(1920, 1080)) -> None:
    for collection in (bpy.data.objects, bpy.data.meshes, bpy.data.cameras, bpy.data.lights, bpy.data.collections):
        for item in list(collection):
            collection.remove(item)
    render = bpy.context.scene.render
    render.resolution_x, render.resolution_y = resolution
    render.pixel_aspect_x = render.pixel_aspect_y = 1.0


def cube(name: str, location=(0, 0, 0), scale=(1, 1, 1), parent=None, link=True):
    bpy.ops.mesh.primitive_cube_add(location=location, scale=scale)
    obj = bpy.context.active_object
    obj.name = obj.data.name = name
    if parent is not None:
        obj.parent = parent
    if not link:
        bpy.context.scene.collection.objects.unlink(obj)
    return obj


def new_camera(name="Shot", location=(6, -6, 4), mode="XYZ", ortho=False):
    obj = bpy.data.objects.new(name, bpy.data.cameras.new(name))
    bpy.context.scene.collection.objects.link(obj)
    obj.location, obj.rotation_mode = location, mode
    obj.data.type = "ORTHO" if ortho else "PERSP"
    bpy.context.scene.camera = obj
    return obj


def vertices(names: list) -> list:
    """Every evaluated vertex of the named objects and their children, in world space, and of what
    a collection-instance empty among them instances."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []

    def add(obj, matrix):
        if obj.type != "MESH":
            return
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        points.extend(matrix @ v.co for v in mesh.vertices)
        evaluated.to_mesh_clear()
    for name in names:
        root = bpy.data.objects[name]
        for obj in (root, *root.children_recursive):
            add(obj, obj.evaluated_get(depsgraph).matrix_world)
            if obj.instance_type == "COLLECTION" and obj.instance_collection:
                for member in obj.instance_collection.all_objects:
                    add(member, obj.matrix_world @ member.matrix_world)
    return points


def frame(**arguments) -> tuple:
    result = tools.execute("frame_camera", json.dumps(arguments))
    assert result.ok, result.text
    return result, bpy.data.objects[re.match(r'Camera "(.+?)"', result.text).group(1)]


def measure(cam, names: list) -> tuple:
    scene = bpy.context.scene
    projected = [world_to_camera_view(scene, cam, p) for p in vertices(names)]
    us, vs, depths = [p.x for p in projected], [p.y for p in projected], [p.z for p in projected]
    margins = (min(us), 1 - max(us), min(vs), 1 - max(vs))
    inside = sum(0 <= u <= 1 and 0 <= v <= 1 and cam.data.clip_start < z < cam.data.clip_end
                 for u, v, z in zip(us, vs, depths)) / len(projected)
    return margins, inside, depths


def row_of(text: str, name: str) -> tuple:
    for line in text.splitlines():
        found = ROW.match(line)
        if found and found["name"] == name:
            numbers = [float(g) for g in found.groups()[1:] if g is not None and g != found["share"]]
            return numbers[:4], numbers[4:], float(found["share"]) / 100
    raise AssertionError(f"no row for {name}:\n{text}")


def check(label: str, result, cam, names: list, margin: float, row: str = "", centred: float = CENTRED) -> tuple:
    """All in, the requested margin on both sides of the limiting axis, centred on the other,
    and the report's bounds, margins and share are what world_to_camera_view says."""
    margins, inside, depths = measure(cam, names)
    assert inside == 1.0, f"{label}: {inside:.4f} inside, margins {margins}"
    assert min(depths) > cam.data.clip_start and max(depths) < cam.data.clip_end, f"{label}: clip {depths}"
    pairs = sorted([margins[:2], margins[2:]], key=min)
    tight, loose = pairs
    assert abs(tight[0] - margin) < EXACT and abs(tight[1] - margin) < EXACT, f"{label}: tight {tight} vs {margin}"
    assert abs(loose[0] - loose[1]) < centred and min(loose) >= margin - EXACT, f"{label}: not centred {loose}"
    bounds, reported, share = row_of(result.text, row or (names[0] if len(names) == 1 else "All together"))
    expected = (margins[0], 1 - margins[1], margins[2], 1 - margins[3])
    assert all(abs(a - b) < 6e-4 for a, b in zip(bounds, expected)), f"{label}: bounds {bounds} vs {expected}"
    assert all(abs(a - b) < 6e-4 for a, b in zip(reported, margins)), f"{label}: margins {reported} vs {margins}"
    assert share == 1.0, f"{label}: report says {share}"
    shown = re.search(r"at \[(.+?)\]", result.text).group(1)
    assert all(abs(float(a) - b) < 6e-4 for a, b in zip(shown.split(","), cam.matrix_world.translation)), label
    print(f"CAMERA {label}: margins L {margins[0]:.4f} R {margins[1]:.4f} B {margins[2]:.4f} T {margins[3]:.4f}")
    return margins


def right_axis(cam) -> Vector:
    return cam.matrix_world.to_3x3().normalized().col[0]


def cases() -> None:
    scene = bpy.context.scene

    # A single cube, no camera yet: create makes "Camera" and sets it as the scene camera.
    fresh()
    cube("Box")
    result, cam = frame(objects=["Box"], create=True, direction="three_quarter")
    assert cam.name == "Camera" and scene.camera == cam and "created and framed" in result.text, result.text
    check("single cube, created", result, cam, ["Box"], 0.08)
    assert result.scene_before != result.scene_after, "the change reaches the turn's changes card"
    print(f"CAMERA report example:\n{result.text}")

    # A wide row, one of them an Array modifier: evaluated vertices, not the base mesh.
    fresh()
    for n in range(4):
        cube(f"Crate{n}", location=(n * 3.0, 0, 0))
    array = bpy.data.objects["Crate3"].modifiers.new("Array", "ARRAY")
    array.count = 3
    new_camera()
    names = [f"Crate{n}" for n in range(4)]
    result, cam = frame(objects=names, direction="front", margin=0.05)
    assert "fitted to the width" in result.text, result.text
    check("wide row with an array, landscape", result, cam, names, 0.05)
    assert max(p.x for p in vertices(["Crate3"])) > 13.5, "the array's copies count: the base mesh ends at 10"

    # A tall box: portrait 9:16, then a VERTICAL sensor fit at 16:9.
    fresh((1080, 1920))
    cube("Tower", scale=(0.5, 0.5, 3))
    cube("Base", location=(1.2, 0.5, -2.5), scale=(1.2, 1, 0.3))
    new_camera()
    result, cam = frame(objects=["Tower", "Base"], direction="three_quarter")
    check("tall, 9:16", result, cam, ["Tower", "Base"], 0.08)
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    cam.data.sensor_fit, cam.data.sensor_height = "VERTICAL", 20.0
    result, cam = frame(objects=["Tower"], direction="front", margin=0.1)
    assert "fitted to the height" in result.text and "sensor fit VERTICAL" in result.text, result.text
    check("tall, vertical sensor fit", result, cam, ["Tower"], 0.1)

    # A root empty with nested children, and a collection instance.
    fresh()
    root = bpy.data.objects.new("Root", None)
    scene.collection.objects.link(root)
    root.location = (2, 1, 0)
    arm = cube("Arm", location=(3, 1, 0), scale=(2, 0.3, 0.3), parent=root)
    cube("Hand", location=(5, 1, 1), scale=(0.4, 0.4, 0.4), parent=arm)
    new_camera()
    result, cam = frame(objects=["Root"], direction="side")
    check("root empty with nested children", result, cam, ["Root"], 0.08)
    kit = bpy.data.collections.new("Kit")
    for n in range(3):
        kit.objects.link(cube(f"Part{n}", location=(n * 1.5, 0, n * 0.5), link=False))
    holder = bpy.data.objects.new("KitInstance", None)
    holder.instance_type, holder.instance_collection = "COLLECTION", kit
    scene.collection.objects.link(holder)
    holder.location = (-6, 4, 0)
    result, cam = frame(objects=["KitInstance"], direction="three_quarter")
    check("collection instance", result, cam, ["KitInstance"], 0.08)

    # Orthographic: the scale is solved.
    fresh()
    for n in range(3):
        cube(f"Tile{n}", location=(n * 2.5, n * 0.7, 0), scale=(1, 1, 0.2))
    cam = new_camera(ortho=True)
    names = ["Tile0", "Tile1", "Tile2"]
    result, cam = frame(objects=names, direction="top", margin=0.12)
    assert "ortho_scale" in result.text, result.text
    check("orthographic, top", result, cam, names, 0.12)
    before = cam.data.ortho_scale
    result, cam = frame(objects=names, direction="three_quarter", margin=0.12)
    check("orthographic, three_quarter", result, cam, names, 0.12)
    assert cam.data.ortho_scale != before
    refused = tools.execute("frame_camera", json.dumps({"objects": names, "lens": 50}))
    assert not refused.ok and "orthographic" in refused.text, refused.text

    # A camera in QUATERNION mode aimed by a vector: the mode stays, roll stays zero.
    fresh()
    cube("Statue", scale=(0.6, 0.6, 1.5))
    cam = new_camera(mode="QUATERNION")
    result, cam = frame(objects=["Statue"], direction="2,-1,0.8")
    assert cam.rotation_mode == "QUATERNION", cam.rotation_mode
    forward = -cam.matrix_world.to_3x3().normalized().col[2]
    assert (forward + Vector((2, -1, 0.8)).normalized()).length < 1e-5, forward
    assert abs(right_axis(cam).z) < 1e-6, "roll"
    check("quaternion camera, vector direction", result, cam, ["Statue"], 0.08)

    # keep: only the position changes, the rotation stays; lens given vs kept.
    rotation = cam.matrix_world.to_3x3().normalized()
    result, cam = frame(objects=["Statue"], margin=0.2)
    assert all((a - b).length < 1e-6 for a, b in zip(cam.matrix_world.to_3x3().normalized().col, rotation.col)), "keep"
    check("keep, margin 0.2", result, cam, ["Statue"], 0.2)
    distance_kept = (cam.matrix_world.translation - Vector((0, 0, 0))).length
    result, cam = frame(objects=["Statue"], lens=85)
    assert abs(cam.data.lens - 85) < 1e-6 and "lens 85.0 mm" in result.text, result.text
    check("lens 85", result, cam, ["Statue"], 0.08)
    assert (cam.matrix_world.translation.length > distance_kept), "a longer lens stands further back"
    result, cam = frame(objects=["Statue"], lens=24)
    check("lens 24", result, cam, ["Statue"], 0.08)

    # Lens shift, and a 9:16 render with several objects.
    cam.data.shift_x, cam.data.shift_y = 0.12, -0.05
    result, cam = frame(objects=["Statue"], direction="front")
    check("lens shift", result, cam, ["Statue"], 0.08)
    fresh((1080, 1920))
    for n, spot in enumerate(((0, 0, 0), (1.5, 1, 0.5), (-1, 2, 1.5))):
        cube(f"Piece{n}", location=spot, scale=(0.5, 0.5, 0.5 + n * 0.3))
    new_camera()
    names = ["Piece0", "Piece1", "Piece2"]
    result, cam = frame(objects=names, direction="three_quarter")
    check("9:16 render", result, cam, names, 0.08)
    result, cam = frame(direction="front")  # No objects: everything visible.
    check("9:16, everything visible", result, cam, names, 0.08)

    # A mesh bigger than the sample: all still inside with the exact margin (the extremes are kept).
    fresh()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=1.5)
    bpy.context.active_object.name = "Dense"
    new_camera()
    kept = camera.SAMPLE, camera.MIN_SAMPLE
    camera.SAMPLE = camera.MIN_SAMPLE = 100
    try:
        result, cam = frame(objects=["Dense"], direction="three_quarter")
    finally:
        camera.SAMPLE, camera.MIN_SAMPLE = kept
    check("sampled mesh (100-point sample)", result, cam, ["Dense"], 0.08, centred=5e-3)

    # A subject beyond clip_end: the range follows it.
    fresh()
    cube("Hangar", scale=(80, 40, 30))
    cam = new_camera()
    cam.data.clip_end = 100.0
    result, cam = frame(objects=["Hangar"], direction="three_quarter")
    assert cam.data.clip_end > 100.0 and "clip_end 100 ->" in result.text, result.text
    check("clip range follows", result, cam, ["Hangar"], 0.08)

    # report_only on a clipped view: nothing changes, and the report says where and how much.
    fresh()
    cube("Left", location=(-2, 0, 0))
    cube("Right", location=(2, 0, 0))
    cam = new_camera(location=(1.5, -6, 0))
    cam.rotation_euler = (math.radians(90), 0, 0)
    bpy.context.view_layer.update()
    matrix, data = cam.matrix_world.copy(), scene_diff.rna_values(cam.data)
    snapshot = scene_diff.snapshot()
    result = tools.execute("frame_camera", json.dumps({"objects": ["Left", "Right"], "report_only": True}))
    assert result.ok and result.scene_before is None, result.text
    assert cam.matrix_world == matrix and scene_diff.rna_values(cam.data) == data, "report_only changed the camera"
    assert scene_diff.snapshot() == snapshot, "report_only changed the scene"
    assert "as it is" in result.text.splitlines()[0], result.text
    margins, inside, _ = measure(cam, ["Left"])
    bounds, reported, share = row_of(result.text, "Left")
    assert "clipped: left" in result.text and inside < 1.0, result.text
    assert all(abs(a - b) < 6e-4 for a, b in zip(reported, margins)), f"clipped margins {reported} vs {margins}"
    assert abs(share - inside) < 0.05, f"share {share} vs {inside}"
    print(f"CAMERA report_only, clipped: Left {share:.1%} inside (exact {inside:.1%}), margins L {reported[0]:.3f}")
    print(f"CAMERA report_only example:\n{result.text}")

    # One undo step: undo puts the camera back.
    before, name = cam.matrix_world.copy(), cam.name
    frame(objects=["Left", "Right"], direction="front")
    assert cam.matrix_world != before
    bpy.ops.ed.undo_push(message="After frame_camera")  # What the next user operation would push.
    bpy.ops.ed.undo()  # Reloads the scene: references from before are gone.
    back = bpy.data.objects[name].matrix_world
    assert all((a - b).length < 1e-6 for a, b in zip(back.col, before.col)), f"undo: {back} vs {before}"
    print("CAMERA undo: one step back restores the camera")

    close_shots()
    coverage_matches_report_only()
    report_at_a_frame()
    regions()

    # Errors the model can act on.
    fresh()
    cube("Left", location=(-2, 0, 0))
    new_camera()
    for arguments, words in (({"objects": ["Nope"]}, "No object named"), ({"objects": ["Left"], "margin": 0.6}, "margin"),
                             ({"objects": ["Left"], "fill": 0.5}, "fill must be"),
                             ({"objects": ["Left"], "fill": 2, "aim": "Nope"}, "No object named"),
                             ({"objects": ["Left"], "aim": "1,2"}, 'aim is an object name or "x,y,z"'),
                             ({"objects": ["Left"], "direction": "diagonal"}, "direction must be"),
                             ({"camera": "Left"}, "not a camera")):
        refused = tools.execute("frame_camera", json.dumps(arguments))
        assert not refused.ok and words in refused.text, (arguments, refused.text)
    fresh()
    cube("Alone")
    refused = tools.execute("frame_camera", json.dumps({"objects": ["Alone"]}))
    assert not refused.ok and "create=true" in refused.text, refused.text


def perfume() -> list:
    """A bottle with a cap on top and a label on its front: the close-shot subject."""
    fresh()
    cube("Bottle", location=(0, 0, 0.5), scale=(0.3, 0.3, 0.5))
    cube("Cap", location=(0, 0, 1.08), scale=(0.1, 0.1, 0.08))
    cube("Label", location=(0, -0.31, 0.45), scale=(0.2, 0.01, 0.15))
    return ["Bottle", "Cap", "Label"]


def spans(cam, names: list) -> tuple:
    projected = [world_to_camera_view(bpy.context.scene, cam, p) for p in vertices(names)]
    return max(p.x for p in projected) - min(p.x for p in projected), max(p.y for p in projected) - min(p.y for p in projected)


def centre_of(names: list) -> Vector:
    points = vertices(names)
    return Vector([(min(p[k] for p in points) + max(p[k] for p in points)) / 2 for k in range(3)])


def close_shots() -> None:
    """fill > 1 crops around aim: aim in the middle, the subject fill times its whole-fit size on the
    tighter side, and the report saying what is cut off."""
    scene = bpy.context.scene
    names = perfume()
    cam = new_camera(location=(0, -5, 0.8))
    result, cam = frame(objects=names, direction="front", fill=2, aim="Cap")
    at = world_to_camera_view(scene, cam, centre_of(["Cap"]))
    assert abs(at.x - 0.5) < EXACT and abs(at.y - 0.5) < EXACT, f"the cap is not in the middle: {at}"
    wide, tall = spans(cam, names)
    assert abs(tall - 2 * (1 - 2 * 0.08)) < 1e-3 and tall > wide, f"spans {wide:.4f} x {tall:.4f}"
    assert "with fill 2, centred on Cap (the subject spans 1.68 of the frame height, cropped)" in result.text, result.text
    assert "clipped: bottom" in result.text, result.text
    bounds, _, share = row_of(result.text, "All together")
    margins, inside, _ = measure(cam, names)
    assert share < 1.0 and abs(bounds[2] - margins[2]) < 6e-4, f"report {bounds} {share} vs {margins} {inside}"
    print(f"CAMERA close shot, fill 2 on Cap: spans {wide:.3f} x {tall:.3f}, {share:.0%} inside")
    print(f"CAMERA close shot example:\n{result.text}")

    # The label, three-quarter, a point for aim, 9:16: fill 3 is three times the whole fit.
    scene.render.resolution_x, scene.render.resolution_y = 1080, 1920
    label = centre_of(["Label"])
    result, cam = frame(objects=["Bottle", "Label"], direction="three_quarter", fill=3,
                        aim=f"{label.x:.4f},{label.y:.4f},{label.z:.4f}")
    at = world_to_camera_view(scene, cam, label)
    assert abs(at.x - 0.5) < 1e-3 and abs(at.y - 0.5) < 1e-3, f"the label point is not in the middle: {at}"
    wide, tall = spans(cam, ["Bottle", "Label"])
    assert abs(max(wide, tall) - 3 * (1 - 2 * 0.08)) < 2e-3, f"spans {wide:.4f} x {tall:.4f}"
    assert min(p.z for p in (world_to_camera_view(scene, cam, v) for v in vertices(["Bottle"]))) > cam.data.clip_start

    # aim with fill 1: everything inside, the aim in the middle, the tight margin the one asked.
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
    result, cam = frame(objects=names, direction="front", aim="Cap", margin=0.1)
    at = world_to_camera_view(scene, cam, centre_of(["Cap"]))
    margins, inside, _ = measure(cam, names)
    assert inside == 1.0 and abs(min(margins) - 0.1) < EXACT, f"{inside} {margins}"
    assert abs(at.x - 0.5) < EXACT and abs(at.y - 0.5) < EXACT, at
    assert "centred on Cap, fitted to the" in result.text, result.text

    # Orthographic: the whole fit's ortho_scale / fill.
    cam.data.type = "ORTHO"
    _, cam = frame(objects=names, direction="front")
    whole = cam.data.ortho_scale
    _, cam = frame(objects=names, direction="front", fill=2.5, aim="Cap")
    at = world_to_camera_view(scene, cam, centre_of(["Cap"]))
    assert abs(cam.data.ortho_scale - whole / 2.5) < 1e-6, (cam.data.ortho_scale, whole)
    assert abs(at.x - 0.5) < EXACT and abs(at.y - 0.5) < EXACT, at
    print(f"CAMERA close shot, orthographic fill 2.5: ortho_scale {whole:.4f} -> {cam.data.ortho_scale:.4f}")


def coverage_matches_report_only() -> None:
    """camera.coverage is report_only's measurement: the same rows, and exact on given points."""
    names = perfume()
    cube("Shelf", location=(2.5, 0.5, 0.2), scale=(1.5, 0.3, 0.2))
    cam = new_camera(location=(0.4, -4.5, 1.2))
    cam.rotation_euler = (math.radians(80), 0, 0)
    bpy.context.view_layer.update()
    names.append("Shelf")
    report = tools.execute("frame_camera", json.dumps({"objects": names, "report_only": True}))
    measured = camera.coverage(cam.name, names)
    assert measured["camera"] == cam.name and set(measured["rows"]) == set(names), measured
    for name in names:
        bounds, margins, share = row_of(report.text, name)
        row = measured["rows"][name]
        assert all(abs(a - b) < 6e-4 for a, b in zip(bounds, row["bounds"])), (name, bounds, row)
        assert all(abs(a - b) < 6e-4 for a, b in zip(margins, row["margins"])), (name, margins, row)
        assert abs(share - row["inside"]) < 1.5e-3, (name, share, row["inside"])
    shelf = measured["rows"]["Shelf"]
    assert "right" in shelf["clips"] and shelf["inside"] < 1.0, shelf
    together = row_of(report.text, "All together")
    assert all(abs(a - b) < 6e-4 for a, b in zip(together[0], measured["all"]["bounds"])), (together, measured["all"])

    # Planned boxes as corners (the orchestrator's dry-run): exact against world_to_camera_view.
    corners = [(x, y, z) for x in (-0.3, 0.3) for y in (-0.3, 0.3) for z in (0.0, 1.0)]
    planned = camera.coverage(cam.name, points={"planned Bottle": corners})
    seen = [world_to_camera_view(bpy.context.scene, cam, Vector(c)) for c in corners]
    expected = (min(p.x for p in seen), max(p.x for p in seen), min(p.y for p in seen), max(p.y for p in seen))
    row = planned["rows"]["planned Bottle"]
    assert list(planned["rows"]) == ["planned Bottle"] and all(abs(a - b) < 1e-6 for a, b in zip(row["bounds"], expected)), row
    both = camera.coverage("", ["Shelf"], {"box": corners})
    assert set(both["rows"]) == {"Shelf", "box"} and both["camera"] == cam.name, both["rows"].keys()
    for bad in (("Nope", None), (cam.name, ["Nope"])):
        try:
            camera.coverage(*bad)
        except tools.ToolError as ex:
            assert "No " in str(ex), ex
        else:
            raise AssertionError(f"coverage{bad} did not refuse")
    print(f"CAMERA coverage = report_only: {len(names)} rows; Shelf {shelf['inside']:.1%} inside, clipped {shelf['clips']}")


def regions() -> None:
    """region: the subject inside that part of the frame, the margins the result says bind exact,
    the lens shift left as it was."""
    for ortho, shift, direction in ((False, (0.0, 0.0), "front"), (False, (0.12, -0.05), "three_quarter"),
                                    (True, (0.0, 0.1), "front")):
        fresh()
        cube("Bottle", scale=(0.4, 0.4, 1.2))
        cube("Cap", location=(0, 0, 1.45), scale=(0.2, 0.2, 0.25))
        cam = new_camera(ortho=ortho)
        cam.data.shift_x, cam.data.shift_y = shift
        margin = 0.05
        result, cam = frame(objects=["Bottle", "Cap"], direction=direction, region="0.4,0,1,1", margin=margin)
        label = f"region right 60% {'ortho' if ortho else 'persp'} shift {shift}"
        assert all(abs(a - b) < 1e-6 for a, b in zip((cam.data.shift_x, cam.data.shift_y), shift)), f"{label}: shift kept"
        assert ("Lens shift" in result.text) == any(shift), result.text
        (left, right, bottom, top), inside, _ = measure(cam, ["Bottle", "Cap"])
        low, high = left, 1 - right  # Bounds in the frame.
        assert inside == 1.0 and low >= 0.4 + margin - EXACT and high <= 1 - margin + EXACT, f"{label}: {low}..{high}"
        assert bottom >= margin - EXACT and top >= margin - EXACT, label
        if "fitted to the width (left and right margins bind)" in result.text:
            assert abs(low - 0.45) < EXACT and abs(high - 0.95) < EXACT, f"{label}: {low}..{high}"
        else:
            assert "fitted to the height (bottom and top margins bind)" in result.text, result.text
            assert abs(bottom - margin) < EXACT and abs(top - margin) < EXACT, f"{label}: {bottom} {top}"
            assert abs((low + high) / 2 - 0.7) < CENTRED, f"{label}: centred in the region {low}..{high}"
        assert "in region x 0.4..1 y 0..1" in result.text, result.text
        print(f"CAMERA {label}: x {low:.4f}..{high:.4f}, margins B {bottom:.4f} T {top:.4f}")

    # A wide bench in the top-left 60 x 50%: the width binds, and the height is centred in the region.
    fresh()
    cube("Bench", scale=(3, 0.4, 0.3))
    cam = new_camera()
    result, cam = frame(objects=["Bench"], direction="front", region="0,0.5,0.6,1", margin=0.05)
    assert "fitted to the width (left and right margins bind)" in result.text, result.text
    (left, right, bottom, top), inside, _ = measure(cam, ["Bench"])
    assert inside == 1.0 and abs(left - 0.05) < EXACT and abs(1 - right - 0.55) < EXACT, (left, right)
    assert abs((bottom + 1 - top) / 2 - 0.75) < CENTRED and bottom >= 0.55 - EXACT, (bottom, top)
    print(f"CAMERA region top-left, wide: x {left:.4f}..{1 - right:.4f} y {bottom:.4f}..{1 - top:.4f}")

    # Aim at the bottle's foot with fill 1: one margin binds, and the result names that one.
    fresh()
    cube("Bottle", scale=(0.9, 0.4, 0.3))
    cam = new_camera()
    result, cam = frame(objects=["Bottle"], direction="front", aim="0,0,-0.3", margin=0.0)
    edge = re.search(r"\(the (left|right|bottom|top) margin binds\)", result.text)
    assert edge, result.text
    margins = dict(zip(("left", "right", "bottom", "top"), measure(cam, ["Bottle"])[0]))
    assert abs(margins[edge.group(1)]) < EXACT and min(margins.values()) > -EXACT, (edge.group(1), margins)
    assert edge.group(1) == "top", "the foot is aimed at: the top edge is furthest, the width spans more"
    print(f"CAMERA aim at the foot: the {edge.group(1)} margin binds, margins {margins}")


def report_at_a_frame() -> None:
    """report_only with frame: the numbers are those of that frame (the camera's Track To follows an
    empty animated from Left to Right), and the scene is back on its own frame, unchanged."""
    fresh()
    cube("Left", location=(-3, 0, 0))
    cube("Right", location=(3, 0, 0))
    aim = bpy.data.objects.new("Aim", None)
    bpy.context.scene.collection.objects.link(aim)
    cam = new_camera(location=(0, -6, 0))
    track = cam.constraints.new("TRACK_TO")
    track.target, track.track_axis, track.up_axis = aim, "TRACK_NEGATIVE_Z", "UP_Y"
    scene = bpy.context.scene
    for frame_number, x in ((1, -3.0), (50, 3.0)):
        aim.location = (x, 0, 0)
        aim.keyframe_insert("location", frame=frame_number)
    scene.frame_set(50)
    at_50, _, _ = measure(cam, ["Right"])
    scene.frame_set(1)
    at_1, _, _ = measure(cam, ["Right"])
    snapshot = scene_diff.snapshot()
    result = tools.execute("frame_camera", json.dumps({"objects": ["Right"], "report_only": True, "frame": 50}))
    assert result.ok and result.text.startswith("At frame 50 (the scene is back on frame 1)"), result.text
    assert scene.frame_current == 1 and scene_diff.snapshot() == snapshot, "the frame and the scene are as they were"
    _, reported, share = row_of(result.text, "Right")
    assert share == 1.0 and all(abs(a - b) < 6e-4 for a, b in zip(reported, at_50)), (reported, at_50)
    assert min(at_50) > 0 and min(at_1) < 0, f"in frame at 50 {at_50}, out of it at 1 {at_1}"
    now = tools.execute("frame_camera", json.dumps({"objects": ["Right"], "report_only": True}))
    assert row_of(now.text, "Right")[2] < 1.0, now.text
    refused = tools.execute("frame_camera", json.dumps({"objects": ["Right"], "frame": 50}))
    assert not refused.ok and "frames works with report_only" in refused.text, refused.text
    refused = tools.execute("frame_camera", json.dumps({"objects": ["Right"], "report_only": True, "frame": "late"}))
    assert not refused.ok and "whole frame number" in refused.text, refused.text
    print(f"CAMERA report_only at frame 50: Right margins {[round(m, 3) for m in reported]}, back on frame 1")

    # Several frames: a line each, the Track To evaluated at each, the scene back on frame 1.
    scene.frame_set(25)
    at_25, _, _ = measure(cam, ["Right", "Left"])
    scene.frame_set(1)
    result = tools.execute("frame_camera", json.dumps({"objects": ["Right", "Left"], "report_only": True,
                                                       "frames": [1, 25, 50]}))
    assert result.ok and result.text.startswith('Camera "Shot" as it is, at frames 1, 25, 50 (the scene is back on frame 1)'), result.text
    assert scene.frame_current == 1 and scene_diff.snapshot() == snapshot, "the frame and the scene are as they were"
    lines = {line.split(":")[0]: line for line in result.text.splitlines() if line.startswith("- frame ")}
    assert list(lines) == ["- frame 1", "- frame 25", "- frame 50"], result.text
    _, reported, _ = row_of(lines["- frame 25"].replace("- frame 25", "- All together"), "All together")
    assert all(abs(a - b) < 6e-4 for a, b in zip(reported, at_25)), (reported, at_25)
    assert "not all inside: Left" in lines["- frame 50"] and "not all inside: Right" in lines["- frame 1"], result.text
    assert "camera at" not in result.text, "Track To turns the camera; it does not move"
    print(f"CAMERA report_only at frames 1, 25, 50:\n{result.text}")


def main() -> int:
    try:
        cases()
    except Exception:
        traceback.print_exc()
        print("CAMERA FAIL")
        return 1
    print("CAMERA OK")
    return 0


code = main()
sys.stdout.flush()
if bpy.app.background:
    sys.exit(code)
