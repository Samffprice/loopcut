"""export_glb: a .glb export that is checked before it replaces anything.

GPT-6.1 Sol (20260929-230207 O0011, O0021) exported the asset pack with its own code and read the
file back with a GLB parser of its own, twice, and the second export overwrote the file before it
was checked. export_glb writes a temporary file beside the destination, reads it back (parse and
validate: the header, the JSON and binary chunks, every accessor inside its buffer view, every image
inside the file), compares it with the scene (each object a node of the same name, its triangles and
its size), lists what glTF leaves out (procedural textures and other nodes the exporter cannot
write), and only then renames it over the destination. A failed check leaves the destination as it
was and says why.

The top half is pure and tested in tests/test_glb.py; harness/glb_check.py exports a real scene.
"""

import json
import os
import struct
import uuid
from pathlib import Path


SCHEMAS = [{"type": "function", "function": {
    "name": "export_glb",
    "title": "Exporting the model",
    "description": ("Export a .glb, checked against the scene (names, triangles, sizes, images, what glTF drops) "
                    "before it replaces path."),
    "parameters": {"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string"},
        "objects": {"type": "array", "items": {"type": "string"},
                    "description": "With children; default every deliverable"},
        "overwrite": {"type": "boolean"}}}}}]

MAGIC, VERSION = 0x46546C67, 2          # "glTF", version 2.
JSON_CHUNK, BIN_CHUNK = 0x4E4F534A, 0x004E4942
COMPONENT_BYTES = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
TYPE_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
SIZE_TOLERANCE = 0.001   # Metres, or this share of the size, whichever is larger: a size in the file off by more fails.
MAX_LINES = 12           # Objects listed in the report; the rest are counted.
# Shader nodes the glTF exporter cannot write: procedural textures and the utilities around them.
# A material using one exports without it (its look changes), so the report says which.
DROPPED_NODES = {
    "ShaderNodeTexNoise": "Noise Texture", "ShaderNodeTexVoronoi": "Voronoi Texture",
    "ShaderNodeTexWave": "Wave Texture", "ShaderNodeTexMagic": "Magic Texture",
    "ShaderNodeTexGradient": "Gradient Texture", "ShaderNodeTexChecker": "Checker Texture",
    "ShaderNodeTexBrick": "Brick Texture", "ShaderNodeTexWhiteNoise": "White Noise Texture",
    "ShaderNodeTexGabor": "Gabor Texture", "ShaderNodeTexMusgrave": "Musgrave Texture",
    "ShaderNodeTexSky": "Sky Texture", "ShaderNodeTexPointDensity": "Point Density",
    "ShaderNodeValToRGB": "Color Ramp", "ShaderNodeBump": "Bump", "ShaderNodeLayerWeight": "Layer Weight",
    "ShaderNodeFresnel": "Fresnel", "ShaderNodeAmbientOcclusion": "Ambient Occlusion",
    "ShaderNodeWireframe": "Wireframe", "ShaderNodeDisplacement": "Displacement",
    "ShaderNodeVectorDisplacement": "Vector Displacement",
}


# ------------------------------------------------------------------ the file, pure

