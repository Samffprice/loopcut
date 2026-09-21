"""The eval tasks: a prompt, the scene it starts from, and a check that reads the resulting scene.

Checks measure what the user would see, never how the model got there: world-space bounds of the
evaluated geometry, not object names or origins, because the model is free to name, join and
parent things however it likes. Every task also carries a reference `solution`, so selfcheck.py
can prove without a model that each check fails on the untouched scene and passes on a correct one.
A wrong check is worse than none: it would send us tuning prompts and tools against noise.

Runs inside Blender.
"""

import math
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import bpy
from mathutils import Vector

EPS = 0.02        # Meters. Placement the user would call exact.
CONTACT = 0.05    # Meters. Parts this close are touching.


# ------------------------------------------------------------------ reading the scene

def bounds(obj) -> tuple[Vector, Vector]:
    """World-space box of the evaluated geometry (modifiers applied)."""
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    corners = [evaluated.matrix_world @ Vector(c) for c in evaluated.bound_box]
    return (Vector(min(c[i] for c in corners) for i in range(3)),
            Vector(max(c[i] for c in corners) for i in range(3)))


def snapshot() -> dict:
    """What the scene looked like before the agent touched it."""
    bpy.context.view_layer.update()
    shot = {}
    for obj in bpy.context.scene.objects:
        low, high = bounds(obj)
        shot[obj.name] = {"type": obj.type, "min": tuple(low), "max": tuple(high),
                          "matrix": [tuple(row) for row in obj.matrix_world]}
    return shot


def meshes() -> list:
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def new_objects(before: dict, kind: str = "MESH") -> list:
    return [o for o in bpy.context.scene.objects if o.type == kind and o.name not in before]


