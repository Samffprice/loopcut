"""What changed in the scene: the diff the user reviews, and the ground truth the model gets.

Cursor shows a code diff before you accept an edit. The equivalent here is a list of what a turn
added, removed and changed, next to a button that takes it all back. The same diff goes to the
model after every run_python, so it reads what its code actually did instead of assuming.

snapshot() reads Blender (main thread). diff() and the formatters are pure, so they are tested
without Blender.
"""

MAX_OBJECTS = 5000      # Above this a snapshot per step would be felt; the diff says so instead.
MAX_NODE_TREES = 300    # Materials and worlds read node by node; above this only their node counts.
MAX_LINES = 40
MAX_FIELDS = 8          # Changed fields listed per object or material before "(+N more)".
_DIGITS = 4            # 0.1 mm: at 1 mm a product shot's small moves (a floor lowered 0.3 mm) went unreported.
_SCENE_SETTINGS = ("frame_start", "frame_end", "frame_current")
# Scene-level settings compared field by field: everything simple on these structs. The model
# changes render, EEVEE and color management settings as often as objects, and a change it cannot
# see is a change it re-checks with a render.
_SETTINGS_STRUCTS = ("render", "eevee", "cycles", "view_settings")
# Node properties that are layout, not look: moving a node is not a change worth reporting.
_NODE_UI = {"location", "location_absolute", "width", "height", "width_hidden", "select", "hide",
            "show_options", "show_preview", "show_texture", "use_custom_color", "color", "color_tag",
            "dimensions", "label", "warning_propagation"}
_RAY_VISIBILITY = ("camera", "diffuse", "glossy", "transmission", "volume_scatter", "shadow")


# ------------------------------------------------------------------ reading Blender

def _rounded(values) -> tuple:
    return tuple(round(v, _DIGITS) for v in values)


def rna_values(struct) -> dict:
    """Simple editable properties of a modifier, light, camera, ... as plain values."""
    values = {}
    for prop in struct.bl_rna.properties:
        name = prop.identifier
        if name in ("rna_type", "name") or prop.is_readonly or prop.type in ("POINTER", "COLLECTION"):
            continue
        if getattr(prop, "is_deprecated", False):  # Reading one warns, and the model should not use it.
            continue
        value = getattr(struct, name, None)
        if prop.type == "FLOAT":
            value = _rounded(value) if getattr(prop, "array_length", 0) else round(value, _DIGITS + 1)
        elif prop.type in ("INT", "BOOLEAN") and getattr(prop, "array_length", 0):
            value = tuple(value)
        elif prop.type == "ENUM" and prop.is_enum_flag:
            value = tuple(sorted(value))
        values[name] = value
    return values


def _plain(value):
    """A socket or property value as something comparable and short."""
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, _DIGITS)
    if hasattr(value, "__len__") and not isinstance(value, str):
        try:
            return _rounded(value)
        except TypeError:
            return str(value)[:40]
    return str(value)[:40]


def node_tree(tree, fallback_color=None, detail: bool = True) -> dict:
    """A material's or world's nodes as flat fields: "node <name>" is the node's type, "<name>.<input>"
    an unlinked input's value (or "linked"), "<name>.<property>" a node setting, "<name>.ramp" a color
    ramp's stops, and "links" every link. Flat, so a diff is a field comparison."""
    if tree is None:
        return {"color": _rounded(fallback_color)} if fallback_color is not None else {}
    if not detail:
        return {"nodes": len(tree.nodes), "links_count": len(tree.links)}
    entry = {}
    for node in tree.nodes:
        name = node.name
        entry[f"node {name}"] = node.type
        dupes = _dupes(node)
        for socket in node.inputs:
            if not socket.enabled or not hasattr(socket, "default_value"):
                continue
            key = f"{name}.{socket.identifier if socket.name in dupes else socket.name}"
            entry[key] = "linked" if socket.is_linked else _plain(socket.default_value)
        for field, value in rna_values(node).items():
            if field not in _NODE_UI:
                entry[f"{name}.{field}"] = value
        ramp = getattr(node, "color_ramp", None)
        if ramp is not None:
            entry[f"{name}.ramp"] = (ramp.interpolation,) + tuple(
                (round(e.position, _DIGITS), _rounded(e.color)) for e in ramp.elements)
    entry["links"] = tuple(sorted(f"{l.from_node.name}.{l.from_socket.name} -> {l.to_node.name}.{l.to_socket.name}"
                                  for l in tree.links))
    return entry


def _dupes(node) -> set:
    """Input names a node has more than once (Mix Shader's two Shader inputs, Mix's A and B per type)."""
    seen, dupes = set(), set()
    for socket in node.inputs:
        (dupes if socket.name in seen else seen).add(socket.name)
    return dupes


