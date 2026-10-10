"""What changed in the scene: the diff the user reviews, and the ground truth the model gets.

Cursor shows a code diff before you accept an edit. The equivalent here is a list of what a turn
added, removed and changed, next to a button that takes it all back. The same diff goes to the
model after every run_python, so it reads what its code actually did instead of assuming.

snapshot() reads Blender (main thread). diff() and the formatters are pure, so they are tested
without Blender.
"""

MAX_OBJECTS = 5000      # Above this a snapshot per step would be felt; the diff says so instead.
MAX_SHAPE_VERTS = 1_000_000  # Vertices whose positions a snapshot hashes (about 50 ms); past this, counts and bounds.
MAX_UV_CORNERS = 2_000_000   # Face corners whose active UV map a snapshot hashes; past this, the map's name only.
# Pixels a snapshot hashes (CRC-32 of the float pixels: a 4096 px square, about 40 ms); past this, a
# packed image's packed data (MAX_PACKED_BYTES of it), else unsaved edits only. Was 2048 px with
# blake2b, and the Arcane chest's 4K atlas (eval 20260930-154037 W0039-W0041) read "Scene changes:
# none" after every paint that packed in the same call.
MAX_IMAGE_PIXELS = 16_777_216
MAX_PACKED_BYTES = 256 * 2 ** 20
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

def rounded_value(value, digits: int = _DIGITS):
    """A float rounded; a Vector, Color, Euler, Quaternion, Matrix, property array or IDProperty
    array as a tuple of rounded floats, nested as deep as the value is; anything else as it is.

    round() takes none of Blender's array types, and rounding "each item" is not enough: a Matrix
    and a multi-dimensional property array iterate as rows. A BlenderKit floor lamp's hook modifier
    (matrix_inverse, 4x4) made every snapshot, so every run_python, fail with "type Vector doesn't
    define __round__ method" (eval 20260927-043942 shared_support)."""
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, digits)
    # mathutils types iterate through __len__ and __getitem__, with no __iter__; an ID has neither.
    if isinstance(value, (str, bytes)) or not hasattr(value, "__len__") or hasattr(value, "keys"):
        return value
    if hasattr(value, "to_list"):  # IDPropertyArray: a copy, not a view into Blender's memory.
        value = value.to_list()
    return tuple(rounded_value(v, digits) for v in value)


def _rounded(values) -> tuple:
    return tuple(rounded_value(v) for v in values)


def rna_values(struct) -> dict:
    """Simple editable properties of a modifier, light, camera, ... as plain values: numbers and
    arrays as (nested) tuples of rounded floats, never a live Blender object."""
    values = {}
    for prop in struct.bl_rna.properties:
        name = prop.identifier
        if name in ("rna_type", "name") or prop.is_readonly or prop.type in ("POINTER", "COLLECTION"):
            continue
        if getattr(prop, "is_deprecated", False):  # Reading one warns, and the model should not use it.
            continue
        value = getattr(struct, name, None)
        if prop.type in ("FLOAT", "INT", "BOOLEAN"):
            value = rounded_value(value, _DIGITS if getattr(prop, "array_length", 0) else _DIGITS + 1)
        elif prop.type == "ENUM" and prop.is_enum_flag:
            value = tuple(sorted(value))
        values[name] = value
    return values


def _plain(value):
    """A socket or property value as something comparable and short."""
    value = rounded_value(value)
    if isinstance(value, (bool, int, float)) or (
            isinstance(value, tuple) and all(isinstance(v, (bool, int, float, tuple)) for v in value)):
        return value
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
    entry["links"] = tuple(sorted(link_line(link) for link in tree.links))
    return entry


def socket_label(socket, sockets) -> str:
    """A socket's name, with its index when its node has another of that name, so which of Mix
    Shader's two Shader inputs a link reaches shows, and a swap with it: "Shader (inputs[2])"."""
    same = [i for i, other in enumerate(sockets) if other.name == socket.name]
    if len(same) < 2:
        return socket.name
    index = next((i for i in same if sockets[i].identifier == socket.identifier), same[0])
    return f"{socket.name} ({'outputs' if socket.is_output else 'inputs'}[{index}])"


def link_line(link) -> str:
    """A link as "From.Socket -> To.Socket", sockets by socket_label."""
    return (f"{link.from_node.name}.{socket_label(link.from_socket, link.from_node.outputs)} -> "
            f"{link.to_node.name}.{socket_label(link.to_socket, link.to_node.inputs)}")


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