def parts(objects) -> list[tuple[Vector, Vector]]:
    """World-space boxes of every connected piece of mesh, so a table joined into one object
    and a table made of five objects read the same."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    boxes = []
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            parent = list(range(len(mesh.vertices)))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            for edge in mesh.edges:
                a, b = find(edge.vertices[0]), find(edge.vertices[1])
                if a != b:
                    parent[a] = b
            islands: dict[int, list[Vector]] = {}
            for vertex in mesh.vertices:
                islands.setdefault(find(vertex.index), []).append(evaluated.matrix_world @ vertex.co)
            for points in islands.values():
                boxes.append((Vector(min(p[i] for p in points) for i in range(3)),
                              Vector(max(p[i] for p in points) for i in range(3))))
        finally:
            evaluated.to_mesh_clear()
    return boxes


def visible_material(obj):
    """The material on most of the faces: what the user sees. A material appended to a second
    slot while the faces still use the first changes nothing on screen."""
    slots = obj.material_slots
    if not slots:
        return None
    index = 0
    if obj.type == "MESH" and len(slots) > 1 and obj.data.polygons:
        used = [0] * len(slots)
        for polygon in obj.data.polygons:
            used[min(polygon.material_index, len(slots) - 1)] += 1
        index = used.index(max(used))
    return slots[index].material


def principled(obj):
    material = visible_material(obj)
    if material is not None and material.node_tree:
        for node in material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                return node
    return None


def base_color(obj) -> tuple[float, float, float] | None:
    material, node = visible_material(obj), principled(obj)
    if material is None:
        return None
    if node is not None and not node.inputs["Base Color"].is_linked:
        return tuple(node.inputs["Base Color"].default_value[:3])
    return tuple(material.diffuse_color[:3])


def close(a, b, tolerance=EPS) -> bool:
    return all(abs(x - y) <= tolerance for x, y in zip(a, b))


def unchanged(before: dict, name: str, tolerance=1e-3) -> bool:
    obj = bpy.context.scene.objects.get(name)
    if obj is None:
        return False
    flat_now = [v for row in obj.matrix_world for v in row]
    flat_then = [v for row in before[name]["matrix"] for v in row]
    return close(flat_now, flat_then, tolerance)


# ------------------------------------------------------------------ scenes to start from

def _remove(name: str) -> None:
    obj = bpy.data.objects.get(name)
    if obj is not None:
        bpy.data.objects.remove(obj, do_unlink=True)


def _add_mesh(name: str, kind: str, location) -> bpy.types.Object:
    import bmesh
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    if kind == "cube":
        bmesh.ops.create_cube(bm, size=2.0)
    elif kind == "sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=1.0)
    elif kind == "cone":
        bmesh.ops.create_cone(bm, cap_ends=True, segments=16, radius1=1.0, radius2=0.0, depth=2.0)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def setup_empty() -> None:
    _remove("Cube")


def setup_three_meshes() -> None:
    _add_mesh("Ball", "sphere", (4, 0, 0))
    _add_mesh("Spike", "cone", (-4, 0, 0))


def setup_cube_out_of_frame() -> None:
    bpy.data.objects["Cube"].location = (9, 12, 0)


def setup_scaled_cube() -> None:
    bpy.data.objects["Cube"].scale = (2.0, 1.0, 0.5)


def setup_selection() -> None:
    _remove("Cube")
    for name, x in (("A", -3), ("B", 0), ("C", 3)):
        _add_mesh(name, "cube", (x, 0, 0))
    for obj in bpy.context.scene.objects:
        obj.select_set(obj.name == "B")
    bpy.context.view_layer.objects.active = bpy.data.objects["B"]


# ------------------------------------------------------------------ checks
# Each returns the list of things that are wrong; empty means pass. `ctx` has `before` (the
# snapshot) and `session` (None under selfcheck, where there is no conversation to read).

def check_red_sphere(ctx) -> list[str]:
    cube_low, cube_high = Vector(ctx.before["Cube"]["min"]), Vector(ctx.before["Cube"]["max"])
    added = new_objects(ctx.before)
    if not added:
        return ["no new mesh object"]
    problems = []
    for obj in added:
        low, high = bounds(obj)
        size, center = high - low, (low + high) / 2
        color = base_color(obj)
        problems = []
        if max(size) - min(size) > 0.1 * max(size) or len(obj.data.vertices) < 40:
            problems.append(f"{obj.name} is not a sphere (size {tuple(round(s, 2) for s in size)})")
        if color is None or not (color[0] > 0.5 and color[1] < 0.3 and color[2] < 0.3):
            problems.append(f"{obj.name} is not red (base color {color})")
        if low.z < cube_high.z - EPS:
            problems.append(f"{obj.name} bottom z={low.z:.2f} is not above the cube top z={cube_high.z:.2f}")
        if not (cube_low.x <= center.x <= cube_high.x and cube_low.y <= center.y <= cube_high.y):
            problems.append(f"{obj.name} is not over the cube")
        if not problems:
            break
    if not unchanged(ctx.before, "Cube"):
        problems.append("the cube was moved or removed")
    return problems


def check_table(ctx) -> list[str]:
    boxes = parts(meshes())
    if len(boxes) < 5:
        return [f"expected a top and four legs, found {len(boxes)} separate piece(s)"]
    top = max(boxes, key=lambda b: b[0].z)
    legs = [b for b in boxes if b is not top and abs(b[0].z) <= CONTACT]
    problems = []
    if not 0.6 <= top[1].z <= 0.95:
        problems.append(f"table is {top[1].z:.2f} m tall, asked for about 0.75")
    if len(legs) < 4:
        problems.append(f"{len(legs)} legs stand on the ground (z=0), need 4")
    for low, high in legs:
        if not (top[0].z - CONTACT <= high.z <= top[1].z + EPS):
            problems.append(f"a leg ends at z={high.z:.2f} but the top starts at z={top[0].z:.2f}")
        if low.x < top[0].x - EPS or high.x > top[1].x + EPS or low.y < top[0].y - EPS or high.y > top[1].y + EPS:
            problems.append("a leg sticks out from under the top")
    return problems


def check_taller(ctx) -> list[str]:
    low, high = bounds(bpy.data.objects["Cube"])
    problems = []
    if abs(low.z - -1.0) > EPS:
        problems.append(f"base moved to z={low.z:.2f}, was -1.00")
    if abs(high.z - 3.0) > EPS:
        problems.append(f"top is at z={high.z:.2f}, twice as tall means 3.00")
    if not close((low.x, low.y, high.x, high.y), (-1, -1, 1, 1)):
        problems.append("width or depth changed")
    return problems


def check_collection(ctx) -> list[str]:
    props = bpy.data.collections.get("Props")
    if props is None:
        return ["no collection named Props"]
    problems = []
    if props not in bpy.context.scene.collection.children_recursive:
        problems.append("Props is not part of the scene")
    for obj in bpy.context.scene.objects:
        inside = obj.name in props.objects
        if obj.type == "MESH" and not inside:
            problems.append(f"{obj.name} is not in Props")
        if obj.type != "MESH" and inside:
            problems.append(f"{obj.name} ({obj.type}) should not be in Props")
    if {"Cube", "Ball", "Spike"} - {o.name for o in meshes()}:
        problems.append("a mesh object went missing")
    return problems


def check_glossy_blue(ctx) -> list[str]:
    cube = bpy.data.objects["Cube"]
    color, node = base_color(cube), principled(cube)
    problems = []
    if color is None or not (color[2] > 0.4 and color[2] > color[0] + 0.2 and color[2] > color[1] + 0.1):
        problems.append(f"base color {color} is not blue")
    if node is None or node.inputs["Roughness"].default_value > 0.35:
        problems.append("not glossy: roughness should be low")
    return problems


def check_second_cube(ctx) -> list[str]:
    problems = [] if unchanged(ctx.before, "Cube") else ["the original cube was changed"]
    for obj in new_objects(ctx.before):
        low, high = bounds(obj)
        if close((low + high) / 2, (3, 0, 0), CONTACT) and close(high - low, (2, 2, 2), CONTACT):
            return problems
    return problems + ["no new 2 m cube centered at x=3"]


def check_bevel(ctx) -> list[str]:
    bevels = [m for m in bpy.data.objects["Cube"].modifiers if m.type == "BEVEL"]
    if not bevels:
        return ["no bevel modifier on the cube"]
    problems = []
    if bevels[0].segments != 3:
        problems.append(f"segments is {bevels[0].segments}, asked for 3")
    if abs(bevels[0].width - 0.05) > 1e-4:
        problems.append(f"width is {bevels[0].width:.3f} m, asked for 0.05")
    return problems


def check_smooth_ball(ctx) -> list[str]:
    cube = bpy.data.objects["Cube"]
    problems = []
    if not any(m.type == "SUBSURF" and m.levels >= 2 for m in cube.modifiers):
        problems.append("no subdivision surface modifier with at least 2 levels")
    if not all(p.use_smooth for p in cube.data.polygons):
        problems.append("faces are not shaded smooth")
    return problems


def check_array(ctx) -> list[str]:
    cube = bpy.data.objects["Cube"]
    arrays = [m for m in cube.modifiers if m.type == "ARRAY"]
    if not arrays:
        return ["no array modifier on the cube"]
    low, high = bounds(cube)
    problems = []
    if arrays[0].count != 5:
        problems.append(f"count is {arrays[0].count}, asked for 5")
    if abs((high.x - low.x) - 14.0) > CONTACT:
        problems.append(f"row is {high.x - low.x:.2f} m long; 5 cubes 3 m apart is 14.00")
    if abs((high.y - low.y) - 2.0) > CONTACT or abs((high.z - low.z) - 2.0) > CONTACT:
        problems.append("the row is not along X only")
    return problems


def check_animation(ctx) -> list[str]:
    scene, cube = bpy.context.scene, bpy.data.objects["Cube"]
    saved = scene.frame_current
    try:
        xs = {}
        for frame in (1, 24, 48):
            scene.frame_set(frame)
            xs[frame] = cube.matrix_world.translation.x
    finally:
        scene.frame_set(saved)
    problems = []
    if abs(xs[1]) > EPS:
        problems.append(f"x is {xs[1]:.2f} at frame 1, asked for 0")
    if abs(xs[48] - 5.0) > EPS:
        problems.append(f"x is {xs[48]:.2f} at frame 48, asked for 5")
    if not 0.5 < xs[24] < 4.5:
        problems.append(f"x is {xs[24]:.2f} at frame 24; it should be on the way")
    return problems


def check_camera(ctx) -> list[str]:
    from bpy_extras.object_utils import world_to_camera_view
    scene = bpy.context.scene
    camera = scene.camera
    if camera is None:
        return ["the scene has no active camera"]
    cube = bpy.data.objects["Cube"]
    problems = [] if unchanged(ctx.before, "Cube") else ["the cube was moved instead of the camera"]
    outside = 0
    for corner in cube.bound_box:
        view = world_to_camera_view(scene, camera, cube.matrix_world @ Vector(corner))
        if not (0.0 <= view.x <= 1.0 and 0.0 <= view.y <= 1.0 and view.z > 0.0):
            outside += 1
    if outside:
        problems.append(f"{outside} of the cube's 8 corners are outside the camera frame")
    return problems


def check_sun(ctx) -> list[str]:
    suns = [o for o in new_objects(ctx.before, "LIGHT") if o.data.type == "SUN"]
    if not suns:
        return ["no new sun light"]
    sun = suns[0]
    problems = []
    if abs(sun.data.energy - 3.0) > 1e-3:
        problems.append(f"strength is {sun.data.energy}, asked for 3")
    direction = sun.matrix_world.to_3x3() @ Vector((0, 0, -1))
    if direction.normalized().z > -0.999:
        problems.append("the sun does not point straight down")
    return problems


def check_rig(ctx) -> list[str]:
    rig = bpy.data.objects.get("Rig")
    if rig is None or rig.type != "EMPTY":
        return ["no empty named Rig"]
    problems = []
    if rig.matrix_world.translation.length > EPS:
        problems.append("Rig is not at the origin")
    for name in ("Cube", "Camera", "Light"):
        obj = bpy.data.objects.get(name)
        if obj is None or obj.parent is not rig:
            problems.append(f"{name} is not parented to Rig")
        elif not unchanged(ctx.before, name):
            problems.append(f"{name} moved when it was parented")
    return problems


def check_apply_scale(ctx) -> list[str]:
    cube = bpy.data.objects["Cube"]
    low, high = bounds(cube)
    problems = []
    if not close(cube.scale, (1, 1, 1), 1e-4):
        problems.append(f"scale is still {tuple(round(s, 3) for s in cube.scale)}")
    if not close(tuple(low) + tuple(high), ctx.before["Cube"]["min"] + ctx.before["Cube"]["max"]):
        problems.append("the cube changed size or place")
    return problems


def check_text(ctx) -> list[str]:
    texts = new_objects(ctx.before, "FONT")
    if not texts:
        return ["no text object"]
    text = texts[0]
    problems = []
    if text.data.body.strip() != "LOOPCUT":
        problems.append(f"text says {text.data.body!r}")
    if abs(text.data.extrude - 0.1) > 1e-3:
        problems.append(f"extrude is {text.data.extrude}, asked for 0.1")
    facing = (text.matrix_world.to_3x3() @ Vector((0, 0, 1))).normalized()
    if facing.y > -0.99:
        problems.append("the text does not stand upright facing the front view (-Y)")
    return problems


def check_top_face(ctx) -> list[str]:
    mesh = bpy.data.objects["Cube"].data
    if bpy.context.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")  # Edit-mode changes reach mesh.polygons only on exit.
    problems = []
    if len(mesh.polygons) != 5:
        problems.append(f"the cube has {len(mesh.polygons)} faces, expected 5")
    if any(p.normal.z > 0.9 and p.center.z > 0.9 for p in mesh.polygons):
        problems.append("the top face is still there")
    return problems


def check_delete_selected(ctx) -> list[str]:
    names = {o.name for o in bpy.context.scene.objects}
    problems = []
    if "B" in names:
        problems.append("B (the selected object) is still there")
    for keep in ("A", "C", "Camera", "Light"):
        if keep not in names:
            problems.append(f"{keep} was deleted too")
    return problems


def check_stack(ctx) -> list[str]:
    boxes = sorted(parts(meshes()), key=lambda b: b[0].z)
    cubes = [b for b in boxes if close(b[1] - b[0], (1, 1, 1), CONTACT)]
    if len(cubes) != 3:
        return [f"expected three 1 m cubes, found {len(cubes)}"]
    problems = []
    for level, (low, high) in enumerate(cubes):
        if abs(low.z - level) > EPS:
            problems.append(f"cube {level + 1} starts at z={low.z:.2f}, expected {level}.00")
        if not close(((low + high) / 2).xy, ((cubes[0][0] + cubes[0][1]) / 2).xy, CONTACT):
            problems.append(f"cube {level + 1} is not centered over the bottom one")
    return problems


def check_scatter(ctx) -> list[str]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    owners = [o for o in meshes() if any(m.type == "NODES" and m.node_group for m in o.modifiers)]
    if not owners:
        return ["no object with a geometry nodes modifier"]
    low, high = bounds(owners[0])
    counts: dict[str, int] = {}
    for instance in depsgraph.object_instances:
        if instance.is_instance and instance.parent is not None:
            counts[instance.parent.original.name] = counts.get(instance.parent.original.name, 0) + 1
    count = max((counts.get(o.name, 0) for o in owners), default=0)
    problems = []
    if not 30 <= count <= 80:
        problems.append(f"{count} instances, asked for about 50")
    if max(high.x - low.x, high.y - low.y) < 9.0:
        problems.append("the plane is not 10 m")
    return problems


def check_render_settings(ctx) -> list[str]:
    scene = bpy.context.scene
    render = scene.render
    wanted = {
        "resolution_x": (render.resolution_x, 1280), "resolution_y": (render.resolution_y, 720),
        "resolution_percentage": (render.resolution_percentage, 100), "fps": (render.fps, 30),
        "frame_start": (scene.frame_start, 1), "frame_end": (scene.frame_end, 120),
        "file_format": (render.image_settings.file_format, "PNG"),
        "color_mode": (render.image_settings.color_mode, "RGBA"),
        "film_transparent": (render.film_transparent, True),
    }
    return [f"{key} is {got!r}, asked for {want!r}" for key, (got, want) in wanted.items() if got != want]


def check_question(ctx) -> list[str]:
    problems = []
    now = snapshot()
    if set(now) != set(ctx.before) or any(not unchanged(ctx.before, name) for name in ctx.before):
        problems.append("the scene was changed by a question")
    if ctx.session is not None:
        answer = " ".join(i["text"] for i in ctx.session["items"] if i["kind"] == "assistant")
        problems += [f"the answer does not mention {name}" for name in ("Cube", "Camera", "Light")
                     if name.lower() not in answer.lower()]
        if any(i["kind"] == "tool" and i["name"] == "run_python" for i in ctx.session["items"]):
            problems.append("ran code to answer a question get_scene_info answers")
    return problems


def check_checker(ctx) -> list[str]:
    node = principled(bpy.data.objects["Cube"])
    if node is None:
        return ["the cube has no Principled material"]
    seen, queue, checker = set(), [node.inputs["Base Color"]], None
    while queue and checker is None:
        for link in queue.pop().links:
            source = link.from_node
            if source.type == "TEX_CHECKER":
                checker = source
                break
            if source.name not in seen:
                seen.add(source.name)
                queue.extend(source.inputs)
    if checker is None:
        return ["no checker texture feeds the base color"]
    problems = []
    if abs(checker.inputs["Scale"].default_value - 8.0) > 1e-3:
        problems.append(f"checker scale is {checker.inputs['Scale'].default_value}, asked for 8")
    colors = sorted((tuple(checker.inputs[name].default_value[:3]) for name in ("Color1", "Color2")), key=sum)
    if sum(colors[0]) > 0.15:
        problems.append(f"no black squares (darker color is {colors[0]})")
    if not (colors[1][0] > 0.6 and colors[1][1] > 0.5 and colors[1][2] < 0.3):
        problems.append(f"no yellow squares (lighter color is {colors[1]})")
    return problems


# ------------------------------------------------------------------ the tasks

@dataclass
class Task:
    id: str
    prompt: str
    check: Callable
    solution: str
    setup: Callable | None = None
    passes_untouched: bool = False  # True when doing nothing is the right answer.
    tags: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)  # Image files attached to the prompt.
    render_frames: list[int] = field(default_factory=list)  # Frames grade.py renders for the judges.
    render_cameras: list[str] = field(default_factory=list)  # Names, or ["*"] for every scene camera.
    # Two-turn tasks: a second instruction sent once the first is done, checked with the scene
    # as it was after turn 1 (ctx.stage1, a snapshot) and whatever `remember` computed on that
    # scene (ctx.memory), so "keep the cameras" and "faster than before" can be measured.
    follow_up: str = ""
    follow_up_check: Callable | None = None
    follow_up_solution: str = ""
    remember: Callable | None = None
    timeout: float = 0.0  # Seconds per brief; 0 means run.py's --timeout. Showcase briefs get 20 minutes.


# ------------------------------------------------------------------ copying a reference image

REFERENCE_IMAGE = str(Path(tempfile.gettempdir()) / "loopcut-eval-reference.png")
_REFERENCE_SCENE = (
    "import bpy\n"
    "def colored(obj, name, rgb):\n"
    "    mat = bpy.data.materials.new(name); mat.use_nodes = True\n"
    "    mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (*rgb, 1)\n"
    "    obj.data.materials.append(mat)\n"
    "bpy.ops.mesh.primitive_plane_add(size=4, location=(0, 0, 0)); ground = bpy.context.object\n"
    "ground.name = 'Ground'; colored(ground, 'Blue', (0.05, 0.2, 0.8))\n"
    "bpy.ops.mesh.primitive_cube_add(size=1, location=(-1, 0, 0.5)); box = bpy.context.object\n"
    "box.name = 'RedBox'; colored(box, 'Red', (0.8, 0.05, 0.05))\n"
    "bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(1, 0, 0.5)); ball = bpy.context.object\n"
    "ball.name = 'GreenBall'; colored(ball, 'Green', (0.05, 0.7, 0.1))\n"
)


def setup_reference() -> None:
    """Render the reference from a known scene, then put the scene back to just camera and light,
    so the agent starts from nothing and the image is all it has."""
    bpy.data.objects.remove(bpy.data.objects["Cube"])
    exec(compile(_REFERENCE_SCENE, "<reference>", "exec"), {"__name__": "__reference__"})
    try:
        from loopcut import tools
        shot = tools.capture_viewport(angle="three_quarter")
        shutil.copy(shot.image_path, REFERENCE_IMAGE)
    except Exception as ex:  # No viewport in a background selfcheck; the check needs no image.
        print(f"reference image not rendered: {ex}")
    for name in ("Ground", "RedBox", "GreenBall"):
        bpy.data.objects.remove(bpy.data.objects[name])
    for name in ("Blue", "Red", "Green"):
        bpy.data.materials.remove(bpy.data.materials[name])


def _dominant(color, channel: int) -> bool:
    return color is not None and color[channel] > 0.35 and all(color[c] < 0.6 * color[channel] for c in range(3) if c != channel)


def check_match_reference(ctx) -> list[str]:
    added = new_objects(ctx.before)
    ground = [o for o in added if _dominant(base_color(o), 2)]
    boxes = [o for o in added if _dominant(base_color(o), 0)]
    balls = [o for o in added if _dominant(base_color(o), 1)]
    problems = []
    if len(ground) != 1:
        problems.append(f"expected one blue ground, found {[o.name for o in ground]}")
    if len(boxes) != 1:
        problems.append(f"expected one red box, found {[o.name for o in boxes]}")
    if len(balls) != 1:
        problems.append(f"expected one green ball, found {[o.name for o in balls]}")
    if problems:
        return problems
    g_low, g_high = bounds(ground[0])
    if g_high.z - g_low.z > 0.1 or min(g_high.x - g_low.x, g_high.y - g_low.y) < 3:
        problems.append(f"ground is not a large flat surface: {tuple(round(v, 2) for v in g_high - g_low)}")
    for obj, what in ((boxes[0], "box"), (balls[0], "ball")):
        low, high = bounds(obj)
        size = high - low
        if not all(0.6 <= s <= 1.5 for s in size):
            problems.append(f"{what} is not about 1 m: {tuple(round(s, 2) for s in size)}")
        if abs(low.z - g_high.z) > CONTACT:
            problems.append(f"{what} does not rest on the ground (bottom {low.z:.2f}, ground top {g_high.z:.2f})")
    if len(balls[0].data.vertices) < 40:
        problems.append("ball is not round")
    b_low, b_high = bounds(boxes[0])
    s_low, s_high = bounds(balls[0])
    apart = ((b_low + b_high) / 2 - (s_low + s_high) / 2).xy.length
    if not 1.2 <= apart <= 3.5:
        problems.append(f"box and ball are {apart:.2f} m apart, expected side by side")
    return problems


_CUBE = "import bpy\ncube = bpy.data.objects['Cube']\n"
_NEW_CUBE = (
    "import bpy, bmesh\n"
    "def cube(name, size, location):\n"
    "    mesh = bpy.data.meshes.new(name)\n"
    "    bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.0); bm.to_mesh(mesh); bm.free()\n"
    "    obj = bpy.data.objects.new(name, mesh)\n"
    "    obj.scale, obj.location = size, location\n"
    "    bpy.context.scene.collection.objects.link(obj)\n"
    "    return obj\n"
)

TASKS = [
    Task("red_sphere", "Add a red sphere above the cube.", check_red_sphere, _CUBE + (
        "bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(0, 0, 2))\n"
        "mat = bpy.data.materials.new('Red'); mat.use_nodes = True\n"
        "mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.8, 0.02, 0.02, 1)\n"
        "bpy.context.object.data.materials.append(mat)\n"), tags=["create", "material"]),

    Task("table", "Build a simple wooden table standing on the ground (z=0): a top and four legs, "
                  "about 0.75 m tall.", check_table, _NEW_CUBE + (
        "cube('Top', (1.2, 0.7, 0.05), (0, 0, 0.725))\n"
        "for i, (x, y) in enumerate(((0.55, 0.3), (-0.55, 0.3), (0.55, -0.3), (-0.55, -0.3))):\n"
        "    cube(f'Leg{i}', (0.06, 0.06, 0.7), (x, y, 0.35))\n"), setup=setup_empty, tags=["create", "spatial"]),

    Task("taller", "Make the cube twice as tall without moving its base.", check_taller, _CUBE + (
        "cube.scale.z = 2\ncube.location.z = 1\n"), tags=["transform", "spatial"]),

    Task("collection", "Put all mesh objects into a new collection called Props.", check_collection, (
        "import bpy\n"
        "props = bpy.data.collections.new('Props')\n"
        "bpy.context.scene.collection.children.link(props)\n"
        "for obj in bpy.context.scene.objects:\n"
        "    if obj.type == 'MESH':\n"
        "        for other in obj.users_collection:\n"
        "            other.objects.unlink(obj)\n"
        "        props.objects.link(obj)\n"), setup=setup_three_meshes, tags=["organize"]),

    Task("glossy_blue", "Give the cube a glossy blue material.", check_glossy_blue, _CUBE + (
        "mat = bpy.data.materials.new('Blue'); mat.use_nodes = True\n"
        "node = mat.node_tree.nodes['Principled BSDF']\n"
        "node.inputs['Base Color'].default_value = (0.02, 0.1, 0.8, 1)\n"
        "node.inputs['Roughness'].default_value = 0.1\n"
        "cube.data.materials.clear(); cube.data.materials.append(mat)\n"), tags=["material"]),

    Task("second_cube", "Add a second cube 3 m to the right (+X) of the existing one, center to center.",
         check_second_cube, _NEW_CUBE + "cube('Cube2', (2, 2, 2), (3, 0, 0))\n", tags=["create", "spatial"]),

    Task("bevel", "Add a bevel modifier to the cube: 3 segments, 5 cm wide.", check_bevel, _CUBE + (
        "mod = cube.modifiers.new('Bevel', 'BEVEL'); mod.segments = 3; mod.width = 0.05\n"), tags=["modifier"]),

    Task("smooth_ball", "Make the cube look like a smooth ball with a subdivision surface modifier "
                        "and smooth shading.", check_smooth_ball, _CUBE + (
        "mod = cube.modifiers.new('Subdivision', 'SUBSURF'); mod.levels = 3; mod.render_levels = 3\n"
        "cube.data.polygons.foreach_set('use_smooth', [True] * len(cube.data.polygons))\n"), tags=["modifier"]),

    Task("array", "Use an Array modifier to make a row of 5 cubes along X, 3 m apart center to center.",
         check_array, _CUBE + (
        "mod = cube.modifiers.new('Array', 'ARRAY'); mod.count = 5\n"
        "mod.use_relative_offset = False; mod.use_constant_offset = True\n"
        "mod.constant_offset_displace = (3, 0, 0)\n"), tags=["modifier", "spatial"]),

    Task("animate", "Animate the cube moving from x=0 at frame 1 to x=5 at frame 48.", check_animation,
         _CUBE + (
        "cube.location.x = 0; cube.keyframe_insert('location', frame=1)\n"
        "cube.location.x = 5; cube.keyframe_insert('location', frame=48)\n"), tags=["animation"]),

    Task("camera", "Point the camera at the cube so the whole cube is in frame. Do not move the cube.",
         check_camera, _CUBE + (
        "from mathutils import Vector\n"
        "camera = bpy.context.scene.camera\n"
        "camera.location = cube.location + Vector((8, -8, 6))\n"
        "camera.rotation_euler = (cube.location - camera.location).to_track_quat('-Z', 'Y').to_euler()\n"),
         setup=setup_cube_out_of_frame, tags=["camera", "spatial"]),

    Task("sun", "Add a sun light with strength 3 pointing straight down.", check_sun, (
        "import bpy\n"
        "data = bpy.data.lights.new('Sun', 'SUN'); data.energy = 3\n"
        "sun = bpy.data.objects.new('Sun', data); sun.location = (0, 0, 5)\n"
        "bpy.context.scene.collection.objects.link(sun)\n"), tags=["light"]),

    Task("rig", "Add an empty named Rig at the origin and parent the cube, the camera and the light "
                "to it without moving them.", check_rig, (
        "import bpy\n"
        "rig = bpy.data.objects.new('Rig', None)\n"
        "bpy.context.scene.collection.objects.link(rig)\n"
        "for name in ('Cube', 'Camera', 'Light'):\n"
        "    bpy.data.objects[name].parent = rig\n"), tags=["organize", "transform"]),

    Task("apply_scale", "Apply the cube's scale.", check_apply_scale, _CUBE + (
        "from mathutils import Matrix\n"
        "cube.data.transform(Matrix.Diagonal((*cube.scale, 1.0)))\n"
        "cube.scale = (1, 1, 1)\n"), setup=setup_scaled_cube, tags=["transform"]),

    Task("text", "Add 3D text that says LOOPCUT, extruded 0.1 m, standing upright and readable from "
                 "the front view.", check_text, (
        "import bpy, math\n"
        "data = bpy.data.curves.new('Title', 'FONT'); data.body = 'LOOPCUT'; data.extrude = 0.1\n"
        "text = bpy.data.objects.new('Title', data); text.location = (-2, -3, 0)\n"
        "text.rotation_euler = (math.radians(90), 0, 0)\n"
        "bpy.context.scene.collection.objects.link(text)\n"), tags=["create"]),

    Task("top_face", "Delete the top face of the cube.", check_top_face, _CUBE + (
        "import bmesh\n"
        "bm = bmesh.new(); bm.from_mesh(cube.data)\n"
        "top = [f for f in bm.faces if f.normal.z > 0.9]\n"
        "bmesh.ops.delete(bm, geom=top, context='FACES')\n"
        "bm.to_mesh(cube.data); bm.free()\n"), tags=["mesh"]),

    Task("delete_selected", "Delete the selected object.", check_delete_selected, (
        "import bpy\n"
        "for obj in list(bpy.context.selected_objects):\n"
        "    bpy.data.objects.remove(obj, do_unlink=True)\n"), setup=setup_selection, tags=["context"]),

    Task("stack", "Stack three 1 m cubes on top of each other, touching, the bottom one resting on the "
                  "ground (z=0).", check_stack, _NEW_CUBE + (
        "for level in range(3):\n"
        "    cube(f'Block{level}', (1, 1, 1), (0, 0, level + 0.5))\n"), setup=setup_empty,
         tags=["create", "spatial"]),

    Task("scatter", "Using geometry nodes, scatter about 50 small spheres over a 10 m plane.", check_scatter, (
        "import bpy\n"
        "bpy.ops.mesh.primitive_plane_add(size=10)\n"
        "plane = bpy.context.object\n"
        "tree = bpy.data.node_groups.new('Scatter', 'GeometryNodeTree')\n"
        "tree.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')\n"
        "tree.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')\n"
        "n = tree.nodes\n"
        "gin, gout = n.new('NodeGroupInput'), n.new('NodeGroupOutput')\n"
        "points = n.new('GeometryNodeDistributePointsOnFaces')\n"
        "points.inputs['Density'].default_value = 0.5\n"
        "sphere = n.new('GeometryNodeMeshUVSphere'); sphere.inputs['Radius'].default_value = 0.1\n"
        "inst = n.new('GeometryNodeInstanceOnPoints'); join = n.new('GeometryNodeJoinGeometry')\n"
        "l = tree.links\n"
        "l.new(gin.outputs[0], points.inputs['Mesh'])\n"
        "l.new(points.outputs['Points'], inst.inputs['Points'])\n"
        "l.new(sphere.outputs['Mesh'], inst.inputs['Instance'])\n"
        "l.new(gin.outputs[0], join.inputs[0]); l.new(inst.outputs[0], join.inputs[0])\n"
        "l.new(join.outputs[0], gout.inputs[0])\n"
        "plane.modifiers.new('Scatter', 'NODES').node_group = tree\n"), setup=setup_empty,
         tags=["geometry_nodes", "api"]),

    Task("render_settings", "Set up the render: 1280x720 at 100%, 30 fps, frames 1 to 120, PNG output "
                            "with a transparent background.", check_render_settings, (
        "import bpy\n"
        "scene = bpy.context.scene; r = scene.render\n"
        "r.resolution_x, r.resolution_y, r.resolution_percentage, r.fps = 1280, 720, 100, 30\n"
        "scene.frame_start, scene.frame_end = 1, 120\n"
        "r.image_settings.file_format = 'PNG'; r.image_settings.color_mode = 'RGBA'\n"
        "r.film_transparent = True\n"), tags=["settings"]),

    Task("question", "What objects are in this scene?", check_question, "", passes_untouched=True,
         tags=["chat"]),

    Task("match_reference", "Recreate what you see in the attached reference image as closely as you can: the "
         "same objects, colors, sizes and arrangement, standing on the ground.", check_match_reference,
         _REFERENCE_SCENE, setup=setup_reference, attachments=[REFERENCE_IMAGE], tags=["reference", "vision"]),
    Task("checker", "Give the cube a black and yellow checker material, checker scale 8.", check_checker,
         _CUBE + (
        "mat = bpy.data.materials.new('Checker'); mat.use_nodes = True\n"
        "tree = mat.node_tree\n"
        "checker = tree.nodes.new('ShaderNodeTexChecker')\n"
        "checker.inputs['Color1'].default_value = (0, 0, 0, 1)\n"
        "checker.inputs['Color2'].default_value = (1, 0.8, 0, 1)\n"
        "checker.inputs['Scale'].default_value = 8\n"
        "tree.links.new(checker.outputs['Color'], tree.nodes['Principled BSDF'].inputs['Base Color'])\n"
        "cube.data.materials.clear(); cube.data.materials.append(mat)\n"), tags=["material", "api"]),
]

# projects.py needs the helpers above, so it is imported once they exist; import `tasks`, never
# `projects` on its own.
from projects import PROJECT_TASKS  # noqa: E402
from showcase import SHOWCASE_TASKS  # noqa: E402

TASKS += PROJECT_TASKS + SHOWCASE_TASKS
BY_ID = {task.id: task for task in TASKS}
