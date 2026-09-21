"""Poly Haven (CC0 models, PBR textures, HDRIs) as two tools: search and import.

Why: the agent builds everything procedurally, and a procedural bottle or brick wall looks cheap
next to a scanned one. A library asset is one call and, being CC0, needs no key and no credit.
The API (https://api.polyhaven.com) and CDN are public; downloads are verified against the md5
the API publishes and kept in a cache under Loopcut's data folder so a second import is free.

Import runs on Blender's main thread like every tool, so a download blocks the UI for its
duration: resolutions are capped at 4k and 1k is the default. Downloads, thumbnails and ranking
are asset_common's, shared with the other library tools.
"""

import json
import re
import time
import urllib.parse
from pathlib import Path

import bpy

from . import asset_common
from .asset_common import NotFound, download, get_json, rank, sheet, valid_slug  # noqa: F401  (re-exported for tests)
from .tools import ToolError, ToolResult, _clip, object_summary

API = "https://api.polyhaven.com"
SITE = "https://polyhaven.com/a/"
LIST_TTL = 3600.0         # The asset list changes weekly; a process re-reads it after an hour.
TYPES = ("hdris", "textures", "models")
TYPE_CODES = {0: "hdris", 1: "textures", 2: "models"}
RESOLUTIONS = ("1k", "2k", "4k")
DEFAULT_LIMIT = 8
MAX_LIMIT = 12
THUMB_SIDE = 256          # What the CDN serves for thumbnails; the sheet scales them down.
# Map keys the API uses, and the Principled input each drives. nor_gl is the OpenGL-convention
# normal Blender's Normal Map node expects; nor_dx is skipped. AO is left out: Principled has no
# input for it and multiplying it into base color darkens the scan twice under real lighting.
TEXTURE_ROLES = {"Diffuse": "base_color", "Rough": "roughness", "Metal": "metallic",
                 "nor_gl": "normal", "Displacement": "displacement"}
COLOR_ROLES = {"base_color"}

SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "search_polyhaven",
        "description": (
            "Search Poly Haven, a CC0 library of scanned models (furniture, props, plants, food, "
            "tools), PBR textures (wood, brick, fabric, ground) and HDRI skies for lighting. Returns "
            "the best matches with a sheet of their thumbnails in the same order. Prefer a library "
            "asset to building a realistic prop, surface or sky yourself; then import_polyhaven."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to match: 'wooden chair', 'brick wall', 'sunset'"},
            "type": {"type": "string", "enum": list(TYPES)},
            "limit": {"type": "integer", "description": f"Default {DEFAULT_LIMIT}, max {MAX_LIMIT}"},
        }, "required": ["query", "type"]},
    }},
    {"type": "function", "function": {
        "name": "import_polyhaven",
        "description": (
            "Download and import a Poly Haven asset by id. A model arrives as a collection at the "
            "origin, in meters: move and scale it after (the result gives its bounds). A texture "
            "becomes a material, applied to `apply_to` objects if given. An HDRI becomes the scene "
            "world. Files are cached; 1k suits previews, 2k final renders."),
        "parameters": {"type": "object", "properties": {
            "asset_id": {"type": "string", "description": "The id from search_polyhaven"},
            "resolution": {"type": "string", "enum": list(RESOLUTIONS), "description": "Default 1k"},
            "apply_to": {"type": "array", "items": {"type": "string"},
                         "description": "Textures only: objects to give the material"},
        }, "required": ["asset_id"]},
    }},
]

_lists: dict[str, tuple[float, dict]] = {}  # type -> (fetched, assets), per process.


# ------------------------------------------------------------------ pure helpers (unit tested)

def match_assets(query: str, assets: dict, limit: int = DEFAULT_LIMIT) -> list[tuple[str, dict]]:
    """(slug, record) pairs ranked by query words in slug, name, tags and categories; ties by popularity."""
    return rank(query, assets.items(),
                text_of=lambda it: " ".join([it[0], it[1].get("name", ""), *it[1].get("tags", []), *it[1].get("categories", [])]),
                name_of=lambda it: f"{it[0]} {it[1].get('name', '')}",
                weight_of=lambda it: it[1].get("download_count", 0), limit=limit)


def describe_asset(index: int, slug: str, record: dict) -> str:
    kind = TYPE_CODES.get(record.get("type"), "?")
    parts = [f"{index}. {slug}: {record.get('name', slug)}"]
    dims = record.get("dimensions")
    if dims and kind == "models":
        parts.append("%.2fx%.2fx%.2f m" % tuple(d / 1000 for d in dims[:3]))
    elif dims and kind == "textures":
        parts.append("%.1fx%.1f m tile" % tuple(d / 1000 for d in dims[:2]))
    if record.get("polycount"):
        parts.append(f"{record['polycount']} faces")
    if kind == "hdris" and record.get("evs_cap"):
        parts.append(f"{record['evs_cap']} EV range")
    categories = ", ".join(record.get("categories", [])[:4])
    if categories:
        parts.append(categories)
    return "; ".join(parts)


