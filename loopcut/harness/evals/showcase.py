"""Showcase tasks: demo-grade briefs with a follow-up edit, one per audience that matters most.

    perfume_ad       product photograph: stone, warm spot, amber gradient, three cameras; then pastel pink
    product_reveal   eight-second reveal of a vintage camera; then energetic and electric blue
    asset_pack       low-poly fantasy shop props, named, origins at base, GLB; then an ice variant
    dungeon_modules  modular dungeon kit on a grid, one room, walkthrough, glTF; then a second room
    exploded_view    scroll-site animation: assemble, rotate, explode, reassemble; then mirrored

Each is two turns. The first checklist reads the scene saved after the first brief, the second
reads the final scene against a snapshot of the first (ctx.stage1) and whatever `remember`
measured on it (ctx.memory), so "keep the cameras" and "faster than before" are real checks.
Every brief asks for a final-quality Cycles setup and is graded on it; grade.py then renders
with the engine the scene was left on, so the choice shows in the pictures too.

Runs inside Blender. Import `tasks`, not this module.
"""

import math
import os
import tempfile
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

import projects as pj
import tasks as t

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ------------------------------------------------------------------ shared requirements

def req_cycles(ctx):
    scene = bpy.context.scene
    if scene.render.engine != "CYCLES":
        return f"render engine is {scene.render.engine}, a final-quality render means Cycles"
    if scene.cycles.samples < 64:
        return f"Cycles samples set to {scene.cycles.samples}, too few for a final render"


def file_dir() -> Path:
    """Where 'next to this .blend file' is; the temp folder when the file was never saved (selfcheck)."""
    return Path(bpy.data.filepath).parent if bpy.data.filepath else Path(tempfile.gettempdir())


def cameras() -> list:
    return [o for o in bpy.context.scene.objects if o.type == "CAMERA"]


def product_distance(camera, box) -> float:
    return (camera.matrix_world.translation - (box[0] + box[1]) / 2).length


def matrix_close(a: Matrix, b: Matrix, tolerance=1e-3) -> bool:
    return all(abs(x - y) <= tolerance for ra, rb in zip(a, b) for x, y in zip(ra, rb))


def tree_colors(tree) -> list[tuple[float, float, float]]:
    """Every color sitting in a node tree: RGBA socket defaults and color ramp stops."""
    colors = []
    if tree is None:
        return colors
    for node in tree.nodes:
        if node.type == "VALTORGB":
            colors += [tuple(e.color[:3]) for e in node.color_ramp.elements]
        for socket in node.inputs:
            if socket.type == "RGBA" and not socket.is_linked:
                colors.append(tuple(socket.default_value[:3]))
    return colors


def has_gradient(tree) -> bool:
    if tree is None:
        return False
    for node in tree.nodes:
        if node.type in ("TEX_GRADIENT", "VALTORGB", "TEX_IMAGE", "TEX_SKY"):
            return True
        if node.type in ("MIX", "MIX_RGB", "MIX_SHADER") and node.inputs[0].is_linked:
            return True
    return False


def backdrop_object(exclude, floor=None, min_extent: float = 1.0):
    """The biggest mesh behind the subject that is not the floor it stands on."""
    biggest, size = None, 0.0
    for obj in t.meshes():
        if obj in exclude or obj is floor:
            continue
        low, high = t.bounds(obj)
        extent = max(high.x - low.x, high.y - low.y, high.z - low.z)
        if extent >= min_extent and extent > size:
            biggest, size = obj, extent
    return biggest


def background_tree(exclude, floor=None):
    """The node tree that paints the background: a backdrop mesh's material, else the world's."""
    obj = backdrop_object(exclude, floor)
    if obj is not None and t.visible_material(obj) is not None:
        return t.visible_material(obj).node_tree
    world = bpy.context.scene.world
    return world.node_tree if world is not None and world.use_nodes else None


def under(parts, box):
    """The object a product rests on, found by a ray beside it."""
    center = (box[0] + box[1]) / 2
    origin = Vector((box[1].x + 0.02, center.y, box[0].z + 0.05))
    depsgraph = bpy.context.evaluated_depsgraph_get()
    hit, _, _, _, obj, _ = bpy.context.scene.ray_cast(depsgraph, origin, Vector((0, 0, -1)), distance=0.1)
    if hit and obj is not None and obj.original not in parts:
        return obj.original
    return None


# ================================================================== 1. perfume ad

def req_stone_floor(ctx):
    parts = pj.product_parts()
    box = pj.box_of(parts)
    if box is None:
        return "no product"
    floor = under(parts, box)
    if floor is None:
        return "the product is not standing on anything"
    low, high = t.bounds(floor)
    if min(high.x - low.x, high.y - low.y) < 3 * max(box[1].x - box[0].x, box[1].y - box[0].y):
        return "the surface under the product is too small to be a set"
    material = t.visible_material(floor)
    node = pj.principled_of(material)
    if node is None:
        return f"{floor.name} has no material to look like polished stone"
    color = pj.color_of(material)
    if node.inputs["Roughness"].default_value > 0.4 and not node.inputs["Roughness"].is_linked:
        return f"the floor's roughness is {node.inputs['Roughness'].default_value:.2f}: not polished, no reflections"
    if pj.luminance(color) > 0.35 and not pj.texture_driven(material):
        return f"the floor color {tuple(round(c, 2) for c in color)} is not dark stone"


def req_warm_light(ctx):
    warm = [o.name for o in pj.lights() if o.data.color[0] > o.data.color[2] + 0.1 and o.data.color[0] >= o.data.color[1]]
    if not warm:
        return "no light has a warm color"


def req_amber_gradient(ctx):
    parts = pj.product_parts()
    box = pj.box_of(parts)
    floor = under(parts, box) if box else None
    tree = background_tree(set(parts), floor)
    if not has_gradient(tree):
        return "the background is a flat color, not a gradient"
    amber = [c for c in tree_colors(tree) if c[0] > c[1] > c[2] and c[0] >= 0.25 and c[1] >= 0.25 * c[0]]
    if not amber:
        return "no amber color anywhere in the background"


def camera_roles(ctx) -> dict:
    """Which cameras qualify as hero, close-up and overhead."""
    parts = pj.product_parts()
    box = pj.box_of(parts)
    roles = {"hero": [], "close": [], "overhead": []}
    if box is None:
        return roles
    points = [c for o in parts for c in pj.corners(o)]
    scene = bpy.context.scene
    saved = scene.camera
    framing = {}
    try:
        for camera in cameras():
            scene.camera = camera
            outside, width, height = pj.in_frame(points)
            framing[camera] = (outside, max(width, height), product_distance(camera, box))
    finally:
        scene.camera = saved
    for camera, (outside, span, distance) in framing.items():
        if outside == 0 and 0.25 <= span <= 0.85:
            roles["hero"].append(camera)
        if pj.camera_forward(camera).z < -0.8:
            roles["overhead"].append(camera)
    hero_distance = min((framing[c][2] for c in roles["hero"]), default=None)
    for camera, (outside, span, distance) in framing.items():
        # A close-up crops the product or fills the frame with it, from nearer than the hero.
        if (outside > 0 or span > 0.85) and (hero_distance is None or distance < hero_distance):
            roles["close"].append(camera)
    return roles


def req_three_cameras(ctx):
    if len(cameras()) < 3:
        return f"{len(cameras())} camera(s), asked for three"
    roles = camera_roles(ctx)
    missing = [role for role, found in roles.items() if not found]
    if missing:
        return f"no camera works as a {' or '.join(missing)} shot"
    used = set()
    for role in ("overhead", "close", "hero"):
        pick = next((c for c in roles[role] if c not in used), None)
        if pick is None:
            return f"the {role} shot shares its camera with another composition"
        used.add(pick)