def _modifiers(obj) -> dict:
    return {m.name: {"type": m.type, **rna_values(m)} for m in obj.modifiers}


def _shapes(objects) -> dict:
    """{mesh session_uid: a hash of its vertex positions} for the objects' meshes, each mesh once, while
    MAX_SHAPE_VERTS lasts: an edit that moves vertices but keeps the counts and the bounds (a lid
    pushed in, a bevel applied) is still a change. Without it the diff said "Scene changes: none"
    after real vertex moves (m4_effort_replay_analysis §5.7)."""
    from .ownership import coords_hash
    shapes, left = {}, MAX_SHAPE_VERTS
    for obj in objects:
        mesh = obj.data if obj.type == "MESH" else None
        if mesh is None or mesh.session_uid in shapes:
            continue
        count = len(mesh.vertices)
        if count > left:
            continue
        shapes[mesh.session_uid] = coords_hash(mesh.vertices, "co", count) if count else ""
        left -= count
    return shapes


def _uvs(objects) -> dict:
    """{mesh session_uid: (active UV map name or "", its hash or None)} while MAX_UV_CORNERS lasts:
    uv_unwrap or a run_python unwrap changes no count, bound or position, and the model was told
    "Scene changes: none" after one."""
    from .ownership import uv_maps
    uvs, left = {}, MAX_UV_CORNERS
    for obj in objects:
        mesh = obj.data if obj.type == "MESH" else None
        if mesh is None or mesh.session_uid in uvs:
            continue
        count = len(mesh.loops)
        if count > left:  # The name alone; in Edit Mode not even that (its layers read empty there).
            active = mesh.uv_layers.active
            uvs[mesh.session_uid] = None if mesh.is_editmode else (active.name if active else "", None)
            continue
        uvs[mesh.session_uid] = (uv_maps(mesh, active_only=True) or [("", "")])[0]
        left -= count
    return uvs


def _tree_images(tree, seen=None) -> list:
    """The images a node tree's Image Texture nodes read, node groups included."""
    seen = set() if seen is None else seen
    if tree is None or tree.session_uid in seen:
        return []
    seen.add(tree.session_uid)
    found = []
    for node in tree.nodes:
        if node.type == "TEX_IMAGE" and node.image is not None:
            found.append(node.image)
        elif node.type == "GROUP":
            found += _tree_images(node.node_tree, seen)
    return found


def _images(objects) -> dict:
    """{image name: {size, pixels hash or None, packed (size:CRC) or "", dirty, materials}} for the
    images the objects' materials read, then every other local one but Blender's render buffers and
    our own temporary ones: a paint edit changes no node, so without it the model was told "Scene
    changes: none" after painting, and a mask painted before it is assigned is a change too. Pixels
    hashed in that order, only once loaded (reading an unloaded one would load it), while
    MAX_IMAGE_PIXELS lasts; past it, a packed one's packed data while MAX_PACKED_BYTES lasts (a paint
    packed in the same call), and the unsaved-edits flag."""
    import zlib
    import bpy
    import numpy as np
    from .ownership import TEMP, packed_crc
    users = {}
    for material in {s.material for obj in objects for s in obj.material_slots if s.material}:
        for image in _tree_images(material.node_tree):
            users.setdefault(image, set()).add(material.name)
    rest = [i for i in bpy.data.images if i.library is None and i not in users and not i.get(TEMP)
            and i.type not in ("RENDER_RESULT", "COMPOSITING")]
    images, left, packed_left = {}, MAX_IMAGE_PIXELS, MAX_PACKED_BYTES
    for image in sorted(users, key=lambda i: i.name) + sorted(rest, key=lambda i: i.name):
        width, height = image.size[:] if image.has_data else (0, 0)
        digest, packed = None, ""
        if image.has_data and 0 < width * height <= left:
            pixels = np.empty(width * height * image.channels, dtype=np.float32)
            image.pixels.foreach_get(pixels)
            digest, left = f"{zlib.crc32(memoryview(pixels)):08x}", left - width * height
        elif image.packed_file is not None and image.packed_file.size <= packed_left:
            packed, packed_left = packed_crc(image), packed_left - image.packed_file.size
        images[image.name] = {"size": (width, height), "pixels": digest, "packed": packed, "dirty": image.is_dirty,
                              "materials": tuple(sorted(users.get(image, ())))}
    return images


