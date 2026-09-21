"""Project tasks: whole small jobs from the audiences the 2026 survey ranked highest, one per
audience, so a run says "can it do a product turntable" rather than "can it add a sphere".

    (perfume_ad and the other two-turn showcase tasks live in showcase.py; the helpers stay here)
    brand_motion        advertising / brand motion                      (priority 2)
    game_prop           game developers making their own assets         (priority 3)
    archviz_room        architecture / interior visualization           (priority 4)
    social_loop         social creators making short 3D loops           (priority 5)

Each task is a checklist of named requirements read from the finished scene, so two tools that
did the same job score the same however they got there, and a partial job scores partially.
The checks are objective ("turns a full 360", "the window is 1.2-2 m wide"); taste is judged
separately, from renders of the same frames, by judge.py and by people. Every task carries a
reference solution so selfcheck.py proves each requirement fails untouched and passes solved.

Runs inside Blender. Import `tasks`, not this module: tasks.py pulls these in at the bottom.
"""

import math
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

import bpy
from mathutils import Vector

import tasks as t

Requirement = tuple[str, Callable]


def checklist(requirements: list[Requirement]) -> Callable:
    """A Task.check that runs every requirement and reports each failure as `name: detail`."""
    def check(ctx) -> list[str]:
        problems = []
        for name, fn in requirements:
            try:
                detail = fn(ctx)
            except Exception as ex:  # A crashed requirement is a failed one, with the reason.
                detail = f"check crashed: {type(ex).__name__}: {ex}"
            if detail:
                problems.append(f"{name}: {detail}")
        return problems
    check.requirements = [name for name, _ in requirements]
    return check


# ------------------------------------------------------------------ reading animated scenes

@contextmanager
def at_frame(frame: int):
    scene = bpy.context.scene
    saved = scene.frame_current
    scene.frame_set(frame)
    try:
        yield
    finally:
        scene.frame_set(saved)


def frame_range() -> tuple[int, int]:
    scene = bpy.context.scene
    return scene.frame_start, scene.frame_end


def sample_frames(count: int) -> list[int]:
    start, end = frame_range()
    return [round(start + i * (end - start) / (count - 1)) for i in range(count)]


def camera_forward(camera) -> Vector:
    return (camera.matrix_world.to_3x3() @ Vector((0, 0, -1))).normalized()


def light_direction(light) -> Vector:
    return (light.matrix_world.to_3x3() @ Vector((0, 0, -1))).normalized()


def in_frame(points, margin: float = 0.0) -> tuple[int, float, float]:
    """How many points fall outside the camera frame, and the fraction of frame width and
    height the points span. Evaluated at the current frame."""
    from bpy_extras.object_utils import world_to_camera_view
    scene = bpy.context.scene
    views = [world_to_camera_view(scene, scene.camera, Vector(p)) for p in points]
    outside = sum(1 for v in views if not (margin <= v.x <= 1 - margin and margin <= v.y <= 1 - margin and v.z > 0))
    xs, ys = [v.x for v in views], [v.y for v in views]
    return outside, max(xs) - min(xs), max(ys) - min(ys)


def corners(obj) -> list[Vector]:
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return [evaluated.matrix_world @ Vector(c) for c in evaluated.bound_box]


def lights() -> list:
    return [o for o in bpy.context.scene.objects if o.type == "LIGHT" and not o.hide_render]


def timing(frames: int, fps: int) -> Callable:
    def requirement(ctx):
        scene = bpy.context.scene
        got = scene.frame_end - scene.frame_start + 1
        if got != frames:
            return f"{got} frames ({scene.frame_start}-{scene.frame_end}), asked for {frames}"
        if scene.render.fps != fps:
            return f"{scene.render.fps} fps, asked for {fps}"
    return requirement


def output(width: int, height: int) -> Callable:
    def requirement(ctx):
        r = bpy.context.scene.render
        if (r.resolution_x, r.resolution_y) != (width, height) or r.resolution_percentage != 100:
            return f"{r.resolution_x}x{r.resolution_y} at {r.resolution_percentage}%, asked for {width}x{height}"
        if r.image_settings.file_format != "PNG":
            return f"output format is {r.image_settings.file_format}, asked for PNG"
    return requirement


def has_camera(ctx):
    if bpy.context.scene.camera is None:
        return "the scene has no active camera"


def materials_used(objects) -> list:
    """Every material some face of these objects actually uses."""
    found = []
    for obj in objects:
        if obj.type != "MESH" or not obj.material_slots:
            continue
        used = set(p.material_index for p in obj.data.polygons) or {0}
        for index in used:
            material = obj.material_slots[min(index, len(obj.material_slots) - 1)].material
            if material is not None and material not in found:
                found.append(material)
    return found


def principled_of(material):
    if material is not None and material.node_tree:
        for node in material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return node
    return None


def color_of(material) -> tuple[float, float, float] | None:
    node = principled_of(material)
    if node is not None and not node.inputs["Base Color"].is_linked:
        return tuple(node.inputs["Base Color"].default_value[:3])
    if material is not None:
        return tuple(material.diffuse_color[:3])
    return None


def luminance(rgb) -> float:
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def saturation(rgb) -> float:
    return max(rgb) - min(rgb)