def pick_texture_maps(files: dict, resolution: str, fmt: str = "jpg") -> dict[str, dict]:
    """{role: file record} for the maps a texture publishes at this resolution. The packed `arm`
    map (AO, roughness, metal in R, G, B) stands in when Rough is missing; a texture whose albedo
    is not named Diffuse (multi-variant fabrics: col_1, col_2) contributes its first color map."""
    picked = {}
    for key, role in TEXTURE_ROLES.items():
        record = (files.get(key) or {}).get(resolution, {}).get(fmt)
        if record:
            picked[role] = record
    if "base_color" not in picked:
        for key in sorted(files):
            if key.lower().startswith(("col", "diff")):
                record = (files[key] or {}).get(resolution, {}).get(fmt)
                if record:
                    picked["base_color"] = record
                    break
    if "roughness" not in picked:
        record = (files.get("arm") or {}).get(resolution, {}).get(fmt)
        if record:
            picked["arm"] = record
    return picked


def available(files: dict) -> str:
    """What an asset offers, for the error when the asked-for resolution is missing."""
    resolutions = set()
    for by_resolution in files.values():
        if isinstance(by_resolution, dict):
            resolutions |= {r for r, v in by_resolution.items() if isinstance(v, dict)}
    return ", ".join(sorted(resolutions & set(RESOLUTIONS), key=RESOLUTIONS.index)) or "none of 1k/2k/4k"


def safe_include(root: Path, include_path: str) -> Path | None:
    """Where a model's sidecar texture lands, or None when the API-supplied path would escape."""
    if not include_path or Path(include_path).is_absolute() or ".." in Path(include_path).parts:
        return None
    target = (root / include_path).resolve()
    return target if root.resolve() in target.parents else None


def describe(name: str, arguments: dict):
    """The step's label in the chat, like job_tools.describe."""
    if name == "search_polyhaven":
        return f"Search Poly Haven {arguments.get('type', '')}: {arguments.get('query', '')}".strip(), ""
    if name == "import_polyhaven":
        return f"Import Poly Haven asset {arguments.get('asset_id', '?')} ({arguments.get('resolution') or '1k'})", ""
    return None


# ------------------------------------------------------------------ network

def cache_root() -> Path:
    return asset_common.cache_root("polyhaven")


def assets(kind: str) -> dict:
    fetched, cached = _lists.get(kind, (0.0, None))
    if cached is None or time.time() - fetched > LIST_TTL:
        cached = get_json(f"{API}/assets?type={kind}")
        _lists[kind] = (time.time(), cached)
    return cached


def _file(record: dict, dest: Path) -> Path:
    """One published file into the cache, checked against the md5 the API gives."""
    md5 = record.get("md5")
    return download(record["url"], dest, f"md5:{md5}" if md5 else None, record.get("size", 0))


def thumbnail_url(slug: str, record: dict) -> str:
    """The CDN thumbnail at THUMB_SIDE, whatever size the asset list asked for."""
    url = record.get("thumbnail_url") or f"https://cdn.polyhaven.com/asset_img/thumbs/{slug}.png"
    parts = urllib.parse.urlsplit(url)
    query = {k: v for k, v in urllib.parse.parse_qsl(parts.query) if k not in ("width", "height")}
    query.update(width=str(THUMB_SIDE), height=str(THUMB_SIDE))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _thumbnail(slug: str, record: dict) -> Path | None:
    dest = cache_root() / "thumbs" / f"{slug}_{record.get('img_version', '0')}.png"
    return asset_common.thumbnail(thumbnail_url(slug, record), dest)


# ------------------------------------------------------------------ tools

def search_polyhaven(query: str, type: str, limit: int = DEFAULT_LIMIT) -> ToolResult:
    if type not in TYPES:
        raise ToolError(f"type must be one of {', '.join(TYPES)}")
    if not str(query).strip():
        raise ToolError("Give a few words to search for.")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    found = match_assets(str(query), assets(type), limit)
    if not found:
        raise ToolError(f"No Poly Haven {type} match {query!r}. Try fewer or more general words.")
    lines = [describe_asset(i, slug, record) for i, (slug, record) in enumerate(found, 1)]
    image = sheet([_thumbnail(slug, record) for slug, record in found], "polyhaven_sheet.png")
    text = f"{len(found)} Poly Haven {type} for {query!r}, numbered as on the sheet (left to right, top to bottom):\n" \
           + "\n".join(lines)
    if image:
        text += "\n\nImage attached: thumbnails in that order. Import with import_polyhaven(asset_id)."
    return ToolResult(_clip(text), image_path=image)


