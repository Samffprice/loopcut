"""inspect_api: the Blender Python API as it exists in the running Blender.

Models learned bpy from years of scripts written for older versions, and they guess: a renamed
property, a node socket that moved, an operator argument that is gone. Blender can describe its
own API exactly (RNA carries every type, property, enum value and operator argument), so the
agent asks instead of guessing. No network, no bundled docs to go stale.

Main thread only: node sockets are read by briefly creating the node in a scratch node tree.
"""

import inspect

import bpy

MAX_CHARS = 6000
MAX_SEARCH_RESULTS = 40
_NODE_TREES = ("ShaderNodeTree", "GeometryNodeTree", "CompositorNodeTree", "TextureNodeTree")
_index: list[tuple[str, str]] | None = None  # (path, lowercased text to match against)


class ApiError(Exception):
    """The path or query made no sense; the message says what would."""


def _socket_types() -> list[str]:
    return sorted(name for name in dir(bpy.types)
                  if name.startswith("NodeSocket") and "Virtual" not in name and "Interface" not in name
                  and name not in ("NodeSocket", "NodeSocketStandard"))


# RNA lists only a placeholder for enums whose items Blender computes at run time, and a model
# told that `socket_type` is one of ['DEFAULT'] will believe it. Where the real values can be
# read from the running Blender, say them. Key: (struct, function or "", property).
_DYNAMIC_ENUMS = {
    ("NodeTreeInterface", "new_socket", "socket_type"): _socket_types,
}

# The few places where the API of recent versions differs from what most scripts online (and so
# most models) assume, stated once where the model will look. harness/api_docs_check.py runs the
# code in each note against the running Blender, so a note cannot outlive the API it describes.
NOTES = {
    "NodeTree": (
        "Group inputs and outputs are made on the interface, not tree.inputs/outputs:\n"
        "  tree.interface.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')\n"
        "  tree.interface.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')\n"
        "  group_in, group_out = tree.nodes.new('NodeGroupInput'), tree.nodes.new('NodeGroupOutput')\n"
        "  tree.links.new(group_in.outputs[0], group_out.inputs[0])"),
    "Action": (
        "Actions are layered and slotted; there is no action.fcurves. For an object's curves:\n"
        "  slot = obj.animation_data.action_slot\n"
        "  bag = action.layers[0].strips[0].channelbag(slot)   # None if nothing is keyed for the slot\n"
        "  for fcurve in bag.fcurves: ...\n"
        "obj.keyframe_insert('location', frame=1) creates the action, slot, layer and strip as needed."),
}


# ------------------------------------------------------------------ formatting RNA

def _type_of(prop, owner: tuple[str, str] = ("", "")) -> str:
    kind = prop.type
    if kind == "ENUM":
        values = [item.identifier for item in prop.enum_items]
        dynamic = _DYNAMIC_ENUMS.get((*owner, prop.identifier))
        if dynamic:
            values = dynamic()
        elif len(values) <= 1:
            return "enum (values are computed at run time; assigning a wrong one raises a TypeError listing the valid ones)"
        shown = ", ".join(repr(v) for v in values[:24]) + (", ..." if len(values) > 24 else "")
        return ("set of " if prop.is_enum_flag else "enum ") + f"[{shown}]"
    if kind in ("POINTER", "COLLECTION"):
        target = prop.fixed_type.identifier if prop.fixed_type else "?"
        return f"bpy.types.{target}" if kind == "POINTER" else f"collection of bpy.types.{target}"
    text = kind.lower()
    if kind in ("INT", "FLOAT", "BOOLEAN") and getattr(prop, "array_length", 0):
        text += f"[{prop.array_length}]"
    if kind in ("INT", "FLOAT"):
        if prop.subtype not in ("NONE", ""):
            text += f" {prop.subtype.lower()}"
        low, high = prop.hard_min, prop.hard_max
        if low > -1e9 or high < 1e9:
            text += f" in [{low:g}, {high:g}]"
    return text