def world_color() -> tuple[float, float, float]:
    world = bpy.context.scene.world
    if world is None:
        return (0.05, 0.05, 0.05)
    if world.use_nodes and world.node_tree:
        for node in world.node_tree.nodes:
            if node.type == "BACKGROUND" and not node.inputs["Color"].is_linked:
                return tuple(node.inputs["Color"].default_value[:3])
    return tuple(world.color[:3])


def background_color(exclude, min_footprint: float) -> tuple[float, float, float]:
    """What is behind the subject: a backdrop mesh at least `min_footprint` m across, else the world."""
    biggest, size = None, 0.0
    for obj in t.meshes():
        if obj in exclude:
            continue
        low, high = t.bounds(obj)
        extent = max(high.x - low.x, high.y - low.y, high.z - low.z)
        if extent >= min_footprint and extent > size:
            biggest, size = obj, extent
    if biggest is not None:
        color = color_of(t.visible_material(biggest))
        if color is not None:
            return color
    return world_color()


def texture_driven(material) -> bool:
    """Base Color comes from a texture node (image, noise, wave, brick...)."""
    node = principled_of(material)
    if node is None or not node.inputs["Base Color"].is_linked:
        return False
    return any(n.type.startswith("TEX_") for n in material.node_tree.nodes)


# ================================================================== 1. perfume ad

PERFUME = Path(__file__).resolve().parents[1] / "fixtures" / "perfume_bottle.blend"
PERFUME_OBJECTS = ["verre", "liquide", "bouchon", "boite", "ouverture"]   # bottle, juice, cap, box, box lid
PERFUME_MATERIALS = {"verre", "liquide", "bouchon", "boite"}


def setup_perfume() -> None:
    """The Magie Noire bottle and its box (a finished asset with glass, liquid and label materials)
    standing on the ground at the origin, and nothing else but the factory camera and light."""
    t._remove("Cube")
    with bpy.data.libraries.load(str(PERFUME), link=False) as (source, target):
        target.objects = [name for name in source.objects if name in PERFUME_OBJECTS]
    for obj in target.objects:
        bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()


def base_name(name: str) -> str:
    return name.split(".")[0]


def product_parts() -> list:
    """The bottle and box however they are now named or joined."""
    named = [o for o in t.meshes() if base_name(o.name) in PERFUME_OBJECTS]
    if named:
        return named
    found = []
    for obj in t.meshes():
        low, high = t.bounds(obj)
        size = high - low
        if 0.2 <= size.z <= 0.5 and size.x <= 0.7 and size.y <= 0.7:
            found.append(obj)
    return found


def box_of(objects) -> tuple[Vector, Vector] | None:
    boxes = [t.bounds(o) for o in objects]
    if not boxes:
        return None
    return (Vector(min(b[0][i] for b in boxes) for i in range(3)),
            Vector(max(b[1][i] for b in boxes) for i in range(3)))


def start_box(ctx) -> tuple[Vector, Vector]:
    entries = [ctx.before[n] for n in PERFUME_OBJECTS if n in ctx.before]
    return (Vector(min(e["min"][i] for e in entries) for i in range(3)),
            Vector(max(e["max"][i] for e in entries) for i in range(3)))


def req_product_kept(ctx):
    box = box_of(product_parts())
    if box is None:
        return "the bottle and box are gone"
    was = start_box(ctx)
    size, was_size = box[1] - box[0], was[1] - was[0]
    with at_frame(frame_range()[0]):
        size = (lambda b: b[1] - b[0])(box_of(product_parts()))
    if any(abs(size[i] - was_size[i]) > 0.05 * was_size[i] + 0.01 for i in range(3)):
        return (f"the product is now {tuple(round(v, 2) for v in size)} m, it was "
                f"{tuple(round(v, 2) for v in was_size)}: it was rescaled or a part is missing")
    materials = {base_name(m.name) for m in materials_used(product_parts())}
    missing = PERFUME_MATERIALS - materials
    if missing:
        return f"the product's own materials were replaced or dropped: {sorted(missing)}"


def req_backdrop(ctx):
    parts = product_parts()
    box = box_of(parts)
    if box is None:
        return "no product to stand on anything"
    center = (box[0] + box[1]) / 2
    # Just beside the product, a little above its base, looking down: the surface it rests on. (A
    # ray from under the product would start below a floor it sits flush on.)
    origin = Vector((box[1].x + 0.02, center.y, box[0].z + 0.05))
    depsgraph = bpy.context.evaluated_depsgraph_get()
    hit, _, _, _, obj, _ = bpy.context.scene.ray_cast(depsgraph, origin, Vector((0, 0, -1)), distance=0.1)
    if not hit or obj is None or obj.original in parts:
        return "nothing under the product within 5 cm: it is not standing on a surface"
    low, high = t.bounds(obj.original)
    footprint = min(high.x - low.x, high.y - low.y)
    if footprint < 3 * max(box[1].x - box[0].x, box[1].y - box[0].y):
        return f"the surface under the product is only {footprint:.2f} m across, not a backdrop"


def req_moody(ctx):
    color = background_color(set(product_parts()), min_footprint=1.0)
    if luminance(color) > 0.35:
        return f"the background {tuple(round(c, 2) for c in color)} is bright, not a dark moody studio"


def req_three_lights(ctx):
    count = len(lights())
    if count < 3:
        return f"{count} light(s); three-point lighting needs three"