def parse(data: bytes) -> tuple[dict, bytes]:
    """(the glTF JSON, the binary chunk) of a .glb; ValueError says what is wrong with it."""
    if len(data) < 20:
        raise ValueError(f"{len(data)} bytes: too short for a .glb")
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != MAGIC:
        raise ValueError("not a .glb (no glTF header)")
    if version != VERSION:
        raise ValueError(f"glTF version {version}, not 2")
    if length != len(data):
        raise ValueError(f"the header says {length} bytes, the file has {len(data)}")
    chunks, offset = [], 12
    while offset < length:
        if offset + 8 > length:
            raise ValueError("a chunk header runs past the end")
        size, kind = struct.unpack_from("<II", data, offset)
        if offset + 8 + size > length:
            raise ValueError("a chunk runs past the end")
        chunks.append((kind, data[offset + 8:offset + 8 + size]))
        offset += 8 + size
    if not chunks or chunks[0][0] != JSON_CHUNK:
        raise ValueError("the first chunk is not the JSON")
    try:
        gltf = json.loads(chunks[0][1].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as ex:
        raise ValueError(f"the JSON chunk does not parse: {ex}") from ex
    if not isinstance(gltf, dict):
        raise ValueError("the JSON chunk is not an object")
    binary = next((body for kind, body in chunks[1:] if kind == BIN_CHUNK), b"")
    return gltf, binary


def _list(gltf: dict, key: str) -> list:
    value = gltf.get(key)
    return value if isinstance(value, list) else []


def validate(gltf: dict, binary: bytes) -> list[str]:
    """What makes the file unreadable or incomplete, one line each; [] when it holds together: the
    buffer is the file's own binary chunk, every view inside its buffer, every accessor inside its
    view, every mesh's attributes and indices real accessors, every image inside the file."""
    problems = []
    buffers, views, accessors = _list(gltf, "buffers"), _list(gltf, "bufferViews"), _list(gltf, "accessors")
    sizes = []
    for index, buffer in enumerate(buffers):
        if not isinstance(buffer, dict):
            problems.append(f"buffer {index} is not an object")
            sizes.append(0)
        elif buffer.get("uri"):
            problems.append(f"buffer {index} is outside the file ({str(buffer['uri'])[:60]})")
            sizes.append(int(buffer.get("byteLength") or 0))
        else:
            if int(buffer.get("byteLength") or 0) > len(binary):
                problems.append(f"buffer {index} is {buffer.get('byteLength')} bytes; the binary chunk has {len(binary)}")
            sizes.append(len(binary))
    for index, view in enumerate(views):
        buffer = view.get("buffer") if isinstance(view, dict) else None
        if not isinstance(buffer, int) or not 0 <= buffer < len(sizes):
            problems.append(f"buffer view {index} names no buffer")
        elif int(view.get("byteOffset") or 0) + int(view.get("byteLength") or 0) > sizes[buffer]:
            problems.append(f"buffer view {index} runs past its buffer")
    for index, accessor in enumerate(accessors):
        if not isinstance(accessor, dict):
            problems.append(f"accessor {index} is not an object")
            continue
        view = accessor.get("bufferView")
        if view is None:
            continue  # All zeros (or sparse): nothing to read past.
        width = COMPONENT_BYTES.get(accessor.get("componentType"), 0) * TYPE_COUNT.get(accessor.get("type"), 0)
        if not isinstance(view, int) or not 0 <= view < len(views) or not isinstance(views[view], dict) or not width:
            problems.append(f"accessor {index} names no buffer view, or has no known type")
            continue
        stride = int(views[view].get("byteStride") or width)
        count = int(accessor.get("count") or 0)
        end = int(accessor.get("byteOffset") or 0) + (stride * (count - 1) + width if count else 0)
        if end > int(views[view].get("byteLength") or 0):
            problems.append(f"accessor {index} ({count} x {accessor.get('type')}) runs past its buffer view")
    for m, mesh in enumerate(_list(gltf, "meshes")):
        for p, primitive in enumerate((mesh.get("primitives") or []) if isinstance(mesh, dict) else []):
            references = dict((primitive.get("attributes") or {}) if isinstance(primitive, dict) else {})
            if isinstance(primitive, dict) and "indices" in primitive:
                references["indices"] = primitive["indices"]
            if "POSITION" not in references:
                problems.append(f"mesh {mesh.get('name') or m} primitive {p} has no positions")
            for what, accessor in references.items():
                if not isinstance(accessor, int) or not 0 <= accessor < len(accessors):
                    problems.append(f"mesh {mesh.get('name') or m} primitive {p}: {what} names no accessor")
    for index, image in enumerate(_list(gltf, "images")):
        if not isinstance(image, dict):
            problems.append(f"image {index} is not an object")
        elif image.get("uri") and not str(image["uri"]).startswith("data:"):
            problems.append(f"image {image.get('name') or index} is outside the file ({str(image['uri'])[:60]})")
        elif not image.get("uri") and not (isinstance(image.get("bufferView"), int) and 0 <= image["bufferView"] < len(views)):
            problems.append(f"image {image.get('name') or index} has no data in the file")
    for index, node in enumerate(_list(gltf, "nodes")):
        mesh = node.get("mesh") if isinstance(node, dict) else None
        if mesh is not None and not (isinstance(mesh, int) and 0 <= mesh < len(_list(gltf, "meshes"))):
            problems.append(f"node {node.get('name') or index} names no mesh")
    return problems


def _matmul(a: list, b: list) -> list:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _local(node: dict) -> list:
    """A node's 4x4 transform, rows, from its matrix (column-major) or its TRS."""
    if isinstance(node.get("matrix"), list) and len(node["matrix"]) == 16:
        m = [float(v) for v in node["matrix"]]
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    tx, ty, tz = (node.get("translation") or [0, 0, 0])[:3]
    x, y, z, w = (node.get("rotation") or [0, 0, 0, 1])[:4]
    sx, sy, sz = (node.get("scale") or [1, 1, 1])[:3]
    rotation = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return [[rotation[r][0] * sx, rotation[r][1] * sy, rotation[r][2] * sz, (tx, ty, tz)[r]] for r in range(3)] + \
        [[0.0, 0.0, 0.0, 1.0]]


def _triangles(primitive: dict, accessors: list) -> tuple[int, str]:
    """(triangles, "" or the mode that has none) of one primitive."""
    mode = primitive.get("mode", 4)
    source = primitive.get("indices", (primitive.get("attributes") or {}).get("POSITION"))
    count = int(accessors[source].get("count") or 0) if isinstance(source, int) and 0 <= source < len(accessors) else 0
    if mode == 4:
        return count // 3, ""
    if mode in (5, 6):
        return max(0, count - 2), ""
    return 0, {0: "points", 1: "lines", 2: "line loops", 3: "line strips"}.get(mode, f"mode {mode}")


def _bounds(mesh: dict, accessors: list):
    """The local box of a mesh's positions, (low, high), from each POSITION accessor's min and max."""
    lows, highs = [], []
    for primitive in mesh.get("primitives") or []:
        index = (primitive.get("attributes") or {}).get("POSITION")
        accessor = accessors[index] if isinstance(index, int) and 0 <= index < len(accessors) else {}
        if isinstance(accessor.get("min"), list) and isinstance(accessor.get("max"), list):
            lows.append(accessor["min"][:3])
            highs.append(accessor["max"][:3])
    if not lows:
        return None
    return [min(v[i] for v in lows) for i in range(3)], [max(v[i] for v in highs) for i in range(3)]


def summary(gltf: dict) -> dict:
    """What the file holds, by node name: {"nodes": {name: {"tris", "size" [x, y, z] in Blender's Z-up
    metres, or None}}, "meshes", "tris", "materials", "images", "warnings"}. Sizes are the world box
    of each node's own mesh, through its parents' transforms (glTF is Y-up: Blender's y is glTF's -z)."""
    nodes, meshes, accessors = _list(gltf, "nodes"), _list(gltf, "meshes"), _list(gltf, "accessors")
    parents = {child: index for index, node in enumerate(nodes) if isinstance(node, dict)
               for child in node.get("children") or [] if isinstance(child, int)}
    worlds = {}

    def world(index: int) -> list:
        if index not in worlds:
            local = _local(nodes[index])
            worlds[index] = _matmul(world(parents[index]), local) if index in parents else local
        return worlds[index]
    found, warnings, total = {}, [], 0
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or f"node {index}")
        mesh = meshes[node["mesh"]] if isinstance(node.get("mesh"), int) and 0 <= node["mesh"] < len(meshes) else None
        tris, size = 0, None
        if isinstance(mesh, dict):
            for primitive in mesh.get("primitives") or []:
                count, other = _triangles(primitive, accessors)
                tris += count
                if other:
                    warnings.append(f"{name}: {other} (not triangles)")
            box = _bounds(mesh, accessors)
            if box is not None:
                matrix = world(index)
                corners = [[sum(matrix[r][c] * p[c] for c in range(3)) + matrix[r][3] for r in range(3)]
                           for p in ([x, y, z] for x in (box[0][0], box[1][0]) for y in (box[0][1], box[1][1])
                                     for z in (box[0][2], box[1][2]))]
                dx, dy, dz = (max(p[i] for p in corners) - min(p[i] for p in corners) for i in range(3))
                size = [dx, dz, dy]
        total += tris
        found[name] = {"tris": tris, "size": size}
    return {"nodes": found, "meshes": len(meshes), "tris": total, "materials": len(_list(gltf, "materials")),
            "images": len(_list(gltf, "images")), "warnings": warnings}


