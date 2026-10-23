"""inspect_texture: which images a material shows, through which nodes, on which UV map, and whether
a paint edit landed, read from the data rather than judged from a picture.

Eval 20260929-235231: bottle workers read cream from their paint while their looks showed grey, and
spent some twenty steps repainting, rebinding and refreshing images to find out which was true;
get_object_info left out the images and their packing. For a material, an image or an object:

- each Image Texture node, the path from its outputs through the nodes it feeds (Mix, Map Range,
  Bump...) to the shader socket it ends in, and the UV map its Vector comes from (a UV Map node,
  Texture Coordinate UV or nothing: the map that renders), through a Mapping node when there is one;
- each image: size, colour space, packed or file, unsaved edits, and a hash of its pixel buffer
  that any paint edit changes (blake2b of the float pixels);
- samples at [u, v] points, or at faces of an object (the face's centre and its mean over points
  spread across it, on the map and through the mapping its material uses), the way the node reads
  the image: its interpolation (Closest, else bilinear) and extension (repeat, extend, clip, mirror).
  Values are as stored: sRGB-encoded for an sRGB image, linear for a float one;
- thumbnail: up to MAX_THUMBS images side by side, each at most THUMB px, alpha dropped;
- for an object, paint stats per material slot from its Base Color image over the slot's UV area
  (paint_stats.py): value levels, patch size, fine noise, accents and their value match, with an
  advisory verdict.
Images past MAX_TEXELS are named but not sampled or hashed: a float copy of an 8K image is a
gigabyte. Paint stats read them anyway, up to MAX_REDUCED, box-averaged to at most REDUCED a side
through a temporary file-backed buffer (_Pixels.reduced): disk cache instead of memory, unsaved
paint included (Image.copy() regenerates or reloads, dropping it; measured in 5.2).
The pure top half is tested in tests/test_texture_tools.py; harness/uv_check.py runs the rest.
"""

import hashlib
import math

MAX_SAMPLES = 16
MAX_BINDINGS = 8      # Image nodes listed per call.
MAX_PATHS = 3         # Paths from one image node.
MAX_DEPTH = 8         # Nodes along one path.
MAX_TEXELS = 4096 * 4096
MAX_REDUCED = 8192 * 8192  # Paint stats read up to 8K, on a copy at most REDUCED a side.
REDUCED = 4096
THUMB = 256
MAX_THUMBS = 4
GAP = 8

SCHEMAS = [{"type": "function", "function": {  # Sent with every request: every word here is paid for on every step.
    "name": "inspect_texture",
    "title": "Checking a texture",
    "description": (
        "Read-only: each image node of a material, its path to the shader socket, UV map, colour space, size, "
        "packed or file, and a pixel hash any edit changes. Samples as mapped, at uv points or an object's faces. "
        "thumbnail: a small image of each."),
    "parameters": {"type": "object", "properties": {
        "target": {"type": "string", "description": "Object, material or image"},
        "faces": {"type": "array", "items": {"type": "integer"}, "description": "The object's face ids"},
        "uv": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}, "description": "[u, v] points"},
        "thumbnail": {"type": "boolean"},
    }, "required": ["target"]},
}}]


# ------------------------------------------------------------------ pure

def texel_index(i: int, n: int, extension: str) -> int | None:
    """A texel index past the image's edge as the extension reads it; None when it reads nothing (clip)."""
    if extension == "EXTEND":
        return min(max(i, 0), n - 1)
    if extension == "CLIP":
        return i if 0 <= i < n else None
    if extension == "MIRROR":
        i %= 2 * n
        return i if i < n else 2 * n - 1 - i
    return i % n