def _default_of(prop) -> str:
    kind = prop.type
    try:
        if kind == "ENUM":
            return repr(set(prop.default_flag)) if prop.is_enum_flag else repr(prop.default)
        if kind in ("INT", "FLOAT", "BOOLEAN"):
            if getattr(prop, "array_length", 0):
                return repr(tuple(round(v, 4) if kind == "FLOAT" else v for v in prop.default_array))
            return repr(round(prop.default, 4) if kind == "FLOAT" else prop.default)
        if kind == "STRING":
            return repr(prop.default)
    except (AttributeError, TypeError):
        pass
    return ""


def _property_line(prop, with_default=True, owner: tuple[str, str] = ("", "")) -> str:
    line = f"  {prop.identifier}: {_type_of(prop, owner)}"
    default = _default_of(prop) if with_default else ""
    if default not in ("", "''"):
        line += f" = {default}"
    if prop.is_readonly:
        line += " (read-only)"
    if getattr(prop, "is_deprecated", False):
        line += f" (DEPRECATED: {prop.deprecated_note})"
    if prop.description:
        line += f"  # {prop.description}"
    return line


def _function_line(function) -> str:
    inputs = [p for p in function.parameters if not p.is_output]
    outputs = [p for p in function.parameters if p.is_output]
    arguments = ", ".join(p.identifier + ("" if p.is_required else "=...") for p in inputs)
    line = f"  {function.identifier}({arguments})"
    if outputs:
        line += " -> " + ", ".join(_type_of(p) for p in outputs)
    if function.description:
        line += f"  # {function.description}"
    return line


def _own_properties(rna) -> list:
    inherited = {p.identifier for p in rna.base.properties} if rna.base else set()
    return [p for p in rna.properties if p.identifier != "rna_type" and p.identifier not in inherited]


def _node_sockets(identifier: str) -> list[str]:
    """Inputs and outputs only exist on a node instance, so make one in a scratch tree."""
    for tree_type in _NODE_TREES:
        try:
            tree = bpy.data.node_groups.new(".loopcut_inspect", tree_type)
        except (RuntimeError, TypeError):
            continue
        try:
            try:
                node = tree.nodes.new(identifier)
            except RuntimeError:
                continue
            lines = [f"As a node in a {tree_type} (nodes.new({identifier!r})):"]
            for title, sockets in (("inputs", node.inputs), ("outputs", node.outputs)):
                lines.append(f" {title}:")
                for index, socket in enumerate(sockets):
                    if not socket.enabled:
                        continue
                    line = f"  [{index}] {socket.name!r} ({socket.bl_idname})"
                    if socket.identifier != socket.name:
                        line += f" identifier={socket.identifier!r}"
                    value = getattr(socket, "default_value", None)
                    if title == "inputs" and value is not None:
                        try:
                            value = tuple(round(v, 4) for v in value)
                        except TypeError:
                            value = round(value, 4) if isinstance(value, float) else value
                        line += f" = {value!r}"
                    lines.append(line)
            return lines
        finally:
            bpy.data.node_groups.remove(tree)
    return []


def _describe_struct(cls) -> list[str]:
    rna = cls.bl_rna
    lines = [f"bpy.types.{rna.identifier}" + (f"({rna.base.identifier})" if rna.base else "")]
    if rna.description:
        lines.append(rna.description)
    own = _own_properties(rna)
    if own:
        lines.append("properties:")
        lines += [_property_line(p) for p in own]
    functions = [f for f in rna.functions if not rna.base or f.identifier not in rna.base.functions]
    if functions:
        lines.append("functions:")
        lines += [_function_line(f) for f in functions]
    if rna.base:
        names = [p.identifier for p in rna.base.properties if p.identifier != "rna_type"]
        lines.append(f"inherited from {rna.base.identifier} (inspect it for details): {', '.join(names)}")
    subclasses = sorted(c.bl_rna.identifier for c in cls.__subclasses__() if hasattr(c, "bl_rna"))
    if subclasses:
        lines.append(f"subtypes ({len(subclasses)}): {', '.join(subclasses[:60])}"
                     + (", ..." if len(subclasses) > 60 else ""))
    if issubclass(cls, bpy.types.Node):
        lines += _node_sockets(rna.identifier)
    for name, note in NOTES.items():
        if issubclass(cls, getattr(bpy.types, name)):
            lines.append("NOTE: " + note)
    return lines


