"""Who owns what in a worker's copy of the scene: identity, content tokens, and the result file.

Used on both sides of a wave. The parent records, when it saves the spawn copy, every local
datablock's key ("OBJECT:Rug") with its live session_uid and a content token (spawn_state). The
child stamps each datablock it loads with its spawn name before any model code runs (stamp_all),
so renames inside the child keep their identity, and after each call finds what changed that the
worker does not own (changed_keys). When the worker finishes, write_result saves what it created
and what it owns to a .blend; libraries.write adds everything those reference as local copies,
which carry their stamps, and that is how the merge recognises them (merge.py).

A token is a hash of what a datablock is, not of its name, and references to other datablocks go
through their spawn names, so a rename (in the child, or by the user in the live scene) is not a
change. Driven and animated channels are masked: they are outputs of other datablocks and of the
current frame, not edits. Images count by absolute path, so the spawn copy's relative remap and
the result's absolute paths read the same; their pixels through the packed data's CRC (a repaint
counts once it is packed again; write_result packs the worker's own unsaved paint so it survives).
A text block counts its contents. A mesh counts its UV maps' coordinates, so an unwrap alone is a
change. A collection counts only members that existed at spawn, so creating into the user's
"Collection" does not change it.

Supporting datablocks (SUPPORT: text blocks and images, which no object shows) get a line each in
the result's manifest (support): new, updated or skipped with the reason, and a short hash the
merge verifies again live (merge._support) and inspect_texture reads the same for an image.
Arcane chest eval 20260930-154037: a painter's ArcaneRecipe text and its painter sources were in
no worker's result (texts were not a kind ownership knew), twice, and owns refused the name.

Linked (library) datablocks are read-only and never stamped; their identity is (library, name).
Temporary capture setups are tagged TEMP and left out of tokens and results.
"""

import hashlib
import json
import os
import re
import time
import zlib

STAMP = "lc_spawn_name"
TEMP = "lc_temp"
QUARANTINE = "lc_quarantine"  # "<wave>/<worker>" on created datablocks outside the worker's role.
POINTER = "lc_pointer"        # "camera" | "world": what the worker set scene.camera / scene.world to.
HOME = "lc_home"              # On a quarantined object: the collection the rest of its worker's work went to.
VARIANT = "lc_variant_of"     # On a worker's copy of a material: the material it was copied from (kept live).
COLLECTIONS = "lc_collections"  # On a created object: where the worker put it (JSON, write_result), for the merge.
OURS = (STAMP, TEMP, QUARANTINE, POINTER, HOME, COLLECTIONS)
ID_COLLECTIONS = ("objects", "meshes", "materials", "lights", "cameras", "collections", "node_groups",
                  "images", "worlds", "curves", "actions", "textures", "armatures", "lattices",
                  "grease_pencils", "fonts", "speakers", "texts")
SUPPORT = ("TEXT", "IMAGE")  # Supporting datablocks no object shows: each gets a manifest line (support_entry).
SUPPORT_LIMIT = 8            # Manifest lines for them at most; the rest counted.
ROLES = ("modeling", "materials", "lighting", "camera", "animation")
ASPECTS = ("lighting", "camera", "materials")
# Materials, said once for the worker prompt (workers.py) and the rollback note (worker_child.py):
# copies are the worker's own; the palette itself changes only with the materials aspect.
# What a role may create: object types, and datablock types (an allowed object's data included).
# Lighting also creates materials, images and node groups for light cards and HDRI worlds, and
# actions: a light's flicker or a rig's sweep is lighting work. Camera and animation create paths
# (curves) to follow. Every role may also create empties (a pivot, an aim target), and a brief's
# aspects add their own types (ASPECT_TYPES): a modeling worker with aspect lighting may make a
# lamp's bulb light; the lighting and camera aspects bring actions, so a shot worker (lighting with
# both) may animate its cameras and lights. What a brief asks for in its own words is let through
# too, however its role (asked_for, verdicts).
ALLOWED = {
    "modeling": ({"MESH", "CURVE", "EMPTY", "FONT"}, {"MESH", "CURVE", "FONT", "MATERIAL", "NODETREE", "IMAGE", "TEXTURE"}),
    "materials": (set(), {"MATERIAL", "NODETREE", "IMAGE", "TEXTURE"}),
    "lighting": ({"LIGHT", "MESH"}, {"LIGHT", "MESH", "WORLD", "MATERIAL", "NODETREE", "IMAGE", "ACTION"}),
    "camera": ({"CAMERA", "CURVE"}, {"CAMERA", "CURVE", "ACTION"}),
    "animation": ({"EMPTY", "CURVE"}, {"ACTION", "CURVE"}),
}
ANY_ROLE = {"EMPTY"}
ASPECT_TYPES = {"lighting": ({"LIGHT"}, {"LIGHT", "WORLD", "ACTION"}), "camera": ({"CAMERA"}, {"CAMERA", "ACTION"}),
                "materials": (set(), {"MATERIAL", "NODETREE", "IMAGE", "TEXTURE"})}
_KIND_WORDS = {"MESH": "meshes", "CURVE": "curves", "FONT": "text", "EMPTY": "empties", "LIGHT": "lights",
               "CAMERA": "cameras", "MATERIAL": "materials", "WORLD": "worlds", "ACTION": "animation (actions)",
               "NODETREE": "node groups", "IMAGE": "images", "TEXTURE": "textures", "SPEAKER": "speakers",
               "ARMATURE": "armatures", "LATTICE": "lattices", "GREASEPENCIL": "grease pencil", "COLLECTION": "collections",
               "TEXT": "text blocks"}