def sample(get, width: int, height: int, u: float, v: float, interpolation: str = "Linear",
           extension: str = "REPEAT") -> tuple:
    """RGBA at (u, v), texel centres at (i + 0.5) / width; get(x, y) returns a texel, y from the
    bottom. Closest reads the texel under the point; every other interpolation blends the four
    nearest (Cubic and Smart as bilinear). A texel clip leaves out reads as transparent black."""
    def texel(x, y):
        x, y = texel_index(x, width, extension), texel_index(y, height, extension)
        return (0.0, 0.0, 0.0, 0.0) if x is None or y is None else tuple(get(x, y))

    if interpolation == "Closest":
        return texel(math.floor(u * width), math.floor(v * height))
    x, y = u * width - 0.5, v * height - 0.5
    x0, y0 = math.floor(x), math.floor(y)
    fx, fy = x - x0, y - y0
    corners = ((x0, y0, (1 - fx) * (1 - fy)), (x0 + 1, y0, fx * (1 - fy)), (x0, y0 + 1, (1 - fx) * fy),
               (x0 + 1, y0 + 1, fx * fy))
    total = [0.0, 0.0, 0.0, 0.0]
    for cx, cy, weight in corners:
        if weight:
            for k, value in enumerate(texel(cx, cy)):
                total[k] += weight * value
    return tuple(total)


def _euler(rotation) -> list:
    """Blender's XYZ Euler as a matrix, Rz Ry Rx, as the Mapping node builds it."""
    (cx, sx), (cy, sy), (cz, sz) = ((math.cos(a), math.sin(a)) for a in rotation)
    return [[cy * cz, sy * sx * cz - cx * sz, sy * cx * cz + sx * sz],
            [cy * sz, sy * sx * sz + cx * cz, sy * cx * sz - sx * cz],
            [-sy, cy * sx, cy * cx]]


def mapping(point, kind: str, location, rotation, scale) -> tuple:
    """A Mapping node on a vector: POINT scales, rotates, then moves; TEXTURE undoes that (the
    texture moves the other way); VECTOR and NORMAL only scale and rotate."""
    m = _euler(rotation)
    if kind == "TEXTURE":
        p = [point[k] - location[k] for k in range(3)]
        p = [sum(m[r][k] * p[r] for r in range(3)) for k in range(3)]  # The transpose: the inverse rotation.
        return tuple(p[k] / scale[k] if scale[k] else 0.0 for k in range(3))
    p = [point[k] * scale[k] for k in range(3)]
    p = [sum(m[k][c] * p[c] for c in range(3)) for k in range(3)]
    return tuple(p[k] + location[k] for k in range(3)) if kind == "POINT" else tuple(p)


def face_points(uv: list) -> tuple[tuple, list]:
    """((u, v) the face's UV centre, [(u, v, weight)] points spread over it): each fan triangle's
    centroid and the points halfway from it to its corners, weighted by the triangle's UV area.
    A face with no UV area gives its corners' mean, weight 1."""
    points, total, cu, cv = [], 0.0, 0.0, 0.0
    for k in range(1, len(uv) - 1):
        a, b, c = uv[0], uv[k], uv[k + 1]
        area = abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2
        if area <= 0:
            continue
        mu, mv = (a[0] + b[0] + c[0]) / 3, (a[1] + b[1] + c[1]) / 3
        points.append((mu, mv, area / 4))
        points += [((mu + p[0]) / 2, (mv + p[1]) / 2, area / 4) for p in (a, b, c)]
        total, cu, cv = total + area, cu + mu * area, cv + mv * area
    if total <= 0:
        mean = (sum(p[0] for p in uv) / len(uv), sum(p[1] for p in uv) / len(uv))
        return mean, [(*mean, 1.0)]
    return (cu / total, cv / total), points


def mean(samples: list) -> tuple:
    """The weighted mean of [(rgba, weight)]."""
    total = sum(w for _, w in samples) or 1.0
    return tuple(sum(c[k] * w for c, w in samples) / total for k in range(4))


def rgba(values) -> str:
    return " ".join(f"{v:.3f}" for v in values)


def describe(name: str, arguments: dict) -> tuple[str, str] | None:
    """(summary, code) for the tool card."""
    if name != "inspect_texture":
        return None
    return f"Inspect the textures of {arguments.get('target', '?')}", ""


# ------------------------------------------------------------------ Blender