def req_rim_light(ctx):
    camera = bpy.context.scene.camera
    box = box_of(product_parts())
    if camera is None or box is None:
        return "no camera or no product"
    center = (box[0] + box[1]) / 2
    with at_frame(frame_range()[0]):
        forward = camera_forward(camera).xy
        forward.normalize()
        behind = [o.name for o in lights()
                  if (o.matrix_world.translation.xy - center.xy).length > 0.1
                  and (o.matrix_world.translation.xy - center.xy).normalized().dot(forward) > 0.3]
    if not behind:
        return "no light behind the product as seen from the camera: nothing rims the glass"


BOTTLE_OBJECTS = ["verre", "liquide", "bouchon"]


def req_bottle_visible(ctx):
    """On the first frame, the hero frame, the bottle must not be hidden behind the box."""
    camera = bpy.context.scene.camera
    if camera is None:
        return "no camera"
    parts = product_parts()
    bottle = [o for o in parts if base_name(o.name) in BOTTLE_OBJECTS] or parts
    if not bottle:
        return "no product"
    with at_frame(frame_range()[0]):
        depsgraph = bpy.context.evaluated_depsgraph_get()
        eye = camera.matrix_world.translation
        targets = [c for o in bottle for c in corners(o)]
        box = box_of(bottle)
        targets.append((box[0] + box[1]) / 2)
        seen = 0
        for target in targets:
            direction = target - eye
            hit, _, _, _, obj, _ = bpy.context.scene.ray_cast(depsgraph, eye, direction.normalized(), distance=direction.length + 0.01)
            if hit and obj is not None and obj.original in bottle:
                seen += 1
    if seen < 3:
        return f"on the first frame the bottle is hidden (only {seen} of {len(targets)} sample points visible from the camera)"


def req_full_turn(ctx):
    camera = bpy.context.scene.camera
    if camera is None:
        return "no camera"
    parts = product_parts()
    if not parts:
        return "no product"
    subject = max(parts, key=lambda o: (lambda b: (b[1] - b[0]).length)(t.bounds(o)))
    angles = []
    for frame in sample_frames(9):
        with at_frame(frame):
            local = subject.matrix_world.inverted() @ camera.matrix_world.translation
            angles.append(math.degrees(math.atan2(local.y, local.x)))
    deltas = [((b - a + 180) % 360) - 180 for a, b in zip(angles, angles[1:])]
    if all(abs(d) < 1.0 for d in deltas):
        return "the view of the product never changes: nothing turns, or the camera turns with it"
    if any(d > 0 for d in deltas) and any(d < 0 for d in deltas):
        return f"the turn changes direction: steps of {[round(d) for d in deltas]} degrees"
    if any(abs(d) > 75 for d in deltas):
        return f"the turn is uneven: steps of {[round(d) for d in deltas]} degrees over 8 equal intervals"
    total = abs(sum(deltas))
    if not 340 <= total <= 380:
        return f"turns {total:.0f} degrees over the frame range, asked for 360"


def req_product_framed(ctx):
    camera = bpy.context.scene.camera
    if camera is None:
        return "no camera"
    for frame in sample_frames(3):
        with at_frame(frame):
            points = [c for o in product_parts() for c in corners(o)]
            if not points:
                return "no product"
            outside, width, height = in_frame(points)
            if outside:
                return f"at frame {frame}, {outside} of the product's corners are out of frame"
            if max(width, height) < 0.3:
                return f"at frame {frame} the product fills only {max(width, height):.0%} of the frame"


PERFUME_AD_SOLUTION = """
import bpy, math
scene = bpy.context.scene
bottle, box = bpy.data.objects['verre'], bpy.data.objects['boite']
def material(name, rgb, **inputs):
    mat = bpy.data.materials.new(name); mat.use_nodes = True
    node = mat.node_tree.nodes['Principled BSDF']
    node.inputs['Base Color'].default_value = (*rgb, 1)
    for key, value in inputs.items():
        node.inputs[key].default_value = value
    return mat
bpy.ops.mesh.primitive_plane_add(size=6, location=(0, 0, 0)); floor = bpy.context.object
floor.name = 'Floor'; floor.data.materials.append(material('DarkGloss', (0.02, 0.02, 0.025), Roughness=0.15))
world = scene.world; world.use_nodes = True
world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.01, 0.01, 0.012, 1)
turntable = bpy.data.objects.new('Turntable', None); scene.collection.objects.link(turntable)
for obj in (bottle, box):
    matrix = obj.matrix_world.copy(); obj.parent = turntable; obj.matrix_world = matrix
scene.frame_start, scene.frame_end, scene.render.fps = 1, 120, 30
bpy.context.preferences.edit.keyframe_new_interpolation_type = 'LINEAR'
turntable.rotation_euler = (0, 0, 0); turntable.keyframe_insert('rotation_euler', frame=1)
turntable.rotation_euler = (0, 0, math.tau); turntable.keyframe_insert('rotation_euler', frame=121)
for name, location, energy in (('Key', (0.9, -0.9, 0.9), 150), ('Fill', (-1.1, -0.7, 0.5), 40), ('Rim', (0.2, 1.0, 0.7), 200)):
    light = bpy.data.objects.new(name, bpy.data.lights.new(name, 'AREA'))
    light.data.energy, light.data.size = energy, 0.6
    light.location = location
    scene.collection.objects.link(light)
    track = light.constraints.new('TRACK_TO'); track.target = bottle
camera = scene.camera
camera.location = (0.0, -1.7, 0.32); camera.rotation_euler = (math.radians(84), 0, 0)
camera.data.lens = 85
r = scene.render
r.resolution_x = r.resolution_y = 1080; r.resolution_percentage = 100; r.image_settings.file_format = 'PNG'
"""

