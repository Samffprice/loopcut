"""get_object_info: how an object is actually set up, so the agent edits what is there.

A model asked to "make the wood darker" on a material it has not seen will rebuild the material
from scratch and throw the user's node tree away. Node trees, modifier settings, constraints and
animation are written out as plain data the model can read and then change surgically.

Main thread only.
"""

import bpy

from . import scene_diff

MAX_NODES = 60
MAX_FCURVES = 40


def _value(value):
    if hasattr(value, "__len__") and not isinstance(value, str):
        return [round(v, 4) if isinstance(v, float) else v for v in value]
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, bpy.types.ID):
        return f"{type(value).__name__} {value.name!r}"
    return value


def node_tree(tree) -> dict:
    """Nodes with their unlinked input values and own settings, and the links between them."""
    nodes = []
    for node in list(tree.nodes)[:MAX_NODES]:
        entry = {"name": node.name, "type": node.bl_idname}
        if node.label:
            entry["label"] = node.label
        if node.mute:
            entry["muted"] = True
        inputs = {s.name: _value(s.default_value) for s in node.inputs
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
    links = [f"{link.from_node.name}.{link.from_socket.name} -> {link.to_node.name}.{link.to_socket.name}"
             for link in tree.links]
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
        for item in group.interface.items_tree:
            if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.identifier in modifier.keys():
                inputs[item.name] = {"identifier": item.identifier, "value": _value(modifier[item.identifier])}
        entry["inputs"] = inputs  # Set with modifier["<identifier>"] = value.
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
    custom = {k: _value(obj[k]) for k in obj.keys() if isinstance(obj[k], (int, float, str, bool))}
    if custom:
        entry["custom_properties"] = custom
    return entry