def _through(socket):
    """The node and output a linked input comes from, past reroutes; (None, None) unlinked."""
    links = [link for link in socket.links if not link.is_muted] if socket.is_linked else []
    while links:
        node, output = links[0].from_node, links[0].from_socket
        if node.type != "REROUTE":
            return node, output
        links = [link for link in node.inputs[0].links if not link.is_muted] if node.inputs[0].is_linked else []
    return None, None


def _paths(node) -> list[str]:
    """Where an image node's outputs go: each path through the nodes it feeds, ending at a shader
    socket, the material output, a group, or a node whose output goes nowhere."""
    found = []

    def walk(current, trail, depth):
        for output in current.outputs:
            for link in [link for link in output.links if not link.is_muted] if output.is_linked else ():
                if len(found) >= MAX_PATHS:
                    return
                to = link.to_node
                here = trail + ([output.name] if current is node else [])
                if to.type == "REROUTE":
                    walk(to, here, depth)
                    continue
                step = here + [f"{to.name}.{link.to_socket.name}"]
                shader = any(o.type == "SHADER" for o in to.outputs) or to.type in ("OUTPUT_MATERIAL", "GROUP")
                if shader or depth >= MAX_DEPTH or not any(o.is_linked for o in to.outputs):
                    end = (" (a group)" if to.type == "GROUP" else "" if shader or depth >= MAX_DEPTH
                           else " (goes no further)")
                    found.append(" -> ".join(step) + end)
                else:
                    walk(to, step, depth + 1)

    walk(node, [], 1)
    return found or ["not connected: it shows nowhere"]


def _uv_source(node) -> tuple[str | None, dict | None, str]:
    """(UV map name, "" for the one that renders, None when the Vector is not a UV map; the Mapping
    node's transform or None; a note)."""
    source, output = _through(node.inputs["Vector"])
    transform, note = None, ""
    if source is not None and source.type == "MAPPING":
        inputs = {s.name: s for s in source.inputs}
        linked = [n for n in ("Location", "Rotation", "Scale") if n in inputs and inputs[n].is_linked]
        if linked:
            note = f"; Mapping {source.name}'s {', '.join(linked)} come from nodes: sampled without it"
        else:
            value = lambda n: tuple(inputs[n].default_value) if n in inputs else None  # noqa: E731
            transform = {"kind": source.vector_type, "location": value("Location") or (0.0, 0.0, 0.0),
                         "rotation": value("Rotation") or (0.0, 0.0, 0.0), "scale": value("Scale") or (1.0, 1.0, 1.0),
                         "name": source.name}
        source, output = _through(source.inputs["Vector"])
        if source is None:
            return None, None, f"; Mapping {transform['name'] if transform else ''} has no vector in: a constant"
    if source is None:
        return "", transform, note
    if source.type == "UVMAP":
        return source.uv_map, transform, note
    if source.type == "TEX_COORD" and output.name == "UV":
        return "", transform, note
    return None, None, f"; its Vector is {source.name}.{output.name}, not a UV map: not sampled"


def _mapping_text(transform) -> str:
    if not transform:
        return ""
    parts = [f"{k} {', '.join(f'{v:g}' for v in transform[k])}" for k, rest in
             (("location", (0, 0, 0)), ("rotation", (0, 0, 0)), ("scale", (1, 1, 1)))
             if tuple(round(v, 6) for v in transform[k]) != rest]
    return f" via Mapping {transform['name']} ({transform['kind'].lower()}{': ' + '; '.join(parts) if parts else ''})"


def _bindings(material) -> list[dict]:
    """The material's Image Texture nodes with what inspect_texture says of each."""
    tree = getattr(material, "node_tree", None)
    found = []
    for node in tree.nodes if tree else ():
        if node.type != "TEX_IMAGE":
            continue
        uv_map, transform, note = _uv_source(node)
        found.append({"material": material, "node": node, "image": node.image, "uv_map": uv_map,
                      "transform": transform, "note": note, "paths": _paths(node),
                      "interpolation": node.interpolation, "extension": node.extension})
    groups = [n.name for n in tree.nodes if n.type == "GROUP"] if tree else []
    if groups:
        found.append({"material": material, "groups": groups})
    return found