def req_hero_active(ctx):
    camera = bpy.context.scene.camera
    if camera is None:
        return "no active camera"
    if camera not in camera_roles(ctx)["hero"]:
        return f"the active camera {camera.name} is not the hero shot (whole product in frame, filling 25-85%)"


def req_cameras_kept(ctx):
    was = [name for name, entry in ctx.stage1.items() if entry["type"] == "CAMERA"]
    if not was:
        return "there were no cameras after the first brief"
    changed = [name for name in was if not t.unchanged(ctx.stage1, name, 1e-3)]
    if changed:
        return f"camera(s) moved or removed: {changed}"


def req_pink_setting(ctx):
    parts = pj.product_parts()
    box = pj.box_of(parts)
    floor = under(parts, box) if box else None
    tree = background_tree(set(parts), floor)
    colors = tree_colors(tree) if tree is not None else [pj.world_color()]
    colors = [c for c in colors if max(c) > 0.05]
    pink = [c for c in colors if c[0] >= 0.6 and c[0] > c[1] + 0.1 and c[2] >= c[1] - 0.08]
    if not pink:
        return f"no pastel pink in the background (colors {[tuple(round(v, 2) for v in c) for c in colors][:4]})"
    if sum(pj.luminance(c) for c in colors) / len(colors) < 0.45:
        return "the background is still dark, asked for bright"


def req_daylight(ctx):
    found = [o for o in pj.lights() if o.data.type == "SUN" or (o.data.type == "AREA" and o.data.size >= 1.0)]
    if not found:
        return "no sun or large soft light: nothing reads as daylight"


PERFUME_SOLUTION = """
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
floor.name = 'Stone'; floor.data.materials.append(material('PolishedStone', (0.03, 0.03, 0.035), Roughness=0.12))
bpy.ops.mesh.primitive_plane_add(size=8, location=(0, 2.5, 3), rotation=(math.radians(90), 0, 0)); wall = bpy.context.object
wall.name = 'Backdrop'
mat = bpy.data.materials.new('AmberGradient'); mat.use_nodes = True; tree = mat.node_tree
ramp = tree.nodes.new('ShaderNodeValToRGB'); grad = tree.nodes.new('ShaderNodeTexGradient'); coords = tree.nodes.new('ShaderNodeTexCoord')
ramp.color_ramp.elements[0].color = (0.02, 0.01, 0.005, 1); ramp.color_ramp.elements[1].color = (0.6, 0.32, 0.08, 1)
tree.links.new(coords.outputs['Generated'], grad.inputs['Vector']); tree.links.new(grad.outputs['Fac'], ramp.inputs['Fac'])
tree.links.new(ramp.outputs['Color'], tree.nodes['Principled BSDF'].inputs['Base Color'])
wall.data.materials.append(mat)
spot = bpy.data.objects.new('WarmSpot', bpy.data.lights.new('WarmSpot', 'SPOT'))
spot.data.energy, spot.data.color, spot.data.spot_size = 400, (1.0, 0.75, 0.5), math.radians(50)
spot.location = (0.8, -0.9, 1.2); scene.collection.objects.link(spot)
track = spot.constraints.new('TRACK_TO'); track.target = bottle
fill = bpy.data.objects.new('Fill', bpy.data.lights.new('Fill', 'AREA')); fill.data.energy, fill.data.size = 30, 0.8
fill.location = (-1.2, -0.8, 0.6); scene.collection.objects.link(fill); fill.constraints.new('TRACK_TO').target = bottle
centre = bpy.data.objects.new('ProductCentre', None); centre.location = (-0.06, 0.0, 0.2); scene.collection.objects.link(centre)
def camera(name, location, target, lens):
    cam = bpy.data.objects.new(name, bpy.data.cameras.new(name)); cam.data.lens = lens
    cam.location = location; scene.collection.objects.link(cam)
    cam.constraints.new('TRACK_TO').target = target
    return cam
hero = camera('Hero', (0.0, -1.7, 0.35), centre, 60)
close = camera('CloseUp', (0.35, -0.45, 0.25), bottle, 85)
top = camera('Overhead', (0.0, -0.05, 2.2), centre, 50)
scene.camera = hero
scene.render.engine = 'CYCLES'; scene.cycles.samples = 256; scene.cycles.use_denoising = True
r = scene.render; r.resolution_x = r.resolution_y = 1080; r.resolution_percentage = 100; r.image_settings.file_format = 'PNG'
bpy.context.view_layer.update()
"""

PERFUME_FOLLOW_UP_SOLUTION = """
import bpy
scene = bpy.context.scene
ramp = bpy.data.materials['AmberGradient'].node_tree.nodes['Color Ramp']
ramp.color_ramp.elements[0].color = (0.98, 0.78, 0.85, 1); ramp.color_ramp.elements[1].color = (1.0, 0.9, 0.93, 1)
stone = bpy.data.materials['PolishedStone'].node_tree.nodes['Principled BSDF']
stone.inputs['Base Color'].default_value = (0.9, 0.8, 0.84, 1)
sun = bpy.data.objects.new('Daylight', bpy.data.lights.new('Daylight', 'SUN')); sun.data.energy = 3
sun.data.color = (1.0, 0.98, 0.95); sun.rotation_euler = (0.9, 0.2, 0.6); scene.collection.objects.link(sun)
bpy.data.objects['WarmSpot'].data.color = (1.0, 0.97, 0.95)
world = scene.world; world.use_nodes = True
world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.95, 0.8, 0.86, 1)
"""

# ================================================================== the camera product

CAMERA_PARTS = ["Camera_body", "Camera_lens", "Camera_lens_body", "Camera_strap", "Camera_strap.001"]
CAMERA_MATERIALS = {"Camera_01_body", "Camera_01_lens", "Camera_01_lens_body", "Camera_01_strap"}


def setup_camera_product() -> None:
    """A CC0 vintage camera (Poly Haven Camera_01) split into body, lens, lens body and strap, on
    the ground at the origin, and nothing else but the factory camera and light."""
    t._remove("Cube")
    with bpy.data.libraries.load(str(FIXTURES / "camera_product.blend"), link=False) as (source, target):
        target.objects = [name for name in source.objects if name in CAMERA_PARTS]
    for obj in target.objects:
        bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()


def camera_parts() -> list:
    named = [o for o in t.meshes() if o.name.startswith("Camera_")]
    if named:
        return named
    return [o for o in t.meshes() if 0.03 <= (lambda b: b[1].z - b[0].z)(t.bounds(o)) <= 0.15]


def body():
    parts = camera_parts()
    return next((o for o in parts if pj.base_name(o.name) == "Camera_body"), None) or (
        max(parts, key=lambda o: (lambda b: (b[1] - b[0]).length)(t.bounds(o))) if parts else None)


def req_camera_kept(ctx):
    parts = camera_parts()
    if len(parts) < 4:
        return f"{len(parts)} product part(s) left, the camera has five"
    entries = [ctx.before[n] for n in CAMERA_PARTS if n in ctx.before]
    was = Vector(max(e["max"][i] for e in entries) - min(e["min"][i] for e in entries) for i in range(3))
    with pj.at_frame(pj.frame_range()[0]):
        box = pj.box_of(parts)
    size = box[1] - box[0]
    if any(abs(size[i] - was[i]) > 0.06 * was[i] + 0.005 for i in range(3)):
        return f"the product is {tuple(round(v, 3) for v in size)} m at the first frame, it was {tuple(round(v, 3) for v in was)}"
    missing = CAMERA_MATERIALS - {pj.base_name(m.name) for m in pj.materials_used(parts)}
    if missing:
        return f"the product's own materials were replaced or dropped: {sorted(missing)}"


# ================================================================== 2. product reveal