def import_polyhaven(asset_id: str, resolution: str = "1k", apply_to: list[str] | None = None) -> ToolResult:
    if not valid_slug(asset_id):
        raise ToolError("asset_id is the id from search_polyhaven: letters, digits, _ and - only.")
    resolution = resolution or "1k"
    if resolution not in RESOLUTIONS:
        raise ToolError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    try:
        info = get_json(f"{API}/info/{urllib.parse.quote(asset_id, safe='')}")
    except NotFound:
        info = {}
    kind = TYPE_CODES.get(info.get("type"))
    if kind is None:
        raise ToolError(f"Poly Haven has no asset {asset_id!r}. Search first and use the id it gives.")
    files = get_json(f"{API}/files/{urllib.parse.quote(asset_id, safe='')}")
    folder = cache_root() / asset_id / resolution
    if kind == "hdris":
        return _import_hdri(asset_id, info, files, resolution, folder)
    if kind == "textures":
        return _import_texture(asset_id, info, files, resolution, folder, apply_to or [])
    return _import_model(asset_id, info, files, resolution, folder)


def _tag(datablocks, asset_id: str, info: dict) -> None:
    authors = ", ".join(info.get("authors", {}))
    for block in datablocks:
        try:
            block["polyhaven"] = asset_id
            block["polyhaven_authors"] = authors
        except Exception:
            pass


def _credit(asset_id: str, info: dict) -> str:
    return f"CC0, by {', '.join(info.get('authors', {})) or 'Poly Haven'} ({SITE}{asset_id})"


def _set_colorspace(image, is_color: bool) -> None:
    try:
        image.colorspace_settings.name = "sRGB" if is_color else "Non-Color"
    except TypeError:
        pass  # A custom OCIO config without these names: Blender's loader default stands.


def _import_hdri(asset_id, info, files, resolution, folder) -> ToolResult:
    record = (files.get("hdri") or {}).get(resolution, {}).get("hdr")
    if not record:
        raise ToolError(f"{asset_id} has no {resolution} HDR; available: {available(files)}")
    path = _file(record, folder / f"{asset_id}_{resolution}.hdr")
    previous = bpy.context.scene.world.name if bpy.context.scene.world else None
    world = asset_common.hdri_world(asset_id, path)
    _tag([world], asset_id, info)
    note = f" The previous world {previous!r} is kept, unused." if previous else ""
    return ToolResult(f"HDRI {asset_id} ({resolution}) is now the scene world {world.name!r}; rotate it with "
                      f"its Mapping node.{note} {_credit(asset_id, info)}")


def _build_material(asset_id: str, maps: dict, info: dict) -> tuple:
    """A Principled material from {role: image}; returns it and the roles that were wired."""
    material = bpy.data.materials.new(asset_id)
    material.use_nodes = True
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (600, 0)
    principled = nodes.new("ShaderNodeBsdfPrincipled")
    principled.location = (300, 0)
    links.new(principled.outputs[0], output.inputs["Surface"])
    coords = nodes.new("ShaderNodeTexCoord")
    coords.location = (-1000, 0)
    mapping = nodes.new("ShaderNodeMapping")
    mapping.name = mapping.label = "Mapping"
    mapping.location = (-800, 0)
    links.new(coords.outputs["UV"], mapping.inputs["Vector"])
    y, wired = 300, []
    for role, image in maps.items():
        tex = nodes.new("ShaderNodeTexImage")
        tex.location, tex.image = (-500, y), image
        tex.label = role
        links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
        y -= 300
        if role == "base_color":
            links.new(tex.outputs["Color"], principled.inputs["Base Color"])
        elif role == "roughness":
            links.new(tex.outputs["Color"], principled.inputs["Roughness"])
        elif role == "metallic":
            links.new(tex.outputs["Color"], principled.inputs["Metallic"])
        elif role == "arm":
            split = nodes.new("ShaderNodeSeparateColor")
            split.location = (-200, tex.location[1])
            links.new(tex.outputs["Color"], split.inputs["Color"])
            links.new(split.outputs["Green"], principled.inputs["Roughness"])
            links.new(split.outputs["Blue"], principled.inputs["Metallic"])
        elif role == "normal":
            normal = nodes.new("ShaderNodeNormalMap")
            normal.location = (-200, tex.location[1])
            links.new(tex.outputs["Color"], normal.inputs["Color"])
            links.new(normal.outputs["Normal"], principled.inputs["Normal"])
        elif role == "displacement":
            disp = nodes.new("ShaderNodeDisplacement")
            disp.location = (300, tex.location[1])
            disp.inputs["Midlevel"].default_value = 0.5
            disp.inputs["Scale"].default_value = 0.05
            links.new(tex.outputs["Color"], disp.inputs["Height"])
            links.new(disp.outputs["Displacement"], output.inputs["Displacement"])
            if hasattr(material, "displacement_method"):
                material.displacement_method = "BUMP"  # True displacement is the user's call: it needs subdivision.
        else:
            continue
        wired.append(role)
    _tag([material, *maps.values()], asset_id, info)
    return material, wired