class _Pixels:
    """An image's pixels, read once per call: None with the reason when it can't be read here."""

    def __init__(self):
        self.read, self.why, self.small = {}, {}, {}

    def of(self, image):
        import numpy as np
        key = image.name
        if key in self.read or key in self.why:
            return self.read.get(key)
        width, height = image.size  # Loads a file image's buffer; never reload(), which drops unsaved paint.
        if width * height == 0:
            self.why[key] = "no pixels (its file is missing or unreadable)" if image.source == "FILE" else "no pixels"
        elif width * height > MAX_TEXELS:
            self.why[key] = f"too big to read here ({width}x{height}); samples and hash skipped"
        else:
            pixels = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(pixels)
            self.read[key] = pixels.reshape(height, width, 4)
        return self.read.get(key)

    def reduced(self, image, most: int | None = None):
        """(pixels box-averaged by a whole stride so neither side passes `most`, the stride); an image
        within MAX_TEXELS as of() reads it, stride 1. None when it can't be read (or passes MAX_REDUCED)."""
        import tempfile
        import numpy as np
        from .paint_stats import shrink
        width, height = image.size
        if width * height <= MAX_TEXELS:
            data = self.of(image)
            return None if data is None else (data, 1)
        if image.name not in self.small:
            self.small[image.name] = None
            if width * height <= MAX_REDUCED:
                stride = math.ceil(max(width, height) / (most or REDUCED))
                with tempfile.TemporaryFile() as handle:
                    buffer = np.memmap(handle, dtype=np.float32, mode="w+", shape=(width * height * 4,))
                    image.pixels.foreach_get(buffer)
                    self.small[image.name] = (shrink(buffer.reshape(height, width, 4), stride), stride)
                    del buffer
        return self.small[image.name]


def _image_line(image, pixels: _Pixels) -> str:
    if image is None:
        return "no image assigned"
    where = f"packed ({image.packed_file.size / 2 ** 20:.1f} MB)" if image.packed_file else (
        f"file {image.filepath}" if image.filepath else image.source.lower())
    if image.source == "GENERATED" and not image.packed_file:
        where = "generated in memory, not packed or saved"
    data = pixels.of(image)
    digest = hashlib.blake2b(data.tobytes(), digest_size=8).hexdigest()[:12] if data is not None else ""
    parts = [f"image {image.name}: {image.size[0]}x{image.size[1]}", image.colorspace_settings.name,
             "float" if image.is_float else "", where,
             "unsaved pixel edits (image.pack() or save keeps them)" if image.is_dirty else "",
             "UDIM: the first tile only" if image.source == "TILED" else "",
             f"hash {digest}" if digest else pixels.why.get(image.name, "")]
    return ", ".join(p for p in parts if p)


def _targets(target: str):
    """(materials to list, the object or None, the image or None) for a name."""
    import bpy
    from .tools import ToolError
    obj, material, image = bpy.data.objects.get(target), bpy.data.materials.get(target), bpy.data.images.get(target)
    if obj is not None:
        return list(dict.fromkeys(s.material for s in obj.material_slots if s.material is not None)), obj, None
    if material is not None:
        return [material], None, None
    if image is not None:
        users = [m for m in bpy.data.materials if m.node_tree is not None and any(
            n.type == "TEX_IMAGE" and n.image == image for n in m.node_tree.nodes)]
        return users, None, image
    raise ToolError(f"No object, material or image named {target!r}.")


