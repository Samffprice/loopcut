"""What changed in the scene: the diff the user reviews, and the ground truth the model gets.

Cursor shows a code diff before you accept an edit. The equivalent here is a list of what a turn
added, removed and changed, next to a button that takes it all back. The same diff goes to the
model after every run_python, so it reads what its code actually did instead of assuming.

snapshot() reads Blender (main thread). diff() and the formatters are pure, so they are tested
without Blender.
"""

MAX_OBJECTS = 5000      # Above this a snapshot per step would be felt; the diff says so instead.
MAX_LINES = 40
_DIGITS = 3
_SCENE_SETTINGS = ("frame_start", "frame_end", "frame_current")
_RENDER_SETTINGS = ("engine", "resolution_x", "resolution_y", "resolution_percentage", "fps",
                    "film_transparent")


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


def _material(material) -> dict:
    entry = {"nodes": 0, "links": 0}
    tree = material.node_tree
    if tree is None:
        entry["color"] = _rounded(material.diffuse_color)
        return entry
    entry["nodes"], entry["links"] = len(tree.nodes), len(tree.links)
    for node in tree.nodes:
        if node.type == "BSDF_PRINCIPLED":
            for name in ("Base Color", "Metallic", "Roughness", "Alpha"):
                socket = node.inputs.get(name)
                if socket is None:
                    continue
                if socket.is_linked:
                    entry[name] = "linked"
                else:
                    value = socket.default_value
                    entry[name] = _rounded(value) if hasattr(value, "__len__") else round(value, _DIGITS)
            break
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
        "data": obj.data.name if obj.data else None,
        "materials": tuple(s.material.name for s in obj.material_slots if s.material),
        "modifiers": {m.name: {"type": m.type, **rna_values(m)} for m in obj.modifiers},
        "collections": tuple(sorted(c.name for c in obj.users_collection)),
        "animated": bool(obj.animation_data and obj.animation_data.action),
    }
    if obj.type == "MESH":
        entry["verts"], entry["faces"] = len(obj.data.vertices), len(obj.data.polygons)
    if obj.type in ("LIGHT", "CAMERA") and obj.data:
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
    return {
        "too_many": too_many,
        "objects": {} if too_many else {o.name: _object(o, with_bounds=True) for o in objects},
        "object_count": len(objects),
        "materials": {m.name: _material(m) for m in bpy.data.materials},
        "collections": {c.name: tuple(sorted(o.name for o in c.objects)) for c in bpy.data.collections},
        "scene": {**{k: getattr(scene, k) for k in _SCENE_SETTINGS},
                  **{f"render.{k}": getattr(scene.render, k) for k in _RENDER_SETTINGS},
                  "camera": scene.camera.name if scene.camera else None},
        "mode": bpy.context.mode,
    }


# ------------------------------------------------------------------ pure

def _changed_keys(before: dict, after: dict) -> list[str]:
    return [k for k in after if before.get(k) != after[k]] + [k for k in before if k not in after]


def _object_changes(before: dict, after: dict) -> list[str]:
    notes = []
    for key in _changed_keys(before, after):
        old, new = before.get(key), after.get(key)
        if key == "modifiers":
            for name in new:
                if name not in old:
                    notes.append(f"+modifier {name} ({new[name]['type']})")
                elif old[name] != new[name]:
                    fields = ", ".join(f"{f} {old[name].get(f)!r} -> {new[name][f]!r}"
                                       for f in _changed_keys(old[name], new[name])[:4])
                    notes.append(f"modifier {name}: {fields}")
            notes += [f"-modifier {name}" for name in old if name not in new]
        elif key == "settings":
            fields = ", ".join(f"{f} {old.get(f)!r} -> {new.get(f)!r}" for f in _changed_keys(old, new)[:4])
            notes.append(fields)
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
    for kind in ("materials", "collections"):
        old, new = before[kind], after[kind]
        label = kind[:-1]
        result["added"] += [f"{label} {name}" for name in new if name not in old]
        result["removed"] += [f"{label} {name}" for name in old if name not in new]
        for name in new:
            if name in old and old[name] != new[name]:
                if kind == "materials":
                    fields = ", ".join(f"{f} {old[name].get(f)!r} -> {new[name].get(f)!r}"
                                       for f in _changed_keys(old[name], new[name])[:4])
                    result["changed"].append(f"material {name}: {fields}")
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
        return "Scene changes: none. (If you expected a change, the code did not do what you think.)"
    return "Scene changes (+ added, - removed, ~ changed; bounds are world space):\n" + "\n".join(lines(changes))


def headline(changes: dict) -> str:
    counts = [(len(changes["added"]), "added"), (len(changes["removed"]), "removed"),
              (len(changes["changed"]) + len(changes["other"]), "changed")]
    return ", ".join(f"{n} {word}" for n, word in counts if n) or "No changes"