def _texts() -> dict:
    """{text block name: (lines, CRC-32 of its contents)}: a recipe or a painter's source written to
    one is a change (eval 20260930-154037: the run's ArcaneRecipe edits were never listed)."""
    import zlib
    import bpy
    return {t.name: (len(t.lines), f"{zlib.crc32(t.as_string().encode('utf-8', 'surrogatepass')):08x}")
            for t in bpy.data.texts if t.library is None}


def _object(obj, shapes=None, uvs=None) -> dict:
    """The first pass: nothing read here evaluates the scene. _place fills in the placement after."""
    entry = {
        "uid": obj.session_uid,  # Identity across a rename: diff() reads this, never shows it as a field.
        "type": obj.type,
        "location": None, "rotation_deg": None, "scale": None,  # _place, once the scene is evaluated.
        "parent": obj.parent.name if obj.parent else None,
        "visible": not obj.hide_viewport and not obj.hide_get(),
        "renders": not obj.hide_render,
        "hidden_from_rays": tuple(n for n in _RAY_VISIBILITY if not getattr(obj, f"visible_{n}", True)),
        "data": obj.data.name if obj.data else None,
        "materials": tuple(s.material.name for s in obj.material_slots if s.material),
        "modifiers": _modifiers(obj),
        "collections": tuple(sorted(c.name for c in obj.users_collection)),
        "animated": bool(obj.animation_data and obj.animation_data.action),
    }
    if obj.type == "MESH":  # The original mesh's counts: reading them does not apply modifiers.
        entry["verts"], entry["faces"] = len(obj.data.vertices), len(obj.data.polygons)
        entry["shape"] = (shapes or {}).get(obj.data.session_uid)
        entry["uv"] = (uvs or {}).get(obj.data.session_uid)
    if obj.type in ("LIGHT", "CAMERA", "LIGHT_PROBE") and obj.data:
        entry["settings"] = rna_values(obj.data)
    return entry


def _place(obj, entry: dict) -> None:
    """The second pass, after the scene is evaluated: matrix_world and bound_box."""
    from mathutils import Vector
    location, rotation, scale = obj.matrix_world.decompose()
    entry["location"] = _rounded(location)
    entry["rotation_deg"] = tuple(round(a * 57.29578, 1) for a in rotation.to_euler())
    entry["scale"] = _rounded(scale)
    if obj.type not in ("CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE", "ARMATURE"):
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        entry["bounds"] = (_rounded(min(c[i] for c in corners) for i in range(3)),
                           _rounded(max(c[i] for c in corners) for i in range(3)))


