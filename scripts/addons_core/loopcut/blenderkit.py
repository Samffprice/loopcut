"""BlenderKit (Blendkit) as two tools: search and import.

The largest library Loopcut can reach: tens of thousands of free models, materials and HDRIs
need no account at all (search is public and free assets download anonymously), and a user's own
Full or Business plan key unlocks the rest. The key is read from Loopcut's credentials file,
LOOPCUT_BLENDERKIT_KEY, or the BlenderKit add-on's preferences when it is installed, so a
subscriber usually pastes nothing. Loopcut never holds a shared key: the licence for a downloaded
asset goes to the account that downloaded it, and it may not be passed to a third party.

Facts checked against the live API on 2026-09-21: a model .blend holds one collection named after
the asset with its textures packed; a material .blend holds one material named after the asset;
an HDR's main file is .hdr and its resolution variants are .exr; resolution variants of models and
materials are whole .blend files. The download endpoint wants a scene id, which feeds the authors'
fair-share payout, so the same id is kept per scene like their add-on does. Cloudflare in front of
their file server rejects Python's default User-Agent; asset_common sends Loopcut's.
"""

import json
import os
import urllib.parse
import uuid
from pathlib import Path

import bpy

from . import asset_common, credentials
from .asset_common import NotFound, download, get_json, sheet, valid_slug
from .tools import ToolError, ToolResult, _clip, object_summary

API = "https://www.blenderkit.com/api/v1"
SITE = "https://www.blenderkit.com/asset-gallery-detail/"
PLANS = "https://www.blenderkit.com/plans/"   # Swap for the affiliate link once the account exists.
KEY_ID = "https://www.blenderkit.com"
ENV_KEY = "LOOPCUT_BLENDERKIT_KEY"
SCENE_KEY = "uuid"                 # The property BlenderKit's own add-on keeps its scene id under.
TYPES = ("model", "material", "hdr")
RESOLUTIONS = {"0.5k": "resolution_0_5K", "1k": "resolution_1K", "2k": "resolution_2K",
               "4k": "resolution_4K", "original": "blend"}
DEFAULT_RESOLUTION = "1k"
DEFAULT_LIMIT = 8
MAX_LIMIT = 12
LICENSES = {"royalty_free": "Royalty Free (commercial use, no credit needed)", "cc_zero": "CC0"}

SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "search_blenderkit",
        "description": (
            "Search BlenderKit, the largest library: models (furniture, props, vehicles, plants, "
            "characters), PBR materials and HDRIs, realistic and stylised. Tens of thousands are free "
            "with no account; a Full-plan key in preferences unlocks the rest. Returns numbered matches "
            "with a thumbnail sheet; then import_blenderkit(asset_id)."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to match: 'office chair', 'oak floor', 'overcast sky'"},
            "type": {"type": "string", "enum": list(TYPES)},
            "limit": {"type": "integer", "description": f"Default {DEFAULT_LIMIT}, max {MAX_LIMIT}"},
        }, "required": ["query", "type"]},
    }},
    {"type": "function", "function": {
        "name": "import_blenderkit",
        "description": (
            "Download and import a BlenderKit asset by id. A model arrives as a collection at the "
            "origin in meters (the result gives its bounds: place it); a material is applied to "
            "`apply_to` objects; an HDRI becomes the scene world. Files are cached; 1k suits previews, "
            "2k final renders."),
        "parameters": {"type": "object", "properties": {
            "asset_id": {"type": "string", "description": "The id from search_blenderkit"},
            "resolution": {"type": "string", "enum": list(RESOLUTIONS), "description": f"Default {DEFAULT_RESOLUTION}"},
            "apply_to": {"type": "array", "items": {"type": "string"},
                         "description": "Materials only: objects to give the material"},
        }, "required": ["asset_id"]},
    }},
]


# ------------------------------------------------------------------ pure helpers (unit tested)

def addon_key() -> str:
    """The key the BlenderKit add-on holds, when it is installed and signed in."""
    try:
        for name, addon in bpy.context.preferences.addons.items():
            if name.rsplit(".", 1)[-1] == "blenderkit":
                return str(getattr(addon.preferences, "api_key", "") or "").strip()
    except Exception:  # No preferences in this context, or an add-on without that field.
        pass
    return ""


def api_key() -> str:
    return os.environ.get(ENV_KEY) or credentials.api_key(KEY_ID) or addon_key()


def search_url(query: str, kind: str, limit: int, free_only: bool) -> str:
    """The search API takes filters inside the query string: 'chair asset_type:model is_free:true'."""
    terms = [str(query).strip(), f"asset_type:{kind}"]
    if free_only:
        terms.append("is_free:true")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    return f"{API}/search/?{urllib.parse.urlencode({'query': ' '.join(terms), 'page_size': limit})}"