def _near(a: float, b: float) -> bool:
    return abs(a - b) <= max(SIZE_TOLERANCE, SIZE_TOLERANCE * max(abs(a), abs(b)))


def _size_text(size) -> str:
    return " x ".join(f"{v:.3g}" for v in size) + " m"


def compare(found: dict, expected: dict) -> list[str]:
    """What the file says differently from the scene, one line per object; expected: {name: {"tris",
    "size" or None}} measured in the scene (_expected)."""
    problems = []
    for name, want in expected.items():
        got = found["nodes"].get(name)
        if got is None:
            problems.append(f"{name}: not in the file")
            continue
        if got["tris"] != want["tris"]:
            problems.append(f"{name}: {got['tris']:,} triangles in the file, {want['tris']:,} in the scene")
        if want.get("size") and got.get("size") and not all(map(_near, got["size"], want["size"])):
            problems.append(f"{name}: {_size_text(got['size'])} in the file, {_size_text(want['size'])} in the scene")
    return problems


def _human(size: int) -> str:
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


def report(path, size: int, found: dict, expected: dict, warnings: list, replaced: bool) -> str:
    """What export_glb says once the file is in place: what it holds, each object as measured in the
    file (names, triangles, sizes: evidence, not a claim), then what glTF left out."""
    lines = [f"Exported {path} ({_human(size)}){', replacing the earlier file' if replaced else ''}: {found['meshes']} "
             f"meshes, {found['tris']:,} triangles, {found['materials']} materials, {found['images']} images "
             f"embedded; {len(expected)} objects match the scene in name, triangles and size."]
    for name in list(expected)[:MAX_LINES]:
        got = found["nodes"][name]
        lines.append(f"  {name}: {got['tris']:,} triangles" + (f", {_size_text(got['size'])}" if got.get("size") else ""))
    if len(expected) > MAX_LINES:
        lines.append(f"  and {len(expected) - MAX_LINES} more")
    lines += [f"Warning: {w}" for w in warnings]
    return "\n".join(lines)