def snapshot(guard=None) -> dict:
    """guard: run_python passes cost_guard.check, which runs after the first pass and before the
    scene is evaluated, and returns the objects whose modifiers it changed; those are read again."""
    import bpy
    scene = bpy.context.scene
    if bpy.context.mode != "OBJECT":
        # Edit-mode changes are not in obj.data until they are flushed.
        for obj in bpy.context.objects_in_mode:
            obj.update_from_editmode()
    objects = list(scene.objects)
    too_many = len(objects) > MAX_OBJECTS
    detail = len(bpy.data.materials) + len(bpy.data.worlds) <= MAX_NODE_TREES
    # First pass: names, settings, modifier settings and mesh counts, none of which evaluates.
    shapes = {} if too_many else _shapes(objects)
    uvs = {} if too_many else _uvs(objects)
    entries = {} if too_many else {o.name: _object(o, shapes, uvs) for o in objects}
    if guard is not None:
        for name in guard():
            if name in entries:
                entries[name]["modifiers"] = _modifiers(scene.objects[name])
    # Second pass: matrix_world and bound_box lag behind obj.location until the scene is
    # evaluated, and the model's code has usually just assigned one. This is what evaluates
    # every modifier, hence the guard before it.
    bpy.context.view_layer.update()
    for obj in objects if entries else ():
        _place(obj, entries[obj.name])
    return {
        "too_many": too_many,
        "objects": entries,
        "object_count": len(objects),
        "materials": {m.name: _material(m, detail) for m in bpy.data.materials},
        "worlds": {w.name: node_tree(w.node_tree, w.color, detail) for w in bpy.data.worlds},
        "images": {} if too_many else _images(objects),
        "texts": _texts(),  # Its presence also says "images" lists every local image, not only the used ones.
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
        if key == "uid":
            continue  # Identity, not a field; a rename is reported by diff() itself.
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
        elif key == "shape":
            # Counts that changed say it already; a mesh past MAX_SHAPE_VERTS (None) says nothing.
            if old is not None and new is not None and not {"verts", "faces"} & set(_changed_keys(before, after)):
                notes.append(f"mesh vertices moved ({after.get('verts')}v, same counts)")
        elif key == "uv":
            # (active map, hash): a new or renamed active map always; new coordinates when the counts
            # held (a count change moves every corner) and both were hashed.
            if old is None or new is None:
                continue
            if old[0] != new[0]:
                notes.append(f"active UV map {old[0] or 'none'} -> {new[0] or 'none'}")
            elif old[1] and new[1] and not {"verts", "faces"} & set(_changed_keys(before, after)):
                notes.append(f"UVs edited (map {new[0]})")
        else:
            notes.append(f"{key} {old!r} -> {new!r}")
    return notes


def _renames(before: dict, after: dict) -> dict[str, str]:
    """{old name: new name} for objects that kept their session_uid (the same datablock) but
    changed name: a rename, not one object removed and an unrelated one added. Skipped once
    either snapshot is too_many, same as the rest of the per-object comparison."""
    if before["too_many"] or after["too_many"]:
        return {}
    removed_by_uid = {entry["uid"]: name for name, entry in before["objects"].items()
                      if name not in after["objects"] and entry.get("uid") is not None}
    return {removed_by_uid[entry["uid"]]: name for name, entry in after["objects"].items()
           if name not in before["objects"] and entry.get("uid") in removed_by_uid}


def diff(before: dict, after: dict) -> dict:
    """{"added": [...], "removed": [...], "changed": [...], "other": [...]} of display strings, and
    "unseen": images whose pixels neither snapshot could compare (for_model names them)."""
    result = {"added": [], "removed": [], "changed": [], "other": [], "unseen": []}
    if before["too_many"] or after["too_many"]:
        delta = after["object_count"] - before["object_count"]
        result["other"].append(f"{after['object_count']} objects ({delta:+d}); too many to compare one by one")
    renamed = _renames(before, after)
    renamed_new, renamed_old = set(renamed.values()), set(renamed)
    for name, entry in after["objects"].items():
        if name in renamed_new:
            continue
        if name not in before["objects"] and not before["too_many"]:
            where = f" at {list(entry['bounds'][0])}..{list(entry['bounds'][1])}" if entry.get("bounds") else ""
            result["added"].append(f"{name} ({entry['type'].lower()}){where}")
        elif name in before["objects"]:
            notes = _object_changes(before["objects"][name], entry)
            if notes:
                result["changed"].append(f"{name}: " + "; ".join(notes))
    for old_name, new_name in renamed.items():
        notes = _object_changes(before["objects"][old_name], after["objects"][new_name])
        label = f"{old_name} → {new_name} (renamed)"
        result["changed"].append(label + ("; " + "; ".join(notes) if notes else ""))
    if not after["too_many"]:
        result["removed"] += [f"{name} ({entry['type'].lower()})"
                              for name, entry in before["objects"].items()
                              if name not in after["objects"] and name not in renamed_old]
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
    old_images, new_images = before.get("images") or {}, after.get("images") or {}
    for name, entry in new_images.items():
        note = _image_change(old_images.get(name), entry)
        if note:
            used = f" (in {', '.join(entry['materials'][:3])})" if entry.get("materials") else ""
            result["changed"].append(f"image {name}: {note}{used}")
        elif _unseen(old_images.get(name), entry):
            result["unseen"].append(name)
    if "texts" in before and "texts" in after:  # Both list every image and text block: added and removed say so.
        result["added"] += [f"image {n}" + (f" ({e['size'][0]}x{e['size'][1]})" if e["size"][0] else "")
                            for n, e in new_images.items() if n not in old_images]
        result["removed"] += [f"image {n}" for n in old_images if n not in new_images]
        old_texts, new_texts = before["texts"], after["texts"]
        result["added"] += [f"text {n} ({new_texts[n][0]} lines)" for n in new_texts if n not in old_texts]
        result["removed"] += [f"text {n}" for n in old_texts if n not in new_texts]
        result["changed"] += [f"text {n}: edited ({_counts(old_texts[n][0], new_texts[n][0])} lines)"
                              for n in new_texts if n in old_texts and tuple(old_texts[n]) != tuple(new_texts[n])]
    for key in _changed_keys(before["scene"], after["scene"]):
        result["other"].append(f"scene {key} {before['scene'].get(key)!r} -> {after['scene'].get(key)!r}")
    if before["mode"] != after["mode"]:
        result["other"].append(f"mode {before['mode']} -> {after['mode']}")
    return result


def _counts(old: int, new: int) -> str:
    return f"{old} -> {new}" if old != new else str(new)


def _image_change(old: dict | None, new: dict) -> str:
    """How an image both snapshots read changed: its size, else its pixels (a hash each side, else
    its packed data, else unsaved edits it did not have); "" when neither can say."""
    if old is None:
        return ""  # Newly read (loaded, or newly used): its material's nodes say what changed.
    if old["size"] != new["size"] and 0 not in old["size"] + new["size"]:
        return f"{old['size'][0]}x{old['size'][1]} -> {new['size'][0]}x{new['size'][1]} px"
    if old["pixels"] and new["pixels"]:
        return "pixels edited" if old["pixels"] != new["pixels"] else ""
    if old.get("packed") and new.get("packed") and old["packed"] != new["packed"]:
        return "pixels edited (packed again)"
    if new.get("packed") and not old.get("packed") and old["size"][0]:
        return "packed"
    return "pixels edited (unsaved)" if new["dirty"] and not old["dirty"] else ""


def _unseen(old: dict | None, new: dict) -> bool:
    """An image with unsaved edits before and after whose pixels were not hashed: a further paint
    would not show."""
    return bool(old and old.get("dirty") and new.get("dirty") and not (old.get("pixels") and new.get("pixels")))


_UNFRAMED = ("CAMERA", "LIGHT", "EMPTY", "SPEAKER", "LIGHT_PROBE")  # No geometry to frame (tools._HIDDEN_TYPES).


def touched(before: dict, after: dict) -> list[str]:
    """What a look after this change should frame: visible objects with geometry that were added or
    changed, or that wear a material that was; [] when the snapshots cannot say (too many objects).
    A dungeon's automatic looks framed its first twelve floor tiles, whatever the step had built."""
    if before.get("too_many") or after.get("too_many"):
        return []
    old, new = before.get("objects") or {}, after.get("objects") or {}
    materials = {name for name, value in (after.get("materials") or {}).items()
                 if (before.get("materials") or {}).get(name) != value}
    materials |= {name for image, entry in (after.get("images") or {}).items()
                  if _image_change((before.get("images") or {}).get(image), entry) for name in entry["materials"]}
    return [name for name, entry in new.items()
            if entry.get("visible", True) and entry.get("type") not in _UNFRAMED
            and (old.get(name) != entry or materials & set(entry.get("materials") or ()))]


def is_empty(changes: dict) -> bool:
    return not any(value for key, value in changes.items() if key != "unseen")


def lines(changes: dict) -> list[str]:
    out = ([f"+ {text}" for text in changes["added"]] + [f"- {text}" for text in changes["removed"]]
           + [f"~ {text}" for text in changes["changed"]] + [f"~ {text}" for text in changes["other"]])
    if len(out) > MAX_LINES:
        out = out[:MAX_LINES] + [f"... and {len(out) - MAX_LINES} more"]
    return out


def _unseen_line(changes: dict) -> str:
    names = changes.get("unseen") or []
    return (f"\nNot compared: the pixels of {', '.join(names[:4])}{' and more' if len(names) > 4 else ''} (too big to "
            f"hash here, unsaved before and after); inspect_texture hashes one." if names else "")


def for_model(changes: dict) -> str:
    if is_empty(changes):
        return ("Scene changes: none. (Tracked: objects, transforms, visibility, modifiers, meshes and their active "
                "UV maps, lights, cameras, light probes, materials and worlds node by node, image pixels, text "
                "blocks, collections and render settings; not custom properties, keyframes, drivers, constraints "
                "or inside node groups. If you expected a tracked one to change, the code did not do what you "
                "think.)" + _unseen_line(changes))
    return ("Scene changes (+ added, - removed, ~ changed; bounds are world space):\n" + "\n".join(lines(changes))
            + _unseen_line(changes))


def headline(changes: dict) -> str:
    counts = [(len(changes["added"]), "added"), (len(changes["removed"]), "removed"),
              (len(changes["changed"]) + len(changes["other"]), "changed")]
    return ", ".join(f"{n} {word}" for n, word in counts if n) or "No changes"
