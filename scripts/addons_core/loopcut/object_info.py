"""get_object_info: how an object is actually set up, so the agent edits what is there.

A model asked to "make the wood darker" on a material it has not seen will rebuild the material
from scratch and throw the user's node tree away. Node trees, modifier settings, constraints and
animation are written out as plain data the model can read and then change surgically.

Main thread only.
"""

import json

import bpy

from . import scene_diff

MAX_NODES = 60
MAX_FCURVES = 40
MAX_PROPERTY_CHARS = 300  # One custom property's value as JSON; a longer one is cut.
# What an entry always keeps when it has to be cut to fit: where it is and what it is.
_CORE = {"name", "type", "location", "rotation_deg", "scale", "dimensions", "visible", "selected", "bounds",
         "parent", "children", "data", "collections", "omitted"}
_ITEMS = {"materials": "material", "modifiers": "modifier", "constraints": "constraint"}  # Left out one by one.


def _value(value):
    if isinstance(value, bpy.types.ID):
        return f"{type(value).__name__} {value.name!r}"
    if hasattr(value, "__len__") and not isinstance(value, str):
        # A Matrix socket or property iterates as row Vectors: rounded all the way down, not per row.
        return list(scene_diff.rounded_value(value, 4))
    if isinstance(value, float):
        return round(value, 4)
    return value


def _keys(struct) -> list:
    """A struct's ID property names, or none where its type has no ID properties: keys() raises
    TypeError there, and since Blender 5 that includes modifiers."""
    try:
        return list(struct.keys())
    except TypeError:
        return []


def _plain(value):
    """An ID property as plain data: groups and arrays through to_dict/to_list, IDs by name,
    anything else by its repr."""
    for convert in ("to_dict", "to_list"):
        if hasattr(value, convert):
            try:
                value = getattr(value, convert)()
            except Exception:
                return repr(value)
            break
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (bool, int, float, str)) or value is None or isinstance(value, bpy.types.ID):
        return _value(value)
    return repr(value)


def _custom_properties(obj) -> dict:
    """The user's custom properties (obj["name"]), each cut to MAX_PROPERTY_CHARS. Properties an
    add-on registered on the type (older Blender lists them in keys() too) and _RNA_UI are not the
    user's."""
    registered = {p.identifier for p in getattr(getattr(obj, "bl_rna", None), "properties", ())}
    custom = {}
    for key in _keys(obj):
        if key.startswith("_") or key in registered:
            continue
        value = _plain(obj[key])
        text = json.dumps(value, separators=(",", ":"), default=repr)
        if len(text) > MAX_PROPERTY_CHARS:
            value = f"{text[:MAX_PROPERTY_CHARS]}… ({len(text)} chars; read obj[{key!r}] for all of it)"
        custom[key] = value
    return custom


def node_tree(tree) -> dict:
    """Nodes with their unlinked input values and own settings, and the links between them. A
    socket name a node has twice carries its index (scene_diff.socket_label)."""
    nodes = []
    for node in list(tree.nodes)[:MAX_NODES]:
        entry = {"name": node.name, "type": node.bl_idname}
        if node.label:
            entry["label"] = node.label
        if node.mute:
            entry["muted"] = True
        inputs = {scene_diff.socket_label(s, node.inputs): _value(s.default_value) for s in node.inputs
                  if s.enabled and not s.is_linked and hasattr(s, "default_value")}
        if inputs:
            entry["inputs"] = inputs
        settings = {k: v for k, v in scene_diff.rna_values(node).items()
                    if k not in _NODE_NOISE and not k.startswith(("bl_", "show_", "use_custom"))}
        if settings:
            entry["settings"] = settings
        if getattr(node, "node_tree", None):
            entry["group"] = node.node_tree.name
        nodes.append(entry)
    links = [scene_diff.link_line(link) for link in tree.links]
    result = {"nodes": nodes, "links": links}
    if len(tree.nodes) > MAX_NODES:
        result["note"] = f"{len(tree.nodes)} nodes; first {MAX_NODES} shown"
    return result