# ================================================================== 2. brand motion

def text_objects() -> list:
    return sorted((o for o in bpy.context.scene.objects if o.type == "FONT"), key=lambda o: o.matrix_world.translation.x)


def logo_text() -> str:
    return "".join(o.data.body.strip() for o in text_objects())


def req_logo_text(ctx):
    if not text_objects():
        return "no 3D text object in the scene"
    if logo_text() != "NOVA":
        return f"the text says {logo_text()!r}, asked for NOVA"


def req_logo_extruded(ctx):
    flat = [o.name for o in text_objects() if o.data.extrude <= 0]
    if flat:
        return f"text is flat (extrude 0): {flat}"


def pose(objects) -> list[float]:
    values = []
    for obj in objects:
        values += [v for row in obj.matrix_world for v in row]
        for material in materials_used(objects) if obj.type == "MESH" else []:
            node = principled_of(material)
            if node is not None:
                values.append(node.inputs["Alpha"].default_value)
    for obj in objects:
        if obj.type == "FONT":
            for slot in obj.material_slots:
                node = principled_of(slot.material)
                if node is not None:
                    values.append(node.inputs["Alpha"].default_value)
    return values


def req_logo_animates(ctx):
    texts = text_objects()
    if not texts:
        return "no text"
    start, end = frame_range()
    with at_frame(start):
        first = pose(texts)
    with at_frame(end):
        last = pose(texts)
    if t.close(first, last, 1e-3):
        return "the text looks the same at the first and last frame: nothing animates in"


def req_logo_settles(ctx):
    texts = text_objects()
    if not texts:
        return "no text"
    start, end = frame_range()
    with at_frame(end - 6):
        before = pose(texts)
    with at_frame(end):
        last = pose(texts)
    if not t.close(before, last, 1e-3):
        return "the logo is still moving in the last quarter second; it should have settled"


def req_logo_framed(ctx):
    if bpy.context.scene.camera is None:
        return "no camera"
    texts = text_objects()
    if not texts:
        return "no text"
    _, end = frame_range()
    with at_frame(end):
        outside, width, _ = in_frame([c for o in texts for c in corners(o)])
    if outside:
        return f"{outside} corner(s) of the finished logo are out of frame"
    if width < 0.25:
        return f"the finished logo spans only {width:.0%} of the frame width"


def req_logo_colors(ctx):
    texts = text_objects()
    if not texts:
        return "no text"
    material = next((s.material for o in texts for s in o.material_slots if s.material), None)
    if material is None:
        return "the text has no material"
    color = color_of(material)
    if saturation(color) < 0.3:
        return f"the text color {tuple(round(c, 2) for c in color)} is not a bold brand color"
    background = background_color(set(texts), min_footprint=3.0)
    if abs(luminance(color) - luminance(background)) < 0.25:
        return (f"text {tuple(round(c, 2) for c in color)} on background "
                f"{tuple(round(c, 2) for c in background)} does not contrast")


BRAND_MOTION_SOLUTION = """
import bpy, math
scene = bpy.context.scene
curve = bpy.data.curves.new('Logo', 'FONT'); curve.body = 'NOVA'; curve.extrude = 0.08; curve.align_x = 'CENTER'
curve.size = 1.2
logo = bpy.data.objects.new('Logo', curve); scene.collection.objects.link(logo)
logo.rotation_euler = (math.radians(90), 0, 0)
mat = bpy.data.materials.new('Brand'); mat.use_nodes = True
mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.05, 0.3, 0.95, 1)
curve.materials.append(mat)
world = scene.world; world.use_nodes = True
world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.97, 0.96, 0.9, 1)
scene.frame_start, scene.frame_end, scene.render.fps = 1, 72, 24
logo.location = (0, 0, -1.5); logo.scale = (0.6, 0.6, 0.6)
logo.keyframe_insert('location', frame=1); logo.keyframe_insert('scale', frame=1)
logo.location = (0, 0, -0.4); logo.scale = (1, 1, 1)
logo.keyframe_insert('location', frame=48); logo.keyframe_insert('scale', frame=48)
camera = scene.camera
camera.location = (0, -6, 0); camera.rotation_euler = (math.radians(90), 0, 0)
r = scene.render
r.resolution_x, r.resolution_y, r.resolution_percentage = 1920, 1080, 100; r.image_settings.file_format = 'PNG'
"""

# ================================================================== 3. game prop

def crate():
    named = [o for o in t.meshes() if o.name.lower().startswith("crate")]
    if named:
        return named[0]
    others = t.meshes()
    return others[0] if len(others) == 1 else None


def req_crate_exists(ctx):
    if crate() is None:
        return "no mesh named Crate (and more than one mesh to choose from)"


def req_crate_size(ctx):
    obj = crate()
    if obj is None:
        return "no crate"
    low, high = t.bounds(obj)
    size = high - low
    if not all(0.9 <= s <= 1.1 for s in size):
        return f"the crate is {tuple(round(s, 2) for s in size)} m, asked for about 1 m"