# Words in a brief that ask for a kind of datablock whatever the role: a floor lamp's bulb light, a
# fan's spin. A light or camera so asked for must also hang under the worker's own objects (a test
# light in the middle of the room is still held back); an action must animate only the worker's own.
ASKS = {
    "LIGHT": r"(?:lights?|lighting|lit)\b(?![ -]?(?:gr[ae]y|wood|weight|blue|green|brown|colou?r|tone|oak|beige|pink|"
             r"yellow|touch))|illuminat|glow|bulb|lamps?\b|lanterns?|candles?|sconces?|chandeliers?|torch|fire|flames?"
             r"|shin(?:e|es|ing)\b|neon",
    "ACTION": r"animat|keyframe|keyed|moves?\b|moving|motion|spin|rotat|turn(?:s|ing)?\b|orbit|swing|sway|bob(?:s|bing)?\b"
              r"|bounc|flicker|puls|blink|sweep|opens?\b|opening|clos(?:e|es|ing)\b|reveal|turntable|loop|frames? \d",
    "CAMERA": r"cameras?\b|shots?\b|point of view|pov\b",
}
_NAME_FIELDS = ("name", "names", "a", "b", "camera", "light", "target", "container", "subject", "objects")
_ATTR_OF = {"OBJECT": "objects", "MESH": "meshes", "MATERIAL": "materials", "LIGHT": "lights", "CAMERA": "cameras",
            "COLLECTION": "collections", "NODETREE": "node_groups", "IMAGE": "images", "WORLD": "worlds",
            "CURVE": "curves", "ACTION": "actions", "TEXTURE": "textures", "ARMATURE": "armatures",
            "LATTICE": "lattices", "FONT": "fonts", "SPEAKER": "speakers", "TEXT": "texts"}
_SKIP_PROPS = {"rna_type", "name", "name_full", "show_expanded", "show_in_editmode", "show_on_cage", "show_pin",
               "execution_time", "persistent_uid", "active", "is_active", "is_override_data_editable",
               "use_apply_on_spline", "show_group_selector", "open_properties_panel"}
_TRANSFORM = (("location", "loc"), ("rotation_euler", "rot"), ("rotation_quaternion", "quat"),
              ("rotation_axis_angle", "axis"), ("scale", "scale"))

SPAWN_TOKENS: dict = {}  # The child's tokens from stamp_all, for write_result's advisory.
SPAWN_ACTIVE: dict = {}  # The child's active collection at stamp_all: {"name": its stamp, "" for the scene's own}.
STAMPED_UIDS: set = set()  # The child's datablocks as stamped (session_uids survive undo), to tell copies apart.


# ------------------------------------------------------------------ identity

_RUNTIME_IMAGES = ("RENDER_RESULT", "COMPOSITING")  # Render Result, Viewer Node: Blender's output buffers.


def all_ids(local: bool = True):
    """Every datablock of the kinds a worker can touch, without temporary capture setups or the
    render and viewer buffers (any render rewrites them; undo does not); local only unless asked."""
    import bpy
    for attr in ID_COLLECTIONS:
        for idb in getattr(bpy.data, attr, ()):
            if (local and idb.library is not None) or idb.get(TEMP) or (
                    attr == "images" and idb.type in _RUNTIME_IMAGES):
                continue
            yield idb


def key_of(idb) -> str:
    return f"{idb.id_type}:{idb.get(STAMP) or idb.name}"


def _ref(idb, uid2spawn):
    """A referenced datablock's identity, stable across processes and renames. Shape keys (a Key) are
    known by their mesh, curve or lattice: they carry no stamp, and their own name drifts (Key.001)."""
    import bpy
    if not isinstance(idb, bpy.types.ID):
        return None
    if idb.id_type == "KEY" and idb.user is not None and idb.user != idb:
        return f"{_ref(idb.user, uid2spawn)}.shape_keys"
    if idb.get(STAMP):
        return idb[STAMP]
    if uid2spawn is not None and idb.session_uid in uid2spawn:
        return uid2spawn[idb.session_uid]
    return idb.name if idb.library is None else f"{idb.name}@{os.path.basename(idb.library.filepath)}"


def _known(idb, uid2spawn) -> bool:
    """Existed at spawn: stamped (child, appended copies) or recorded (parent)."""
    return bool(idb.get(STAMP)) or (uid2spawn is not None and idb.session_uid in uid2spawn)


# ------------------------------------------------------------------ tokens

def _rnd(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "to_list"):
        value = value.to_list()
    if isinstance(value, str) or value is None or isinstance(value, bool):
        return value
    try:
        return [_rnd(v) for v in value]
    except TypeError:
        pass
    try:
        return round(float(value), 4) + 0.0
    except (TypeError, ValueError):
        return str(value)


def _custom(holder) -> list:
    """Custom properties with plain values; ours and groups left out."""
    try:
        items = list(holder.items())
    except (AttributeError, TypeError):
        return []
    return sorted((k, _rnd(v)) for k, v in items if k not in OURS and not hasattr(v, "to_dict"))


def _props(struct, uid2spawn, skip=()) -> list:
    """The simple RNA values of a modifier, constraint or other small struct: numbers, flags, enums,
    text and datablock pointers; UI state and runtime values left out."""
    import bpy
    out = []
    for prop in struct.bl_rna.properties:
        name = prop.identifier
        if prop.is_readonly or name in _SKIP_PROPS or name in skip or name.startswith(("is_", "open_", "ui_")) or (
                name.startswith("show_") and name not in ("show_viewport", "show_render")):
            continue  # Read-only values are derived (constraint errors, evaluated sizes): not edits.
        try:
            value = getattr(struct, name)
        except (AttributeError, RuntimeError, TypeError):
            continue
        if prop.type == "POINTER":
            if isinstance(value, bpy.types.ID):
                out.append((name, _ref(value, uid2spawn)))
        elif prop.type in ("BOOLEAN", "INT", "FLOAT", "STRING", "ENUM"):
            if getattr(prop, "is_array", False) and not 0 < prop.array_length <= 32:
                continue
            out.append((name, _rnd(value)))
    return out


def _moving(idb) -> set:
    """Data paths that drivers or an action write: outputs, not edits."""
    anim = getattr(idb, "animation_data", None)
    if anim is None:
        return set()
    paths = {fc.data_path for fc in anim.drivers}
    for fc in _fcurves(anim.action) if anim.action else ():
        paths.add(fc.data_path)
    return paths


def _fcurves(action) -> list:
    curves = list(getattr(action, "fcurves", None) or ())  # Before layered actions.
    for layer in getattr(action, "layers", ()):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                curves += list(bag.fcurves)
    return curves


def _anim(idb, uid2spawn) -> list | None:
    anim = getattr(idb, "animation_data", None)
    if anim is None:
        return None
    slot = getattr(anim, "action_slot", None)
    drivers = sorted((fc.data_path, fc.array_index, fc.driver.expression,
                      [_ref(t.id, uid2spawn) for v in fc.driver.variables for t in v.targets]) for fc in anim.drivers)
    return [_ref(anim.action, uid2spawn), slot.identifier if slot else None, drivers]