# ------------------------------------------------------------------ the scene (main thread)

def _deliverables():
    """The roots this session's workers built that are still in the scene (waves.remember)."""
    import bpy
    from . import state
    threads = state.session().get("worker_threads") or {}
    return [bpy.data.objects[t["deliverable"]] for t in threads.values()
            if isinstance(t.get("deliverable"), str) and t["deliverable"] in bpy.data.objects]


def _targets(objects) -> list:
    """The objects to export: those named with their children, or every deliverable's; with neither,
    every visible object with geometry or children. Raises ToolError for a name not in the scene."""
    import bpy
    from . import ownership
    from .tools import ToolError
    objects = [objects] if isinstance(objects, str) else [str(n) for n in objects or ()]
    if objects:
        missing = [n for n in objects if n not in bpy.data.objects]
        if missing:
            raise ToolError(f"No object named {', '.join(map(repr, missing[:5]))}; export_glb takes object names.")
        roots = [bpy.data.objects[n] for n in objects]
    else:
        roots = _deliverables()
        if not roots:
            layer = bpy.context.view_layer
            roots = [ob for ob in layer.objects if ob.parent is None and ob.visible_get() and ob.type not in
                     ("CAMERA", "LIGHT") and not ob.get(ownership.QUARANTINE)]
    chosen = {}
    for root in roots:  # A hidden child (a boolean cutter, a helper) is not the asset.
        for ob in [root, *(child for child in root.children_recursive if child.visible_get())]:
            chosen[ob.session_uid] = ob
    return list(chosen.values())


def _expected(objects: list) -> dict:
    """{name: {"tris", "size"}} of each object with geometry, as the exporter writes it: modifiers
    applied, sizes the world box of its evaluated mesh's own box (bound_box kept the unsubdivided
    cube's under a Subdivision modifier)."""
    import bpy
    import numpy as np
    from mathutils import Vector
    depsgraph = bpy.context.evaluated_depsgraph_get()
    expected = {}
    for ob in objects:
        if ob.type not in ("MESH", "CURVE", "SURFACE", "META", "FONT"):
            continue
        evaluated = ob.evaluated_get(depsgraph)
        try:
            mesh = evaluated.to_mesh()
        except RuntimeError:
            mesh = None
        if mesh is None or not mesh.polygons:
            if mesh is not None:
                evaluated.to_mesh_clear()
            continue
        tris = sum(len(p.vertices) - 2 for p in mesh.polygons)
        points = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
        mesh.vertices.foreach_get("co", points)
        low, high = points.reshape(-1, 3).min(axis=0), points.reshape(-1, 3).max(axis=0)
        evaluated.to_mesh_clear()
        corners = [evaluated.matrix_world @ Vector((x, y, z)) for x in (low[0], high[0]) for y in (low[1], high[1])
                   for z in (low[2], high[2])]
        expected[ob.name] = {"tris": tris, "size": [max(p[i] for p in corners) - min(p[i] for p in corners)
                                                    for i in range(3)]}
    return expected