def req_crate_budget(ctx):
    obj = crate()
    if obj is None:
        return "no crate"
    mesh = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
    try:
        triangles = sum(len(p.vertices) - 2 for p in mesh.polygons)
    finally:
        obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
    if triangles > 600:
        return f"{triangles} triangles, the budget is 600"
    if triangles < 60:
        return f"only {triangles} triangles: no chamfer or plank detail is modelled in"


def req_crate_origin(ctx):
    obj = crate()
    if obj is None:
        return "no crate"
    low, high = t.bounds(obj)
    center = (low + high) / 2
    origin = obj.matrix_world.translation
    if abs(origin.x - center.x) > t.EPS or abs(origin.y - center.y) > t.EPS or abs(origin.z - low.z) > t.EPS:
        return (f"origin is at {tuple(round(v, 2) for v in origin)}, the bottom centre is "
                f"({center.x:.2f}, {center.y:.2f}, {low.z:.2f})")
    if abs(low.z) > t.EPS or origin.xy.length > t.EPS:
        return "the crate does not sit on the ground at the world origin"


def req_crate_transforms(ctx):
    obj = crate()
    if obj is None:
        return "no crate"
    if not t.close(obj.scale, (1, 1, 1), 1e-3) or not t.close(obj.rotation_euler, (0, 0, 0), 1e-3):
        return f"scale {tuple(round(s, 2) for s in obj.scale)} / rotation not applied"


def req_crate_uvs(ctx):
    obj = crate()
    if obj is None:
        return "no crate"
    layer = obj.data.uv_layers.active
    if layer is None or not obj.data.loops:
        return "no UV map"
    uvs = [tuple(item.uv) for item in layer.data]
    xs, ys = [u for u, _ in uvs], [v for _, v in uvs]
    if (max(xs) - min(xs)) * (max(ys) - min(ys)) < 0.05:
        return "the UV map is collapsed (every face on the same spot)"
    if min(xs) < -0.01 or min(ys) < -0.01 or max(xs) > 1.01 or max(ys) > 1.01:
        return "UVs fall outside the 0-1 tile"


def req_crate_material(ctx):
    obj = crate()
    if obj is None:
        return "no crate"
    slots = [s for s in obj.material_slots if s.material]
    if len(slots) != 1:
        return f"{len(slots)} materials, asked for one"


def req_crate_alone(ctx):
    others = [o.name for o in t.meshes() if o is not crate()]
    if others:
        return f"other meshes in the scene: {others}"


GAME_PROP_SOLUTION = """
import bpy, bmesh
mesh = bpy.data.meshes.new('Crate')
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bmesh.ops.bevel(bm, geom=bm.verts[:] + bm.edges[:], offset=0.03, segments=1, affect='EDGES')
big = [f for f in bm.faces if f.calc_area() > 0.5]
bmesh.ops.inset_individual(bm, faces=big, thickness=0.08, depth=-0.02)
bmesh.ops.translate(bm, verts=bm.verts[:], vec=(0, 0, 0.5))
bm.to_mesh(mesh); bm.free()
crate = bpy.data.objects.new('Crate', mesh)
bpy.context.scene.collection.objects.link(crate)
mat = bpy.data.materials.new('CrateWood'); mat.use_nodes = True
mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.45, 0.28, 0.12, 1)
mesh.materials.append(mat)
bpy.ops.object.select_all(action='DESELECT')
crate.select_set(True); bpy.context.view_layer.objects.active = crate
bpy.ops.object.mode_set(mode='EDIT'); bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.uv.smart_project(island_margin=0.02)
bpy.ops.object.mode_set(mode='OBJECT')
"""

# ================================================================== 4. archviz room

ROOM = (4.0, 5.0)
ROOM_HEIGHT = 2.7


def floor():
    for obj in t.meshes():
        low, high = t.bounds(obj)
        size = high - low
        footprint = sorted((size.x, size.y))
        if abs(footprint[0] - ROOM[0]) <= 0.15 and abs(footprint[1] - ROOM[1]) <= 0.15 \
                and size.z <= 0.3 and abs(high.z) <= 0.1:
            return obj
    return None


def room_box() -> tuple[Vector, Vector] | None:
    slab = floor()
    if slab is None:
        return None
    low, high = t.bounds(slab)
    return Vector((low.x, low.y, high.z)), Vector((high.x, high.y, high.z + ROOM_HEIGHT))


WALL_DIRECTIONS = {"+X": Vector((1, 0, 0)), "-X": Vector((-1, 0, 0)), "+Y": Vector((0, 1, 0)), "-Y": Vector((0, -1, 0))}


def cast(origin, direction, distance):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    hit, location, _, _, obj, _ = bpy.context.scene.ray_cast(depsgraph, origin, direction, distance=distance)
    return (obj.original if hit and obj else None), (location - origin).length if hit else None


def walls_hit() -> dict:
    """For each side, the object a ray from the room's centre hits and how far away. Cast low,
    under any window, and again high when the low ray finds nothing (a doorway)."""
    box = room_box()
    if box is None:
        return {}
    center = (box[0] + box[1]) / 2
    half = {"+X": (box[1].x - box[0].x) / 2, "-X": (box[1].x - box[0].x) / 2,
            "+Y": (box[1].y - box[0].y) / 2, "-Y": (box[1].y - box[0].y) / 2}
    found = {}
    for side, direction in WALL_DIRECTIONS.items():
        for height in (0.5, 2.5):
            origin = Vector((center.x, center.y, box[0].z + height))
            obj, distance = cast(origin, direction, half[side] + 0.6)
            if obj is not None:
                break
        found[side] = (obj, distance, half[side])
    return found