def lens_center() -> Vector | None:
    lens = next((o for o in camera_parts() if pj.base_name(o.name) == "Camera_lens"), None)
    box = pj.box_of([lens] if lens else camera_parts())
    return (box[0] + box[1]) / 2 if box else None


def camera_path(samples: int = 48) -> list[Vector]:
    camera = bpy.context.scene.camera
    path = []
    for frame in pj.sample_frames(samples):
        with pj.at_frame(frame):
            path.append(camera.matrix_world.translation.copy())
    return path


def progress(path: list[Vector]) -> list[float]:
    """Fraction of the camera's total travel completed at each sample."""
    steps = [(b - a).length for a, b in zip(path, path[1:])]
    total = sum(steps)
    if total < 1e-6:
        return [0.0] * len(path)
    done, out = 0.0, [0.0]
    for step in steps:
        done += step
        out.append(done / total)
    return out


def req_close_start(ctx):
    camera = bpy.context.scene.camera
    if camera is None:
        return "no camera"
    start, end = pj.frame_range()
    with pj.at_frame(start):
        lens = lens_center()
        box = pj.box_of(camera_parts())
        if lens is None:
            return "no product"
        d_start = (camera.matrix_world.translation - lens).length
        outside, _, _ = pj.in_frame([lens])
    with pj.at_frame(end):
        d_end = product_distance(camera, pj.box_of(camera_parts()))
    if outside:
        return "the lens is not in frame at the first frame"
    if d_start > 0.5 * d_end:
        return f"the first frame is {d_start:.2f} m from the lens and the last {d_end:.2f} m from the product: not a close-up pulling back"


def req_hero_end(ctx):
    camera = bpy.context.scene.camera
    if camera is None:
        return "no camera"
    with pj.at_frame(pj.frame_range()[1]):
        parts = camera_parts()
        points = [c for o in parts for c in pj.corners(o)]
        if not points:
            return "no product"
        outside, width, height = pj.in_frame(points)
        from bpy_extras.object_utils import world_to_camera_view
        box = pj.box_of(parts)
        center = world_to_camera_view(bpy.context.scene, camera, (box[0] + box[1]) / 2)
    if outside:
        return f"{outside} product corner(s) out of frame on the last frame"
    if not 0.3 <= max(width, height) <= 0.9:
        return f"the product fills {max(width, height):.0%} of the last frame, a hero shot wants 30-90%"
    if not (0.35 <= center.x <= 0.65 and 0.3 <= center.y <= 0.7):
        return f"the product is off-centre on the last frame ({center.x:.2f}, {center.y:.2f})"


def req_product_rotates(ctx):
    camera = bpy.context.scene.camera
    subject = body()
    if camera is None or subject is None:
        return "no camera or product"
    angles = []
    for frame in pj.sample_frames(9):
        with pj.at_frame(frame):
            local = subject.matrix_world.inverted() @ camera.matrix_world.translation
            angles.append(math.degrees(math.atan2(local.y, local.x)))
    deltas = [((b - a + 180) % 360) - 180 for a, b in zip(angles, angles[1:])]
    if abs(sum(deltas)) < 60:
        return f"the view of the product only turns {abs(sum(deltas)):.0f} degrees over the shot"