def inspect_texture(target: str, faces: list | None = None, uv: list | None = None, thumbnail: bool = False):
    """The tool (see SCHEMAS and the module docstring)."""
    import numpy as np
    from . import scratch, tools
    from .tools import ToolError, ToolResult
    faces, points = list(faces or []), list(uv or [])
    if len(faces) + len(points) > MAX_SAMPLES:
        raise ToolError(f"At most {MAX_SAMPLES} faces and uv points together per call.")
    if not all(isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(c, (int, float)) for c in p) for p in points):
        raise ToolError("uv: a list of [u, v] points")
    materials, obj, image = _targets(str(target))
    if faces and obj is None:
        raise ToolError("faces are an object's: name the object as target.")
    if faces and obj.type != "MESH":
        raise ToolError(f"{obj.name} is not a mesh; faces are a mesh's.")
    pixels = _Pixels()
    bindings = [b for m in materials for b in _bindings(m)]
    if image is not None:
        bindings = [b for b in bindings if b.get("image") == image or "groups" in b]
    lines = []
    if image is not None:
        lines.append(_image_line(image, pixels) + (f"; used by {', '.join(m.name for m in materials)}" if materials
                                                   else "; no material's image node uses it"))
    elif not materials:
        lines.append(f"{target} has no material." if obj is not None else f"{target} has no image nodes.")
    counts, slots = {}, _face_slots(obj) if obj is not None and obj.type == "MESH" else []
    for slot_index, slot in enumerate(obj.material_slots if slots else ()):
        if slot.material is not None:
            counts[slot.material.name] = counts.get(slot.material.name, 0) + slots.count(slot_index)
    shown = [b for b in bindings if "node" in b][:MAX_BINDINGS]
    current = None
    for binding in [b for b in bindings if "node" in b or "groups" in b]:
        material = binding["material"]
        if material != current:
            current = material
            on = f" (on {counts[material.name]:,} faces)" if material.name in counts else ""
            lines.append(f"Material {material.name}{on}:")
        if "groups" in binding:
            lines.append(f"  group nodes {', '.join(binding['groups'])}: images inside groups are not listed")
            continue
        if binding not in shown:
            continue
        node = binding["node"]
        lines.append(f"- {node.name}: " + "; ".join(binding["paths"]))
        if image is None:
            lines.append(f"  {_image_line(binding['image'], pixels)}")
        uv_map = binding["uv_map"]
        if uv_map is None:
            where = "not a UV map" + binding["note"]
        else:
            where = (uv_map or "the one that renders") + _mapping_text(binding["transform"]) + binding["note"]
            if obj is not None and obj.type == "MESH":
                layer = obj.data.uv_layers.get(uv_map) if uv_map else next(
                    (l for l in obj.data.uv_layers if l.active_render), None)
                if layer is None:
                    where += f" - {obj.name} has no such map: it renders with none"
                elif not uv_map:
                    where = f"{layer.name} (renders)" + where[len("the one that renders"):]
        lines.append(f"  UV map: {where}; {binding['interpolation']}, {binding['extension'].lower()}")
    sampled = [b for b in shown if b["image"] is not None and b["uv_map"] is not None]
    if image is not None and not sampled:
        sampled = [{"image": image, "node": None, "uv_map": "", "transform": None, "interpolation": "Linear",
                    "extension": "REPEAT", "material": None}]
    if obj is not None and obj.type == "MESH":
        lines += _paint_stats(obj, pixels)
    if points or faces:
        lines += _samples(obj, sampled, faces, points, pixels, slots)
    result = ToolResult("\n".join(lines))
    if thumbnail:
        images = list(dict.fromkeys(b["image"] for b in sampled if pixels.of(b["image"]) is not None))[:MAX_THUMBS]
        if not images:
            raise ToolError("No thumbnail: no image here could be read.")
        tiles = []
        for picture in images:
            data = pixels.of(picture)
            step = max(1, math.ceil(max(data.shape[:2]) / THUMB))
            tile = data[::step, ::step].copy()
            tile[..., 3] = 1.0
            tiles.append(tile)
        height = max(t.shape[0] for t in tiles)
        width = sum(t.shape[1] for t in tiles) + GAP * (len(tiles) - 1)
        canvas = np.full((height, width, 4), (0.12, 0.12, 0.12, 1.0), dtype=np.float32)
        x = 0
        for tile in tiles:
            canvas[:tile.shape[0], x:x + tile.shape[1]] = tile
            x += tile.shape[1] + GAP
        path = scratch.folder() / "texture_thumbnail.png"
        tools._save_pixels(canvas, path)
        result.image_path = path
        result.text += (f"\n\nImage attached: {', '.join(i.name for i in images)}, left to right, each shrunk to at "
                        f"most {THUMB} px, alpha dropped, values as stored.")
    return result