def req_room_floor(ctx):
    if floor() is None:
        return f"no floor of about {ROOM[0]} x {ROOM[1]} m at z=0"


def req_room_walls(ctx):
    hits = walls_hit()
    if not hits:
        return "no floor to build on"
    missing = [side for side, (obj, distance, half) in hits.items()
               if obj is None or abs(distance - half) > 0.35]
    if missing:
        return f"no wall at the floor's edge on the {', '.join(missing)} side(s)"
    box = room_box()
    center = (box[0] + box[1]) / 2
    obj, distance = cast(center, Vector((0, 0, 1)), ROOM_HEIGHT + 0.6)
    if obj is None or abs(distance - (ROOM_HEIGHT - (center.z - box[0].z))) > 0.2:
        return f"no ceiling at {ROOM_HEIGHT} m"


def window() -> tuple[str, float, float] | None:
    """The side with a hole in its wall, and the hole's width and height, from a grid of rays
    shot at each wall from just inside the room."""
    box = room_box()
    if box is None:
        return None
    center = (box[0] + box[1]) / 2
    for side, normal in WALL_DIRECTIONS.items():
        tangent = Vector((-normal.y, normal.x, 0))
        half_out = (box[1] - box[0]).x / 2 if normal.x else (box[1] - box[0]).y / 2
        half_along = (box[1] - box[0]).y / 2 if normal.x else (box[1] - box[0]).x / 2
        misses = []
        steps_u = int((2 * half_along - 0.2) / 0.1)
        for i in range(steps_u + 1):
            u = -half_along + 0.1 + i * 0.1
            for j in range(int((ROOM_HEIGHT - 0.3) / 0.1)):
                z = box[0].z + 0.2 + j * 0.1
                origin = Vector((center.x, center.y, z)) + normal * (half_out - 0.5) + tangent * u
                obj, _ = cast(origin, normal, 1.0)
                if obj is None:
                    misses.append((u, z))
        if len(misses) >= 20:
            us, zs = [u for u, _ in misses], [z for _, z in misses]
            return side, max(us) - min(us) + 0.1, max(zs) - min(zs) + 0.1
    return None


def req_room_window(ctx):
    if room_box() is None:
        return "no room"
    found = window()
    if found is None:
        return "no opening in any wall"
    side, width, height = found
    if not 1.2 <= width <= 2.0 or not 0.9 <= height <= 1.6:
        return f"the opening on the {side} wall is {width:.1f} x {height:.1f} m, asked for about 1.5 x 1.2"


def req_room_sun(ctx):
    suns = [o for o in lights() if o.data.type == "SUN"]
    if not suns:
        return "no sun light"
    found = window()
    if found is None:
        return "no window for the sun to shine through"
    inward = -WALL_DIRECTIONS[found[0]]
    for sun in suns:
        direction = light_direction(sun)
        if direction.z < -0.15 and direction.dot(inward) > 0.3:
            return None
    return f"the sun does not shine in through the {found[0]} window (it points elsewhere)"


def req_room_camera(ctx):
    camera = bpy.context.scene.camera
    if camera is None:
        return "no camera"
    box = room_box()
    if box is None:
        return "no room"
    p = camera.matrix_world.translation
    if not (box[0].x + 0.1 <= p.x <= box[1].x - 0.1 and box[0].y + 0.1 <= p.y <= box[1].y - 0.1
            and box[0].z + 0.2 <= p.z <= box[1].z - 0.2):
        return f"the camera at {tuple(round(v, 1) for v in p)} is not inside the room"
    found = window()
    if found is None:
        return "no window to look at"
    if camera_forward(camera).dot(WALL_DIRECTIONS[found[0]]) < 0.5:
        return f"the camera does not look towards the {found[0]} window wall"


def req_room_materials(ctx):
    slab = floor()
    if slab is None:
        return "no floor"
    problems = []
    material = t.visible_material(slab)
    color = color_of(material)
    if material is None:
        problems.append("the floor has no material")
    elif not texture_driven(material) and not (color and color[0] > color[1] > color[2] and color[0] > 0.15):
        problems.append(f"the floor color {tuple(round(c, 2) for c in color)} is not wood-like and has no texture")
    walls = [obj for obj, _, _ in walls_hit().values() if obj is not None and obj is not slab]
    if not walls:
        problems.append("no walls to check")
    for wall in walls:
        color = color_of(t.visible_material(wall))
        if color is None or min(color) < 0.45 or saturation(color) > 0.2:
            problems.append(f"{wall.name} is not a light neutral color")
            break
    return "; ".join(problems)


def req_room_empty(ctx):
    box = room_box()
    if box is None:
        return "no room"
    inside = []
    for obj in t.meshes():
        low, high = t.bounds(obj)
        if (low.x > box[0].x + 0.15 and high.x < box[1].x - 0.15 and low.y > box[0].y + 0.15
                and high.y < box[1].y - 0.15 and low.z > box[0].z + 0.02 and high.z < box[1].z - 0.02):
            inside.append(obj.name)
    if inside:
        return f"meshes inside the room: {inside}"