def _material(material, detail: bool = True) -> dict:
    # use_nodes is deprecated in 5.x (always on); rna_values skips it, and node_tree is what counts.
    entry = node_tree(material.node_tree, material.diffuse_color, detail)
    entry.update({f"settings.{k}": v for k, v in rna_values(material).items() if not k.startswith("preview")})
    return entry


def _object(obj, with_bounds: bool) -> dict:
    from mathutils import Vector
    location, rotation, scale = obj.matrix_world.decompose()
    entry = {
        "type": obj.type,
        "location": _rounded(location),
        "rotation_deg": tuple(round(a * 57.29578, 1) for a in rotation.to_euler()),
        "scale": _rounded(scale),
        "parent": obj.parent.name if obj.parent else None,
        "visible": not obj.hide_viewport and not obj.hide_get(),
        "renders": not obj.hide_render,
        "hidden_from_rays": tuple(n for n in _RAY_VISIBILITY if not getattr(obj, f"visible_{n}", True)),
        "data": obj.data.name if obj.data else None,
        "materials": tuple(s.material.name for s in obj.material_slots if s.material),
        "modifiers": {m.name: {"type": m.type, **rna_values(m)} for m in obj.modifiers},
        "collections": tuple(sorted(c.name for c in obj.users_collection)),
        "animated": bool(obj.animation_data and obj.animation_data.action),
    }
    if obj.type == "MESH":
        entry["verts"], entry["faces"] = len(obj.data.vertices), len(obj.data.polygons)
    if obj.type in ("LIGHT", "CAMERA", "LIGHT_PROBE") and obj.data:
        entry["settings"] = rna_values(obj.data)
    if with_bounds and obj.type not in ("CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE", "ARMATURE"):
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        entry["bounds"] = (_rounded(min(c[i] for c in corners) for i in range(3)),
                           _rounded(max(c[i] for c in corners) for i in range(3)))
    return entry


def snapshot() -> dict:
    import bpy
    scene = bpy.context.scene
    if bpy.context.mode != "OBJECT":
        # Edit-mode changes are not in obj.data until they are flushed.
        for obj in bpy.context.objects_in_mode:
            obj.update_from_editmode()
    # matrix_world and bound_box lag behind obj.location until the scene is evaluated, and the
    # model's code has usually just assigned one.
    bpy.context.view_layer.update()
    objects = list(scene.objects)
    too_many = len(objects) > MAX_OBJECTS
    detail = len(bpy.data.materials) + len(bpy.data.worlds) <= MAX_NODE_TREES
    return {
        "too_many": too_many,
        "objects": {} if too_many else {o.name: _object(o, with_bounds=True) for o in objects},
        "object_count": len(objects),
        "materials": {m.name: _material(m, detail) for m in bpy.data.materials},
        "worlds": {w.name: node_tree(w.node_tree, w.color, detail) for w in bpy.data.worlds},
        "collections": {c.name: tuple(sorted(o.name for o in c.objects)) for c in bpy.data.collections},
        "scene": {**{k: getattr(scene, k) for k in _SCENE_SETTINGS},
                  **{f"{struct}.{k}": v for struct in _SETTINGS_STRUCTS if getattr(scene, struct, None) is not None
                     for k, v in rna_values(getattr(scene, struct)).items()},
                  "camera": scene.camera.name if scene.camera else None,
                  "world": scene.world.name if scene.world else None},
        "mode": bpy.context.mode,
    }


# ------------------------------------------------------------------ pure

def _changed_keys(before: dict, after: dict) -> list[str]:
    return [k for k in after if before.get(k) != after[k]] + [k for k in before if k not in after]


def _fields(old: dict, new: dict) -> str:
    keys = _changed_keys(old, new)
    text = ", ".join(f"{f} {old.get(f)!r} -> {new.get(f)!r}" for f in keys[:MAX_FIELDS])
    return text + (f" (+{len(keys) - MAX_FIELDS} more)" if len(keys) > MAX_FIELDS else "")


def _tree_changes(old: dict, new: dict) -> str:
    """Nodes added and removed, links gained and lost, then changed values; the inputs of a node
    that was just added or removed are that node, not changes of their own."""
    notes = []
    added = [k[5:] for k in new if k.startswith("node ") and k not in old]
    removed = [k[5:] for k in old if k.startswith("node ") and k not in new]
    notes += [f"+node {n} ({new['node ' + n]})" for n in added] + [f"-node {n}" for n in removed]
    gone = {n + "." for n in added + removed}
    old_links, new_links = set(old.get("links") or ()), set(new.get("links") or ())
    notes += [f"+link {l}" for l in sorted(new_links - old_links)] + [f"-link {l}" for l in sorted(old_links - new_links)]
    values = [k for k in _changed_keys(old, new)
              if k != "links" and not k.startswith("node ") and not any(k.startswith(g) for g in gone)]
    notes += [f"{k} {old.get(k)!r} -> {new.get(k)!r}" for k in values]
    shown = "; ".join(notes[:MAX_FIELDS])
    return shown + (f" (+{len(notes) - MAX_FIELDS} more)" if len(notes) > MAX_FIELDS else "")


