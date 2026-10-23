"""bake_paint_masks and stylize_material: the painterly recipe's plumbing as two worker tools, so a
worker spends its steps on values, patches, strokes and accents, not on bake and shader code.

Eval 20260930-154037 (an Arcane chest, GPT-6.1 Sol workers): the painters had the whole recipe in their
prompt, some 1,100 tokens of bake, numpy and node notes, and used none of its techniques: no bake, no
normal map, no rim or AO node, no brush layer. Their own painter code was fragile, and the chest read as
vector camo. These are capabilities, not a "paint it" button: the worker still picks every value, patch,
stroke and accent, and paints the images itself.

bake_paint_masks(objects, key, resolution, edge_radius): per part (each material slot of a mesh
that has faces; a mesh with no slot is one part), three float Non-Color images, each from one Cycles EMIT
bake of every part (the mask at MASK_SAMPLES, the others at 1), the margin extended:
- <part>_mask: R edges (a Bevel node's normal against the shading normal: 1 on a bevel), G ambient
  occlusion (other objects included), B key facing, 0.5 + 0.5 N.L: 0.5 on the terminator. L points
  toward the key: a sun's axis or a given direction, else from each point toward the light.
- <part>_pos: metres from the corner of the part's own box, along the object's axes (scale applied).
- <part>_id: 0 off the part, 1 + a random value per loose piece (Geometry Random Per Island).
Each slot shows a temporary bake material for the bake and its own after: the worker's materials and
their node trees are never edited, so ownership tokens stay as they were (worker_child's guard), and
the slots, the render UV map made active, selection, mode and render settings are put back. Also per
part: its share of the image, its texel in mm (world area over UV area), and at the scene camera, texels
a screen px and what a 6-12 px brush is in mm and texels. MAX_TEXELS bounds a call's memory and time.

stylize_material(object, paint, slot, normal, orm, unlit, key, step, rim, ao): the slot's material rebuilt,
its nodes named NODE + what they do:
Mix Shader (Fac unlit) of [1] Principled (Base Color the paint, ORM's G and B as Roughness and Metallic,
Normal a tangent Normal Map of the stroke normal: no Bump after it, which glTF export took for the normal
map) and [2] Emission of (paint x step + rim) x AO: step = N.L (the Normal Map's normal) mapped from
terminator -+ softness to shadow..1 (step = [terminator, softness, shadow]); rim = Layer Weight Facing over
the last 2 x width, times the rim colour (rim = [r, g, b, width]); AO a live Ambient Occlusion node mapped
to 1 - ao..1. ORM's R feeds a glTF Material Output group's
Occlusion, so glTF gets occlusion too. A palette (stamped) material, or one other slots show too, is
copied first; running it again rebuilds the same nodes with the new values. The key's direction or
position is written into the nodes: run it again after moving the key.

The pure top half is tested in tests/test_paint_tools.py; harness/paint_tools_check.py runs the rest.
"""

import math
import re
import time

MAX_PARTS = 16
MAX_TEXELS = 16 * 1024 * 1024   # Parts x resolution^2 a call: three float images a part, 48 MB at 1024.
RESOLUTIONS = (128, 4096)
# The mask's AO and Bevel are sampled; position and pieces are exact at 1 sample. On the calibration chest
# (7 parts at 1024, harness probe 2026-09-30), against 16 samples of 16 AO and 16 Bevel rays at full size
# (98-121 s): these settings took 9.4 s, RMS off it R 0.076, G 0.037, B 0.029 (B only from scaling up).
MASK_SAMPLES = 8
AO_RAYS, BEVEL_RAYS = 4, 16     # Each node's own rays a sample: the edge channel is the noisy one.
MASK_TEXELS = 2 * 1024 * 1024   # The mask is smooth: past this many texels a call, baked at 1/2 or 1/4 and scaled up.
EDGE_FROM = 0.97                # Bevel normal . shading normal at or below this is a full edge.
EDGE_MAX, EDGE_SHARE = 0.015, 0.04   # Default edge radius: 4 % of the object, at most 15 mm...
AO_MAX, AO_SHARE = 0.15, 0.3         # ...AO distance: 30 % of it, at most 0.15 m.
BRUSH_PX = (6, 12)              # The recipe's brush width in screen px.
DEFAULT_KEY = (-0.5, -0.6, 0.62)  # Front left above, when the scene has no light.
NODE = "Paint "                 # stylize_material's node names start with this.
MASK = "lc_paint_mask"          # Custom property on a baked image: which kind.
DEFAULTS = {"unlit": 0.65, "terminator": 0.0, "softness": 0.3, "shadow": 0.72, "rim_width": 0.15, "ao": 0.5}
RIM = (0.19, 0.24, 0.35)        # Linear: 0.35x a cool rim light's colour (sRGB 0.75, 0.85, 1).
LIVE_AO = 0.2                   # m: the shader's own Ambient Occlusion distance.
RIM_BLEND = 0.3

SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "bake_paint_masks",
        "title": "Preparing paint guides",
        "description": (
            "Painterly step 1: per part (material slot) of meshes you own, bakes float Non-Color masks: edges, AO, key "
            "facing, position in metres, piece id. Returns their names and the texel scale. Materials stay as they are."),
        "parameters": {"type": "object", "properties": {
            "objects": {"type": "array", "items": {"type": "string"}},
            "key": {"type": "string", "description": "Light, or [x,y,z] toward it; default the brightest"},
            "resolution": {"type": "integer", "description": "Default 1024"},
            "edge_radius": {"type": "number", "description": "m"},
        }, "required": ["objects"]},
    }},
    {"type": "function", "function": {
        "name": "stylize_material",
        "title": "Updating a material",
        "description": (
            "Painterly step 3: rebuilds a slot's material from your images (a shared or palette one is copied first): "
            "Mix of Principled (paint, ORM, tangent normal; glTF-ready) and Emission (paint x stepped key + rim) x AO. "
            "Call again to update."),
        "parameters": {"type": "object", "properties": {
            "object": {"type": "string"},
            "slot": {"type": "integer"},
            "paint": {"type": "string", "description": "Image, or #rrggbb"},
            "normal": {"type": "string"},
            "orm": {"type": "string", "description": "R AO, G roughness, B metallic"},
            "unlit": {"type": "number", "description": "Emission's share"},
            "key": {"type": "string"},
            "step": {"type": "array", "items": {"type": "number"}, "description": "[terminator N.L, softness, shadow value]"},
            "rim": {"type": "array", "items": {"type": "number"}, "description": "[r, g, b, width]; [] none"},
            "ao": {"type": "number"},
        }, "required": ["object", "paint"]},
    }},
]


# ------------------------------------------------------------------ pure

def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def direction(value) -> tuple | None:
    """[x, y, z], or text holding three numbers ("0.3, -0.5, 0.8", "[1 0 1]"), as a unit vector; None
    when it is not one (a light's name). A zero vector is an error."""
    from .tools import ToolError
    if isinstance(value, str):
        found = re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", value)
        if len(found) != 3 or re.sub(r"[-+.\deE\s,\[\]()]", "", value):
            return None
        value = [float(f) for f in found]
    if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(_number(v) for v in value):
        return None
    length = math.sqrt(sum(v * v for v in value))
    if length < 1e-9:
        raise ToolError("key: a direction toward the light, not [0, 0, 0]")
    return tuple(v / length for v in value)


def pick_key(lights: list) -> str | None:
    """The key among (name, type, energy) rows: one named for it, else the brightest (a sun's
    strength counts 100x: 3-5 is a bright sun, 100-1000 W a bright lamp), else None."""
    named = [row for row in lights if "key" in row[0].lower()]
    if named:
        return named[0][0]
    if not lights:
        return None
    return max(lights, key=lambda row: row[2] * (100 if row[1] == "SUN" else 1))[0]