def dropped_nodes(objects: list) -> list[str]:
    """Materials of these objects that use nodes glTF leaves out (DROPPED_NODES), connected and not
    muted: "Wood: Noise Texture, Color Ramp"."""
    seen, lines = set(), []
    for ob in objects:
        for slot in getattr(ob, "material_slots", ()):
            material = slot.material
            if material is None or material.name in seen or material.node_tree is None:
                continue
            seen.add(material.name)
            names = sorted({DROPPED_NODES[n.bl_idname] for n in material.node_tree.nodes if n.bl_idname in DROPPED_NODES
                            and not n.mute and any(o.is_linked for o in n.outputs)})
            if names:
                lines.append(f"{material.name}: {', '.join(names)}")
    return lines


def _export(path: Path, objects: list) -> None:
    """The glTF exporter on just these objects (the selection is put back after)."""
    import bpy
    layer = bpy.context.view_layer
    selected, active = [ob for ob in layer.objects if ob.select_get()], layer.objects.active
    try:
        for ob in selected:
            ob.select_set(False)
        for ob in objects:
            if ob.name in layer.objects:
                ob.select_set(True)
        result = bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB", use_selection=True,
                                           export_apply=True)
        if "FINISHED" not in result:
            raise RuntimeError(f"the exporter returned {sorted(result)}")
    finally:
        for ob in layer.objects:
            ob.select_set(False)
        for ob in selected:
            ob.select_set(True)
        layer.objects.active = active


def _destination(path: str) -> Path:
    from . import files
    from .tools import ToolError
    target = files.check(files.resolve(path))
    if not target.suffix:
        target = target.with_name(target.name + ".glb")
    if target.suffix.lower() != ".glb":
        raise ToolError(f"{target.name}: export_glb writes .glb files.")
    if target.is_dir():
        raise ToolError(f"{target} is a folder.")
    if not target.parent.is_dir():
        raise ToolError(f"The folder {target.parent} does not exist.")
    return target


def export_glb(path: str, objects: list | None = None, overwrite: bool = False) -> "ToolResult":
    """Main thread. Export to a temporary file beside path, read it back and check it (validate,
    compare, dropped_nodes), then rename it over path; a failed check leaves path as it was."""
    from .tools import ToolError, ToolResult  # tools.py needs bpy; orchestrator.py imports this module outside Blender too.
    target = _destination(path)
    replacing = target.exists()
    if replacing and not overwrite:
        raise ToolError(f"{target} already exists; pass overwrite=true to replace it (only once the new file checks out).")
    chosen = _targets(objects)
    if not chosen:
        raise ToolError("Nothing to export: name the objects.")
    expected = _expected(chosen)
    temp = target.with_name(f".{target.stem}.loopcut-{uuid.uuid4().hex[:8]}.glb")
    try:
        try:
            _export(temp, chosen)
            data = temp.read_bytes()
        except (RuntimeError, OSError) as ex:
            return ToolResult(f"Not exported: the exporter failed ({ex}); {target} is as it was.", ok=False)
        try:
            gltf, binary = parse(data)
            problems = validate(gltf, binary)
        except ValueError as ex:
            problems = [str(ex)]
        found = summary(gltf) if not problems else None
        problems += compare(found, expected) if found else []
        if problems:
            return ToolResult("\n".join([f"Not exported: the new file failed its checks, so {target} is "
                                         f"{'as it was' if replacing else 'not written'}."]
                                        + [f"- {p}" for p in problems[:MAX_LINES]]), ok=False)
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    warnings = found["warnings"] + [f"glTF leaves out {line} (bake it to an image to keep the look)"
                                    for line in dropped_nodes(chosen)]
    return ToolResult(report(target, len(data), found, expected, warnings, replacing))


def describe(name: str, arguments: dict) -> tuple[str, str] | None:
    if name != "export_glb":
        return None
    what = ", ".join(map(str, arguments.get("objects") or [])) or "every deliverable"
    verb = "Export (replacing)" if arguments.get("overwrite") else "Export"
    return f"{verb} {what} to {arguments.get('path', '')}", ""


DISPATCH = {"export_glb": export_glb}