_NODE_NOISE = {"location", "location_absolute", "width", "height", "select", "hide", "mute", "label",
               "color", "color_tag", "warning_propagation", "parent"}


def describe_material(material) -> dict:
    entry = {"name": material.name, "users": material.users}
    if material.node_tree:
        entry.update(node_tree(material.node_tree))
    else:
        entry["diffuse_color"] = _value(material.diffuse_color)
    return entry


def describe_world(world, scene_world: bool = False) -> dict:
    """A world as get_object_info shows it: its node tree like a material's, or its colour."""
    entry = {"name": world.name, "type": "WORLD", "users": world.users}
    if scene_world:
        entry["scene_world"] = True
    if world.node_tree:
        entry.update(node_tree(world.node_tree))
    else:
        entry["color"] = _value(world.color)
    return entry


def _modifier(modifier) -> dict:
    entry = {"name": modifier.name, "type": modifier.type, **scene_diff.rna_values(modifier)}
    for prop in modifier.bl_rna.properties:  # Pointers (target objects, node groups) by name.
        if prop.type == "POINTER" and prop.identifier != "rna_type":
            target = getattr(modifier, prop.identifier, None)
            if isinstance(target, bpy.types.ID):
                entry[prop.identifier] = _value(target)
    group = getattr(modifier, "node_group", None) if modifier.type == "NODES" else None
    if group:
        inputs = {}
        properties = getattr(modifier, "properties", None)
        sockets = getattr(properties, "inputs", None)
        for item in group.interface.items_tree:
            if item.item_type != "SOCKET" or item.in_out not in ("INPUT", "BOTH"):
                continue
            if sockets is not None:  # Blender 5.2 moved sockets off modifier ID properties.
                socket = getattr(sockets, item.identifier, None)
                if socket is None or not hasattr(socket, "value"):
                    continue  # Geometry sockets have no editable value.
                value = {"identifier": item.identifier, "value": _value(socket.value),
                         "access": f"properties.inputs.{item.identifier}.value"}
                if getattr(socket, "type", None) == "ATTRIBUTE":
                    value.update(type="ATTRIBUTE", attribute_name=socket.attribute_name)
                inputs[item.name] = value
            elif item.identifier in _keys(modifier):  # Older Blender releases.
                inputs[item.name] = {"identifier": item.identifier, "value": _value(modifier[item.identifier])}
        entry["inputs"] = inputs
        entry["node_group_tree"] = node_tree(group)
    return entry


def _animation(obj) -> dict | None:
    data = obj.animation_data
    if data is None or data.action is None:
        return None
    action = data.action
    entry = {"action": action.name, "frame_range": _value(action.frame_range)}
    slot = getattr(data, "action_slot", None)
    fcurves = []
    if slot is not None and hasattr(action, "layers"):  # Slotted actions (Blender 4.4+).
        entry["slot"] = slot.name_display
        for layer in action.layers:
            for strip in layer.strips:
                bag = strip.channelbag(slot)
                if bag:
                    fcurves += list(bag.fcurves)
    else:
        fcurves = list(getattr(action, "fcurves", []))
    entry["fcurves"] = [
        {"path": f"{curve.data_path}[{curve.array_index}]",
         "keys": [[round(k.co[0], 2), round(k.co[1], 4)] for k in list(curve.keyframe_points)[:12]]}
        for curve in fcurves[:MAX_FCURVES]]
    return entry