def _describe_operator(module: str, name: str) -> list[str]:
    operator = getattr(getattr(bpy.ops, module), name)
    try:
        rna = operator.get_rna_type()
    except KeyError as ex:
        raise ApiError(f"No operator bpy.ops.{module}.{name}. "
                       f"inspect_api('bpy.ops.{module}') lists that module.") from ex
    arguments = [p for p in rna.properties if p.identifier != "rna_type"]
    lines = [f"bpy.ops.{module}.{name}({', '.join(p.identifier + '=...' for p in arguments)})"]
    if rna.description:
        lines.append(rna.description)
    lines.append("Operators depend on context (mode, selection, active object) and raise "
                 "RuntimeError when their poll fails; prefer the data API when it can do the job.")
    if arguments:
        lines.append("arguments (all optional keywords):")
        lines += [_property_line(p) for p in arguments]
    return lines


def _describe_python(path: str, obj) -> list[str]:
    lines = [path]
    try:
        lines[0] += str(inspect.signature(obj))
    except (TypeError, ValueError):
        pass
    doc = inspect.getdoc(obj)
    if doc:
        lines.append(doc)
    if inspect.ismodule(obj) or inspect.isclass(obj):
        members = [n for n in dir(obj) if not n.startswith("_")]
        lines.append(f"members: {', '.join(members)}")
    return lines


# ------------------------------------------------------------------ lookup

def _closest(name: str) -> str:
    """Type names near a wrong guess: GeometryNodeMeshIcosahedron -> ...MeshIcoSphere."""
    import difflib
    import re
    candidates = [n for n in dir(bpy.types) if hasattr(getattr(bpy.types, n), "bl_rna")]
    close = difflib.get_close_matches(name, candidates, n=6, cutoff=0.6)
    words = [w.lower() for w in re.findall(r"[A-Z][a-z0-9]+|[a-z0-9]+", name)][-2:]
    close += [n for n in candidates if n not in close and all(w in n.lower() for w in words)][:6]
    return f" Closest type names: {', '.join(close[:10])}." if close else ""


def _walk_rna(cls, names: list[str]) -> list[str]:
    """Follow property names from an RNA type: Object -> modifiers -> (the collection's type)."""
    for position, name in enumerate(names):
        rna = cls.bl_rna
        if name in rna.functions:
            return [f"bpy.types.{rna.identifier}.{name}", _function_line(rna.functions[name]).strip(),
                    *("  " + _property_line(p, with_default=False, owner=(rna.identifier, name)).strip()
                      for p in rna.functions[name].parameters)]
        prop = rna.properties.get(name)
        if prop is None:
            raise ApiError(f"bpy.types.{rna.identifier} has no property or function {name!r}. It has: "
                           + ", ".join(p.identifier for p in rna.properties if p.identifier != "rna_type"))
        if prop.type not in ("POINTER", "COLLECTION") or prop.fixed_type is None:
            return [f"bpy.types.{rna.identifier}.{name}", _property_line(prop).strip()]
        header = f"bpy.types.{rna.identifier}.{name} is a {_type_of(prop)}"
        # A collection's own methods (new, remove, link, ...) live on a separate RNA struct.
        cls = getattr(bpy.types, (prop.srna or prop.fixed_type).identifier)
        if position == len(names) - 1:
            described = _describe_struct(cls)
            if prop.srna and prop.fixed_type:
                described.append(f"items are bpy.types.{prop.fixed_type.identifier}")
            return [header, *described]
    return _describe_struct(cls)