def _drivers(anim, uid2spawn) -> list:
    """Every driver in full: where it writes, its kind and expression, each variable with what it reads
    (the datablock by identity, the path, the bone and transform). Runtime state (is_valid) left out."""
    drivers = []
    for fc in anim.drivers:
        variables = []
        for var in fc.driver.variables:
            used = {"LOC_DIFF": 2, "ROTATION_DIFF": 2, "CONTEXT_PROP": 0}.get(var.type, 1)
            variables.append([var.name, var.type, [[_ref(t.id, uid2spawn), t.id_type, t.data_path, t.bone_target,
                                                   t.transform_type, t.transform_space, t.rotation_mode]
                                                  for t in list(var.targets)[:used]]])
        drivers.append([fc.data_path, fc.array_index, fc.mute, fc.driver.type, fc.driver.expression,
                        fc.driver.use_self, variables])
    return sorted(drivers, key=lambda d: (d[0], d[1]))


def _shape_keys(key, uid2spawn) -> dict:
    """A mesh's (curve's, lattice's) shape keys as part of its token: each key block's name, value,
    range, relative key, vertex group, mute, interpolation and point positions, and the Key's drivers
    and action. A value a driver or an action writes is an output, not an edit (_moving): it follows
    the pose, live and in the build alike."""
    moving = _moving(key)
    blocks = []
    for kb in key.key_blocks:
        def value(name, kb=kb):
            return "animated" if kb.path_from_id(name) in moving else _rnd(getattr(kb, name))
        blocks.append([kb.name, value("value"), value("slider_min"), value("slider_max"), kb.relative_key.name,
                       kb.vertex_group, kb.mute, kb.interpolation, coords_hash(kb.data, "co", len(kb.data))])
    anim = key.animation_data
    return {"blocks": blocks, "relative": key.use_relative,
            "time": "animated" if "eval_time" in moving else _rnd(key.eval_time),
            "drivers": _drivers(anim, uid2spawn) if anim is not None else None,
            "action": _anim(key, uid2spawn)[:2] if anim is not None else None, "custom": _custom(key)}


def _hash(array) -> str:
    return hashlib.blake2b(array.tobytes(), digest_size=8).hexdigest()  # 7x sha1 in Blender's Python.


def coords_hash(collection, attribute: str, count: int, width: int = 3):
    """A short hash of a collection's float vectors (vertex or point positions, keyframes) at 0.1 mm."""
    import numpy as np
    values = np.empty(count * width, dtype=np.float32)
    collection.foreach_get(attribute, values)
    return _hash(np.round(values, 4) + 0.0)  # + 0.0: -0.0 and 0.0 alike.


def uv_maps(mesh, active_only: bool = False) -> list[tuple[str, str]]:
    """[(name, hash)] of a mesh's UV maps (the active one alone with active_only), each hash of its
    corner coordinates at 1e-4 in corner order. In Edit Mode from the edit mesh: the mesh's own UV
    layers read empty meanwhile, even after update_from_editmode (Blender 5.2), so the object's mode
    must not change the hash. No layer is held past this call (one held across a mode switch crashed
    5.2)."""
    import numpy as np
    if mesh.is_editmode:
        import bmesh
        edit = bmesh.from_edit_mesh(mesh)
        layers = edit.loops.layers.uv
        chosen = [layers.active] if active_only else list(layers.values())
        return [(layer.name, _hash(np.round(np.array([loop[layer].uv[:] for face in edit.faces
                                                       for loop in face.loops], dtype=np.float32), 4) + 0.0))
                for layer in chosen if layer is not None]
    count = len(mesh.loops)
    chosen = [mesh.uv_layers.active] if active_only else list(mesh.uv_layers)
    maps = []
    for layer in chosen:
        if layer is None:
            continue
        values = np.empty(count * 2, dtype=np.float32)
        layer.uv.foreach_get("vector", values)
        maps.append((layer.name, _hash(np.round(values, 4) + 0.0)))
    return maps


def _tree(tree, uid2spawn):
    """A node tree: nodes with their input values, datablocks and enum settings, and the links."""
    import bpy
    if tree is None:
        return None
    moving, nodes = _moving(tree), []
    for node in tree.nodes:
        inputs = []
        for i, socket in enumerate(node.inputs):
            if not hasattr(socket, "default_value"):
                continue
            value = socket.default_value
            if f'nodes["{node.name}"].inputs[{i}].default_value' in moving:
                value = "animated"
            elif isinstance(value, bpy.types.ID) or value is None:
                value = _ref(value, uid2spawn)
            inputs.append((socket.identifier, _rnd(value)))
        settings = []
        for prop in node.bl_rna.properties:
            if prop.type == "ENUM" and not prop.is_readonly or prop.type == "POINTER":
                try:
                    value = getattr(node, prop.identifier)
                except (AttributeError, RuntimeError):
                    continue
                if prop.type == "ENUM":
                    settings.append((prop.identifier, _rnd(value)))
                elif isinstance(value, bpy.types.ID):
                    settings.append((prop.identifier, _ref(value, uid2spawn)))
        nodes.append([node.name, node.bl_idname, node.mute, inputs, settings])
    links = sorted([link.from_node.name, link.from_socket.identifier, link.to_node.name,
                    link.to_socket.identifier, link.is_muted] for link in tree.links)
    return {"nodes": sorted(nodes, key=lambda n: n[0]), "links": links}


def _values(idb, names, moving=()) -> dict:
    return {name: "animated" if name in moving else _rnd(getattr(idb, name, None)) for name in names}


def _object(ob, uid2spawn) -> dict:
    moving = _moving(ob)
    d = {"type": ob.type, "mode": ob.rotation_mode, "parent": _ref(ob.parent, uid2spawn), "ptype": ob.parent_type,
         "pbone": ob.parent_bone, "pinv": _rnd([c for row in ob.matrix_parent_inverse for c in row]),
         "data": _ref(ob.data, uid2spawn), "hide": [ob.hide_render, ob.hide_viewport],
         "instance": [ob.instance_type, _ref(ob.instance_collection, uid2spawn)],
         "cons": [[c.type, _props(c, uid2spawn)] for c in ob.constraints],
         "mods": [[m.type, _props(m, uid2spawn), _custom(m)] for m in ob.modifiers],
         "mats": [[_ref(s.material, uid2spawn), s.link] for s in ob.material_slots],
         "anim": _anim(ob, uid2spawn)}
    rotation = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}.get(ob.rotation_mode, "rotation_euler")
    for path, short in _TRANSFORM:
        if path.startswith("rotation") and path != rotation:
            continue
        d[short] = "driven" if path in moving else _rnd(getattr(ob, path))
    return d