def describe(obj) -> dict:
    entry: dict = {"data": _value(obj.data) if obj.data else None,
                   "collections": [c.name for c in obj.users_collection],
                   "children": [c.name for c in obj.children][:50]}
    if obj.modifiers:
        entry["modifiers"] = [_modifier(m) for m in obj.modifiers]
    if obj.constraints:
        entry["constraints"] = [{"name": c.name, "type": c.type, **scene_diff.rna_values(c),
                                 "target": _value(getattr(c, "target", None))} for c in obj.constraints]
    materials = [s.material for s in obj.material_slots if s.material]
    if materials:
        entry["materials"] = [describe_material(m) for m in materials]
    if obj.type in ("LIGHT", "CAMERA", "FONT", "CURVE") and obj.data:
        entry["data_settings"] = scene_diff.rna_values(obj.data)
    if obj.type == "MESH":
        mesh = obj.data
        entry["mesh"] = {"verts": len(mesh.vertices), "edges": len(mesh.edges), "faces": len(mesh.polygons),
                         "uv_maps": [uv.name for uv in mesh.uv_layers],
                         "vertex_groups": [g.name for g in obj.vertex_groups],
                         "shape_keys": [k.name for k in mesh.shape_keys.key_blocks] if mesh.shape_keys else [],
                         "attributes": [a.name for a in mesh.attributes if not a.is_internal][:30]}
    animation = _animation(obj)
    if animation:
        entry["animation"] = animation
    custom = _custom_properties(obj)
    if custom:
        entry["custom_properties"] = custom
    return entry


def brief(obj) -> dict:
    """detail=transform, beside tools.object_summary: dimensions, children, every material slot by
    name ("" for an empty one) and the mesh's counts; no node trees, settings or animation."""
    entry: dict = {"dimensions": _value(obj.dimensions), "children": [c.name for c in obj.children][:50]}
    if obj.material_slots:
        entry["material_slots"] = [slot.material.name if slot.material else "" for slot in obj.material_slots]
    if obj.type == "MESH":
        mesh = obj.data
        entry["mesh"] = {"verts": len(mesh.vertices), "edges": len(mesh.edges), "faces": len(mesh.polygons)}
    return entry


def _json(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def fit(entries: list, limit: int) -> tuple[str, list]:
    """entries as compact JSON within limit characters, cut between sections and never inside a
    value: the largest units go first (a material, a modifier or a constraint one by one, or a whole
    section such as animation), each named with its size in its entry's "omitted"; then, if need be,
    whole entries from the end. Returns (text, what was left out)."""
    units = []
    for n, entry in enumerate(entries):
        for key, value in entry.items():
            if key in _CORE:
                continue
            if key in _ITEMS and isinstance(value, list):
                units += [(len(_json(item)), n, key, i, f"{_ITEMS[key]} {item.get('name', i) if isinstance(item, dict) else i}")
                          for i, item in enumerate(value)]
            else:
                units.append((len(_json(value)), n, key, None, key))
    units.sort(key=lambda unit: -unit[0])
    dropped, left_out = set(), []

    def view(whole: int) -> list:
        shown = []
        for n, entry in enumerate(entries[:whole]):
            kept, omitted = {}, [text for _, m, _, _, text in left_out if m == n]
            for key, value in entry.items():
                if (n, key, None) in dropped:
                    continue
                if key in _ITEMS and isinstance(value, list):
                    value = [item for i, item in enumerate(value) if (n, key, i) not in dropped]
                    if not value:
                        continue
                kept[key] = value
            if omitted:
                kept["omitted"] = omitted
            shown.append(kept)
        return shown

    text = _json(entries)
    for size, n, key, i, label in units:
        if len(text) <= limit:
            break
        dropped.add((n, key, i))
        left_out.append((size, n, key, i, f"{label} ({size:,} chars)"))
        text = _json(view(len(entries)))
    whole = len(entries)
    while len(text) > limit and whole > 1:
        whole -= 1
        rest = [entry.get("name", "?") for entry in entries[whole:]]
        text = _json(view(whole) + [{"omitted": f"{', '.join(rest)}: ask for them alone"}])
        left_out.append((0, whole, None, None, rest[0]))
    return text, [item[4] for item in left_out]