ARCHVIZ_ROOM_SOLUTION = """
import bpy, bmesh, math
scene = bpy.context.scene
bpy.data.objects.remove(bpy.data.objects['Cube'])
W, D, H = 4.0, 5.0, 2.7
def material(name, rgb):
    mat = bpy.data.materials.new(name); mat.use_nodes = True
    mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (*rgb, 1)
    return mat
wood, plaster = material('Wood', (0.42, 0.25, 0.12)), material('Plaster', (0.85, 0.84, 0.8))
def sheet(name, verts, faces, mat):
    mesh = bpy.data.meshes.new(name); mesh.from_pydata(verts, [], faces); mesh.materials.append(mat)
    obj = bpy.data.objects.new(name, mesh); scene.collection.objects.link(obj); return obj
x, y = W / 2, D / 2
sheet('Floor', [(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0)], [(0, 1, 2, 3)], wood)
sheet('Ceiling', [(-x, -y, H), (x, -y, H), (x, y, H), (-x, y, H)], [(0, 3, 2, 1)], plaster)
sheet('WallWest', [(-x, -y, 0), (-x, y, 0), (-x, y, H), (-x, -y, H)], [(0, 1, 2, 3)], plaster)
sheet('WallEast', [(x, -y, 0), (x, y, 0), (x, y, H), (x, -y, H)], [(0, 3, 2, 1)], plaster)
sheet('WallSouth', [(-x, -y, 0), (x, -y, 0), (x, -y, H), (-x, -y, H)], [(0, 1, 2, 3)], plaster)
# North wall with a 1.5 x 1.2 m window from z=0.9 to 2.1: four quads around the hole.
wx, z0, z1 = 0.75, 0.9, 2.1
v = [(-x, y, 0), (-wx, y, 0), (wx, y, 0), (x, y, 0), (-x, y, z0), (-wx, y, z0), (wx, y, z0), (x, y, z0),
     (-x, y, z1), (-wx, y, z1), (wx, y, z1), (x, y, z1), (-x, y, H), (-wx, y, H), (wx, y, H), (x, y, H)]
f = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (4, 5, 9, 8), (6, 7, 11, 10), (8, 9, 13, 12), (9, 10, 14, 13), (10, 11, 15, 14)]
sheet('WallNorth', v, f, plaster)
sun = bpy.data.objects.new('Sun', bpy.data.lights.new('Sun', 'SUN')); sun.data.energy = 4
sun.location = (0, 6, 4); sun.rotation_euler = (math.radians(-55), 0, 0); scene.collection.objects.link(sun)
camera = scene.camera
camera.location = (0.3, -1.8, 1.4); camera.rotation_euler = (math.radians(88), 0, 0)
camera.data.lens = 24
"""

# ================================================================== 5. social loop

def ring_instances(min_count: int = 8) -> list[tuple]:
    """(object, world position, materials) for every round mesh instance, sorted so a ring can
    be picked out: the ones at about the same distance from their common centre."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    found = []
    for instance in depsgraph.object_instances:
        obj = instance.object
        if obj.type != "MESH" or len(obj.data.vertices) < 40:
            continue
        found.append((obj.original, instance.matrix_world.translation.copy(),
                      [s.material for s in obj.material_slots if s.material]))
    if len(found) < min_count:
        return found
    center = sum((p for _, p, _ in found), Vector()) / len(found)
    distances = sorted((p - center).xy.length for _, p, _ in found)
    median = distances[len(distances) // 2]
    return [item for item in found if abs((item[1] - center).xy.length - median) <= 0.2 * max(median, 0.1)]


def req_ring(ctx):
    ring = ring_instances()
    if len(ring) < 8:
        return f"{len(ring)} round meshes arranged in a ring, asked for 8"


def req_candy_colors(ctx):
    colors = set()
    for _, _, materials in ring_instances():
        for material in materials:
            color = color_of(material)
            if color and saturation(color) > 0.3:
                colors.add(tuple(round(c, 1) for c in color))
    if len(colors) < 4:
        return f"{len(colors)} distinct bright colors on the spheres, expected several"


def req_wave(ctx):
    heights = {}
    for frame in sample_frames(4)[:-1] + [frame_range()[0] + 9]:
        with at_frame(frame):
            heights[frame] = [p.z for _, p, _ in ring_instances()]
    if any(len(h) < 8 for h in heights.values()):
        return "the ring falls apart during the animation"
    first = next(iter(heights.values()))
    if max(first) - min(first) < 0.03:
        return "all spheres are at the same height: no wave"
    per_sphere = list(zip(*heights.values()))
    still = sum(1 for zs in per_sphere if max(zs) - min(zs) < 0.03)
    if still:
        return f"{still} sphere(s) never move up or down"


def req_seamless(ctx):
    start, end = frame_range()
    with at_frame(start):
        first = [v for _, p, _ in ring_instances() for v in p]
    with at_frame(end + 1):
        after = [v for _, p, _ in ring_instances() for v in p]
    with at_frame(end):
        last = [v for _, p, _ in ring_instances() for v in p]
    if len(first) != len(after) or not (t.close(first, after, 1e-3) or t.close(first, last, 1e-3)):
        return "the frame after the last one does not match the first: the loop will jump"


def req_pastel_background(ctx):
    ring = ring_instances()
    color = background_color({obj for obj, _, _ in ring}, min_footprint=4.0)
    if min(color) < 0.45 or saturation(color) > 0.35:
        return f"background {tuple(round(c, 2) for c in color)} is not a soft pastel"


def req_ring_camera(ctx):
    camera = bpy.context.scene.camera
    if camera is None:
        return "no camera"
    forward = camera_forward(camera)
    if not -0.95 <= forward.z <= -0.3:
        return "the camera does not look down at the ring from an angle"
    with at_frame(frame_range()[0]):
        outside, _, _ = in_frame([p for _, p, _ in ring_instances()], margin=0.05)
    if outside:
        return f"{outside} sphere(s) out of frame"


SOCIAL_LOOP_SOLUTION = """
import bpy, math
scene = bpy.context.scene
bpy.data.objects.remove(bpy.data.objects['Cube'])
scene.frame_start, scene.frame_end, scene.render.fps = 1, 72, 24
colors = [(1, 0.2, 0.4), (1, 0.6, 0.1), (1, 0.9, 0.1), (0.3, 0.9, 0.3), (0.1, 0.7, 1), (0.4, 0.3, 1), (0.9, 0.3, 0.9), (0.1, 0.9, 0.8)]
for i, rgb in enumerate(colors):
    angle = i / 8 * math.tau
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.3, location=(1.5 * math.cos(angle), 1.5 * math.sin(angle), 0))
    ball = bpy.context.object
    mat = bpy.data.materials.new(f'Candy{i}'); mat.use_nodes = True
    mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (*rgb, 1)
    ball.data.materials.append(mat)
    for k in range(5):
        frame = 1 + k * 18
        ball.location.z = 0.3 * math.sin(math.tau * (k / 4 + i / 8))
        ball.keyframe_insert('location', index=2, frame=frame)