def describe_asset(index: int, asset: dict) -> str:
    parts = [f"{index}. {asset.get('id')}: {asset.get('name', '')}"]
    dims = [asset.get(k) for k in ("dimensionX", "dimensionY", "dimensionZ")]
    if all(isinstance(d, (int, float)) and d > 0 for d in dims):
        parts.append("%.2fx%.2fx%.2f m" % tuple(dims))
    if asset.get("faceCount"):
        parts.append(f"{asset['faceCount']} faces")
    parts.append("free" if asset.get("isFree") else "Full plan")
    if not asset.get("canDownload", True):
        parts.append("needs a BlenderKit key")
    author = (asset.get("author") or {}).get("fullName")
    if author:
        parts.append(f"by {author}")
    return "; ".join(parts)


def pick_file(files: list, resolution: str) -> tuple[str, dict] | None:
    """(fileType, file) for the asked resolution, else the nearest lower one, else the original."""
    by_type = {f.get("fileType"): f for f in files if isinstance(f, dict) and f.get("downloadUrl")}
    order = list(RESOLUTIONS)
    if resolution not in RESOLUTIONS:
        raise ToolError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
    start = order.index(resolution)
    for key in order[start::-1] + ["original"]:
        file_type = RESOLUTIONS[key]
        if file_type in by_type:
            return file_type, by_type[file_type]
    return None


def credit_line(asset: dict) -> str:
    license_name = LICENSES.get(str(asset.get("license") or ""), str(asset.get("license") or "licence unstated"))
    author = (asset.get("author") or {}).get("fullName") or "unknown author"
    return f"From BlenderKit ({SITE}{asset.get('assetBaseId') or asset.get('id')}/), {license_name}, by {author}."


def describe(name: str, arguments: dict):
    if name == "search_blenderkit":
        return f"Search BlenderKit {arguments.get('type', '')}: {arguments.get('query', '')}".strip(), ""
    if name == "import_blenderkit":
        return f"Import BlenderKit asset {arguments.get('asset_id', '?')} ({arguments.get('resolution') or DEFAULT_RESOLUTION})", ""
    return None


# ------------------------------------------------------------------ network

def _headers() -> dict:
    key = api_key()
    return {"Authorization": f"Bearer {key}"} if key else {}


def cache_root() -> Path:
    return asset_common.cache_root("blenderkit")


def scene_id() -> str:
    """One id per scene, shared with BlenderKit's add-on, so authors' fair-share counts are right."""
    scene = bpy.context.scene
    value = scene.get(SCENE_KEY)
    if not isinstance(value, str) or not value:
        value = str(uuid.uuid4())
        scene[SCENE_KEY] = value
    return value


def _asset(asset_id: str) -> dict:
    """One asset with its files and download rights: the detail endpoint, else a search by id
    (the detail serializer has carried the file list so far; the search filter is the fallback)."""
    try:
        asset = get_json(f"{API}/assets/{urllib.parse.quote(asset_id, safe='')}/", _headers())
    except NotFound as ex:
        raise ToolError(f"BlenderKit has no asset {asset_id!r}. Search first and use the id it gives.") from ex
    if isinstance(asset, dict) and asset.get("files"):
        return asset
    data = get_json(f"{API}/search/?{urllib.parse.urlencode({'query': f'asset_id:{asset_id}', 'page_size': 1})}", _headers())
    results = data.get("results") or []
    if not results:
        raise ToolError(f"BlenderKit has no asset {asset_id!r}. Search first and use the id it gives.")
    return results[0]


def _fetch(asset: dict, file: dict, dest_stem: Path) -> Path:
    """Resolve a download URL into a signed file URL, then download it into the cache."""
    query = urllib.parse.urlencode({"scene_uuid": scene_id()})
    try:
        signed = get_json(f"{file['downloadUrl']}?{query}", _headers())
    except ToolError as ex:
        if "Refused" in str(ex):
            error = (asset.get("canDownloadError") or {}).get("messages") or []
            raise ToolError(f"BlenderKit refused the download: {' '.join(error) or ex}. "
                            + ("This asset needs a Full plan key in Loopcut's preferences." if not asset.get("isFree")
                               else "Check the BlenderKit key in Loopcut's preferences.")) from ex
        raise
    url = str(signed.get("filePath") or "")
    if not url:
        raise ToolError("BlenderKit answered without a file location; try again")
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".bin"
    return download(url, dest_stem.with_suffix(suffix), size=int(asset.get("filesSize") or 0))


# ------------------------------------------------------------------ tools

def search_blenderkit(query: str, type: str, limit: int = DEFAULT_LIMIT) -> ToolResult:
    if type not in TYPES:
        raise ToolError(f"type must be one of {', '.join(TYPES)}")
    if not str(query).strip():
        raise ToolError("Give a few words to search for.")
    headers = _headers()
    data = get_json(search_url(query, type, limit, free_only=not headers), headers)
    results = [a for a in (data.get("results") or []) if isinstance(a, dict) and a.get("id")]
    if not results:
        raise ToolError(f"No BlenderKit {type}s match {query!r}. Try fewer or more general words.")
    lines = [describe_asset(i, asset) for i, asset in enumerate(results, 1)]
    image = sheet([_thumbnail(asset) for asset in results], "blenderkit_sheet.png")
    scope = "free assets" if not headers else "free and plan assets"
    text = (f"{len(results)} of {data.get('count', len(results))} BlenderKit {type}s for {query!r} ({scope}), "
            f"numbered as on the sheet (left to right, top to bottom):\n" + "\n".join(lines))
    if image:
        text += "\n\nImage attached: thumbnails in that order. Import with import_blenderkit(asset_id)."
    if any(not a.get("canDownload", True) for a in results):
        text += f"\nFull-plan assets need the user's BlenderKit key ({PLANS})."
    return ToolResult(_clip(text), image_path=image)