def _paint_stats(obj, pixels: _Pixels) -> list[str]:
    """paint_stats' lines for the object's material slots; a measuring bug costs the lines, not the call."""
    import bpy
    from . import paint_stats
    try:
        return paint_stats.object_lines(obj, pixels, bpy.context.scene)
    except Exception as ex:  # noqa: BLE001
        return [f"Paint stats: not measured ({type(ex).__name__}: {ex})"[:200]]


def _face_slots(obj) -> list[int]:
    """Each face's material slot index; from the edit mesh in Edit Mode, whose data is stale meanwhile."""
    import numpy as np
    if obj.mode == "EDIT":
        import bmesh
        return [face.material_index for face in bmesh.from_edit_mesh(obj.data).faces]
    index = np.empty(len(obj.data.polygons), dtype=np.int32)
    obj.data.polygons.foreach_get("material_index", index)
    return index.tolist()


def _samples(obj, bindings: list, faces: list, points: list, pixels: _Pixels, slots: list) -> list[str]:
    """One line per face or point: every sampled image's value there, as its node reads it."""
    from .uv_tools import _gather
    if not bindings:
        return ["Samples: nothing to sample (no image on a UV map here)."]
    lines = ["Samples (RGBA as stored):"]
    corners = {}

    def read(binding, u, v):
        data = pixels.of(binding["image"])
        if data is None:
            return None
        if binding["transform"]:
            t = binding["transform"]
            u, v, _ = mapping((u, v, 0.0), t["kind"], t["location"], t["rotation"], t["scale"])
        height, width = data.shape[:2]
        return sample(lambda x, y: data[y, x], width, height, u, v, binding["interpolation"], binding["extension"])

    def label(binding):
        return binding["node"].name if binding["node"] is not None else binding["image"].name

    for u, v in points:
        values = []
        for binding in bindings:
            value = read(binding, float(u), float(v))
            values.append(f"{label(binding)} {rgba(value)}" if value else
                          f"{label(binding)}: {pixels.why.get(binding['image'].name, 'unreadable')}")
        lines.append(f"  uv {u:g}, {v:g}: " + "; ".join(values))
    if faces:
        mesh, count = obj.data, len(slots)
        for face in faces:
            if not isinstance(face, int) or not 0 <= face < count:
                lines.append(f"  face {face}: no such face ({count:,} faces)")
                continue
            slot = obj.material_slots[slots[face]] if slots[face] < len(obj.material_slots) else None
            material = slot.material if slot else None
            mine = [b for b in bindings if b["material"] == material]
            if not mine:
                lines.append(f"  face {face}: its material {material.name if material else '(none)'} samples no image")
                continue
            values, where = [], ""
            for binding in mine:
                name = binding["uv_map"] or next((l.name for l in mesh.uv_layers if l.active_render), "")
                layer = mesh.uv_layers.get(name) if name else None
                if layer is None:
                    values.append(f"{label(binding)}: no UV map {name!r}")
                    continue
                if name not in corners:
                    corners[name] = _gather(obj, name)[2]  # Each face's corner UVs, Edit Mode or not.
                centre, spread = face_points(corners[name][face])
                where = f", uv {centre[0]:.3f}, {centre[1]:.3f}"
                middle = read(binding, *centre)
                if middle is None:
                    values.append(f"{label(binding)}: {pixels.why.get(binding['image'].name, 'unreadable')}")
                    continue
                average = mean([(read(binding, pu, pv), w) for pu, pv, w in spread])
                values.append(f"{label(binding)} centre {rgba(middle)}, mean {rgba(average)}")
            lines.append(f"  face {face} ({material.name}{where}): " + "; ".join(values))
    return lines


DISPATCH = {"inspect_texture": inspect_texture}