world = scene.world; world.use_nodes = True
world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.95, 0.85, 0.9, 1)
camera = scene.camera
camera.location = (0, -5, 4.5); camera.rotation_euler = (math.radians(48), 0, 0)
r = scene.render
r.resolution_x = r.resolution_y = 1080; r.resolution_percentage = 100; r.image_settings.file_format = 'PNG'
"""

# ================================================================== the tasks

PROJECT_TASKS = [
    t.Task("brand_motion",
           "Make a 3-second logo reveal for the brand NOVA: extruded 3D text in a bold brand color, the "
           "letters animate in (rise, scale or fade) and settle by the end, on a solid contrasting "
           "background, with a camera framing the finished logo. 72 frames at 24 fps, 1920x1080 PNG.",
           checklist([("text", req_logo_text), ("extruded", req_logo_extruded), ("animates", req_logo_animates),
                      ("settles", req_logo_settles), ("camera", has_camera), ("framed", req_logo_framed),
                      ("colors", req_logo_colors), ("timing", timing(72, 24)), ("output", output(1920, 1080))]),
           BRAND_MOTION_SOLUTION, setup=t.setup_empty, tags=["project", "advertising", "animation", "text"],
           render_frames=[1, 18, 36, 54, 72]),

    t.Task("game_prop",
           "Model a game-ready wooden crate: a 1 m cube shape with chamfered edges and plank or frame detail "
           "modelled into the mesh, under 600 triangles, one material, UV unwrapped, sitting on the ground at "
           "the origin with its origin at the bottom centre, rotation and scale applied, named Crate. No other "
           "meshes in the scene.",
           checklist([("crate", req_crate_exists), ("size", req_crate_size), ("triangle budget", req_crate_budget),
                      ("origin", req_crate_origin), ("transforms applied", req_crate_transforms),
                      ("uvs", req_crate_uvs), ("one material", req_crate_material), ("nothing else", req_crate_alone)]),
           GAME_PROP_SOLUTION, setup=t.setup_empty, tags=["project", "game", "modeling"], render_frames=[1]),

    t.Task("archviz_room",
           "Build a simple empty interior: a 4 m by 5 m room with a 2.7 m ceiling, floor, ceiling and four "
           "walls, one window opening about 1.5 m wide and 1.2 m tall in one wall, a sun light shining in "
           "through the window, a wood-look floor material, light neutral walls, and a camera inside the room "
           "looking towards the window wall. Remove the default cube.",
           checklist([("floor", req_room_floor), ("walls and ceiling", req_room_walls), ("window", req_room_window),
                      ("sun through window", req_room_sun), ("camera inside", req_room_camera),
                      ("materials", req_room_materials), ("empty interior", req_room_empty)]),
           ARCHVIZ_ROOM_SOLUTION, tags=["project", "archviz", "modeling", "lighting"], render_frames=[1]),

    t.Task("social_loop",
           "Make a seamless looping animation for social media: a ring of 8 spheres in bright candy colors "
           "bobbing up and down in a wave, on a soft pastel background, with the camera looking down at the "
           "ring from a three-quarter angle. 72 frames at 24 fps, 1080x1080 PNG. Frame 72 must flow straight "
           "back into frame 1. Remove the default cube.",
           checklist([("ring of 8", req_ring), ("candy colors", req_candy_colors), ("wave", req_wave),
                      ("seamless", req_seamless), ("pastel background", req_pastel_background),
                      ("camera", req_ring_camera), ("timing", timing(72, 24)), ("output", output(1080, 1080))]),
           SOCIAL_LOOP_SOLUTION, tags=["project", "social", "animation"], render_frames=[1, 18, 36, 54, 72]),
]