def _import_texture(asset_id, info, files, resolution, folder, apply_to: list[str]) -> ToolResult:
    picked = pick_texture_maps(files, resolution)
    if not picked:
        raise ToolError(f"{asset_id} has no {resolution} maps; available: {available(files)}")
    missing = [name for name in apply_to if name not in bpy.data.objects]
    if missing:
        raise ToolError(f"No such object(s) to apply to: {', '.join(missing)}")
    maps = {}
    for role, record in picked.items():
        path = _file(record, folder / Path(urllib.parse.urlparse(record["url"]).path).name)
        image = bpy.data.images.load(str(path), check_existing=True)
        _set_colorspace(image, role in COLOR_ROLES)
        image.pack()
        maps[role] = image
    material, wired = _build_material(asset_id, maps, info)
    applied = []
    for name in apply_to:
        obj = bpy.data.objects[name]
        if not hasattr(obj.data, "materials"):
            continue
        if obj.data.materials:
            obj.data.materials[obj.active_material_index] = material
        else:
            obj.data.materials.append(material)
        applied.append(name)
    dims = info.get("dimensions")
    scale = ("; one UV tile covers %.1fx%.1f m, so set the Mapping node's Scale to object size over that for "
             "real-world scale" % (dims[0] / 1000, dims[1] / 1000)) if dims else ""
    where = f", applied to {', '.join(applied)}" if applied else ", not applied to anything yet"
    return ToolResult(f"Material {material.name!r} built from {asset_id} ({resolution}: {', '.join(wired)}){where}"
                      f"{scale}. {_credit(asset_id, info)}")


def _fetch_model(files: dict, fmt: str, resolution: str, folder: Path) -> Path | None:
    record = (files.get(fmt) or {}).get(resolution, {}).get(fmt)
    if not record:
        return None
    main = _file(record, folder / Path(urllib.parse.urlparse(record["url"]).path).name)
    for include_path, include in (record.get("include") or {}).items():
        target = safe_include(folder, include_path)
        if target is None:
            print(f"Loopcut: skipping Poly Haven include with unsafe path {include_path!r}")
            continue
        _file(include, target)
    return main


def _import_model(asset_id, info, files, resolution, folder) -> ToolResult:
    before = {obj.name for obj in bpy.data.objects}
    note = ""
    blend = _fetch_model(files, "blend", resolution, folder)
    if blend is None:
        raise ToolError(f"{asset_id} has no {resolution} .blend; available: {available(files)}")
    try:
        _append_blend(blend, asset_id)
    except Exception as ex:  # A .blend from a newer Blender, or a broken download: glTF is worse but works.
        gltf = _fetch_model(files, "gltf", resolution, folder)
        if gltf is None:
            raise ToolError(f"Could not open {asset_id}'s .blend ({ex}) and it publishes no glTF") from ex
        bpy.ops.import_scene.gltf(filepath=str(gltf))
        note = f" Imported from glTF because the .blend could not be opened ({ex}); materials are a conversion."
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    if not imported:
        raise ToolError(f"{asset_id} downloaded but nothing arrived in the scene")
    materials = []
    for obj in imported:
        for slot in getattr(obj, "material_slots", []):
            material = slot.material
            if material is None or material in materials:
                continue
            materials.append(material)
            if material.use_nodes:
                for node in material.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image and not node.image.packed_file:
                        try:
                            node.image.pack()
                        except RuntimeError:
                            pass
    _tag([*imported, *materials], asset_id, info)
    summaries = [object_summary(obj) for obj in imported]
    return ToolResult(_clip(f"Imported {asset_id} ({resolution}) as {len(imported)} object(s) at the origin, "
                            f"in meters.{note} Place and scale it now. {_credit(asset_id, info)}\n"
                            + json.dumps(summaries, separators=(",", ":"), default=str)))


def _append_blend(path: Path, asset_id: str) -> None:
    """Append the asset's own collection: every published model holds one named after its id,
    with `<id>_LOD0` the full-detail version when levels of detail exist."""
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        wanted = next((n for n in (f"{asset_id}_LOD0", asset_id) if n in data_from.collections), None)
        if wanted:
            data_to.collections = [wanted]
        else:
            data_to.objects = list(data_from.objects)
    for collection in data_to.collections:
        if collection is not None:
            bpy.context.scene.collection.children.link(collection)
    if not data_to.collections:
        for obj in data_to.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)


DISPATCH = {"search_polyhaven": search_polyhaven, "import_polyhaven": import_polyhaven}