def req_smooth_camera(ctx):
    if bpy.context.scene.camera is None:
        return "no camera"
    path = camera_path(48)
    steps = sorted((b - a).length for a, b in zip(path, path[1:]))
    if sum(steps) < 0.2:
        return "the camera barely moves"
    median = steps[len(steps) // 2]
    if median > 0 and steps[-1] > 4 * median:
        return f"the camera jumps: one step is {steps[-1]:.2f} m against a typical {median:.3f} m"
    if steps[0] == 0 and steps.count(0) > len(steps) // 2:
        return "the camera stands still for most of the shot"


def req_dark_studio(ctx):
    color = pj.background_color(set(camera_parts()), min_footprint=1.0)
    if pj.luminance(color) > 0.35:
        return f"the background {tuple(round(c, 2) for c in color)} is bright, not a dark studio"


def req_moving_rim(ctx):
    camera = bpy.context.scene.camera
    box = pj.box_of(camera_parts())
    if camera is None or box is None:
        return "no camera or product"
    start, end = pj.frame_range()
    moved = []
    for light in pj.lights():
        with pj.at_frame(start):
            a = light.matrix_world.translation.copy()
        with pj.at_frame(end):
            b = light.matrix_world.translation.copy()
            center = (lambda bx: (bx[0] + bx[1]) / 2)(pj.box_of(camera_parts()))
            forward = pj.camera_forward(camera).xy.normalized()
            behind = (b.xy - center.xy).length > 0.05 and (b.xy - center.xy).normalized().dot(forward) > 0.2
        if (a - b).length >= 0.1 and behind:
            moved.append(light.name)
    if not moved:
        return "no light both moves during the shot and ends behind the product as a rim"


def remember_reveal(ctx) -> dict:
    p = progress(camera_path(48))
    n = len(p) - 1
    t90 = next((i / n for i, v in enumerate(p) if v >= 0.9), 1.0)
    return {"middle": p[int(0.75 * n)] - p[int(0.25 * n)], "t90": t90,
            "colors": [tuple(o.data.color[:3]) for o in pj.lights()]}


def req_faster_middle(ctx):
    p = progress(camera_path(48))
    n = len(p) - 1
    middle = p[int(0.75 * n)] - p[int(0.25 * n)]
    t90 = next((i / n for i, v in enumerate(p) if v >= 0.9), 1.0)
    if middle < ctx.memory["middle"] + 0.08 and t90 > ctx.memory["t90"] - 0.1:
        return (f"the camera covers {middle:.0%} of its travel in the middle half (was {ctx.memory['middle']:.0%}) "
                f"and reaches 90% at {t90:.0%} of the shot (was {ctx.memory['t90']:.0%}): no faster")


def req_blue_lights(ctx):
    found = pj.lights()
    if not found:
        return "no lights"
    blue = [o for o in found if o.data.color[2] > o.data.color[0] + 0.2 and o.data.color[2] > o.data.color[1]]
    if len(blue) < max(1, (2 * len(found) + 2) // 3):
        return f"{len(blue)} of {len(found)} lights are blue"


REVEAL_SOLUTION = """
import bpy, math
scene = bpy.context.scene
body = bpy.data.objects['Camera_body']
parts = [o for o in scene.objects if o.type == 'MESH' and o.name.startswith('Camera_')]
scene.frame_start, scene.frame_end, scene.render.fps = 1, 192, 24
bpy.ops.mesh.primitive_plane_add(size=6); floor = bpy.context.object; floor.name = 'Floor'
mat = bpy.data.materials.new('Dark'); mat.use_nodes = True
mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.02, 0.02, 0.022, 1)
mat.node_tree.nodes['Principled BSDF'].inputs['Roughness'].default_value = 0.3
floor.data.materials.append(mat)
world = scene.world; world.use_nodes = True
world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.005, 0.005, 0.006, 1)
pivot = bpy.data.objects.new('Pivot', None); scene.collection.objects.link(pivot)
for obj in parts:
    if obj.parent is None:
        m = obj.matrix_world.copy(); obj.parent = pivot; obj.matrix_world = m
bpy.context.preferences.edit.keyframe_new_interpolation_type = 'BEZIER'
pivot.rotation_euler = (0, 0, 0); pivot.keyframe_insert('rotation_euler', frame=1)
pivot.rotation_euler = (0, 0, math.radians(200)); pivot.keyframe_insert('rotation_euler', frame=192)
cam = scene.camera
cam.data.lens = 50
cam.location = (0.12, -0.16, 0.06); cam.keyframe_insert('location', frame=1)
cam.location = (0.0, -0.9, 0.35); cam.keyframe_insert('location', frame=192)
track = cam.constraints.new('TRACK_TO'); track.target = body
for name, start, end, energy in (('Rim1', (0.5, 0.6, 0.4), (-0.4, 0.7, 0.5), 60), ('Rim2', (-0.5, 0.5, 0.3), (0.5, 0.6, 0.4), 40), ('Key', (0.6, -0.6, 0.6), (0.6, -0.6, 0.6), 50)):
    light = bpy.data.objects.new(name, bpy.data.lights.new(name, 'AREA')); light.data.energy, light.data.size = energy, 0.4
    scene.collection.objects.link(light); light.constraints.new('TRACK_TO').target = body
    light.location = start; light.keyframe_insert('location', frame=1)
    light.location = end; light.keyframe_insert('location', frame=192)
scene.render.engine = 'CYCLES'; scene.cycles.samples = 128; scene.cycles.use_denoising = True
r = scene.render; r.resolution_x, r.resolution_y, r.resolution_percentage = 1920, 1080, 100; r.image_settings.file_format = 'PNG'
bpy.context.view_layer.update()
"""

REVEAL_FOLLOW_UP_SOLUTION = """
import bpy
scene = bpy.context.scene
cam = scene.camera
# Same start and end, but the pull-back happens between frames 48 and 96 instead of across the whole shot.
cam.location = (0.12, -0.16, 0.06); cam.keyframe_insert('location', frame=48)
cam.location = (0.0, -0.9, 0.35); cam.keyframe_insert('location', frame=96)
for obj in scene.objects:
    if obj.type == 'LIGHT':
        obj.data.color = (0.2, 0.5, 1.0)
        obj.data.energy *= 1.5
"""

# ================================================================== 3. asset pack

PROP_NAMES = {"potion": 3, "chest": 1, "shelf": 1, "sign": 1, "barrel": 1}


def props() -> dict[str, list]:
    found = {key: [] for key in PROP_NAMES}
    for obj in t.meshes():
        name = obj.name.lower()
        for key in PROP_NAMES:
            if key in name or (key == "potion" and ("flask" in name or "bottle" in name)):
                found[key].append(obj)
                break
    return found


def all_props() -> list:
    return [o for group in props().values() for o in group]


def triangles(obj) -> int:
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        return sum(len(p.vertices) - 2 for p in mesh.polygons)
    finally:
        evaluated.to_mesh_clear()


def req_props_named(ctx):
    found = props()
    missing = [f"{key} x{count}" for key, count in PROP_NAMES.items() if len(found[key]) < count]
    if missing:
        return f"missing or unnamed props: {missing}"


def req_low_poly(ctx):
    heavy = [(o.name, triangles(o)) for o in all_props() if triangles(o) > 2000]
    if heavy:
        return f"over 2000 triangles: {heavy}"
    if not all_props():
        return "no props"


def req_origins_at_base(ctx):
    wrong = []
    for obj in all_props():
        low, high = t.bounds(obj)
        origin = obj.matrix_world.translation
        if abs(origin.z - low.z) > 0.02 or not (low.x - 0.02 <= origin.x <= high.x + 0.02 and low.y - 0.02 <= origin.y <= high.y + 0.02):
            wrong.append(obj.name)
    if wrong:
        return f"origin not at the base: {wrong}"


def palette() -> set:
    colors = set()
    for material in pj.materials_used(all_props()):
        color = pj.color_of(material)
        if color:
            colors.add(tuple(round(c, 1) for c in color))
    return colors


def req_palette(ctx):
    materials = pj.materials_used(all_props())
    if not materials:
        return "the props have no materials"
    if any(n.type == "TEX_IMAGE" for m in materials if m.node_tree for n in m.node_tree.nodes):
        return "image textures used, asked for flat hand-painted colors"
    colors = palette()
    if len(colors) > 12:
        return f"{len(colors)} different colors, not one small palette"
    if len(colors) < 3:
        return f"only {len(colors)} color(s), that is not a palette"


def shrunk_tree(obj):
    """A BVH of the evaluated mesh pulled 1% towards its centre, so touching faces do not count."""
    from mathutils.bvhtree import BVHTree
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        low, high = t.bounds(obj)
        centre = (low + high) / 2
        verts = [centre + (evaluated.matrix_world @ v.co - centre) * 0.99 for v in mesh.vertices]
        polygons = [list(p.vertices) for p in mesh.polygons]
        return BVHTree.FromPolygons(verts, polygons)
    finally:
        evaluated.to_mesh_clear()


def overlap(a, b) -> bool:
    """Whether two props' meshes run into each other (boxes may overlap: a potion on a shelf)."""
    la, ha = t.bounds(a)
    lb, hb = t.bounds(b)
    if any(min(ha[i], hb[i]) - max(la[i], lb[i]) <= 0 for i in range(3)):
        return False
    return bool(shrunk_tree(a).overlap(shrunk_tree(b)))


def req_arranged(ctx):
    items = all_props()
    if not items:
        return "no props"
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if overlap(a, b):
                return f"{a.name} and {b.name} overlap"
    sign = props()["sign"]
    if sign and t.bounds(sign[0])[0].z < 0.2:
        return "the sign sits on the ground instead of hanging"
    if any(t.bounds(o)[0].xy.length > 6 for o in items):
        return "props are spread more than 6 m apart, not a preview arrangement"


def req_glb(name: str):
    def requirement(ctx):
        path = file_dir() / name
        if not path.is_file():
            return f"no {name} next to the .blend file"
        if path.stat().st_size < 10_000:
            return f"{name} is only {path.stat().st_size} bytes"
    return requirement


def remember_pack(ctx) -> dict:
    glb = file_dir() / "asset_pack.glb"
    return {"props": {o.name: {"size": tuple(t.bounds(o)[1] - t.bounds(o)[0]), "origin": tuple(o.matrix_world.translation)}
                      for o in all_props()},
            "glb_mtime": glb.stat().st_mtime if glb.is_file() else 0}


def req_pack_preserved(ctx):
    wrong = []
    for name, was in ctx.memory["props"].items():
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != "MESH":
            wrong.append(f"{name} gone")
            continue
        low, high = t.bounds(obj)
        size = high - low
        if any(abs(size[i] - was["size"][i]) > 0.05 * was["size"][i] + 0.005 for i in range(3)):
            wrong.append(f"{name} resized")
        if (obj.matrix_world.translation - Vector(was["origin"])).length > 0.02:
            wrong.append(f"{name} moved")
    if wrong:
        return "; ".join(wrong)


def req_icy(ctx):
    colors = [pj.color_of(m) for m in pj.materials_used(all_props())]
    colors = [c for c in colors if c]
    if not colors:
        return "no materials"
    icy = [c for c in colors if c[2] >= c[0] + 0.05 and c[2] >= c[1] - 0.05 and pj.luminance(c) >= 0.3]
    if len(icy) < 0.6 * len(colors):
        return f"{len(icy)} of {len(colors)} materials are icy blues and whites"


def req_reexported(name: str):
    def requirement(ctx):
        path = file_dir() / name
        if not path.is_file():
            return f"no {name}"
        if path.stat().st_mtime <= ctx.memory["glb_mtime"] + 0.5:
            return f"{name} was not exported again after the change"
    return requirement


PACK_SOLUTION = """
import bpy, bmesh, math, os, tempfile
scene = bpy.context.scene
def flat(name, rgb):
    mat = bpy.data.materials.new(name); mat.use_nodes = True
    mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (*rgb, 1)
    mat.node_tree.nodes['Principled BSDF'].inputs['Roughness'].default_value = 0.9
    return mat
wood, dark, glass, gold, red, green, blue = (flat('Wood', (0.55, 0.35, 0.18)), flat('DarkWood', (0.3, 0.18, 0.1)), flat('Glass', (0.7, 0.85, 0.9)),
    flat('Gold', (0.9, 0.7, 0.2)), flat('Red', (0.8, 0.15, 0.2)), flat('Green', (0.2, 0.7, 0.3)), flat('Blue', (0.2, 0.4, 0.9)))
def prop(name, size, location, mat, kind='cube', segments=8):
    mesh = bpy.data.meshes.new(name); bm = bmesh.new()
    if kind == 'cube':
        bmesh.ops.create_cube(bm, size=1.0)
    else:
        bmesh.ops.create_cone(bm, cap_ends=True, segments=segments, radius1=0.5, radius2=0.5, depth=1.0)
    bmesh.ops.translate(bm, verts=bm.verts[:], vec=(0, 0, 0.5))
    bmesh.ops.scale(bm, verts=bm.verts[:], vec=size)
    bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh); obj.location = location; scene.collection.objects.link(obj)
    mesh.materials.append(mat)
    return obj
prop('Barrel', (0.6, 0.6, 0.8), (-1.2, 0, 0), dark, 'cyl', 10)
prop('Chest', (0.8, 0.5, 0.5), (0, 0, 0), wood)
prop('Shelf', (1.2, 0.3, 1.6), (1.6, 0.2, 0), wood)
for i, mat in enumerate((red, green, blue)):
    prop(f'Potion_{i + 1}', (0.14, 0.14, 0.3), (1.3 + i * 0.28, 0.2, 1.0), mat, 'cyl', 8)
prop('Sign', (0.7, 0.05, 0.4), (0, -0.9, 1.4), gold)
folder = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else tempfile.gettempdir()
bpy.ops.object.select_all(action='DESELECT')
bpy.ops.export_scene.gltf(filepath=os.path.join(folder, 'asset_pack.glb'), export_format='GLB', use_selection=False)
scene.render.engine = 'CYCLES'; scene.cycles.samples = 128
bpy.context.view_layer.update()
"""

PACK_FOLLOW_UP_SOLUTION = """
import bpy, os, tempfile, time
icy = {'Wood': (0.75, 0.88, 1.0), 'DarkWood': (0.45, 0.65, 0.9), 'Glass': (0.9, 0.97, 1.0), 'Gold': (0.85, 0.95, 1.0),
       'Red': (0.5, 0.7, 1.0), 'Green': (0.6, 0.85, 1.0), 'Blue': (0.3, 0.55, 0.95)}
for name, rgb in icy.items():
    bpy.data.materials[name].node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (*rgb, 1)
time.sleep(1.0)
folder = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else tempfile.gettempdir()
bpy.ops.export_scene.gltf(filepath=os.path.join(folder, 'asset_pack.glb'), export_format='GLB', use_selection=False)
"""

# ================================================================== 4. dungeon modules

MODULES = ("floor", "wall", "doorway", "pillar")


def module_objects(kind: str) -> list:
    return [o for o in t.meshes() if o.name.lower().startswith(kind)]


def tiles() -> list:
    return module_objects("floor")


def tile_rect() -> tuple[Vector, Vector] | None:
    return pj.box_of(tiles()) if tiles() else None


def req_modules_reused(ctx):
    counts: dict[str, int] = {}
    for obj in t.meshes():
        counts[obj.data.name] = counts.get(obj.data.name, 0) + 1
    shared = [name for name, n in counts.items() if n >= 4]
    if len(shared) < 2:
        return f"only {len(shared)} mesh(es) are reused by four or more objects: the modules are not linked duplicates"
    missing = [kind for kind in MODULES if not module_objects(kind)]
    if missing:
        return f"no object named after the module(s): {missing}"


def req_on_grid(ctx):
    objects = [o for kind in MODULES for o in module_objects(kind)]
    if not objects:
        return "no modules"
    off = [o.name for o in objects if any(abs(v - round(v)) > 0.02 for v in o.matrix_world.translation.xy)]
    if len(off) > 0.1 * len(objects):
        return f"{len(off)} of {len(objects)} modules are off the 1 m grid, e.g. {off[:3]}"


def req_room(ctx):
    rect = tile_rect()
    if rect is None or len(tiles()) < 16:
        return f"{len(tiles())} floor tiles, a 4 x 4 room needs 16"
    size = rect[1] - rect[0]
    if size.x < 7.9 or size.y < 7.9:
        return f"the floor covers {size.x:.1f} x {size.y:.1f} m, asked for at least 8 x 8 (4 x 4 tiles of 2 m)"


def req_enclosed(ctx):
    rect = tile_rect()
    if rect is None:
        return "no floor"
    center = Vector(((rect[0].x + rect[1].x) / 2, (rect[0].y + rect[1].y) / 2, rect[1].z + 1.0))
    half = (rect[1] - rect[0]) / 2
    open_sides = []
    for name, direction, reach in (("+X", Vector((1, 0, 0)), half.x), ("-X", Vector((-1, 0, 0)), half.x),
                                   ("+Y", Vector((0, 1, 0)), half.y), ("-Y", Vector((0, -1, 0)), half.y)):
        # Two rays per side, off-centre, so a doorway in the middle does not read as a missing wall.
        hits = 0
        for offset in (0.7, -0.7):
            origin = center + Vector((-direction.y, direction.x, 0)) * offset * (half.y if direction.x else half.x)
            obj, _ = pj.cast(origin, direction, reach + 1.5)
            hits += obj is not None and any(obj.name.lower().startswith(k) for k in ("wall", "doorway", "pillar"))
        if hits == 0:
            open_sides.append(name)
    if open_sides:
        return f"no wall on the {', '.join(open_sides)} side(s)"


def req_one_doorway(ctx):
    doors = module_objects("doorway")
    if len(doors) != 1:
        return f"{len(doors)} doorway(s), asked for exactly one"


def req_chest_centred(ctx):
    rect = tile_rect()
    chests = [o for o in t.meshes() if "chest" in o.name.lower()]
    if rect is None or not chests:
        return "no chest or no floor"
    center = ((rect[0] + rect[1]) / 2).xy
    low, high = t.bounds(chests[0])
    if (((low + high) / 2).xy - center).length > 1.5:
        return "the chest is not in the centre of the room"
    if abs(low.z - rect[1].z) > 0.15:
        return f"the chest floats {low.z - rect[1].z:.2f} m above the floor"


def glows(obj) -> bool:
    for material in pj.materials_used([obj]):
        node = pj.principled_of(material)
        if node is not None and node.inputs["Emission Strength"].default_value > 0 and max(node.inputs["Emission Color"].default_value[:3]) > 0.1:
            return True
        if material.node_tree and any(n.type == "EMISSION" for n in material.node_tree.nodes):
            return True
    return False


def req_crystals(ctx):
    crystals = [o for o in t.meshes() if "crystal" in o.name.lower()]
    lit = [o for o in crystals if glows(o)]
    if len(lit) < 3:
        return f"{len(lit)} glowing crystal(s), asked for a few (three or more with an emissive material)"


def req_walkthrough(ctx):
    camera = bpy.context.scene.camera
    rect = tile_rect()
    if camera is None or rect is None:
        return "no camera or floor"
    start, end = pj.frame_range()
    with pj.at_frame(start):
        a = camera.matrix_world.translation.copy()
    with pj.at_frame(end):
        b = camera.matrix_world.translation.copy()
    inside = lambda p: rect[0].x < p.x < rect[1].x and rect[0].y < p.y < rect[1].y
    if inside(a):
        return "the walkthrough starts inside the room, asked to start outside the doorway"
    if not inside(b):
        return "the walkthrough does not end inside the room"
    if (a - b).length < 3:
        return "the camera travels less than 3 m"


def remember_dungeon(ctx) -> dict:
    return {"tiles": len(tiles()), "floor_mesh": tiles()[0].data.name if tiles() else None,
            "rect": [tuple(v) for v in tile_rect()] if tile_rect() else None}


def req_original_untouched(ctx):
    changed = [name for name, entry in ctx.stage1.items()
               if entry["type"] == "MESH" and not t.unchanged(ctx.stage1, name, 1e-3)]
    if changed:
        return f"objects of the original room moved or vanished: {changed[:5]}"


def req_second_room(ctx):
    doors = module_objects("doorway")
    if len(doors) != 2:
        return f"{len(doors)} doorway(s), a connected second room needs two"
    if len(tiles()) < ctx.memory["tiles"] + 9:
        return f"{len(tiles()) - ctx.memory['tiles']} floor tiles added, a room needs at least 9"
    if ctx.memory["floor_mesh"] and any(o.data.name != ctx.memory["floor_mesh"] for o in tiles()):
        return "the new floor tiles do not share the floor module's mesh"
    old = ctx.memory["rect"]
    new = [o for o in tiles() if o.name not in ctx.stage1]
    rect = pj.box_of(new)
    if rect is None:
        return "no new tiles"
    gap_x = max(rect[0].x - old[1][0], old[0][0] - rect[1].x, 0)
    gap_y = max(rect[0].y - old[1][1], old[0][1] - rect[1].y, 0)
    if max(gap_x, gap_y) > 2.1:
        return f"the new room is {max(gap_x, gap_y):.1f} m away from the original, not connected"


DUNGEON_SOLUTION = """
import bpy, bmesh, math, os, tempfile
scene = bpy.context.scene
def flat(name, rgb, emit=0):
    mat = bpy.data.materials.new(name); mat.use_nodes = True
    node = mat.node_tree.nodes['Principled BSDF']; node.inputs['Base Color'].default_value = (*rgb, 1)
    if emit:
        node.inputs['Emission Color'].default_value = (*rgb, 1); node.inputs['Emission Strength'].default_value = emit
    return mat
stone, crystal_mat, wood = flat('Stone', (0.35, 0.33, 0.3)), flat('Crystal', (0.3, 0.8, 1.0), emit=5), flat('Wood', (0.5, 0.3, 0.15))
def module_mesh(name, size, mat, offset=(0, 0, 0)):
    mesh = bpy.data.meshes.new(name); bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, verts=bm.verts[:], vec=size); bmesh.ops.translate(bm, verts=bm.verts[:], vec=offset)
    bm.to_mesh(mesh); bm.free(); mesh.materials.append(mat); return mesh
floor_mesh = module_mesh('FloorModule', (2, 2, 0.2), stone, (0, 0, -0.1))
wall_mesh = module_mesh('WallModule', (2, 0.3, 3), stone, (0, 0, 1.5))
door_mesh = bpy.data.meshes.new('DoorwayModule')
bm = bmesh.new()
for x in (-0.75, 0.75):
    r = bmesh.ops.create_cube(bm, size=1.0); bmesh.ops.scale(bm, verts=r['verts'], vec=(0.5, 0.3, 3)); bmesh.ops.translate(bm, verts=r['verts'], vec=(x, 0, 1.5))
r = bmesh.ops.create_cube(bm, size=1.0); bmesh.ops.scale(bm, verts=r['verts'], vec=(1.0, 0.3, 0.8)); bmesh.ops.translate(bm, verts=r['verts'], vec=(0, 0, 2.6))
bm.to_mesh(door_mesh); bm.free(); door_mesh.materials.append(stone)
pillar_mesh = module_mesh('PillarModule', (0.4, 0.4, 3), stone, (0, 0, 1.5))
def place(name, mesh, location, rotation=0):
    obj = bpy.data.objects.new(name, mesh); obj.location = location; obj.rotation_euler = (0, 0, rotation)
    scene.collection.objects.link(obj); return obj
def room(ox, oy, n, door_side):
    for i in range(n):
        for j in range(n):
            place('Floor', floor_mesh, (ox + 1 + 2 * i, oy + 1 + 2 * j, 0))
    for i in range(n):
        x, y = ox + 1 + 2 * i, oy + 1 + 2 * i
        for side, loc, rot in (('S', (x, oy, 0), 0), ('N', (x, oy + 2 * n, 0), 0), ('W', (ox, y, 0), math.pi / 2), ('E', (ox + 2 * n, y, 0), math.pi / 2)):
            if side == door_side and i == n // 2:
                place('Doorway', door_mesh, loc, rot)
            else:
                place('Wall', wall_mesh, loc, rot)
    for cx, cy in ((ox, oy), (ox + 2 * n, oy), (ox, oy + 2 * n), (ox + 2 * n, oy + 2 * n)):
        place('Pillar', pillar_mesh, (cx, cy, 0))
room(0, 0, 4, 'S')
chest = place('Chest', module_mesh('ChestMesh', (0.8, 0.5, 0.5), wood, (0, 0, 0.25)), (4, 4, 0))
for k, (x, y) in enumerate(((1, 7), (7, 7), (7, 1))):
    place(f'Crystal_{k}', module_mesh(f'CrystalMesh{k}', (0.3, 0.3, 0.8), crystal_mat, (0, 0, 0.4)), (x, y, 0))
scene.frame_start, scene.frame_end, scene.render.fps = 1, 120, 24
cam = scene.camera; cam.data.lens = 24
cam.location = (5, -4, 1.6); cam.rotation_euler = (math.radians(88), 0, 0); cam.keyframe_insert('location', frame=1)
cam.location = (5, 3, 1.6); cam.keyframe_insert('location', frame=120)
scene.render.engine = 'CYCLES'; scene.cycles.samples = 128
folder = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else tempfile.gettempdir()
bpy.ops.object.select_all(action='DESELECT')
bpy.ops.export_scene.gltf(filepath=os.path.join(folder, 'dungeon.glb'), export_format='GLB', use_selection=False)
bpy.context.view_layer.update()
"""

DUNGEON_FOLLOW_UP_SOLUTION = """
import bpy, math
scene = bpy.context.scene
floor_mesh, wall_mesh, door_mesh, pillar_mesh = (bpy.data.meshes[n] for n in ('FloorModule', 'WallModule', 'DoorwayModule', 'PillarModule'))
def place(name, mesh, location, rotation=0):
    obj = bpy.data.objects.new(name, mesh); obj.location = location; obj.rotation_euler = (0, 0, rotation)
    scene.collection.objects.link(obj); return obj
# Treasure room to the east, 3 x 3 tiles, sharing the original east wall line; its doorway replaces
# nothing in the original room: the connection is a doorway module on the new room's west edge, and
# the original east wall segment behind it stays where it is (the check only asks for a second doorway).
ox, oy, n = 8, 2, 3
for i in range(n):
    for j in range(n):
        place('Floor', floor_mesh, (ox + 1 + 2 * i, oy + 1 + 2 * j, 0))
for i in range(n):
    x, y = ox + 1 + 2 * i, oy + 1 + 2 * i
    place('Wall', wall_mesh, (x, oy, 0)); place('Wall', wall_mesh, (x, oy + 2 * n, 0))
    place('Wall', wall_mesh, (ox + 2 * n, y, 0), math.pi / 2)
    if i == 1:
        place('Doorway', door_mesh, (ox, y, 0), math.pi / 2)
    else:
        place('Wall', wall_mesh, (ox, y, 0), math.pi / 2)
for cx, cy in ((ox + 2 * n, oy), (ox + 2 * n, oy + 2 * n)):
    place('Pillar', pillar_mesh, (cx, cy, 0))
bpy.context.view_layer.update()
"""

# ================================================================== 5. exploded view

def offsets_from_body() -> dict[str, Vector]:
    anchor = body()
    if anchor is None:
        return {}
    inverse = anchor.matrix_world.inverted()
    return {o.name: inverse @ o.matrix_world.translation for o in camera_parts() if o is not anchor}


def start_offsets(ctx) -> dict[str, Vector]:
    anchor = Matrix(ctx.before["Camera_body"]["matrix"]).inverted()
    return {n: anchor @ Vector(Matrix(ctx.before[n]["matrix"]).translation) for n in CAMERA_PARTS if n != "Camera_body" and n in ctx.before}


def req_assembled_ends(ctx):
    start, end = pj.frame_range()
    was = start_offsets(ctx)
    for frame in (start, end):
        with pj.at_frame(frame):
            now = offsets_from_body()
        drift = [name for name, off in was.items() if name in now and (now[name] - off).length > 0.01]
        if drift:
            return f"at frame {frame} the parts are not back in place: {drift}"


def spread(ctx, frame) -> float:
    was = start_offsets(ctx)
    with pj.at_frame(frame):
        now = offsets_from_body()
    return max(((now[n] - was[n]).length for n in was if n in now), default=0.0)


def req_exploded_middle(ctx):
    widest = max(spread(ctx, f) for f in pj.sample_frames(13))
    if widest < 0.08:
        return f"the parts never separate by more than {widest:.3f} m"


def req_rotates_to_side(ctx):
    anchor = body()
    if anchor is None:
        return "no product"
    start, _ = pj.frame_range()
    frames = pj.sample_frames(13)
    peak = max(frames, key=lambda f: spread(ctx, f))
    with pj.at_frame(start):
        a = anchor.matrix_world.to_quaternion()
    with pj.at_frame(peak):
        b = anchor.matrix_world.to_quaternion()
    angle = math.degrees(a.rotation_difference(b).angle)
    if angle < 45:
        return f"the product only turns {angle:.0f} degrees before it explodes"


def req_camera_fixed(ctx):
    camera = bpy.context.scene.camera
    if camera is None:
        return "no camera"
    matrices = []
    for frame in pj.sample_frames(13):
        with pj.at_frame(frame):
            matrices.append(camera.matrix_world.copy())
    if any(not matrix_close(matrices[0], m, 1e-4) for m in matrices):
        return "the camera moves; it was asked to stay fixed"


def side_requirement(low: float, high: float, side: str):
    def requirement(ctx):
        if bpy.context.scene.camera is None:
            return "no camera"
        from bpy_extras.object_utils import world_to_camera_view
        scene = bpy.context.scene
        for frame in pj.sample_frames(13):
            with pj.at_frame(frame):
                points = [c for o in camera_parts() for c in pj.corners(o)]
                views = [world_to_camera_view(scene, scene.camera, p) for p in points]
            if any(not (0 <= v.y <= 1 and v.z > 0) or not (low <= v.x <= high) for v in views):
                return f"at frame {frame} the product leaves the {side} part of the frame"
    return requirement


def sequence_dir() -> Path:
    return file_dir() / "sequence"


def req_sequence(ctx):
    folder = sequence_dir()
    frames = sorted(folder.glob("*.png")) if folder.is_dir() else []
    if len(frames) < 100:
        return f"{len(frames)} PNG frames in {folder.name}/, the sequence needs the whole animation"


def remember_sequence(ctx) -> dict:
    folder = sequence_dir()
    frames = list(folder.glob("*.png")) if folder.is_dir() else []
    return {"newest": max((f.stat().st_mtime for f in frames), default=0)}


def req_sequence_rerendered(ctx):
    problem = req_sequence(ctx)
    if problem:
        return problem
    newest = max(f.stat().st_mtime for f in sequence_dir().glob("*.png"))
    if newest <= ctx.memory["newest"] + 0.5:
        return "the sequence was not rendered again after the change"


EXPLODED_SOLUTION = """
import bpy, math, os, tempfile
scene = bpy.context.scene
body = bpy.data.objects['Camera_body']
parts = {o.name: o for o in scene.objects if o.type == 'MESH' and o.name.startswith('Camera_')}
scene.frame_start, scene.frame_end, scene.render.fps = 1, 120, 30
pivot = bpy.data.objects.new('Pivot', None); scene.collection.objects.link(pivot)
for obj in parts.values():
    m = obj.matrix_world.copy(); obj.parent = pivot; obj.matrix_world = m
pivot.rotation_euler = (0, 0, 0); pivot.keyframe_insert('rotation_euler', frame=1)
pivot.rotation_euler = (0, 0, math.radians(90)); pivot.keyframe_insert('rotation_euler', frame=40)
pivot.keyframe_insert('rotation_euler', frame=80)
pivot.rotation_euler = (0, 0, 0); pivot.keyframe_insert('rotation_euler', frame=120)
from mathutils import Vector
moves = {'Camera_lens': (0, -0.09, 0), 'Camera_lens_body': (0, -0.05, 0), 'Camera_strap': (0.08, 0, 0), 'Camera_strap.001': (0, 0, -0.08)}
for name, delta in moves.items():
    obj = parts[name]; home = obj.location.copy()
    obj.keyframe_insert('location', frame=1); obj.keyframe_insert('location', frame=40)
    obj.location = home + Vector(delta); obj.keyframe_insert('location', frame=60)
    obj.location = home; obj.keyframe_insert('location', frame=80); obj.keyframe_insert('location', frame=120)
cam = scene.camera; cam.data.lens = 50
cam.location = (-0.16, -1.3, 0.35); cam.rotation_euler = (math.radians(80), 0, 0)
r = scene.render; r.resolution_x, r.resolution_y, r.resolution_percentage = 1920, 1080, 100; r.image_settings.file_format = 'PNG'
world = scene.world; world.use_nodes = True; world.node_tree.nodes['Background'].inputs['Color'].default_value = (0.9, 0.9, 0.9, 1)
folder = os.path.join(os.path.dirname(bpy.data.filepath) if bpy.data.filepath else tempfile.gettempdir(), 'sequence')
os.makedirs(folder, exist_ok=True)
for old in os.listdir(folder):
    os.remove(os.path.join(folder, old))
scene.render.engine = 'BLENDER_EEVEE'; scene.eevee.taa_render_samples = 1; r.resolution_percentage = 2
r.filepath = os.path.join(folder, 'frame_'); bpy.ops.render.render(animation=True)
r.resolution_percentage = 100
scene.render.engine = 'CYCLES'; scene.cycles.samples = 128
bpy.context.view_layer.update()
"""

EXPLODED_FOLLOW_UP_SOLUTION = """
import bpy, math, os, tempfile, time
scene = bpy.context.scene
cam = scene.camera
cam.location = (0.16, -1.3, 0.35); cam.rotation_euler = (math.radians(80), 0, 0)
time.sleep(1.0)
folder = os.path.join(os.path.dirname(bpy.data.filepath) if bpy.data.filepath else tempfile.gettempdir(), 'sequence')
r = scene.render
scene.render.engine = 'BLENDER_EEVEE'; scene.eevee.taa_render_samples = 1; r.resolution_percentage = 2
r.filepath = os.path.join(folder, 'frame_'); bpy.ops.render.render(animation=True)
r.resolution_percentage = 100
scene.render.engine = 'CYCLES'
bpy.context.view_layer.update()
"""

# ================================================================== the tasks

SHOWCASE_TASKS = [
    t.Task("perfume_ad",
           "Use the perfume bottle and its box in this scene to create a luxury product photograph, set up for a "
           "final-quality Cycles render. Keep the bottle, the box and their materials unchanged. Place them on dark "
           "polished stone with soft reflections, light them with a warm spotlight, and put a subtle amber gradient "
           "in the background. Set up three cameras: a hero shot, a close-up, and an overhead composition, and leave "
           "the hero camera active. Output 1080x1080 PNG.",
           pj.checklist([("product kept", pj.req_product_kept), ("polished stone", req_stone_floor),
                         ("warm light", req_warm_light), ("amber gradient", req_amber_gradient),
                         ("three cameras", req_three_cameras), ("hero active", req_hero_active),
                         ("cycles", req_cycles), ("output", pj.output(1080, 1080))]),
           PERFUME_SOLUTION, setup=pj.setup_perfume, tags=["project", "showcase", "product", "lighting"],
           render_frames=[1], render_cameras=["*"], timeout=1200,
           follow_up="Change the setting to bright pastel pink with soft daylight, keeping the same bottle and "
                     "camera compositions.",
           follow_up_check=pj.checklist([("product kept", pj.req_product_kept), ("cameras kept", req_cameras_kept),
                                         ("pink setting", req_pink_setting), ("daylight", req_daylight),
                                         ("cycles", req_cycles)]),
           follow_up_solution=PERFUME_FOLLOW_UP_SOLUTION),

    t.Task("product_reveal",
           "Using the vintage camera product in this scene (the mesh objects named Camera_*), create an eight-second "
           "product reveal at 24 fps, set up for a final-quality Cycles render: start on a close-up of the lens, pull "
           "back while the product slowly rotates, and finish on a centred hero shot with the whole product in frame. "
           "Dark studio, rim lights that move during the shot, smooth camera motion. Keep the product's geometry and "
           "materials unchanged. Output 1920x1080 PNG.",
           pj.checklist([("product kept", req_camera_kept), ("timing", pj.timing(192, 24)), ("close start", req_close_start),
                         ("hero end", req_hero_end), ("rotates", req_product_rotates), ("smooth camera", req_smooth_camera),
                         ("dark studio", req_dark_studio), ("moving rim", req_moving_rim), ("cycles", req_cycles),
                         ("output", pj.output(1920, 1080))]),
           REVEAL_SOLUTION, setup=setup_camera_product, tags=["project", "showcase", "product", "animation"],
           render_frames=[1, 48, 96, 144, 192], timeout=1200,
           follow_up="Make the reveal feel energetic instead of luxurious: speed up the middle section of the camera "
                     "move and change the lighting to electric blue.",
           follow_up_check=pj.checklist([("product kept", req_camera_kept), ("timing", pj.timing(192, 24)),
                                         ("close start", req_close_start), ("hero end", req_hero_end),
                                         ("faster middle", req_faster_middle), ("blue lights", req_blue_lights),
                                         ("cycles", req_cycles)]),
           follow_up_solution=REVEAL_FOLLOW_UP_SOLUTION, remember=remember_reveal),

    t.Task("asset_pack",
           "Create a matching low-poly fantasy shop asset pack: three potion bottles, a treasure chest, a wooden "
           "shelf, a hanging sign and a barrel. Use a consistent hand-painted look with flat colours from one small "
           "palette and no image textures. Keep each prop a separate mesh object with a clear name, put every "
           "object's origin at the base of its prop, keep each prop under 2000 triangles, arrange them into an "
           "attractive preview with nothing overlapping, set the scene up for Cycles, and export the whole pack as "
           "asset_pack.glb next to this .blend file.",
           pj.checklist([("props named", req_props_named), ("low poly", req_low_poly), ("origins at base", req_origins_at_base),
                         ("palette", req_palette), ("arranged", req_arranged), ("glb", req_glb("asset_pack.glb")),
                         ("cycles", req_cycles)]),
           PACK_SOLUTION, setup=t.setup_empty, tags=["project", "showcase", "game", "modeling"], render_frames=[1], timeout=1200,
           follow_up="Turn the whole pack into an ice-themed variation while preserving each object's name, size and "
                     "origin. Export asset_pack.glb again.",
           follow_up_check=pj.checklist([("pack preserved", req_pack_preserved), ("icy palette", req_icy),
                                         ("re-exported", req_reexported("asset_pack.glb"))]),
           follow_up_solution=PACK_FOLLOW_UP_SOLUTION, remember=remember_pack),

    t.Task("dungeon_modules",
           "Build a small low-poly dungeon kit and assemble a room from it. Make four reusable modules on a 2 m "
           "grid: a floor tile (2 x 2 m), a wall segment, a doorway segment and a pillar, each modelled once and "
           "reused as linked duplicates sharing one mesh. Assemble one enclosed room of at least 4 x 4 tiles with "
           "exactly one doorway, put a treasure chest in the centre and a few glowing crystals around the room. "
           "Name every object by its module (Floor, Wall, Doorway, Pillar, Chest, Crystal). Add a camera walkthrough "
           "that starts outside the doorway and ends inside the room, 120 frames at 24 fps. Set the scene up for "
           "Cycles and export everything as dungeon.glb next to this .blend file, for Godot.",
           pj.checklist([("modules reused", req_modules_reused), ("on grid", req_on_grid), ("room", req_room),
                         ("enclosed", req_enclosed), ("one doorway", req_one_doorway), ("chest centred", req_chest_centred),
                         ("crystals glow", req_crystals), ("walkthrough", req_walkthrough), ("timing", pj.timing(120, 24)),
                         ("glb", req_glb("dungeon.glb")), ("cycles", req_cycles)]),
           DUNGEON_SOLUTION, setup=t.setup_empty, tags=["project", "showcase", "game", "environment"],
           render_frames=[1, 30, 60, 90, 120], timeout=1200,
           follow_up="Using the same modules, add a connected treasure room through a second doorway, without "
                     "changing anything in the original room.",
           follow_up_check=pj.checklist([("original untouched", req_original_untouched), ("second room", req_second_room),
                                         ("modules reused", req_modules_reused), ("on grid", req_on_grid)]),
           follow_up_solution=DUNGEON_FOLLOW_UP_SOLUTION, remember=remember_dungeon),

    t.Task("exploded_view",
           "Using the vintage camera product in this scene (the mesh objects named Camera_*), create a product "
           "animation for a scroll-driven website: 120 frames at 30 fps, 1920x1080, set up for Cycles. Start "
           "assembled, rotate the product into a side view, separate the lens, lens body and strap away from the "
           "body to reveal the parts, then bring them back together so the last frame matches the first. Keep the "
           "camera fixed and keep the whole product inside the right 60% of the frame, leaving the left 40% empty "
           "for website text. Render a preview image sequence at 25% resolution and low samples as numbered PNGs "
           "into a folder called sequence next to this .blend file.",
           pj.checklist([("product kept", req_camera_kept), ("assembled at both ends", req_assembled_ends),
                         ("exploded in the middle", req_exploded_middle), ("rotates to the side", req_rotates_to_side),
                         ("camera fixed", req_camera_fixed), ("right of frame", side_requirement(0.4, 1.0, "right")),
                         ("timing", pj.timing(120, 30)), ("output", pj.output(1920, 1080)), ("cycles", req_cycles),
                         ("sequence", req_sequence)]),
           EXPLODED_SOLUTION, setup=setup_camera_product, tags=["project", "showcase", "product", "animation", "web"],
           render_frames=[1, 30, 60, 90, 120], timeout=1200,
           follow_up="Mirror the composition: the product inside the left 60% of the frame and the empty space on "
                     "the right. Render the preview sequence again.",
           follow_up_check=pj.checklist([("assembled at both ends", req_assembled_ends), ("camera fixed", req_camera_fixed),
                                         ("left of frame", side_requirement(0.0, 0.6, "left")),
                                         ("sequence re-rendered", req_sequence_rerendered)]),
           follow_up_solution=EXPLODED_FOLLOW_UP_SOLUTION, remember=remember_sequence),
]
