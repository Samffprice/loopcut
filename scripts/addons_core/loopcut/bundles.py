"""Freeze a checkpoint into portable files and a content-addressed contract manifest.

collect() always uses a factory-startup child Blender with script autoexecution disabled and
private data/config folders, so enabled core add-ons cannot touch the user's stored data.
Opening a checkpoint in the UI would replace the live scene and its unsaved edits; even a
save-copy there would collect today's scene rather than the checkpoint. Only the child edits
paths. Recovery snapshots are opened through recover_auto_save, which also resolves linked
libraries against the recorded original filename before they are loaded. Stock copy-saves
are opened normally: Blender already rebased their relative paths when writing the copy.

Missing inputs and unsupported dependencies are returned separately from scene metadata.
Callers must inspect those lists before describing a bundle as complete. Nested libraries or
libraries with unpacked external dependencies are unsupported; plain libraries are copied.
Installation inputs stay at their original paths and are listed under system: the matching
Blender version supplies them, including the factory startup's essentials brush libraries.
Implicit point caches and unknown external path types are reported rather than guessed.
Paths Blender lists but never reads are not inputs: where an appended ID came from (its weak
reference, as BlenderKit leaves), a sound strip's own copy of a path, and empty fields.
The render service runs no Python: a driver that would need it is reported as unsupported, by the
service's own rule (driver_acceptable), so the call is refused before anything is uploaded. So is
animation of driver settings (animates_drivers), which could switch such a driver on mid-render.
The manifest/hash/upload side imports no bpy and uses only the standard library.
Image paths use filepath_raw: filepath would reload and discard recovered painted pixels.
Dirty still/tiled images are saved only into staging, keeping those checkpoint buffers.
"""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

SCHEMA = "loopcut.bundle/0.1"
ENTRY = "scene.blend"


class BundleError(RuntimeError):
    pass


def _safe_path(value):
    if (not isinstance(value, str) or not value or value.startswith("/") or "\\" in value
            or PureWindowsPath(value).drive or any(ord(c) < 32 for c in value)
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise BundleError(f"Unsafe bundle path: {value!r}")
    return value


def _root(staging_dir):
    root = Path(staging_dir)
    if root.is_symlink():
        raise BundleError("Symlink in bundle staging directory")
    return root.resolve()


def _file(root, relative):
    relative = _safe_path(relative)
    path = root / relative
    # Reject symlinks even when they point inside; the bundle must contain actual bytes.
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(root)):
        raise BundleError(f"Symlink in bundle: {relative}")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise BundleError(f"Missing or escaped bundle file: {relative}")
    return path


def _staged_files(root):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BundleError(f"Symlink in bundle: {path.name}")
        if path.is_file():
            relative = _safe_path(path.relative_to(root).as_posix())
            yield relative, _file(root, relative)
        elif not path.is_dir():
            raise BundleError(f"Not a regular bundle file: {path.name}")