def colour(value) -> tuple | None:
    """#rrggbb (sRGB) as linear rgb; None when it is not one (an image's name)."""
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        return None
    srgb = [int(value.strip()[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in srgb)


def resolution_of(value) -> int:
    from .tools import ToolError
    if not _number(value) or int(value) != value or not RESOLUTIONS[0] <= value <= RESOLUTIONS[1]:
        raise ToolError(f"resolution: the image's size in px, {RESOLUTIONS[0]} to {RESOLUTIONS[1]}")
    return int(value)


def check_budget(parts: int, resolution: int) -> None:
    from .tools import ToolError
    if parts > MAX_PARTS:
        raise ToolError(f"{parts} parts: at most {MAX_PARTS} a call; bake the rest in another call.")
    if parts * resolution * resolution > MAX_TEXELS:
        most = max(1, MAX_TEXELS // (resolution * resolution))
        raise ToolError(f"{parts} parts at {resolution} px is past what one call bakes ({MAX_TEXELS // 2 ** 20}M "
                        f"texels): {most} at a time, or a smaller resolution.")


def mask_step(parts: int, resolution: int) -> int:
    """1, 2 or 4: how much smaller than the images the mask is baked, to keep the sampled bake at most
    MASK_TEXELS for the call (never under 128 px a side)."""
    step = 1
    while step < 4 and parts * (resolution // step) ** 2 > MASK_TEXELS and resolution // (2 * step) >= 128:
        step *= 2
    return step


def upsample(pixels, step: int):
    """(h, w, c) to (h step, w step, c), bilinear between texel centres, the border held."""
    import numpy as np
    if step == 1:
        return pixels
    height, width = pixels.shape[:2]

    def axis(n):
        at = (np.arange(n * step) + 0.5) / step - 0.5
        low = np.clip(np.floor(at).astype(int), 0, n - 1)
        return low, np.minimum(low + 1, n - 1), np.clip(at - low, 0, 1).astype(pixels.dtype)

    y0, y1, wy = axis(height)
    x0, x1, wx = axis(width)
    rows = pixels[y0] * (1 - wy)[:, None, None] + pixels[y1] * wy[:, None, None]
    return rows[:, x0] * (1 - wx)[None, :, None] + rows[:, x1] * wx[None, :, None]


def edge_default(largest: float, bevel_width: float = 0.0) -> float:
    """The Bevel node's radius: twice a Bevel modifier's width when there is one, else 4 % of the
    object's largest side; at most 15 mm unless the modifier's bevel is wider."""
    radius = min(EDGE_MAX, EDGE_SHARE * largest) if largest > 0 else EDGE_MAX
    return max(radius, 2 * bevel_width) if bevel_width > 0 else radius


def ao_default(largest: float) -> float:
    return min(AO_MAX, AO_SHARE * largest) if largest > 0 else AO_MAX


def px_per_metre(width: float, height: float, lens: float, sensor_width: float, sensor_height: float,
                 sensor_fit: str, depth: float, ortho_scale: float | None = None) -> float | None:
    """Screen px a metre spans at depth metres in front of the camera (render size in px). AUTO fits
    the sensor width to the image's longer side; an orthographic camera ignores depth."""
    if sensor_fit == "VERTICAL":
        side, sensor = height, sensor_height
    elif sensor_fit == "HORIZONTAL":
        side, sensor = width, sensor_width
    else:
        side, sensor = max(width, height), sensor_width
    if ortho_scale is not None:
        return side / ortho_scale if ortho_scale > 0 else None
    if depth <= 1e-6 or sensor <= 0:
        return None
    return side * lens / (sensor * depth)


def texel_line(texel_m: float, share: float, px_m: float | None, camera: str) -> str:
    """A part's scale facts: share of the image, texel size, and at the camera texels a screen px and
    the recipe's brush width in mm and texels."""
    text = f"{share * 100:.0f} % of the image, texel {texel_m * 1000:.2g} mm"
    if px_m:
        per_px = 1 / (texel_m * px_m)
        lo, hi = BRUSH_PX
        text += (f"; {camera}: {per_px:.2g} texels a screen px, a {lo}-{hi} px brush is "
                 f"{lo / px_m * 1000:.2g}-{hi / px_m * 1000:.2g} mm, {lo * per_px:.0f}-{hi * per_px:.0f} texels")
    return text


def part_label(obj_name: str, index: int, used: int) -> str:
    """Images are named for the part: the object alone when one slot has faces, else with the slot."""
    return obj_name if used <= 1 else f"{obj_name}_{index}"


def describe(name: str, arguments: dict) -> tuple[str, str] | None:
    """(summary, code) for the tool card."""
    if name == "bake_paint_masks":
        objects = arguments.get("objects")
        names = objects if isinstance(objects, list) else [objects]
        subject = ", ".join(map(str, names[:3])) + (f", +{len(names) - 3} more" if len(names) > 3 else "")
        return f"Bake paint masks for {subject or '?'}", ""
    if name == "stylize_material":
        return f"Stylise the material of {arguments.get('object', '?')} (slot {arguments.get('slot', 0)})", ""
    return None


def _numbers(value, name: str, most: int) -> list | None:
    from .tools import ToolError
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) > most or not all(_number(v) for v in value):
        raise ToolError(f"{name}: a list of up to {most} numbers")
    return [float(v) for v in value]


def values_of(unlit=None, step=None, rim=None, ao=None) -> dict:
    """stylize_material's numbers, defaults filled in and each in its range. step is [terminator, softness,
    shadow], a short list keeping the rest's defaults; rim is [r, g, b] or [r, g, b, width], [] for none."""
    from .tools import ToolError
    values = dict(DEFAULTS)
    step = _numbers(step, "step", 3) or []
    for name, value in zip(("terminator", "softness", "shadow"), step):
        values[name] = value
    rim = _numbers(rim, "rim", 4)
    if rim is not None and len(rim) not in (0, 3, 4):
        raise ToolError("rim: [r, g, b] or [r, g, b, width] (linear colour, width default 0.15); [] for none")
    if rim is not None and len(rim) == 4:
        values["rim_width"] = rim.pop()
    values["rim"] = RIM if rim is None else tuple(rim)
    for name, value in (("unlit", unlit), ("ao", ao)):
        if value is not None:
            if not _number(value):
                raise ToolError(f"{name}: a number from 0 to 1 (default {DEFAULTS[name]:g})")
            values[name] = float(value)
    ranges = {"unlit": (0, 1), "terminator": (-1, 1), "softness": (0.01, 1), "shadow": (0, 1), "rim_width": (0.01, 0.5),
              "ao": (0, 1)}
    for name, (low, high) in ranges.items():
        if not low <= values[name] <= high:
            where = "step's " if name in ("terminator", "softness", "shadow") else "rim's " if name == "rim_width" else ""
            raise ToolError(f"{where}{name}: {low:g} to {high:g} (default {DEFAULTS[name]:g}), not {values[name]:g}")
    if any(c < 0 for c in values["rim"]):
        raise ToolError("rim: a linear colour, each channel 0 or more")
    return values


# ------------------------------------------------------------------ Blender: shared

def _key(key, scene) -> tuple[str, tuple, str]:
    """("direction", unit vector toward the light, words) or ("point", the light's world position, words)."""
    import bpy
    from mathutils import Vector
    from .tools import ToolError
    bpy.context.view_layer.update()  # A light made in this step has an identity matrix until then.
    vector = direction(key) if key not in (None, "") else None
    if vector is not None:
        return "direction", vector, f"direction ({_v(vector)})"
    if key in (None, ""):
        lights = [(o.name, o.data.type, float(o.data.energy)) for o in scene.objects
                  if o.type == "LIGHT" and not o.hide_render]
        key = pick_key(lights)
        if key is None:
            unit = direction(DEFAULT_KEY)
            return "direction", unit, f"no light in the scene: direction ({_v(unit)}), front left above"
    if not isinstance(key, str):
        raise ToolError("key: a light's name, or [x, y, z] toward the light")
    light = bpy.data.objects.get(key)
    if light is None or light.type != "LIGHT":
        found = ", ".join(o.name for o in scene.objects if o.type == "LIGHT")[:200] or "none"
        raise ToolError(f"key: {key!r} is not a light (lights: {found}); or give [x, y, z] toward the light")
    matrix = light.matrix_world
    if light.data.type == "SUN":
        axis = matrix.to_3x3() @ Vector((0, 0, 1))
        unit = direction(tuple(axis))
        return "direction", unit, f"sun {light.name}, toward ({_v(unit)})"
    position = tuple(matrix.translation)
    return "point", position, f"{light.data.type.lower()} light {light.name}, toward its position ({_v(position)}) from each point"


def _v(values) -> str:
    return ", ".join(f"{v:.2f}" for v in values)


def _facing(tree, normal, key):
    """N.L as a Vector Math dot product: L a constant, or per point toward the light's position."""
    dot = _node(tree, "ShaderNodeVectorMath", operation="DOT_PRODUCT")
    tree.links.new(normal, dot.inputs[0])
    kind, vector, _ = key
    if kind == "point":
        geometry = _node(tree, "ShaderNodeNewGeometry")
        toward = _node(tree, "ShaderNodeVectorMath", operation="SUBTRACT")
        toward.inputs[0].default_value = vector
        tree.links.new(geometry.outputs["Position"], toward.inputs[1])
        unit = _node(tree, "ShaderNodeVectorMath", operation="NORMALIZE")
        tree.links.new(toward.outputs["Vector"], unit.inputs[0])
        tree.links.new(unit.outputs["Vector"], dot.inputs[1])
        return dot, (geometry, toward, unit)
    dot.inputs[1].default_value = vector
    return dot, ()


def _node(tree, kind: str, name: str = "", location=None, operation: str = "", **inputs):
    node = tree.nodes.new(kind)
    if name:
        node.name = node.label = name
    if operation:
        node.operation = operation
    if location is not None:
        node.location = location
    for socket, value in inputs.items():
        node.inputs[socket.replace("_", " ")].default_value = value
    return node


def _render_uv(mesh):
    layers = mesh.uv_layers
    return next((layer for layer in layers if layer.active_render), None) or (layers[0] if len(layers) else None)


# ------------------------------------------------------------------ Blender: bake_paint_masks

def _parts(obj, depsgraph) -> list[dict]:
    """The object's parts with faces, from the evaluated mesh (what the bake sees): slot index, local
    box, world area and area on the render UV map."""
    import numpy as np
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        count = len(mesh.loop_triangles)
        uv_layer = _render_uv(mesh)
        if not count or uv_layer is None:
            return []
        loops = np.empty(count * 3, np.int32)
        mesh.loop_triangles.foreach_get("loops", loops)
        verts = np.empty(count * 3, np.int32)
        mesh.loop_triangles.foreach_get("vertices", verts)
        slots = np.empty(count, np.int32)
        mesh.loop_triangles.foreach_get("material_index", slots)
        co = np.empty(len(mesh.vertices) * 3, np.float64)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        uv = np.empty(len(mesh.loops) * 2, np.float64)
        uv_layer.uv.foreach_get("vector", uv)
        uv = uv.reshape(-1, 2)
    finally:
        evaluated.to_mesh_clear()
    matrix = np.array(obj.matrix_world)
    world = co @ matrix[:3, :3].T + matrix[:3, 3]
    tri = world[verts.reshape(-1, 3)]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    t = uv[loops.reshape(-1, 3)]
    uv_area = 0.5 * np.abs((t[:, 1, 0] - t[:, 0, 0]) * (t[:, 2, 1] - t[:, 0, 1])
                           - (t[:, 2, 0] - t[:, 0, 0]) * (t[:, 1, 1] - t[:, 0, 1]))
    slots = np.clip(slots, 0, max(len(obj.material_slots) - 1, 0))
    parts = []
    for index in np.unique(slots):
        mine = slots == index
        local = co[verts.reshape(-1, 3)[mine].ravel()]
        box = world[verts.reshape(-1, 3)[mine].ravel()]
        parts.append({"slot": int(index), "low": local.min(axis=0), "high": local.max(axis=0),
                      "centre": (box.min(axis=0) + box.max(axis=0)) / 2, "area": float(area[mine].sum()),
                      "uv_area": float(uv_area[mine].sum())})
    return parts


def _bevel_width(obj) -> float:
    widths = [m.width for m in obj.modifiers if m.type == "BEVEL" and m.show_render and m.offset_type == "OFFSET"]
    return max(widths) * max(obj.matrix_world.to_scale()) if widths else 0.0


def _mask_image(name: str, kind: str, size: int):
    """A float Non-Color image for a mask: the one of this name a bake made before when it fits, else new.
    Tagged temporary (ownership.TEMP, as inspect_uv's checker): never in a worker's result or the merge,
    which would otherwise pack and carry three float images a part (ownership._kept)."""
    import bpy
    from .ownership import TEMP
    image = bpy.data.images.get(name)
    if image is not None and image.get(MASK) == kind and image.is_float and tuple(image.size) == (size, size):
        return image
    image = bpy.data.images.new(name, size, size, alpha=False, float_buffer=True)
    image.colorspace_settings.name = "Non-Color"
    image[MASK] = kind
    image[TEMP] = True
    return image


def _bake_tree(material, kind: str, image, part: dict, key, scale) -> None:
    tree = material.node_tree
    tree.nodes.clear()
    output = _node(tree, "ShaderNodeOutputMaterial")
    emission = _node(tree, "ShaderNodeEmission")
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    if kind == "mask":
        geometry = _node(tree, "ShaderNodeNewGeometry")
        bevel = _node(tree, "ShaderNodeBevel", Radius=part["edge"])
        bevel.samples = BEVEL_RAYS
        flat = _node(tree, "ShaderNodeVectorMath", operation="DOT_PRODUCT")
        tree.links.new(bevel.outputs["Normal"], flat.inputs[0])
        tree.links.new(geometry.outputs["Normal"], flat.inputs[1])
        edge = _node(tree, "ShaderNodeMapRange", From_Min=1.0, From_Max=EDGE_FROM)
        tree.links.new(flat.outputs["Value"], edge.inputs["Value"])
        occlusion = _node(tree, "ShaderNodeAmbientOcclusion", Distance=part["ao"])
        occlusion.samples = AO_RAYS
        facing, _ = _facing(tree, geometry.outputs["Normal"], key)
        half = _node(tree, "ShaderNodeMapRange", From_Min=-1.0, From_Max=1.0)
        tree.links.new(facing.outputs["Value"], half.inputs["Value"])
        combine = _node(tree, "ShaderNodeCombineColor")
        tree.links.new(edge.outputs["Result"], combine.inputs["Red"])
        tree.links.new(occlusion.outputs["AO"], combine.inputs["Green"])
        tree.links.new(half.outputs["Result"], combine.inputs["Blue"])
        tree.links.new(combine.outputs["Color"], emission.inputs["Color"])
    elif kind == "pos":
        coordinates = _node(tree, "ShaderNodeTexCoord")
        metres = _node(tree, "ShaderNodeVectorMath", operation="MULTIPLY_ADD")
        tree.links.new(coordinates.outputs["Object"], metres.inputs[0])
        metres.inputs[1].default_value = scale
        metres.inputs[2].default_value = tuple(-low * s for low, s in zip(part["low"], scale))
        tree.links.new(metres.outputs["Vector"], emission.inputs["Color"])
    else:
        geometry = _node(tree, "ShaderNodeNewGeometry")
        piece = _node(tree, "ShaderNodeMath", operation="ADD")
        tree.links.new(geometry.outputs["Random Per Island"], piece.inputs[0])
        piece.inputs[1].default_value = 1.0
        tree.links.new(piece.outputs["Value"], emission.inputs["Color"])
    texture = _node(tree, "ShaderNodeTexImage")
    texture.image = image
    tree.nodes.active = texture


class _Baking:
    """Every part's slot on a temporary bake material, the parts' objects alone selected and the render
    UV map active, Cycles; all of it put back after, the temporary materials removed."""

    def __init__(self, parts: list[dict]):
        self.parts = parts

    def __enter__(self):
        import bpy
        scene = bpy.context.scene
        layer = bpy.context.view_layer
        self.scene, self.layer = scene, layer
        self.selected = [o for o in layer.objects if o.select_get()]
        self.active = layer.objects.active
        self.render = (scene.render.engine, scene.cycles.samples, scene.render.bake.margin,
                       scene.render.bake.margin_type, scene.render.bake.use_clear, scene.render.bake.target,
                       scene.render.bake.use_selected_to_active)
        self.slots, self.added, self.uv, self.shown = [], [], {}, []
        objects = list(dict.fromkeys(part["object"] for part in self.parts))
        for obj in objects:
            if obj.hide_get():
                obj.hide_set(False)
                self.shown.append(obj)
            uv = _render_uv(obj.data)
            if obj.data not in self.uv:
                self.uv[obj.data] = obj.data.uv_layers.active.name if obj.data.uv_layers.active else None
            obj.data.uv_layers.active = uv
        for part in self.parts:
            obj = part["object"]
            material = bpy.data.materials.new("lc_paint_bake")
            part["bake_material"] = material
            if not obj.material_slots:
                obj.data.materials.append(material)
                self.added.append(obj)
            else:
                slot = obj.material_slots[part["slot"]]
                self.slots.append((slot, slot.material, slot.link, obj, part["slot"]))
                slot.material = material
        for obj in self.selected:
            obj.select_set(False)
        for obj in objects:
            obj.select_set(True)
        layer.objects.active = objects[0]
        scene.render.engine = "CYCLES"
        bake = scene.render.bake
        bake.target, bake.use_clear, bake.use_selected_to_active, bake.margin_type = "IMAGE_TEXTURES", True, False, "EXTEND"
        return self

    def __exit__(self, *exc):
        import bpy
        scene, layer = self.scene, self.layer
        for slot, material, link, obj, index in reversed(self.slots):
            obj.material_slots[index].material = material
        for obj in self.added:
            obj.data.materials.pop(index=len(obj.data.materials) - 1)
        for part in self.parts:
            material = part.pop("bake_material", None)
            if material is not None:
                bpy.data.materials.remove(material)
        for mesh, name in self.uv.items():
            if name is not None and mesh.uv_layers.get(name) is not None:
                mesh.uv_layers.active = mesh.uv_layers[name]
        for obj in layer.objects:
            obj.select_set(obj in self.selected)
        for obj in self.shown:
            obj.hide_set(True)
        layer.objects.active = self.active
        bake = scene.render.bake
        (scene.render.engine, scene.cycles.samples, bake.margin, bake.margin_type, bake.use_clear, bake.target,
         bake.use_selected_to_active) = self.render
        return False


def _not_bakeable(targets) -> list[str]:
    import bpy
    bpy.context.view_layer.update()  # An object linked in this step is in the view layer after it.
    problems = []
    for obj in targets:
        if obj.type != "MESH":
            problems.append(f"{obj.name} is not a mesh ({obj.type})")
        elif obj.library is not None or obj.data.library is not None:
            problems.append(f"{obj.name}'s mesh is linked from a library")
        elif obj.name not in bpy.context.view_layer.objects:
            problems.append(f"{obj.name} is not in this view layer")
        elif not obj.data.uv_layers:
            problems.append(f"{obj.name} has no UV map: uv_unwrap it first")
    return problems


def _px_m(camera, scene, centre) -> float | None:
    from mathutils import Vector
    if camera is None:
        return None
    data, render = camera.data, scene.render
    scale = render.resolution_percentage / 100
    depth = -(camera.matrix_world.inverted() @ Vector(centre)).z
    return px_per_metre(render.resolution_x * scale, render.resolution_y * scale, data.lens, data.sensor_width,
                        data.sensor_height, data.sensor_fit, depth, data.ortho_scale if data.type == "ORTHO" else None)


def _bake_all(parts: list, key, scene, step: int, margin: int) -> dict:
    """The three bakes, every part in each: seconds each took."""
    import bpy
    from .tools import ToolError
    times = {}
    for kind, samples in (("mask", MASK_SAMPLES), ("pos", 1), ("id", 1)):
        for part in parts:
            scale = tuple(part["object"].matrix_world.to_scale())
            target = part["small"] if kind == "mask" and step > 1 else part["images"][kind]
            _bake_tree(part["bake_material"], kind, target, part, key, scale)
        scene.cycles.samples = samples
        began = time.perf_counter()
        try:
            bpy.ops.object.bake(type="EMIT", margin=max(2, margin // step) if kind == "mask" else margin,
                                margin_type="EXTEND", use_clear=True, target="IMAGE_TEXTURES",
                                use_selected_to_active=False)
        except RuntimeError as ex:
            raise ToolError(f"The bake failed: {str(ex).strip()}") from None
        times[kind] = time.perf_counter() - began
    return times


def bake_paint_masks(objects: list, key=None, resolution: int = 1024, edge_radius=None):
    """The tool (see SCHEMAS and the module docstring)."""
    import bpy
    import numpy as np
    from . import scene_diff, tools
    from .camera import _undo_push
    from .tools import ToolError, ToolResult
    from .uv_tools import _named, _ObjectMode
    targets = _named(objects)
    resolution = resolution_of(resolution)
    if edge_radius is not None and (not _number(edge_radius) or not 0 < edge_radius <= 1):
        raise ToolError("edge_radius: metres, above 0 and at most 1 (default 2x a Bevel modifier's width)")
    problems = _not_bakeable(targets)
    if problems:
        raise ToolError("bake_paint_masks refused: " + "; ".join(problems) + ".")
    scene = bpy.context.scene
    view = scene.camera
    keyed = _key(key, scene)
    depsgraph = bpy.context.evaluated_depsgraph_get()
    parts, owners, notes = [], {}, []
    for obj in list(targets):  # A mesh two objects share has one set of slots: baked once.
        if obj.data in owners:
            notes.append(f"{obj.name} shares {owners[obj.data].name}'s mesh: paint it from {owners[obj.data].name}'s masks.")
            targets.remove(obj)
        owners.setdefault(obj.data, obj)
    for obj in targets:
        found = _parts(obj, depsgraph)
        largest = max(obj.dimensions) if len(obj.dimensions) else 0.0
        edge = float(edge_radius) if edge_radius is not None else edge_default(largest, _bevel_width(obj))
        for part in found:
            part.update(object=obj, used=len(found), edge=edge, ao=ao_default(largest))
        parts += found
    empty = [o.name for o in targets if not any(p["object"] == o for p in parts)]
    if empty:
        raise ToolError(f"bake_paint_masks: {', '.join(empty)} has no faces to bake.")
    check_budget(len(parts), resolution)
    margin = max(4, resolution // 128)
    step = mask_step(len(parts), resolution)
    for part in parts:
        label = part_label(part["object"].name, part["slot"], part["used"])
        part["label"] = label
        part["images"] = {kind: _mask_image(f"{label}_{kind}", kind, resolution) for kind in ("mask", "pos", "id")}
        if step > 1:
            part["small"] = _mask_image(f"{label}_mask_{resolution // step}", "small", resolution // step)
    before = scene_diff.snapshot()
    _undo_push("Before Loopcut: bake paint masks")
    started = time.perf_counter()
    try:
        with tools._editor_context("VIEW_3D"), _ObjectMode(), _Baking(parts):
            times = _bake_all(parts, keyed, scene, step, margin)
        for part in parts:
            small = part.get("small")
            if small is not None:
                size = resolution // step
                pixels = np.empty(size * size * 4, np.float32)
                small.pixels.foreach_get(pixels)
                part["images"]["mask"].pixels.foreach_set(upsample(pixels.reshape(size, size, 4), step).ravel())
    finally:
        for part in parts:
            small = part.pop("small", None)
            if small is not None:
                bpy.data.images.remove(small)
    total = time.perf_counter() - started
    scaled = f" at 1/{step} size, scaled up" if step > 1 else ""
    lines = [f"Baked {len(parts)} part{'s' * (len(parts) != 1)} at {resolution} px in {total:.1f} s (mask{scaled} "
             f"{times['mask']:.1f} s at {MASK_SAMPLES} samples, pos {times['pos']:.1f} s, id {times['id']:.1f} s; margin "
             f"{margin} px). Key: {keyed[2]}."]
    for part in parts:
        obj, images = part["object"], part["images"]
        ids = np.empty(resolution * resolution * 4, np.float32)
        images["id"].pixels.foreach_get(ids)
        pieces = len(np.unique(np.round(ids[0::4][ids[0::4] > 0.5], 5)))
        material = obj.material_slots[part["slot"]].material if obj.material_slots else None
        size = [(h - lo) * s for lo, h, s in zip(part["low"], part["high"], obj.matrix_world.to_scale())]
        texel = math.sqrt(part["area"] / max(part["uv_area"] * resolution * resolution, 1e-12))
        px_m = _px_m(view, scene, part["centre"])
        lines.append(
            f"- {part['label']} (slot {part['slot']}, {material.name if material else 'no material'}): "
            f"{images['mask'].name}, {images['pos'].name} (box {' x '.join(f'{v:.3g}' for v in size)} m), "
            f"{images['id'].name} ({pieces} piece{'s' * (pieces != 1)}); edges {part['edge'] * 1000:.3g} mm, AO "
            f"{part['ao']:.3g} m; {texel_line(texel, min(part['uv_area'], 1.0), px_m, view.name if view else '')}.")
    lines += notes
    lines.append("Float Non-Color, (h, w, 4) by pixels.foreach_get: mask R edges, G AO, B key facing (0.5 terminator, "
                 "1 facing it); pos metres from the box corner on the object's axes; id 0 off the part. Temporary: "
                 "never merged; remove them once painted.")
    return ToolResult("\n".join(lines), scene_before=before, scene_after=scene_diff.snapshot())


# ------------------------------------------------------------------ Blender: stylize_material

def _image(name, what: str):
    import bpy
    from .tools import ToolError
    if not name:
        return None
    if not isinstance(name, str):
        raise ToolError(f"{what}: an image's name")
    image = bpy.data.images.get(name)
    if image is None:
        near = [i.name for i in bpy.data.images if i.type == "IMAGE" and not i.get(MASK)][:8]
        raise ToolError(f"{what}: no image {name!r}" + (f" (images: {', '.join(near)})" if near else ""))
    return image


def _material_for(obj, index: int) -> tuple:
    """(the slot's material to rebuild, words on where it came from): a new one for an empty slot, the
    slot's own when only it shows it, else a copy (a palette material, or one shared with other slots)."""
    import bpy
    from . import ownership
    if not obj.material_slots:
        material = bpy.data.materials.new(f"{obj.name}_Paint")
        obj.data.materials.append(material)
        return material, f"new material {material.name}"
    slot = obj.material_slots[index]
    material = slot.material
    if material is None:
        material = bpy.data.materials.new(f"{obj.name}_Paint" + (f"_{index}" if index else ""))
        slot.material = material
        return material, f"new material {material.name}"
    shown = sum(1 for o in bpy.data.objects for s in o.material_slots if s.material == material)
    if material.library is None and not material.get(ownership.STAMP) and shown <= 1:
        return material, f"material {material.name}, rebuilt in place"
    copy = material.copy()
    copy.name = f"{obj.name}_{material.name}"
    slot.material = copy
    why = "linked" if material.library else "a palette material" if material.get(ownership.STAMP) else "shared"
    return copy, f"material {copy.name}, a copy of {material.name} ({why}), now this slot's"


def _gltf_group():
    """The glTF exporter's settings group (any group with an Occlusion input works for it)."""
    import bpy
    group = next((g for g in bpy.data.node_groups if g.name.lower().startswith("gltf material output")
                  and any(getattr(i, "name", "") == "Occlusion" for i in g.interface.items_tree)), None)
    if group is None:
        group = bpy.data.node_groups.new("glTF Material Output", "ShaderNodeTree")
        group.interface.new_socket("Occlusion", in_out="INPUT", socket_type="NodeSocketFloat")
    return group


def _non_color(image, notes: list) -> None:
    if image is not None and image.colorspace_settings.name != "Non-Color":
        notes.append(f"{image.name} was {image.colorspace_settings.name}: now Non-Color (it holds data, not colour).")
        image.colorspace_settings.name = "Non-Color"


def _build(tree, base, normal_image, orm_image, values: dict, key, uv_name: str) -> list[str]:
    """The stylised nodes; returns what feeds what, for the result."""
    links = tree.links
    tree.nodes.clear()
    x = lambda column: column * 220 - 1300  # noqa: E731
    output = _node(tree, "ShaderNodeOutputMaterial", NODE + "Output", (x(6), 0))
    mix = _node(tree, "ShaderNodeMixShader", NODE + "Mix", (x(5), 0), Fac=values["unlit"])
    bsdf = _node(tree, "ShaderNodeBsdfPrincipled", NODE + "BSDF", (x(4), 300))
    links.new(mix.outputs["Shader"], output.inputs["Surface"])
    links.new(bsdf.outputs["BSDF"], mix.inputs[1])
    if isinstance(base, tuple):
        paint = _node(tree, "ShaderNodeRGB", NODE + "Base", (x(0), 300))
        paint.outputs["Color"].default_value = (*base, 1.0)
        wired = [f"Base Color: flat {_v(base)} (linear)"]
    else:
        paint = _node(tree, "ShaderNodeTexImage", NODE + "Base", (x(0), 300))
        paint.image = base
        wired = [f"Base Color: {base.name} ({base.colorspace_settings.name})"]
    colour_out = paint.outputs["Color"]
    links.new(colour_out, bsdf.inputs["Base Color"])
    if orm_image is not None:
        orm = _node(tree, "ShaderNodeTexImage", NODE + "ORM", (x(0), 0))
        orm.image = orm_image
        split = _node(tree, "ShaderNodeSeparateColor", NODE + "ORM Split", (x(1), 0))
        links.new(orm.outputs["Color"], split.inputs["Color"])
        links.new(split.outputs["Green"], bsdf.inputs["Roughness"])
        links.new(split.outputs["Blue"], bsdf.inputs["Metallic"])
        gltf = _node(tree, "ShaderNodeGroup", NODE + "glTF", (x(2), -150))
        gltf.node_tree = _gltf_group()
        links.new(split.outputs["Red"], gltf.inputs["Occlusion"])
        wired.append(f"Roughness, Metallic: {orm_image.name} G, B (R is glTF occlusion)")
    else:
        bsdf.inputs["Metallic"].default_value = 0.0
        wired.append("Roughness 0.5, Metallic 0 (no ORM)")
    if normal_image is not None:
        image = _node(tree, "ShaderNodeTexImage", NODE + "Normal", (x(0), -300))
        image.image = normal_image
        normal_map = _node(tree, "ShaderNodeNormalMap", NODE + "Normal Map", (x(1), -300))
        normal_map.space = "TANGENT"
        normal_map.uv_map = uv_name
        links.new(image.outputs["Color"], normal_map.inputs["Color"])
        links.new(normal_map.outputs["Normal"], bsdf.inputs["Normal"])
        normal = normal_map.outputs["Normal"]
        wired.append(f"Normal: Normal Map (tangent{', ' + uv_name if uv_name else ''}) of {normal_image.name}")
    else:
        normal = _node(tree, "ShaderNodeNewGeometry", NODE + "Geometry", (x(1), -300)).outputs["Normal"]
    facing, helpers = _facing(tree, normal, key)
    facing.name = facing.label = NODE + "Key"
    facing.location = (x(2), -300)
    for i, helper in enumerate(helpers):
        helper.name = helper.label = NODE + ("Key Position", "Key Toward", "Key Unit")[i]
        helper.location = (x(i), -550)
    low, high = values["terminator"] - values["softness"], values["terminator"] + values["softness"]
    step = _node(tree, "ShaderNodeMapRange", NODE + "Step", (x(3), -300), From_Min=low, From_Max=high,
                 To_Min=values["shadow"], To_Max=1.0)
    links.new(facing.outputs["Value"], step.inputs["Value"])
    lit = _node(tree, "ShaderNodeVectorMath", NODE + "Lit", (x(4), -100), operation="MULTIPLY")
    links.new(colour_out, lit.inputs[0])
    links.new(step.outputs["Result"], lit.inputs[1])
    total = lit.outputs["Vector"]
    words = (f"step = N.L ({'the normal map' if normal_image is not None else 'the shading normal'}) from {low:.2f} "
             f"to {high:.2f} -> {values['shadow']:.2f}..1, key {key[2]}")
    formula = "paint x step"
    if values["rim"] and any(values["rim"]):
        weight = _node(tree, "ShaderNodeLayerWeight", NODE + "Rim Weight", (x(2), -800), Blend=RIM_BLEND)
        width = values["rim_width"]
        band = _node(tree, "ShaderNodeMapRange", NODE + "Rim Band", (x(3), -800), From_Min=1 - 2 * width,
                     From_Max=1 - width)
        links.new(weight.outputs["Facing"], band.inputs["Value"])
        rim = _node(tree, "ShaderNodeVectorMath", NODE + "Rim", (x(4), -800), operation="SCALE")
        rim.inputs[0].default_value = values["rim"]
        links.new(band.outputs["Result"], rim.inputs["Scale"])
        add = _node(tree, "ShaderNodeVectorMath", NODE + "Rim Add", (x(5), -300), operation="ADD")
        links.new(total, add.inputs[0])
        links.new(rim.outputs["Vector"], add.inputs[1])
        total = add.outputs["Vector"]
        formula += " + rim"
        words += f"; rim {_v(values['rim'])} over Facing {1 - 2 * width:.2f}..{1 - width:.2f}"
    if values["ao"] > 0:
        occlusion = _node(tree, "ShaderNodeAmbientOcclusion", NODE + "AO", (x(3), -1050), Distance=LIVE_AO)
        strength = _node(tree, "ShaderNodeMapRange", NODE + "AO Range", (x(4), -1050), To_Min=1 - values["ao"])
        links.new(occlusion.outputs["AO"], strength.inputs["Value"])
        shade = _node(tree, "ShaderNodeVectorMath", NODE + "AO Mix", (x(5), -550), operation="MULTIPLY")
        links.new(total, shade.inputs[0])
        links.new(strength.outputs["Result"], shade.inputs[1])
        total = shade.outputs["Vector"]
        formula = f"({formula}) x AO"
        words += f"; AO {LIVE_AO:g} m -> {1 - values['ao']:.2f}..1"
    emission = _node(tree, "ShaderNodeEmission", NODE + "Emission", (x(5), -150))
    links.new(total, emission.inputs["Color"])
    links.new(emission.outputs["Emission"], mix.inputs[2])
    return [f"Mix Shader Fac {values['unlit']:.2f}: [1] Principled: " + "; ".join(wired),
            f"[2] Emission = {formula}: {words}"]


def stylize_material(object: str, paint: str, slot: int = 0, normal: str = "", orm: str = "",  # noqa: A002
                     unlit=None, key=None, step=None, rim=None, ao=None):
    """The tool (see SCHEMAS and the module docstring)."""
    import bpy
    from . import scene_diff
    from .camera import _undo_push
    from .tools import ToolError, ToolResult
    name = object
    obj = bpy.data.objects.get(name) if isinstance(name, str) else None
    if obj is None:
        raise ToolError(f"object: no object {name!r}")
    if not hasattr(obj.data, "materials"):
        raise ToolError(f"{obj.name} is a {obj.type}: it has no materials")
    if obj.library is not None or obj.data.library is not None:
        raise ToolError(f"{obj.name} is linked from a library")
    if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < max(len(obj.material_slots), 1):
        raise ToolError(f"slot: 0 to {max(len(obj.material_slots), 1) - 1} ({obj.name} has "
                        f"{len(obj.material_slots)} material slots)")
    values = values_of(unlit, step, rim, ao)
    base = colour(paint)
    if base is None:
        if not paint or not isinstance(paint, str):
            raise ToolError("paint: the base colour image's name, or #rrggbb for a flat part")
        base = _image(paint, "paint")
    normal_image, orm_image = _image(normal, "normal"), _image(orm, "orm")
    keyed = _key(key, bpy.context.scene)
    before = scene_diff.snapshot()
    _undo_push("Before Loopcut: stylise material")
    notes = []
    for image in (normal_image, orm_image):
        _non_color(image, notes)
    if not isinstance(base, tuple) and base.colorspace_settings.name == "Non-Color":
        notes.append(f"{base.name} is Non-Color: its values show as linear colour.")
    material, where = _material_for(obj, slot)
    uv = _render_uv(obj.data) if obj.type == "MESH" else None
    if normal_image is not None and uv is None:
        notes.append(f"{obj.name} has no UV map: the normal map has no tangents (uv_unwrap it).")
    count = len(material.node_tree.nodes)
    wired = _build(material.node_tree, base, normal_image, orm_image, values, keyed, uv.name if uv else "")
    shared = [o.name for o in bpy.data.objects if o != obj and o.data == obj.data]
    if shared and obj.material_slots[slot].link == "DATA":
        notes.append(f"{', '.join(shared[:5])} share{'s' * (len(shared) == 1)} this mesh and show it too.")
    lines = [f"{obj.name} slot {slot}: {where}; replaced its {count} nodes with {len(material.node_tree.nodes)} named "
             f"{NODE.strip()} *.", *wired, *notes,
             "glTF export carries Base Color, ORM and the normal map, not the Emission half. The key is written "
             "in: call again after moving it."]
    return ToolResult("\n".join(lines), scene_before=before, scene_after=scene_diff.snapshot())


DISPATCH = {"bake_paint_masks": bake_paint_masks, "stylize_material": stylize_material}