def _object_changes(before: dict, after: dict) -> list[str]:
    notes = []
    for key in _changed_keys(before, after):
        old, new = before.get(key), after.get(key)
        if key == "modifiers":
            for name in new:
                if name not in old:
                    notes.append(f"+modifier {name} ({new[name]['type']})")
                elif old[name] != new[name]:
                    notes.append(f"modifier {name}: {_fields(old[name], new[name])}")
            notes += [f"-modifier {name}" for name in old if name not in new]
        elif key == "settings":
            notes.append(_fields(old or {}, new or {}))
        elif key == "bounds":
            notes.append(f"bounds now {list(new[0])}..{list(new[1])}" if new else "bounds gone")
        elif key in ("verts", "faces"):
            if key == "verts" or "verts" not in _changed_keys(before, after):
                notes.append(f"mesh {before.get('verts')}v/{before.get('faces')}f -> "
                             f"{after.get('verts')}v/{after.get('faces')}f")
        else:
            notes.append(f"{key} {old!r} -> {new!r}")
    return notes


def diff(before: dict, after: dict) -> dict:
    """{"added": [...], "removed": [...], "changed": [...], "other": [...]} of display strings."""
    result = {"added": [], "removed": [], "changed": [], "other": []}
    if before["too_many"] or after["too_many"]:
        delta = after["object_count"] - before["object_count"]
        result["other"].append(f"{after['object_count']} objects ({delta:+d}); too many to compare one by one")
    for name, entry in after["objects"].items():
        if name not in before["objects"] and not before["too_many"]:
            where = f" at {list(entry['bounds'][0])}..{list(entry['bounds'][1])}" if entry.get("bounds") else ""
            result["added"].append(f"{name} ({entry['type'].lower()}){where}")
        elif name in before["objects"]:
            notes = _object_changes(before["objects"][name], entry)
            if notes:
                result["changed"].append(f"{name}: " + "; ".join(notes))
    if not after["too_many"]:
        result["removed"] += [f"{name} ({entry['type'].lower()})"
                              for name, entry in before["objects"].items() if name not in after["objects"]]
    for kind in ("materials", "worlds", "collections"):
        old, new = before.get(kind, {}), after.get(kind, {})
        label = kind[:-1]
        result["added"] += [f"{label} {name}" for name in new if name not in old]
        result["removed"] += [f"{label} {name}" for name in old if name not in new]
        for name in new:
            if name in old and old[name] != new[name]:
                if kind != "collections":
                    result["changed"].append(f"{label} {name}: {_tree_changes(old[name], new[name])}")
                else:
                    gained = sorted(set(new[name]) - set(old[name]))
                    lost = sorted(set(old[name]) - set(new[name]))
                    result["changed"].append(f"collection {name}: "
                                             + "; ".join(filter(None, [gained and f"+{', '.join(gained)}",
                                                                       lost and f"-{', '.join(lost)}"])))
    for key in _changed_keys(before["scene"], after["scene"]):
        result["other"].append(f"scene {key} {before['scene'].get(key)!r} -> {after['scene'].get(key)!r}")
    if before["mode"] != after["mode"]:
        result["other"].append(f"mode {before['mode']} -> {after['mode']}")
    return result


def is_empty(changes: dict) -> bool:
    return not any(changes.values())


def lines(changes: dict) -> list[str]:
    out = ([f"+ {text}" for text in changes["added"]] + [f"- {text}" for text in changes["removed"]]
           + [f"~ {text}" for text in changes["changed"]] + [f"~ {text}" for text in changes["other"]])
    if len(out) > MAX_LINES:
        out = out[:MAX_LINES] + [f"... and {len(out) - MAX_LINES} more"]
    return out


def for_model(changes: dict) -> str:
    if is_empty(changes):
        return ("Scene changes: none. (Tracked: objects, transforms, visibility, modifiers, meshes, lights, "
                "cameras, light probes, materials and worlds node by node, collections and render settings. If "
                "you expected one of those to change, the code did not do what you think.)")
    return "Scene changes (+ added, - removed, ~ changed; bounds are world space):\n" + "\n".join(lines(changes))


def headline(changes: dict) -> str:
    counts = [(len(changes["added"]), "added"), (len(changes["removed"]), "removed"),
              (len(changes["changed"]) + len(changes["other"]), "changed")]
    return ", ".join(f"{n} {word}" for n, word in counts if n) or "No changes"