def _timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_manifest(value):
    from . import blobs
    if (not isinstance(value, dict) or set(value) != {"schema", "blender", "entry", "files", "scene", "source", "created_at"}
            or value.get("schema") != SCHEMA or value.get("entry") != ENTRY):
        raise BundleError("Not a loopcut.bundle/0.1 manifest")
    if not isinstance(value.get("blender"), str) or not value["blender"]:
        raise BundleError("Missing Blender version")
    files = value.get("files")
    if not isinstance(files, list):
        raise BundleError("Missing bundle files")
    paths = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise BundleError("Invalid bundle file")
        paths.append(_safe_path(item.get("path")))
        try:
            blobs._hash(item.get("sha256"))
        except ValueError as ex:
            raise BundleError("Invalid bundle digest") from ex
        if type(item.get("size")) is not int or item["size"] < 0:
            raise BundleError("Invalid bundle file size")
    if len(paths) != len(set(paths)) or paths != sorted(paths) or ENTRY not in paths:
        raise BundleError("Bundle files must be unique, sorted and include scene.blend")
    scene, source = value.get("scene"), value.get("source")
    if (not isinstance(scene, dict) or not isinstance(scene.get("name"), str)
            or set(scene) != {"name", "camera", "frame_start", "frame_end", "fps"}
            or not scene["name"]
            or not (scene["camera"] is None or isinstance(scene["camera"], str) and scene["camera"])
            or type(scene.get("frame_start")) is not int or type(scene.get("frame_end")) is not int
            or scene["frame_end"] < scene["frame_start"]
            or type(scene.get("fps")) not in {int, float} or not math.isfinite(scene["fps"]) or scene["fps"] < 1):
        raise BundleError("Invalid bundle scene metadata")
    if (not isinstance(source, dict) or source.get("kind") != "checkpoint"
            or set(source) != {"kind", "session", "checkpoint"}
            or any(not isinstance(source.get(k), str) or not source[k] for k in ("session", "checkpoint"))):
        raise BundleError("Invalid checkpoint source")
    try:
        datetime.strptime(value["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError) as ex:
        raise BundleError("Invalid bundle creation time") from ex


def manifest(staging_dir, scene_info, source):
    """Build the contract manifest. Repeated calls on unchanged staging bytes are stable.

    scene_info is collect's result {blender, scene, created_at?, missing?, unsupported?, system?}.
    Prefer the checkpoint's creation time; otherwise use scene.blend's mtime, never the
    wall clock at serialization time. Diagnostics stay local, outside the contract JSON.
    """
    from . import blobs
    root = _root(staging_dir)
    scene_file = _file(root, ENTRY)
    files = [{"path": name, "sha256": blobs.sha256(path), "size": path.stat().st_size}
             for name, path in _staged_files(root)]
    value = {"schema": SCHEMA, "blender": scene_info["blender"], "entry": ENTRY,
             "files": sorted(files, key=lambda item: item["path"]),
             "scene": {key: scene_info["scene"][key]
                       for key in ("name", "camera", "frame_start", "frame_end", "fps")},
             "source": {key: source[key] for key in ("kind", "session", "checkpoint")},
             "created_at": scene_info.get("created_at") or _timestamp(scene_file.stat().st_mtime)}
    _validate_manifest(value)
    return value


def _js_numbers(value):
    """The service re-serializes the manifest in JavaScript and checks its hash, and JavaScript
    writes a whole float as an integer: Python's "fps":24.0 must go out as "fps":24."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _js_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_js_numbers(item) for item in value]
    return value


def canonical_json(value):
    _validate_manifest(value)
    return json.dumps(_js_numbers(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def upload(staging_dir, manifest, *, client=None, cancel=None, progress=None, multipart=None):
    """Upload absent file blobs, then the canonical manifest blob; return its SHA-256.

    multipart: the manifest's capabilities.blob_multipart, so a large scene.blend goes in parts.

    The manifest temporary file lives outside staging, so another manifest() call cannot
    accidentally include it (or the bundle's own hash) in its files list.
    """
    from . import blobs, cloud_http
    body = canonical_json(manifest)
    root = _root(staging_dir)
    files = [{**item, "path": _file(root, item["path"])} for item in manifest["files"]]
    actual = {name for name, _ in _staged_files(root)}
    if actual != {item["path"] for item in manifest["files"]}:
        raise BundleError("Staging files differ from the manifest")
    client = client or cloud_http.Client()
    blobs.upload_missing(files, client=client, cancel=cancel, progress=progress, multipart=multipart)
    digest = hashlib.sha256(body).hexdigest()
    with tempfile.TemporaryDirectory(prefix="loopcut-manifest-") as temporary:
        path = Path(temporary) / "manifest.json"
        path.write_bytes(body)
        blobs.upload_missing([{"path": path, "sha256": digest, "size": len(body), "mime": "application/json"}],
                             client=client, cancel=cancel, progress=progress)
    return digest


# ------------------------------------------------------------------ Blender child

def _resolve_path(raw, base):
    if raw.startswith("//"):
        return (Path(base).parent / raw[2:]).resolve()
    path = Path(raw)
    return (path if path.is_absolute() else Path(base).parent / path).resolve()


def _is_system_path(path, system_roots):
    """Classify normalized absolute paths without bpy or filesystem access.

    Use path components, not string prefixes: a sibling such as 5.2-custom is external.
    Empty resource roots mean unavailable, never the current working directory.
    """
    path = Path(os.path.normcase(os.path.abspath(path)))
    return any(path.is_relative_to(Path(os.path.normcase(os.path.abspath(root))))
               for root in system_roots if root)


def _system_roots(bpy):
    roots = [bpy.utils.resource_path(kind) for kind in ("SYSTEM", "LOCAL")]
    roots.append(bpy.utils.system_resource("DATAFILES"))
    return tuple(Path(root).resolve() for root in roots if root)


def _members(path, sequence):
    """Preserve numbering and tile names; regex input is escaped before compilation."""
    if not sequence:
        return [path] if path.is_file() else []
    pattern = re.escape(path.name)
    if "<UDIM>" in path.name:
        pattern = pattern.replace(re.escape("<UDIM>"), r"[0-9]{4}")
    elif "<UVTILE>" in path.name:
        pattern = pattern.replace(re.escape("<UVTILE>"), r"u[0-9]+_v[0-9]+")
    else:
        match = re.fullmatch(r"(.*?)([0-9]+)(\.[^.]*)", path.name)
        if not match:
            return [path] if path.is_file() else []
        pattern = re.escape(match[1]) + r"[0-9]{" + str(len(match[2])) + "}" + re.escape(match[3])
    if not path.parent.is_dir():
        return []
    return sorted(p for p in path.parent.iterdir() if p.is_file() and re.fullmatch(pattern, p.name))


def _references(bpy, unsupported):
    """Known input paths, including explicit simulation caches and sequencer media."""
    refs, seen = [], set()

    def add(owner, attribute, folder, *, sequence=False, directory=False, members=None, image=False):
        key = (owner.as_pointer(), attribute)
        raw = getattr(owner, attribute, "")
        if not raw or key in seen or raw == "<builtin>":
            return
        seen.add(key)
        idb = getattr(owner, "id_data", owner)
        refs.append({"owner": owner, "attribute": attribute, "folder": folder, "raw": raw,
                     "sequence": sequence, "directory": directory, "members": members,
                     "image": image,
                     "library": getattr(idb, "library", None), "name": getattr(idb, "name", folder)})

    for collection, folder in (("images", "textures"), ("sounds", "sounds"), ("movieclips", "clips"),
                               ("fonts", "fonts"), ("volumes", "volumes"), ("cache_files", "caches")):
        for item in getattr(bpy.data, collection, ()):
            if getattr(item, "packed_file", None) or getattr(item, "packed_files", ()):
                continue
            if collection == "images" and item.source not in {"FILE", "MOVIE", "SEQUENCE", "TILED"}:
                continue
            sequence = (getattr(item, "is_sequence", False)
                        or getattr(item, "source", "") in {"SEQUENCE", "TILED"})
            add(item, "filepath_raw" if collection == "images" else "filepath", folder,
                sequence=sequence, image=collection == "images")
            for layer in getattr(item, "layers", ()) if collection == "cache_files" else ():
                add(layer, "filepath", folder)

    def point_cache(cache, label):
        if cache is None:
            return
        if cache.use_external:
            add(cache, "filepath", "caches", directory=True)
        elif cache.use_disk_cache:
            unsupported.append({"type": "cache", "name": label, "reason": "Implicit disk point cache"})

    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            point_cache(getattr(mod, "point_cache", None), obj.name)
            domain = getattr(mod, "domain_settings", None)
            if domain:
                add(domain, "cache_directory", "caches", directory=True)
            if mod.type in {"MESH_CACHE", "MULTIRES"}:
                add(mod, "filepath", "caches")
            if mod.type == "OCEAN" and mod.is_cached:
                add(mod, "filepath", "caches", directory=True)
            if mod.type == "NODES":
                if mod.bake_target == "DISK":
                    add(mod, "bake_directory", "caches", directory=True)
                for bake in mod.bakes:
                    target = bake.bake_target if bake.bake_target != "INHERIT" else mod.bake_target
                    if bake.use_custom_path and target == "DISK":
                        add(bake, "directory", "caches", directory=True)
        for particles in obj.particle_systems:
            point_cache(particles.point_cache, obj.name)
    for scene in bpy.data.scenes:
        if scene.rigidbody_world:
            point_cache(scene.rigidbody_world.point_cache, scene.name)
        if scene.sequence_editor:
            for strip in scene.sequence_editor.strips_all:
                if strip.type == "IMAGE":
                    add(strip, "directory", "textures", directory=True,
                        members=[element.filename for element in strip.elements])
                elif strip.type == "MOVIE":
                    add(strip, "filepath", "clips")
    return refs


def _not_inputs(bpy, base, library_paths):
    """Paths blend_paths lists that Blender never reads, so the render machine never needs them.

    ID.library_weak_reference is the file an ID was appended from with Append (Reuse Data), as
    BlenderKit does; it is listed even for packed IDs (Report Missing Files skips it). A sound
    strip keeps its own copy of the path it was made from (scene.cc lists it even when the sound
    is packed); it plays the Sound, which is packed or copied. Python cannot read that copy, and
    it goes stale once the sound is unpacked or relinked (a generated sound's temporary download),
    so it is found per scene: the paths file_path_map lists for a scene that none of its movie or
    image strips claim.
    """
    paths = set()
    collection_type = getattr(getattr(bpy, "types", None), "bpy_prop_collection", list)
    for name in dir(bpy.data):
        collection = getattr(bpy.data, name, None)
        if name.startswith("_") or not isinstance(collection, collection_type):
            continue
        for item in collection:
            weak = getattr(item, "library_weak_reference", None)
            if weak is not None and weak.filepath:
                library = getattr(item, "library", None)
                paths.add(_resolve_path(weak.filepath,
                                        library_paths[library.as_pointer()] if library else base))
    scenes = [scene for scene in bpy.data.scenes if scene.sequence_editor]
    for scene, listed in (bpy.data.file_path_map(subset=scenes) if scenes else {}).items():
        scene_base = library_paths[scene.library.as_pointer()] if scene.library else base
        claimed = set()
        for strip in scene.sequence_editor.strips_all:
            if strip.type == "MOVIE":
                claimed.add(_resolve_path(strip.filepath, scene_base))
            elif strip.type == "IMAGE":
                claimed.update(_resolve_path(os.path.join(strip.directory, element.filename), scene_base)
                               for element in strip.elements)
        paths.update(path for raw in listed if raw and (path := _resolve_path(raw, scene_base)) not in claimed)
    return paths


# Drivers. The render service runs no Python, so it refuses a driver that needs it (render_job.py secure_scene).
# Rigs carry many scripted drivers: BlenderKit's free rigged characters have 100 each ("var" and "1-var" IK/FK
# switches), all simple expressions, which Blender evaluates without Python (fcurve_driver.cc tries that first).
PYTHON_DRIVER = ("Python driver expression; the cloud renderer runs no Python. Replace it with a simple expression "
                 "(such as var * 2 or 1 - var) or bake the property to keyframes")
LINKED_PYTHON_DRIVER = PYTHON_DRIVER + ", in the library file or after making the data local"
# A driver's mute and type are animatable: keyframing animation_data.drivers[0].mute turned a muted Python driver on
# mid-render on the worker (Blender 5.2.2, 2026-10-21), after its load-time check had passed it.
DRIVER_ANIMATED = ("Animation that changes driver settings (such as a driver's mute or type), which can switch a "
                   "Python driver on mid-render; the cloud renderer refuses it. Remove the keyframes or drivers on "
                   "driver settings and leave each driver as it should render")
LINKED_DRIVER_ANIMATED = DRIVER_ANIMATED + ", in the library file or after making the data local"


def driver_acceptable(fcurve):
    """The render service's driver rule, identical to its own (loopcut-web workers/render/render_job.py): a driver
    renders there when its F-curve is muted, it is not a scripted expression, or its expression is simple. Data
    from a linked library follows the same rule."""
    return bool(fcurve.mute or fcurve.driver.type != "SCRIPTED" or fcurve.driver.is_simple_expression)


def animates_drivers(fcurve):
    """The render service's second rule, identical to its own (render_job.py animates_drivers): it refuses keyframes
    or a driver whose data path writes into animation data, such as animation_data.drivers[0].mute."""
    return "animation_data" in fcurve.data_path


def _action_curves(action):
    """Every F-curve of a layered (Blender 5.x) action, in every layer, strip and slot, as the render service reads
    it."""
    for layer in getattr(action, "layers", ()):
        for strip in layer.strips:
            for channelbag in getattr(strip, "channelbags", ()):
                yield from channelbag.fcurves


def _driver_holders(bpy):
    """(holder, host) for every data-block and embedded node tree that can carry drivers, as the render service
    walks them; host is the data-block that names the holder. Once each: bpy.data.all_ids lists every ID again."""
    holders, seen = [], set()
    collection_type = getattr(getattr(bpy, "types", None), "bpy_prop_collection", list)
    for name in dir(bpy.data):
        collection = getattr(bpy.data, name, None)
        if name.startswith("_") or not isinstance(collection, collection_type):
            continue
        for block in collection:
            for holder in (block, getattr(block, "node_tree", None)):
                key = holder.as_pointer() if hasattr(holder, "as_pointer") else id(holder)
                if holder is not None and key not in seen:
                    seen.add(key)
                    holders.append((holder, block))
    return holders


def _driver_path(holder, curve):
    """data_path[index] for an array item, data_path for a single value."""
    try:
        value = holder.path_resolve(curve.data_path)
    except (AttributeError, ValueError, TypeError):
        value = None
    array = curve.array_index > 0 or (hasattr(value, "__len__") and not isinstance(value, str))
    return curve.data_path + (f"[{curve.array_index}]" if array else "")


def _refused_row(kind, host, path, reason, linked_reason):
    """An unsupported row named by its data-block's type and name and its data path: driver Object "Armature"
    location[2]. Linked data gets the reason that says where to fix it."""
    block = getattr(getattr(host, "bl_rna", None), "name", None) or type(host).__name__
    return {"type": kind, "name": f'{block} "{host.name}"', "path": path,
            "reason": reason if getattr(host, "library", None) is None else linked_reason}


def _refused_drivers(bpy):
    """Unsupported rows for the drivers the render service refuses, then for the animation of driver settings it
    refuses: drivers on any holder, and keyframes in any action (keyframes Action "Rig" animation_data.drivers[0].mute),
    as render_job.py secure_scene scans them."""
    rows, animated = [], []
    for holder, host in _driver_holders(bpy):
        animation = getattr(holder, "animation_data", None)
        for curve in list(animation.drivers) if animation is not None else ():
            python, changes = not driver_acceptable(curve), animates_drivers(curve)
            if not (python or changes):
                continue
            path = ("" if holder is host else "node_tree.") + _driver_path(holder, curve)
            if python:
                rows.append(_refused_row("driver", host, path, PYTHON_DRIVER, LINKED_PYTHON_DRIVER))
            if changes:
                animated.append(_refused_row("driver", host, path, DRIVER_ANIMATED, LINKED_DRIVER_ANIMATED))
    animated.extend(_refused_row("keyframes", action, _driver_path(action, curve), DRIVER_ANIMATED,
                                 LINKED_DRIVER_ANIMATED)
                    for action in getattr(bpy.data, "actions", ()) for curve in _action_curves(action)
                    if animates_drivers(curve))
    return rows + animated


def _collect_opened(blend_path, staging_dir, original_path=None):
    """Child only. The only save target is staging/scene.blend, with remapping disabled."""
    import bpy
    _root(staging_dir)
    root = Path(staging_dir)
    # Recheck in the child: the parent checked before launching another process.
    if any(root.iterdir()):
        raise BundleError("Bundle staging directory must be empty")
    base = original_path or str(blend_path)
    missing, unsupported = [], []
    refs = _references(bpy, unsupported)
    libraries = list(bpy.data.libraries)
    library_paths = {lib.as_pointer(): _resolve_path(lib.filepath, base) for lib in libraries}
    system_roots, system = _system_roots(bpy), set()

    def system_path(path):
        if _is_system_path(path, system_roots):
            system.add(path)
            return True
        return False

    refs = [ref for ref in refs if not system_path(_resolve_path(
        ref["raw"], library_paths[ref["library"].as_pointer()] if ref["library"] else base))]
    system_libraries = {key for key, path in library_paths.items() if system_path(path)}
    complex_libraries = {ref["library"].as_pointer() for ref in refs if ref["library"]}
    # A nested dependency also makes its ancestors complex, not just the leaf library.
    for lib in libraries:
        if lib.as_pointer() in system_libraries:
            continue
        parent = lib.parent
        while parent:
            complex_libraries.add(parent.as_pointer())
            parent = parent.parent
    handled, copied = set(), {}

    def copy_reference(ref):
        raw, owner, attribute = ref["raw"], ref["owner"], ref["attribute"]
        path = _resolve_path(raw, base)
        handled.add(str(path))
        group_base = path if ref["directory"] else path.parent
        group_key = str(group_base)
        if ref.get("image") and owner.is_dirty:
            # Two images can use the same disk file but have different recovered buffers.
            group_key += "\0" + owner.name
        group = hashlib.sha256(os.fsencode(group_key)).hexdigest()[:16]
        relative = Path(ref["folder"]) / group
        if not ref["directory"]:
            relative /= path.name
        _safe_path(relative.as_posix())
        destination = root / relative
        if ref["directory"]:
            if ref["members"] is not None:
                sources = [path / _safe_path(name) for name in ref["members"]]
            else:
                sources = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else []
                for child in path.rglob("*") if path.is_dir() else ():
                    if child.is_symlink() and child.is_dir():
                        unsupported.append({"type": "cache", "path": str(child),
                                            "reason": "Symlinked cache directory"})
        else:
            sources = _members(path, ref["sequence"])
        if not sources or (not ref["directory"] and not path.is_file()
                           and "<UDIM>" not in raw and "<UVTILE>" not in raw):
            missing.append({"type": ref["folder"], "name": ref["name"], "path": raw})
        for source in sources:
            handled.add(str(source.resolve()))
            if not source.is_file():
                missing.append({"type": ref["folder"], "name": ref["name"], "path": str(source)})
                continue
            target = destination / source.relative_to(path) if ref["directory"] else destination.parent / source.name
            _safe_path(target.relative_to(root).as_posix())
            previous = copied.get(target)
            if previous is not None and previous != source.resolve():
                raise BundleError("Two external inputs map to the same bundle path")
            copied[target] = source.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(source, target)
            except FileNotFoundError:
                if source.is_file():
                    raise
                missing.append({"type": ref["folder"], "name": ref["name"], "path": str(source)})
        if ref.get("image") and owner.is_dirty:
            if owner.source in {"FILE", "TILED"}:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if owner.source == "FILE":
                    destination.unlink(missing_ok=True)
                owner.save(filepath=str(destination), save_copy=True)
                if owner.source == "FILE" and not destination.is_file():
                    raise BundleError("Blender could not save a recovered image buffer")
            else:
                unsupported.append({"type": "image", "name": ref["name"], "path": raw,
                                    "reason": "Modified movie or sequence image buffer"})
        setattr(owner, attribute, "//" + relative.as_posix() + ("/" if ref["directory"] else ""))

    # Inspect unhandled inputs before rewriting anything. blend_paths also sees linked inputs
    # which are not represented by the supported datablock types (for example external meshes).
    not_inputs = _not_inputs(bpy, base, library_paths)

    # An empty path names no file; resolved, it would be the scene's whole folder.
    def input_paths(local):
        return {path for raw in bpy.utils.blend_paths(absolute=True, local=local)
                if raw and (path := _resolve_path(raw, base)) not in not_inputs and not system_path(path)}

    local_paths, all_paths = input_paths(True), input_paths(False)
    linked_paths = set()
    for ref in refs:
        if ref["library"]:
            path = _resolve_path(ref["raw"], library_paths[ref["library"].as_pointer()])
            linked_paths.add(path)
            linked_paths.update(_members(path, ref["sequence"]))
            if ref["directory"] and path.is_dir():
                linked_paths.update(path.rglob("*"))
    if all_paths - local_paths - linked_paths:
        complex_libraries.update(lib.as_pointer() for lib in libraries)
    for ref in refs:
        if ref["library"]:
            continue
        copy_reference(ref)
    for lib in libraries:
        path = library_paths[lib.as_pointer()]
        handled.add(str(path))
        if lib.as_pointer() in system_libraries:
            continue
        if getattr(lib, "packed_file", None) and lib.as_pointer() not in complex_libraries:
            continue
        if lib.parent or lib.as_pointer() in complex_libraries or getattr(lib, "is_missing", False):
            if not path.is_file() and not getattr(lib, "packed_file", None):
                missing.append({"type": "libraries", "name": lib.name, "path": lib.filepath})
            unsupported.append({"type": "library", "name": lib.name, "path": lib.filepath,
                                "reason": "Nested, missing or externally dependent linked library"})
            continue
        copy_reference({"owner": lib, "attribute": "filepath", "folder": "libraries", "raw": str(path),
                        "sequence": False, "directory": False, "members": None, "name": lib.name})
    for path in sorted(local_paths):
        if str(path) in handled:
            continue
        if path.exists():
            unsupported.append({"type": "external", "path": str(path), "reason": "Unhandled input path"})
        else:
            missing.append({"type": "external", "path": str(path)})
    unsupported.extend(_refused_drivers(bpy))
    scene = bpy.context.scene
    result = {"blender": ".".join(map(str, bpy.app.version)),
              "scene": {"name": scene.name, "camera": scene.camera.name if scene.camera else None,
                        "frame_start": scene.frame_start, "frame_end": scene.frame_end,
                        "fps": scene.render.fps / scene.render.fps_base},
              "created_at": _timestamp(Path(blend_path).stat().st_mtime),
              "missing": missing, "unsupported": unsupported, "system": [str(path) for path in sorted(system)]}
    bpy.context.preferences.filepaths.save_version = 0
    bpy.context.preferences.filepaths.save_modified_images = "NEVER_SAVE"
    # Paths are already relative to staging; the default remap would rebase them a second time.
    status = bpy.ops.wm.save_as_mainfile(filepath=str(root / ENTRY), compress=True, relative_remap=False)
    if status != {"FINISHED"}:
        raise BundleError("Blender could not save the bundled scene")
    return result


def collect(blend_path, staging_dir, original_path=None):
    """Collect in an isolated child. staging_dir must be empty; original_path identifies a
    recovery snapshot's source filename (including '' for an unsaved scene). For stock
    copy-saves pass None, because their relative paths were rebased to the snapshot folder.
    Returns {blender, scene, created_at, missing, unsupported, system}; never uploads anything.
    """
    import bpy
    from . import worker_ipc
    blend = Path(blend_path).resolve()
    root = _root(staging_dir)
    if not blend.is_file():
        raise BundleError("Checkpoint file is missing")
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise BundleError("Bundle staging directory must be empty")
    with tempfile.TemporaryDirectory(prefix="loopcut-bundle-") as temporary:
        control = Path(temporary)
        (control / "tmp").mkdir()
        (control / "blender-config").mkdir()
        worker_ipc.write_env_file(control, 0)
        environment = worker_ipc.child_env(control, control)
        environment.update(LOOPCUT_DATA_DIR=str(control / "data"), LOOPCUT_CONFIG_DIR=str(control / "config"),
                           BLENDER_USER_CONFIG=str(control / "blender-config"))
        request, reply, log = control / "request.json", control / "reply.json", control / "blender.log"
        request.write_text(json.dumps({"blend": str(blend), "staging": str(root), "original": original_path}),
                           encoding="utf-8")
        if threading.current_thread() is threading.main_thread():
            binary = bpy.app.binary_path
        else:
            from . import mainthread
            binary = mainthread.run_on_main(lambda: bpy.app.binary_path).result(timeout=30)
        command = [binary, "-b", "--factory-startup", "--disable-autoexec",
                   "--python-exit-code", "1", "--python", str(Path(__file__).resolve()),
                   "--", str(request), str(reply)]
        try:
            with log.open("wb") as output:
                completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                           env=environment, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as ex:
            raise BundleError("Could not run Blender bundle collection") from ex
        if completed.returncode or not reply.is_file():
            with log.open("rb") as output:
                output.seek(max(0, output.seek(0, os.SEEK_END) - 4000))
                tail = output.read(4000).decode("utf-8", errors="replace")
            raise BundleError("Blender bundle collection failed: " + tail)
        result = json.loads(reply.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not (root / ENTRY).is_file():
            raise BundleError("Blender returned an invalid bundle result")
        return result


def _worker_main():
    import bpy
    request, reply = sys.argv[sys.argv.index("--") + 1:]
    spec = json.loads(Path(request).read_text(encoding="utf-8"))
    if spec["original"] is not None:
        status = bpy.ops.wm.recover_auto_save(filepath=spec["blend"], use_scripts=False)
    else:
        status = bpy.ops.wm.open_mainfile(filepath=spec["blend"], load_ui=False, use_scripts=False)
    if status != {"FINISHED"}:
        raise BundleError("Blender could not open the checkpoint")
    result = _collect_opened(spec["blend"], spec["staging"], spec["original"])
    Path(reply).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    _worker_main()