def _mesh(me, uid2spawn) -> dict:
    import numpy as np
    loops = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("vertex_index", loops)
    return {"v": len(me.vertices), "e": len(me.edges), "f": len(me.polygons), "loops": _hash(loops),
            "co": coords_hash(me.vertices, "co", len(me.vertices)), "mats": [_ref(m, uid2spawn) for m in me.materials],
            "attrs": sorted(a.name for a in me.attributes if not a.name.startswith(".")),
            "uv": uv_maps(me)}  # New UVs on an existing map change nothing else: the merge kept the live mesh.


def _curve(cu, uid2spawn) -> dict:
    splines = []
    for spline in getattr(cu, "splines", ()):
        points = spline.bezier_points if spline.type == "BEZIER" else spline.points
        width = 3 if spline.type == "BEZIER" else 4
        splines.append([spline.type, len(points), coords_hash(points, "co", len(points), width) if len(points) else ""])
    return {"props": _props(cu, uid2spawn), "splines": splines, "mats": [_ref(m, uid2spawn) for m in cu.materials]}


def _abspath(path: str, library) -> str:
    import bpy
    return bpy.path.abspath(path, library=library)


def packed_crc(image) -> str:
    """The packed data's size and CRC-32 ("50425045:1a2b3c4d"), "" when not packed: exact for a
    repaint once packed, and cheap (about 30 ms for 50 MB; a pixel hash of a 4K image takes 0.6 s)."""
    packed = image.packed_file
    return f"{packed.size}:{zlib.crc32(packed.data):08x}" if packed is not None else ""