def _resolve(path: str) -> list[str]:
    parts = path.split(".")
    if parts[0] != "bpy" and hasattr(bpy.types, parts[0]):
        parts = ["bpy", "types", *parts]  # "BevelModifier" and "Object.modifiers" are enough.
    if parts[:2] == ["bpy", "ops"]:
        if len(parts) == 2:
            return ["bpy.ops modules: " + ", ".join(sorted(dir(bpy.ops)))]
        if parts[2] not in dir(bpy.ops):
            raise ApiError(f"No operator module bpy.ops.{parts[2]}. Modules: {', '.join(sorted(dir(bpy.ops)))}")
        if len(parts) == 3:
            return [f"bpy.ops.{parts[2]}: " + ", ".join(sorted(dir(getattr(bpy.ops, parts[2]))))]
        return _describe_operator(parts[2], parts[3])
    if parts[:2] == ["bpy", "types"] and len(parts) >= 3:
        cls = getattr(bpy.types, parts[2], None)
        if cls is None or not hasattr(cls, "bl_rna"):
            raise ApiError(f"No type bpy.types.{parts[2]}.{_closest(parts[2])}")
        return _walk_rna(cls, parts[3:])
    # Plain Python: bpy.data, bpy.context, bmesh, mathutils, bpy_extras, ...
    import importlib
    try:
        obj = importlib.import_module(parts[0])
    except ImportError as ex:
        raise ApiError(f"Nothing called {parts[0]!r}.{_closest(parts[0])} Paths look like bpy.types.Object, "
                       f"bpy.ops.mesh.subdivide, bmesh.ops.bevel, mathutils.Vector.") from ex
    walked = parts[0]
    for position, name in enumerate(parts[1:], start=1):
        if hasattr(type(obj), "bl_rna") and not inspect.isclass(obj):
            return _walk_rna(type(obj), parts[position:])  # bpy.data.objects, bpy.context.scene.render, ...
        if not hasattr(obj, name):
            raise ApiError(f"{walked} has no attribute {name!r}. It has: "
                           + ", ".join(n for n in dir(obj) if not n.startswith("_")))
        obj, walked = getattr(obj, name), f"{walked}.{name}"
    if hasattr(type(obj), "bl_rna") and not inspect.isclass(obj):
        return [f"{walked} is a bpy.types.{type(obj).bl_rna.identifier}", *_describe_struct(type(obj))]
    return _describe_python(walked, obj)


def _build_index() -> list[tuple[str, str]]:
    entries = []
    for name in dir(bpy.types):
        cls = getattr(bpy.types, name)
        rna = getattr(cls, "bl_rna", None)
        if rna is None:
            continue
        entries.append((f"bpy.types.{name}", f"{name} {rna.name} {rna.description}".lower()))
        for prop in _own_properties(rna):
            entries.append((f"bpy.types.{name}.{prop.identifier}",
                            f"{name}.{prop.identifier} {prop.name} {prop.description}".lower()))
    for module in dir(bpy.ops):
        for operator in dir(getattr(bpy.ops, module)):
            try:
                rna = getattr(getattr(bpy.ops, module), operator).get_rna_type()
            except KeyError:
                continue
            entries.append((f"bpy.ops.{module}.{operator}",
                            f"{module}.{operator} {rna.name} {rna.description}".lower()))
    return entries


def _search(query: str) -> list[str]:
    global _index
    terms = query.lower().split()
    if not terms:
        raise ApiError("search needs at least one word.")
    if _index is None:
        _index = _build_index()
    # Matches in the identifier rank above matches in the description; shorter paths first.
    hits = sorted(((0 if all(t in path.lower() for t in terms) else 1, len(path), path)
                   for path, text in _index if all(t in text for t in terms)))
    if not hits:
        return [f"Nothing in bpy.types or bpy.ops matches {query!r}. Try fewer or different words."]
    lines = [f"{len(hits)} match(es) for {query!r}" + (f", first {MAX_SEARCH_RESULTS}" if len(hits) > MAX_SEARCH_RESULTS else "")
             + "; pass one as `path` for details:"]
    return lines + ["  " + path for _, _, path in hits[:MAX_SEARCH_RESULTS]]


def inspect_api(path: str = "", search: str = "") -> str:
    """Raises ApiError with a message meant for the model."""
    path, search = path.strip(), search.strip()
    if bool(path) == bool(search):
        raise ApiError("Pass exactly one of `path` (describe it) or `search` (find names).")
    text = "\n".join(_search(search) if search else _resolve(path))
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n... [{len(text) - MAX_CHARS} more chars; inspect a narrower path]"
    return text