def _thumbnail(asset: dict) -> Path | None:
    url = str(asset.get("thumbnailSmallUrl") or asset.get("thumbnailMiddleUrl") or "")
    if not url or not valid_slug(str(asset.get("id"))):
        return None
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".png"
    return asset_common.thumbnail(url, cache_root() / "thumbs" / f"{asset['id']}{suffix}")


def _tag(blocks, asset: dict) -> None:
    """Custom properties for provenance. `blenderkit_id`, not `blenderkit`: BlenderKit's own upload
    data already lives under that key as a property group on the datablocks it published."""
    for block in blocks:
        try:
            block["blenderkit_id"] = str(asset.get("id"))
            block["blenderkit_license"] = str(asset.get("license") or "")
            block["blenderkit_author"] = str((asset.get("author") or {}).get("fullName") or "")
        except Exception as ex:  # A linked or otherwise read-only datablock: provenance is a nicety.
            print(f"Loopcut: could not tag {block.name}: {ex}")


def import_blenderkit(asset_id: str, resolution: str = DEFAULT_RESOLUTION, apply_to: list[str] | None = None) -> ToolResult:
    if not valid_slug(str(asset_id)):
        raise ToolError("asset_id is the id from search_blenderkit: letters, digits, _ and - only.")
    resolution = resolution or DEFAULT_RESOLUTION
    apply_to = apply_to or []
    missing = [n for n in apply_to if n not in bpy.data.objects]
    if missing:
        raise ToolError(f"No such object(s) to apply to: {', '.join(missing)}")
    asset = _asset(str(asset_id))
    kind = str(asset.get("assetType") or "")
    if kind not in TYPES:
        raise ToolError(f"{asset.get('name')} is a {kind}; Loopcut imports models, materials and HDRIs")
    if not asset.get("canDownload", True) and not api_key():
        raise ToolError(f"{asset.get('name')!r} is a Full-plan asset; it needs the user's BlenderKit key in "
                        f"Loopcut's preferences ({PLANS}). Free assets need no key.")
    picked = pick_file(asset.get("files") or [], resolution)
    if picked is None:
        raise ToolError(f"{asset.get('name')!r} publishes no downloadable file")
    file_type, file = picked
    path = _fetch(asset, file, cache_root() / str(asset_id) / file_type)
    name = str(asset.get("name") or asset_id)
    if kind == "hdr":
        world = asset_common.hdri_world(name, path)
        _tag([world], asset)
        return ToolResult(f"HDRI {name!r} ({file_type}) is now the scene world {world.name!r}; rotate it with its "
                          f"Mapping node. {credit_line(asset)}")
    if kind == "material":
        with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
            names = list(data_from.materials)
            if not names:
                raise ToolError(f"{path.name} holds no material")
            data_to.materials = [name if name in names else names[0]]
        material = data_to.materials[0]
        applied = []
        for obj_name in apply_to:
            obj = bpy.data.objects[obj_name]
            if not hasattr(obj.data, "materials"):
                continue
            if obj.data.materials:
                obj.data.materials[obj.active_material_index] = material
            else:
                obj.data.materials.append(material)
            applied.append(obj_name)
        _tag([material], asset)
        where = f", applied to {', '.join(applied)}" if applied else ", not applied to anything yet"
        return ToolResult(f"Material {material.name!r} ({file_type}){where}. {credit_line(asset)}")
    before = {obj.name for obj in bpy.data.objects}
    with bpy.data.libraries.load(str(path), link=False) as (data_from, data_to):
        collections = list(data_from.collections)
        if collections:
            data_to.collections = [name if name in collections else collections[0]]
        else:
            data_to.objects = list(data_from.objects)
    for collection in data_to.collections:
        if collection is not None:
            bpy.context.scene.collection.children.link(collection)
    if not data_to.collections:
        for obj in data_to.objects:
            if obj is not None:
                bpy.context.scene.collection.objects.link(obj)
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    if not imported:
        raise ToolError(f"{name!r} downloaded but nothing arrived in the scene")
    _tag([*imported, *data_to.collections], asset)
    rows = [object_summary(obj) for obj in imported]
    return ToolResult(_clip(f"Imported {name!r} ({file_type}) as {len(imported)} object(s) at the origin, in meters. "
                            f"Place it now. {credit_line(asset)}\n" + json.dumps(rows, separators=(",", ":"), default=str)))


DISPATCH = {"search_blenderkit": search_blenderkit, "import_blenderkit": import_blenderkit}