def text_hash(text) -> str:
    """A text block's contents, hashed."""
    return hashlib.blake2b(text.as_string().encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def pixel_hash(image, most: int = 4096 * 4096) -> str:
    """blake2b of the float pixels, 12 hex digits: inspect_texture's own hash (texture_tools._image_line),
    so the two can be compared; "" past `most` texels, with no pixels, or when they can't be read."""
    import numpy as np
    width, height = image.size[:]
    if not 0 < width * height <= most:
        return ""
    pixels = np.empty(width * height * image.channels, dtype=np.float32)
    try:
        image.pixels.foreach_get(pixels)
    except (RuntimeError, TypeError, ValueError):
        return ""
    return hashlib.blake2b(pixels.tobytes(), digest_size=8).hexdigest()[:12]


def _image(image) -> dict:
    path = os.path.normpath(_abspath(image.filepath, image.library)) if image.filepath else ""
    d = {"path": path, "source": image.source, "packed": packed_crc(image) or None,
         "space": image.colorspace_settings.name, "alpha": image.alpha_mode}
    if image.source == "GENERATED":
        d["generated"] = [image.generated_type, image.generated_width, image.generated_height]
    return d


def token(idb, uid2spawn=None) -> str:
    """What a datablock is, as a short hash. uid2spawn (parent side: live session_uid -> spawn name)
    makes references to renamed live datablocks read as at spawn, and marks which collection members
    existed then; in the child, the stamps do both."""
    t = idb.id_type
    moving = _moving(idb)
    if t == "OBJECT":
        d = _object(idb, uid2spawn)
    elif t == "MESH":
        d = _mesh(idb, uid2spawn)
    elif t == "MATERIAL":
        d = {"v": _values(idb, ("diffuse_color", "metallic", "roughness", "blend_method", "surface_render_method"),
                          moving), "tree": _tree(idb.node_tree, uid2spawn)}
    elif t == "LIGHT":
        d = {"v": _values(idb, ("type", "energy", "color", "shadow_soft_size", "spot_size", "spot_blend", "shape",
                                "size", "size_y", "use_shadow", "exposure", "temperature", "use_temperature"), moving),
             "tree": _tree(getattr(idb, "node_tree", None), uid2spawn)}
    elif t == "CAMERA":
        dof = idb.dof
        d = {"v": _values(idb, ("type", "lens", "lens_unit", "ortho_scale", "sensor_fit", "sensor_width", "sensor_height",
                                "shift_x", "shift_y", "clip_start", "clip_end"), moving),
             "dof": [dof.use_dof, _ref(dof.focus_object, uid2spawn), _rnd(dof.focus_distance), _rnd(dof.aperture_fstop)]}
    elif t == "WORLD":
        d = {"color": _rnd(idb.color), "tree": _tree(idb.node_tree, uid2spawn)}
    elif t == "NODETREE":
        d = {"type": idb.bl_idname, "tree": _tree(idb, uid2spawn),
             "interface": [[i.item_type, getattr(i, "in_out", None), getattr(i, "socket_type", None), i.name]
                           for i in idb.interface.items_tree] if hasattr(idb, "interface") else None}
    elif t == "IMAGE":
        d = _image(idb)
    elif t == "TEXT":
        d = {"text": text_hash(idb)}
    elif t == "ACTION":
        curves = []
        for fc in _fcurves(idb):
            count = len(fc.keyframe_points)
            curves.append([fc.data_path, fc.array_index, count, fc.mute,
                           coords_hash(fc.keyframe_points, "co", count, 2) if count else ""])
        d = {"curves": sorted(curves), "slots": sorted(s.identifier for s in getattr(idb, "slots", ()))}
    elif t == "COLLECTION":
        d = {"objects": sorted(_ref(o, uid2spawn) for o in idb.objects if _known(o, uid2spawn) and not o.get(TEMP)),
             "children": sorted(_ref(c, uid2spawn) for c in idb.children if _known(c, uid2spawn)),
             "hide": [idb.hide_render, idb.hide_viewport], "offset": _rnd(idb.instance_offset)}
    elif t == "CURVE":
        d = _curve(idb, uid2spawn)
    else:
        d = {"props": _props(idb, uid2spawn, skip=("filepath", "filepath_raw", "use_fake_user", "use_extra_user", "tag"))}
        if getattr(idb, "filepath", ""):
            d["path"] = os.path.normpath(_abspath(idb.filepath, idb.library))
    d["custom"] = _custom(idb)
    if t not in ("OBJECT",):
        d["anim"] = _anim(idb, uid2spawn)
    keys = getattr(idb, "shape_keys", None)  # A mesh's, curve's or lattice's; one without keeps its old token.
    if keys is not None:
        d["keys"] = _shape_keys(keys, uid2spawn)
    return hashlib.blake2b(json.dumps(d, sort_keys=True, default=str).encode(), digest_size=8).hexdigest()


def describe(idb) -> dict:
    """A small human-readable view of a datablock, for conflict and dropped-edit reports."""
    if idb is None:
        return {}
    if idb.id_type == "OBJECT":
        return {"location": _rnd(idb.location), "rotation": _rnd(idb.rotation_euler), "scale": _rnd(idb.scale)}
    if idb.id_type == "LIGHT":
        return {"energy": _rnd(idb.energy), "color": _rnd(idb.color)}
    if idb.id_type == "MESH":
        return {"vertices": len(idb.vertices), "faces": len(idb.polygons)}
    if idb.id_type == "MATERIAL":
        return {"diffuse": _rnd(idb.diffuse_color)}
    if idb.id_type == "CAMERA":
        return {"lens": _rnd(idb.lens), "type": idb.type}
    return {}


# ------------------------------------------------------------------ the parent: spawn

def spawn_state() -> tuple[dict[str, int], dict[str, str]]:
    """Parent, when the spawn copy is saved: key -> live session_uid, and key -> token, for every
    local datablock."""
    ids = {key_of(idb): idb.session_uid for idb in all_ids()}
    uid2spawn = {uid: key.split(":", 1)[1] for key, uid in ids.items()}
    tokens = {key_of(idb): token(idb, uid2spawn) for idb in all_ids()}
    return ids, tokens


def find(name: str):
    """The local datablock a brief means: "TYPE:Name", else an object, else the first kind that has it."""
    import bpy
    kind, _, rest = name.partition(":")
    if rest and kind in _ATTR_OF:
        return getattr(bpy.data, _ATTR_OF[kind]).get(rest)
    for attr in ID_COLLECTIONS:
        idb = getattr(bpy.data, attr, {}).get(name)
        if idb is not None:
            return idb
    return None


def expand_owns(owns: list[str], aspects: list[str]) -> set[str]:
    """The keys a worker owns: the named datablocks, an owned object's data when every user of that
    data is an owned object (shared data stays the user's), the actions only owned datablocks use
    (_owned_actions), and the aspects: lighting = every light
    object and its data plus scene.world; camera = every camera object and its data (the
    scene.camera pointer is applied by the merge); materials = every material and shader node group
    (the palette: an edit changes every object wearing it). ValueError names what can't be owned."""
    import bpy
    unknown = [a for a in aspects or () if a not in ASPECTS]
    if unknown:
        raise ValueError(f"unknown aspect {unknown[0]!r}: use {', '.join(ASPECTS)}")
    owned, problems = [], []
    for name in owns or ():
        idb = find(str(name))
        if idb is None:
            problems.append(f"no datablock named {name!r}")
        elif idb.library is not None:
            problems.append(f"{name!r} is linked from a library and can't be changed")
        else:
            owned.append(idb)
    if problems:
        raise ValueError("; ".join(problems))
    scene = bpy.context.scene
    for aspect, kind in (("lighting", "LIGHT"), ("camera", "CAMERA")):
        if aspect in (aspects or ()):
            owned += [ob for ob in bpy.data.objects if ob.type == kind and ob.library is None]
    if "lighting" in (aspects or ()) and scene.world is not None and scene.world.library is None:
        owned.append(scene.world)
    if "materials" in (aspects or ()):
        owned += [m for m in bpy.data.materials if m.library is None]
        owned += [g for g in bpy.data.node_groups if g.library is None and g.bl_idname == "ShaderNodeTree"]
    objects = {ob.session_uid for ob in owned if ob.id_type == "OBJECT"}
    for ob in [ob for ob in owned if ob.id_type == "OBJECT"]:
        data = ob.data
        if data is None or data.library is not None:
            continue
        users = [o for o in bpy.data.objects if o.data == data]
        if all(o.session_uid in objects for o in users) and data.users == len(users) + int(data.use_fake_user):
            owned.append(data)
    owned += _owned_actions(owned)
    return {key_of(idb) for idb in owned}


# ------------------------------------------------------------------ the child

def stamp_all() -> dict[str, str]:
    """Child, before any model code: stamp every local datablock with its spawn name; returns the
    spawn tokens (also kept in SPAWN_TOKENS). The active collection is kept too (SPAWN_ACTIVE)."""
    import bpy
    for idb in all_ids():
        idb[STAMP] = idb.name
    STAMPED_UIDS.clear()
    STAMPED_UIDS.update(idb.session_uid for idb in all_ids())
    SPAWN_TOKENS.clear()
    SPAWN_TOKENS.update({key_of(idb): token(idb) for idb in all_ids()})
    layer = bpy.context.view_layer.active_layer_collection if bpy.context.view_layer else None
    active = layer.collection if layer is not None else None
    SPAWN_ACTIVE.clear()
    SPAWN_ACTIVE["name"] = "" if active is None or active.is_embedded_data else active.get(STAMP, active.name)
    return dict(SPAWN_TOKENS)


def _owned_actions(owned: list) -> list:
    """The actions only owned datablocks use: an owned light's flicker, an owned camera's move. They
    go with what they animate (slots and channelbags live inside the action), so a worker that owns
    a light may re-key it, and one continuing its own earlier work owns its earlier keys."""
    import bpy
    uids = {idb.session_uid for idb in owned}
    actions = {}
    for idb in all_ids():
        anim = getattr(idb, "animation_data", None)
        action = getattr(anim, "action", None) if anim is not None else None
        if action is not None and action.library is None:
            actions.setdefault(action.session_uid, (action, []))[1].append(idb)
    found = []
    for action, users in actions.values():
        extra = action.users - len(users) - int(action.use_fake_user)  # An NLA strip or another holder.
        if extra <= 0 and all(u.session_uid in uids for u in users):
            found.append(action)
    return found


def unstamp_copies() -> list[str]:
    """A copy of a stamped datablock (datablock.copy(), Shift+D) carries the stamp too. The original
    keeps it: the one stamp_all stamped, else the one still called by the stamp, else the oldest.
    The copies lose it and count as created; a material copy is tagged VARIANT with the original's
    name, so a check that names the palette material accepts the worker's own copy of it
    (acceptance's material_on) and the merge knows an unchanged copy for the original. Returns
    their keys."""
    groups = {}
    for idb in all_ids():
        if idb.get(STAMP):
            groups.setdefault(key_of(idb), []).append(idb)
    copies = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        stamped = [m for m in members if m.session_uid in STAMPED_UIDS]
        original = stamped[0] if len(stamped) == 1 else (
            next((m for m in members if m.name == m[STAMP]), None) or min(members, key=lambda m: m.session_uid))
        for member in members:
            if member != original:
                if member.id_type == "MATERIAL" and not member.get(VARIANT):
                    member[VARIANT] = member[STAMP]
                del member[STAMP]
                copies.append(f"{member.id_type}:{member.name}")
    return copies


def prefixed(name: str, prefix: str, declared=()) -> bool:
    """A name under a worker's prefix: Prefix_Part or PrefixPart (the underscore is not required),
    the prefix itself (a root named for it), or one of declared: names the brief itself pins down
    (its deliverable, or a check's own name). The merge no longer renames created datablocks onto
    the prefix (workers.opening, merge._rename_created); this is used to tell a worker's own,
    not-yet-built name from another worker's already-live one (acceptance.elsewhere) while a check
    runs mid-task, and for the count check's kind="count" prefix grouping."""
    return name in declared or (bool(prefix) and name.startswith(prefix))


def collections_of(ob) -> list[dict]:
    """Child: where the worker put a created object, each collection but the scene's own, for the
    merge to put it back (merge._worker_homes; 20260929-230207 O0008: seven props linked into the
    live ShopAssetPack came back in the main collection). A spawn one by its stamp, and whether it
    was the active collection, where new objects land by default ({"spawn", "active"}); one the
    worker made by its name and those it made above it, up to a spawn one or the scene ({"made":
    [outer, ..., its own], "under": that spawn one's stamp, or ""})."""
    import bpy
    parents = {child.session_uid: parent for parent in bpy.data.collections for child in parent.children}
    found = []
    for collection in ob.users_collection:
        if collection.is_embedded_data or collection.library is not None:
            continue
        if collection.get(STAMP):
            found.append({"spawn": collection[STAMP], "active": collection[STAMP] == SPAWN_ACTIVE.get("name")})
            continue
        chain, above = [collection.name], parents.get(collection.session_uid)
        while above is not None and not above.get(STAMP) and above.name not in chain:
            chain.insert(0, above.name)
            above = parents.get(above.session_uid)
        found.append({"made": chain, "under": above[STAMP] if above is not None and above.get(STAMP) else ""})
    return found


def _kept(idb) -> bool:
    """Worth merging though nothing may use it: in use; a text block (its own content is the point:
    a recipe, a painter's source); an image with pixels of its own (packed, or painted and unsaved:
    write_result packs it). The merge gives the unused ones a fake user so a save keeps them."""
    if idb.users > 0 or idb.id_type == "TEXT":
        return True
    return idb.id_type == "IMAGE" and (idb.packed_file is not None or idb.is_dirty)


def created_ids() -> list:
    """Child: what the worker made so far: local datablocks with no stamp, in use or kept anyway
    (_kept: text blocks, images it painted); collections left out (the merge puts created objects
    in their home collection, merge._home_name)."""
    unstamp_copies()
    return [idb for idb in all_ids() if not idb.get(STAMP) and idb.id_type != "COLLECTION" and _kept(idb)]


def changed_keys(spawn_tokens: dict, owned: set, prefix: str = "") -> list[str]:
    """Stamped datablocks that changed, or are gone, and are not owned. Whatever it is called: the
    palette and the supports may carry the worker's prefix (Chest_Wood for the Chest worker), and are
    everyone's; everything else live under the prefix is owned (orchestrator.validate_wave)."""
    unstamp_copies()
    present = {key_of(idb): idb for idb in all_ids() if idb.get(STAMP)}
    changed = []
    for key, before in spawn_tokens.items():
        if key in owned:
            continue
        idb = present.get(key)
        if idb is None or token(idb) != before:
            changed.append(key)
    return sorted(changed)


def creatable(role: str, aspects=()) -> tuple[set, set] | None:
    """(object types, datablock types) a worker with this role and these aspects may create; None
    for an unknown role, which may create anything."""
    if role not in ALLOWED:
        return None
    objects, kinds = set(ALLOWED[role][0]) | ANY_ROLE, set(ALLOWED[role][1])
    for aspect in aspects or ():
        extra_objects, extra_kinds = ASPECT_TYPES.get(aspect, (set(), set()))
        objects |= extra_objects
        kinds |= extra_kinds
    return objects, kinds


def allowed(role: str, idb, aspects=()) -> bool:
    """Whether a worker with this role and these aspects may create this datablock (creatable)."""
    types = creatable(role, aspects)
    if types is None or idb.id_type in ("COLLECTION", "TEXT"):  # A container; notes and code, any role's.
        return True
    objects, kinds = types
    return idb.type in objects if idb.id_type == "OBJECT" else idb.id_type in kinds


# ------------------------------------------------------------------ what a brief asks for

def brief_words(brief) -> list[str]:
    """The brief's own sentences: requirements (quote and do), defaults, art direction, the
    deliverable and the checks' quotes. What asked_for and pinned read."""
    if not isinstance(brief, dict):
        return []
    words = []
    for item in brief.get("requirements") or ():
        words += [str(item.get("quote") or ""), str(item.get("do") or "")] if isinstance(item, dict) else [str(item)]
    for item in brief.get("defaults") or ():
        words.append(item if isinstance(item, str) else json.dumps(item))
    for check in brief.get("checks") or ():
        if isinstance(check, dict) and check.get("quote"):
            words.append(str(check["quote"]))
    for key in ("deliverable", "style", "detail", "direction"):
        value = brief.get(key)
        if isinstance(value, str):
            words.append(value)
        elif isinstance(value, (dict, list)):
            words.append(json.dumps(value))
    return [w for w in words if w.strip()]


def _clip(sentence: str, found) -> str:
    """The words around what matched, at most about 80 characters."""
    if len(sentence) <= 80:
        return sentence.strip()
    start = max(0, found.start() - 35)
    return ("..." if start else "") + sentence[start:start + 80].strip() + "..."


def asked_for(kind: str, brief) -> str:
    """The brief's words that ask for this kind of datablock (ASKS), or "": a light, a camera, motion."""
    pattern = ASKS.get(kind)
    for sentence in brief_words(brief) if pattern else ():
        found = re.search(rf"\b(?:{pattern})", sentence, re.I)
        if found:
            return _clip(sentence, found)
    return ""


def pinned(name: str, brief) -> bool:
    """A name the brief itself names: its deliverable, an object a check is about, a name it owns, or
    (4 characters or more, written as the brief writes it) a word in its sentences."""
    if not isinstance(brief, dict):
        return False
    base = re.sub(r"\.\d{3}$", "", name)
    names = {brief.get("deliverable")} | {n for n in brief.get("owns") or () if isinstance(n, str)}
    for check in brief.get("checks") or ():
        for key in _NAME_FIELDS if isinstance(check, dict) else ():
            value = check.get(key)
            names |= {value} if isinstance(value, str) else {v for v in value or () if isinstance(v, str)} \
                if isinstance(value, list) else set()
    if base in names:
        return True
    return len(base) >= 4 and re.search(rf"(?<!\w){re.escape(base)}(?!\w)", " ".join(brief_words(brief))) is not None


def refusal(role: str, aspects, kind: str) -> str:
    """Why something is held back: what the role makes, and what would have let this through."""
    types = creatable(role, aspects)
    made = sorted({_KIND_WORDS.get(k, k.lower()) for k in (types[0] | types[1]) - {"NODETREE", "IMAGE", "TEXTURE"}}) \
        if types else []
    what = _KIND_WORDS.get(kind, kind.lower())
    grant = {"LIGHT": "aspect lighting, or a brief that asks for light from the worker's own object",
             "CAMERA": "aspect camera, or a brief that asks for a camera on the worker's own rig",
             "WORLD": "aspect lighting", "ACTION": "a brief that asks for motion of the worker's own objects",
             "MATERIAL": "aspect materials"}.get(kind, "a brief that names it")
    with_aspects = f" with aspect {', '.join(aspects)}" if aspects else ""
    return (f"outside its role: a {role} worker{with_aspects} makes {', '.join(made) or 'nothing of this kind'}; "
            f"{what} need {grant}")


def verdicts(created: list, role: str, aspects, brief, own_uids=()) -> dict:
    """session_uid -> (merge it, why) for what a worker created. Its role and aspects allow it (why
    ""); or the brief asks for it: a name it gives, a light or camera it asks for hanging under the
    worker's own objects, an action it asks for that animates only the worker's own, data used only
    by what merges (why says which); else held back (why: refusal). own_uids: what the worker owns,
    besides what it created."""
    import bpy
    own = set(own_uids) | {i.session_uid for i in created}
    found = {}

    def under_own(ob):
        parent = ob.parent
        while parent is not None:
            if parent.session_uid in own and allowed(role, parent, aspects):
                return parent
            parent = parent.parent
        return None
    for ob in [i for i in created if i.id_type == "OBJECT"]:
        if allowed(role, ob, aspects):
            found[ob.session_uid] = (True, "")
        elif pinned(ob.name, brief):
            found[ob.session_uid] = (True, "the brief names it")
        else:
            asked, root = asked_for(ob.type, brief), under_own(ob)
            found[ob.session_uid] = (True, f"part of {root.name}, and the brief asks for it: \"{asked}\"") \
                if asked and root is not None else (False, refusal(role, aspects, ob.type))
    rest = [i for i in created if i.id_type != "OBJECT"]
    users = bpy.data.user_map(subset=rest) if rest else {}
    for idb in rest:
        holders = _objects_using(idb, users.get(idb, ()))
        if allowed(role, idb, aspects):
            found[idb.session_uid] = (True, "")
        elif pinned(idb.name, brief):
            found[idb.session_uid] = (True, "the brief names it")
        elif idb.id_type == "ACTION":
            animated = [u for u in users.get(idb, ()) if getattr(getattr(u, "animation_data", None), "action", None) == idb]
            asked = asked_for("ACTION", brief)
            names = ", ".join(sorted(u.name for u in animated)[:3])
            found[idb.session_uid] = (True, f"animates only its own {names}, and the brief asks for motion: \"{asked}\"") \
                if asked and animated and all(u.session_uid in own for u in animated) else \
                (False, refusal(role, aspects, "ACTION"))
        elif holders and all(found.get(h.session_uid, (h.session_uid in own, ""))[0] for h in holders):
            why = next((found[h.session_uid][1] for h in holders if found.get(h.session_uid, (0, ""))[1]), "")
            found[idb.session_uid] = (True, why or f"used only by {', '.join(sorted(h.name for h in holders)[:3])}")
        else:
            found[idb.session_uid] = (False, refusal(role, aspects, idb.id_type))
    return found


def _objects_using(idb, users) -> list:
    """The objects that use a datablock, directly or through their data (a material through its mesh)."""
    import bpy
    objects = []
    for user in users:
        if isinstance(user, bpy.types.Object):
            objects.append(user)
        elif isinstance(user, bpy.types.ID) and user.id_type in _ATTR_OF and user.id_type != "OBJECT":
            objects += [ob for ob in bpy.data.objects if ob.data == user]
    return list({ob.session_uid: ob for ob in objects}.values())


def support_about(idb) -> str:
    """A few words on a text block or an image: "212 lines", "4096x4096 packed"."""
    if idb.id_type == "TEXT":
        return f"{len(idb.lines)} lines"
    width, height = idb.size[:] if idb.has_data or idb.packed_file is not None else (0, 0)
    where = "packed" if idb.packed_file is not None else "file" if idb.filepath else idb.source.lower()
    return f"{width}x{height} {where}" if width else where


def support_entry(idb, state: str, why: str = "") -> dict:
    """A supporting datablock's manifest line: key, state (new | updated | skipped), why (why it is
    skipped, or that its paint could not be packed), hash (a text's contents; an image's pixels as
    inspect_texture hashes them; shown), token (what the merge checks the live one against), about
    (support_about)."""
    entry = {"key": key_of(idb), "state": state, "about": support_about(idb)}
    if why:
        entry["why"] = why
    if state != "skipped":
        entry["hash"] = text_hash(idb)[:12] if idb.id_type == "TEXT" else pixel_hash(idb)
        entry["token"] = token(idb)
    return entry


def _pack_paint(images: list) -> dict:
    """The worker's own images (created or owned) with unsaved paint, packed: an image never packed
    arrives blank, or as its file on disk. Returns {name: why} for those that could not be."""
    failed = {}
    for image in [i for i in images if i.is_dirty]:
        try:
            image.pack()
        except RuntimeError as ex:
            failed[image.name] = f"its paint could not be packed ({ex})"[:120]
    return failed


def _reload_packed(images: list) -> None:
    """Packed images reloaded from their packed data, so their pixels and hash here are what arrives
    (a buffer painted in floats is 8-bit once packed as PNG): what support_entry hashes."""
    for image in [i for i in images if i.packed_file is not None and not i.is_dirty]:
        try:
            image.reload()
        except RuntimeError:
            pass


def _support(created: list, updated: list, failed: dict, changed: list) -> list:
    """Manifest lines for the supporting datablocks (SUPPORT) the worker made or changed: new (created),
    updated (owned and changed), each with why when its paint could not be packed; skipped with the
    reason: an image unused and never painted, or a text or image edited that is not its own. At most
    SUPPORT_LIMIT, then {"more": n}."""
    entries = [support_entry(i, "new", failed.get(i.name, "")) for i in created if i.id_type in SUPPORT]
    entries += [support_entry(i, "updated", failed.get(i.name, "")) for i in updated if i.id_type in SUPPORT]
    listed = {i.session_uid for i in created + updated}
    changed = set(changed)
    for idb in all_ids():
        if idb.id_type not in SUPPORT or idb.session_uid in listed:
            continue
        if not idb.get(STAMP):
            if idb.id_type == "IMAGE":
                entries.append(support_entry(idb, "skipped", "unused and never painted"))
        elif key_of(idb) in changed or (idb.id_type == "IMAGE" and idb.is_dirty):
            entries.append(support_entry(idb, "skipped", "not owned: the edit stays in the worker's copy"))
    return entries[:SUPPORT_LIMIT] + ([{"more": len(entries) - SUPPORT_LIMIT}] if len(entries) > SUPPORT_LIMIT else [])


def _with_shape_key_owners(ids: set) -> set:
    """ids, and the mesh (curve, lattice) of every Key they reach. libraries.write follows a driver to
    a Key but never a Key back to its mesh (Key.user is a loop-back pointer, which it clears), and a Key
    only drivers use has no users, so it was not written: a build that rigged Tongue and left Face
    alone brought Tongue's mouth_open driver in aimed at nothing instead of at Face's shape keys.
    Written with its mesh, the Key arrives as part of a copy that the merge points back at the live
    mesh, shape keys included (merge._replace)."""
    import bpy
    uses = {}
    for used, users in bpy.data.user_map().items():
        for user in users:
            uses.setdefault(user, []).append(used)
    found, todo = set(ids), list(ids)
    while todo:
        for used in uses.get(todo.pop(), ()):
            owner = used.user if used.id_type == "KEY" else None
            for idb in (used, owner):
                if idb is not None and idb not in found:
                    found.add(idb)
                    todo.append(idb)
    return set(ids) | {idb.user for idb in found if idb.id_type == "KEY" and idb.user is not None}


def write_result(path, owned: set, prefix: str, role: str, aspects=(), brief=None) -> dict:
    """Child, at finish: save what the worker created (local, unstamped, in use or kept anyway: text
    blocks, painted images; collections left out: each created object carries the ones it is in,
    COLLECTIONS, and the merge puts it back there or in its home collection) and what it owns, its
    images' unsaved paint packed first (_pack_paint), and return the manifest:
    result, created, owned_written, owned_deleted (owned keys the worker removed), pointers (what
    scene.camera and scene.world are now; a created target is tagged POINTER), support (_support),
    advisory (outside_role: what the merge will hold back, by verdicts with what the child knows of
    the brief; changed_not_owned). Names are the worker's own; the merge renames only a genuine
    collision."""
    import bpy
    t0 = time.time()
    owned = set(owned or ())
    created, owned_ids, present = created_ids(), [], set()
    for idb in all_ids():
        if idb.get(STAMP):
            present.add(key_of(idb))
            if key_of(idb) in owned:
                owned_ids.append(idb)
    failed = _pack_paint([i for i in created + owned_ids if i.id_type == "IMAGE"])
    changed = changed_keys(SPAWN_TOKENS, owned, prefix) if SPAWN_TOKENS else []
    try:  # Listing them never costs the result.
        updated = [i for i in owned_ids if i.id_type in SUPPORT and SPAWN_TOKENS.get(key_of(i)) != token(i)]
        _reload_packed([i for i in created + updated if i.id_type == "IMAGE"])
        support = _support(created, updated, failed, changed)
    except Exception as ex:  # noqa: BLE001
        support = [{"error": f"{type(ex).__name__}: {ex}"[:160]}]
    scene = bpy.context.scene
    pointers = {}
    for aspect, target in (("camera", scene.camera), ("world", scene.world)):
        pointers[aspect] = key_of(target) if target is not None else None
        if target is not None and target.library is None and not target.get(STAMP):
            target[POINTER] = aspect
    held = verdicts(created, role, aspects, brief, {i.session_uid for i in owned_ids})
    for ob in [i for i in created if i.id_type == "OBJECT"]:
        homes = collections_of(ob)
        if homes:
            ob[COLLECTIONS] = json.dumps(homes)
    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    bpy.data.libraries.write(path, _with_shape_key_owners(set(created) | set(owned_ids)), path_remap="ABSOLUTE",
                             compress=True)
    name = lambda idb: f"{idb.id_type}:{idb.name}"  # noqa: E731
    return {
        "result": path, "bytes": os.path.getsize(path), "seconds": round(time.time() - t0, 4),
        "prefix": prefix, "role": role, "aspects": sorted(aspects or ()), "owned": sorted(owned),
        "created": sorted(name(i) for i in created), "owned_written": sorted(key_of(i) for i in owned_ids),
        "owned_deleted": sorted(k for k in owned if k not in present), "pointers": pointers, "support": support,
        "advisory": {
            "outside_role": sorted(name(i) for i in created if not held.get(i.session_uid, (True,))[0]),
            "changed_not_owned": changed,
        },
    }
