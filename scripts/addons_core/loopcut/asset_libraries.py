"""The user's Blender asset libraries as two tools: search and import.

Every library registered in Preferences counts: folders of .blend files on disk (kits the user
bought or made), remote libraries (a URL serving Blender's listing format, like ambientCG), and
the Online Essentials library when the user has turned it on. One code path covers them all, so
any library anyone publishes later works without Loopcut changes.

Local libraries are listed with bpy's assets-only library loading, one call per .blend, cached per
process by file stamp; tags and descriptions come from the index Blender's Asset Browser writes to
its cache folder when it has listed the library. Remote libraries are read from the listing JSON
(local copy Blender may have fetched first, then the network) and their files are downloaded into
the same cache folder Blender uses, verified against the published hash, so the Asset Browser and
the agent share downloads.

Loopcut never adds a library to the user's preferences: bandwidth and choice are theirs.
"""

import hashlib
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import bpy

from . import asset_common
from .asset_common import download, get_json, rank, sheet
from .tools import ToolError, ToolResult, _clip, object_summary

ESSENTIALS_URL = "https://cdn.extensions.blender.org/asset-libraries/essentials/"
ESSENTIALS_NAME = "Essentials (online)"
META_FILE = "_asset-library-meta.json"
LIST_TTL = 3600.0
DEFAULT_LIMIT = 8
MAX_LIMIT = 12
REF_SEP = "::"
# ID types a scene can use, with their bpy.data collection; anything else (brushes, palettes) is
# not what the agent builds scenes from.
ID_TYPES = {"OBJECT": "objects", "COLLECTION": "collections", "MATERIAL": "materials", "WORLD": "worlds",
            "NODETREE": "node_groups", "MESH": "meshes", "IMAGE": "images", "ACTION": "actions",
            "LIGHT": "lights", "CAMERA": "cameras", "ARMATURE": "armatures", "CURVE": "curves"}
SEARCH_TYPES = ("OBJECT", "COLLECTION", "MATERIAL", "WORLD", "NODETREE")
# The two-letter ID codes Blender's index files prefix names with (ID.name convention).
ID_CODES = {"OB": "OBJECT", "GR": "COLLECTION", "MA": "MATERIAL", "WO": "WORLD", "NT": "NODETREE",
            "ME": "MESH", "IM": "IMAGE", "AC": "ACTION", "LA": "LIGHT", "CA": "CAMERA", "AR": "ARMATURE", "CU": "CURVE"}

SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "search_assets",
        "description": (
            "Search the user's Blender asset libraries: their own kits on disk, the Online Essentials "
            "library (base meshes, materials, HDRI worlds) and any remote library they added (ambientCG: "
            "PBR materials). Returns numbered matches with a sheet of thumbnails where the library has "
            "them; import with import_asset(ref)."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to match names, tags and descriptions"},
            "type": {"type": "string", "enum": list(SEARCH_TYPES), "description": "Only this kind of asset"},
            "limit": {"type": "integer", "description": f"Default {DEFAULT_LIMIT}, max {MAX_LIMIT}"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "import_asset",
        "description": (
            "Bring an asset from search_assets into the scene by its ref. An object or collection "
            "arrives at the origin (the result gives its bounds: place and scale it); a material is "
            "applied to `apply_to` objects; a world becomes the scene world; a node group is added to "
            "the file. Remote assets download first, once."),
        "parameters": {"type": "object", "properties": {
            "ref": {"type": "string", "description": "The ref from search_assets"},
            "apply_to": {"type": "array", "items": {"type": "string"},
                         "description": "Materials only: objects to give the material"},
        }, "required": ["ref"]},
    }},
]


@dataclass
class Library:
    name: str
    path: Path
    remote_url: str = ""
    import_method: str = "APPEND"

    @property
    def remote(self) -> bool:
        return bool(self.remote_url)


@dataclass
class Entry:
    library: str
    file: str            # Relative to the library folder, posix.
    id_type: str
    name: str
    tags: list = field(default_factory=list)
    description: str = ""
    author: str = ""
    license: str = ""
    thumbnail_url: str = ""
    files: list = field(default_factory=list)   # Remote only: [{path, url, hash, size}] to fetch.
    min_version: str = ""

    @property
    def ref(self) -> str:
        return REF_SEP.join((self.library, self.file, self.id_type, self.name))

    def text(self) -> str:
        return " ".join([self.name, Path(self.file).stem, *self.tags, self.description])


# ------------------------------------------------------------------ pure helpers (unit tested)

def parse_ref(ref: str) -> tuple[str, str, str, str]:
    parts = str(ref).split(REF_SEP)
    if len(parts) != 4 or not all(parts) or parts[2] not in ID_TYPES:
        raise ToolError("ref is the string search_assets gave: library::file::TYPE::name")
    library, file, id_type, name = parts
    if Path(file).is_absolute() or ".." in Path(file).parts:
        raise ToolError("ref names a file outside its library")
    return library, file, id_type, name


def entries_from_page(library_name: str, remote_url: str, page: dict) -> list[Entry]:
    """Entries of one listing page (Blender's remote asset library schema v1)."""
    files = {f.get("path"): f for f in page.get("files", []) if isinstance(f, dict)}
    entries = []
    for asset in page.get("assets", []):
        id_type = str(asset.get("id_type", ""))
        paths = [p for p in asset.get("files", []) if isinstance(p, str)]
        if id_type not in ID_TYPES or not paths or not paths[0].lower().endswith(".blend"):
            continue
        meta = asset.get("meta") or {}
        thumb = (asset.get("thumbnail") or {}).get("url", "")
        entries.append(Entry(
            library=library_name, file=paths[0], id_type=id_type, name=str(asset.get("name", "")),
            tags=[str(t) for t in meta.get("tags") or []], description=str(meta.get("description") or ""),
            author=str(meta.get("author") or ""), license=str(meta.get("license") or ""),
            thumbnail_url=urllib.parse.urljoin(remote_url, thumb) if thumb else "",
            files=[{"path": p, "url": (files.get(p) or {}).get("url") or urllib.parse.urljoin(remote_url, p),
                    "hash": (files.get(p) or {}).get("hash"), "size": (files.get(p) or {}).get("size_in_bytes", 0)}
                   for p in paths],
            min_version=str((asset.get("bl_versions") or {}).get("min") or "")))
    return entries


def index_metadata(index: dict) -> dict[tuple[str, str], dict]:
    """{(TYPE, name): {tags, description, author, license}} from one of Blender's index files,
    whose names carry the two-letter ID code in front like ID.name."""
    found = {}
    for entry in index.get("entries", []):
        raw = str(entry.get("name", ""))
        id_type = ID_CODES.get(raw[:2])
        if id_type:
            found[(id_type, raw[2:])] = {"tags": [str(t) for t in entry.get("tags") or []],
                                         "description": str(entry.get("description") or ""),
                                         "author": str(entry.get("author") or ""),
                                         "license": str(entry.get("license") or "")}
    return found


def describe_entry(index: int, entry: Entry) -> str:
    parts = [f"{index}. {entry.ref}"]
    if entry.description:
        parts.append(entry.description[:80])
    if entry.tags:
        parts.append(", ".join(entry.tags[:5]))
    if entry.license:
        parts.append(entry.license)
    return "; ".join(parts)


def remote_cache_dirname(url: str) -> str:
    """Blender's folder for a remote library: the first 16 hex digits of the URL's md5."""
    return hashlib.md5(url.encode()).hexdigest()[:16]


def describe(name: str, arguments: dict):
    if name == "search_assets":
        return f"Search asset libraries: {arguments.get('query', '')}", ""
    if name == "import_asset":
        return f"Import asset {str(arguments.get('ref', '?')).split(REF_SEP)[-1]}", ""
    return None


# ------------------------------------------------------------------ libraries

def libraries() -> list[Library]:
    prefs = bpy.context.preferences.filepaths
    found = []
    for lib in prefs.asset_libraries:
        remote = str(getattr(lib, "remote_url", "") or "")
        path = Path(bpy.path.abspath(lib.path)) if lib.path else None
        if remote and path is None:
            path = Path(bpy.app.cachedir) / "remote-assets" / remote_cache_dirname(remote)
        if path is None:
            continue
        found.append(Library(lib.name, path, remote, str(lib.import_method)))
    # The switch lives on its own preferences struct (Preferences > Extensions/Asset Libraries), not on file paths.
    if getattr(getattr(bpy.context.preferences, "asset_libraries", None), "use_online_essentials", False):
        found.append(Library(ESSENTIALS_NAME, Path(bpy.app.cachedir) / "remote-assets" / "online-essentials",
                             ESSENTIALS_URL, "APPEND"))
    return found


_remote_lists: dict[str, tuple[float, list]] = {}
_local_scans: dict[tuple[str, float], list] = {}


def _listing_json(library: Library, relative: str):
    """A listing file: Blender's local copy when it has one, else the network."""
    local = library.path / relative
    if local.is_file():
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return get_json(urllib.parse.urljoin(library.remote_url, relative))


def remote_entries(library: Library) -> list[Entry]:
    fetched, cached = _remote_lists.get(library.remote_url, (0.0, None))
    if cached is not None and time.time() - fetched < LIST_TTL:
        return cached
    meta = _listing_json(library, META_FILE)
    versions = meta.get("api_versions") or {}
    if "v1" not in versions:
        raise ToolError(f"{library.name} speaks no listing version this Loopcut knows")
    index = _listing_json(library, versions["v1"]["url"])
    entries = []
    for page in index.get("pages", []):
        entries += entries_from_page(library.name, library.remote_url, _listing_json(library, page["url"]))
    _remote_lists[library.remote_url] = (time.time(), entries)
    return entries


def _index_files(blend: Path) -> list[Path]:
    root = Path(bpy.app.cachedir) / "asset-library-indices"
    return list(root.glob(f"*/*_{blend.name}.index.json")) if root.is_dir() else []


def local_entries(library: Library) -> list[Entry]:
    if not library.path.is_dir():
        return []
    entries = []
    for blend in sorted(library.path.rglob("*.blend")):
        if any(part.startswith((".", "_")) for part in blend.relative_to(library.path).parts):
            continue
        key = (str(blend), blend.stat().st_mtime)
        if key not in _local_scans:
            _local_scans[key] = _scan_blend(library, blend)
        entries += _local_scans[key]
    return entries


def _scan_blend(library: Library, blend: Path) -> list[Entry]:
    relative = blend.relative_to(library.path).as_posix()
    try:
        with bpy.data.libraries.load(str(blend), assets_only=True) as (data_from, _):
            names = {id_type: list(getattr(data_from, attr, [])) for id_type, attr in ID_TYPES.items()}
    except OSError as ex:
        print(f"Loopcut: could not list assets in {blend}: {ex}")
        return []
    extra = {}
    for index_file in _index_files(blend):
        try:
            extra.update(index_metadata(json.loads(index_file.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            pass
    entries = []
    for id_type, found in names.items():
        for name in found:
            entries.append(Entry(library=library.name, file=relative, id_type=id_type, name=name,
                                 **extra.get((id_type, name), {})))
    return entries


def all_entries() -> tuple[list[Entry], list[str]]:
    """Every asset the agent may use, and notes about libraries that could not be read."""
    entries, notes = [], []
    for library in libraries():
        try:
            entries += remote_entries(library) if library.remote else local_entries(library)
        except ToolError as ex:
            notes.append(f"{library.name}: {ex}")
    return entries, notes


# ------------------------------------------------------------------ tools

def search_assets(query: str, type: str = "", limit: int = DEFAULT_LIMIT) -> ToolResult:
    if type and type not in SEARCH_TYPES:
        raise ToolError(f"type must be one of {', '.join(SEARCH_TYPES)}")
    if not str(query).strip():
        raise ToolError("Give a few words to search for.")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    registered = libraries()
    if not registered:
        raise ToolError("No asset libraries are registered in Preferences > File Paths > Asset Libraries, "
                        "and Online Essentials is off. Ask the user to add one, or use search_polyhaven.")
    entries, notes = all_entries()
    pool = [e for e in entries if e.id_type in SEARCH_TYPES and (not type or e.id_type == type)]
    found = rank(query, pool, text_of=Entry.text, name_of=lambda e: e.name, limit=limit)
    if not found:
        where = ", ".join(f"{lib.name} ({'remote' if lib.remote else 'on disk'})" for lib in registered)
        raise ToolError(f"No asset matches {query!r} in {where}." + (" " + " ".join(notes) if notes else ""))
    lines = [describe_entry(i, entry) for i, entry in enumerate(found, 1)]
    image = sheet([_thumbnail(entry) for entry in found], "assets_sheet.png")
    text = (f"{len(found)} asset(s) for {query!r}, numbered as on the sheet (left to right, top to bottom); "
            f"pass the ref to import_asset:\n" + "\n".join(lines))
    if image:
        text += "\n\nImage attached: thumbnails in that order (blank: the library has no preview)."
    if notes:
        text += "\n" + " ".join(notes)
    return ToolResult(_clip(text), image_path=image)


def _thumbnail(entry: Entry) -> Path | None:
    if not entry.thumbnail_url:
        return None
    suffix = Path(urllib.parse.urlparse(entry.thumbnail_url).path).suffix or ".png"
    dest = asset_common.cache_root("assets") / "thumbs" / (hashlib.md5(entry.thumbnail_url.encode()).hexdigest() + suffix)
    return asset_common.thumbnail(entry.thumbnail_url, dest)


def _find(ref: str) -> tuple[Library, Entry]:
    library_name, file, id_type, name = parse_ref(ref)
    library = next((lib for lib in libraries() if lib.name == library_name), None)
    if library is None:
        raise ToolError(f"No asset library named {library_name!r} is registered; search again.")
    entries = remote_entries(library) if library.remote else local_entries(library)
    entry = next((e for e in entries if e.file == file and e.id_type == id_type and e.name == name), None)
    if entry is None:
        raise ToolError(f"{library_name} has no {id_type} {name!r} in {file}; search again.")
    return library, entry


def _ensure_files(library: Library, entry: Entry) -> Path:
    """The asset's .blend on disk, downloading a remote asset's files into Blender's cache first."""
    if not library.remote:
        return library.path / entry.file
    for item in entry.files:
        target = library.path / item["path"]
        if Path(item["path"]).is_absolute() or ".." in Path(item["path"]).parts:
            raise ToolError(f"{library.name} lists a file path outside its folder: {item['path']}")
        download(item["url"], target, item.get("hash"), item.get("size") or 0)
    return library.path / entry.file


def _pack_images(materials) -> None:
    for material in materials:
        if material is None or not material.use_nodes:
            continue
        for node in material.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image and not node.image.packed_file and node.image.filepath:
                try:
                    node.image.pack()
                except RuntimeError:
                    pass


def import_asset(ref: str, apply_to: list[str] | None = None) -> ToolResult:
    library, entry = _find(ref)
    apply_to = apply_to or []
    missing = [n for n in apply_to if n not in bpy.data.objects]
    if missing:
        raise ToolError(f"No such object(s) to apply to: {', '.join(missing)}")
    if entry.min_version:
        try:
            wanted = tuple(int(v) for v in entry.min_version.split(".")[:2])
            if wanted > bpy.app.version[:2]:
                raise ToolError(f"{entry.name} needs Blender {entry.min_version}; this is {bpy.app.version_string}")
        except ValueError:
            pass
    blend = _ensure_files(library, entry)
    if not blend.is_file():
        raise ToolError(f"{blend.name} is missing from {library.name}")
    attr = ID_TYPES[entry.id_type]
    link = library.import_method == "LINK"
    before = {obj.name for obj in bpy.data.objects}
    with bpy.data.libraries.load(str(blend), link=link) as (data_from, data_to):
        if entry.name not in getattr(data_from, attr):
            raise ToolError(f"{blend.name} no longer holds {entry.id_type} {entry.name!r}")
        setattr(data_to, attr, [entry.name])
    block = getattr(data_to, attr)[0]
    if block is None:
        raise ToolError(f"Blender could not load {entry.name!r} from {blend.name}")
    credit = f"{entry.license or 'license unstated'}" + (f", by {entry.author}" if entry.author else "")
    scene = bpy.context.scene
    if entry.id_type == "OBJECT":
        scene.collection.objects.link(block)
        for child in block.children_recursive:
            if child.name not in scene.objects:
                scene.collection.objects.link(child)
    elif entry.id_type == "COLLECTION":
        scene.collection.children.link(block)
    elif entry.id_type == "WORLD":
        previous = scene.world.name if scene.world else None
        scene.world = block
        return ToolResult(f"World {block.name!r} from {library.name} is now the scene world"
                          + (f"; the previous world {previous!r} is kept, unused" if previous else "") + f". {credit}.")
    elif entry.id_type == "MATERIAL":
        applied = []
        for name in apply_to:
            obj = bpy.data.objects[name]
            if not hasattr(obj.data, "materials"):
                continue
            if obj.data.materials:
                obj.data.materials[obj.active_material_index] = block
            else:
                obj.data.materials.append(block)
            applied.append(name)
        if not link:
            _pack_images([block])
        where = f", applied to {', '.join(applied)}" if applied else ", not applied to anything yet"
        return ToolResult(f"Material {block.name!r} from {library.name}{where}. {credit}.")
    else:
        return ToolResult(f"{entry.id_type.title()} {block.name!r} from {library.name} is in the file"
                          f" (bpy.data.{attr}[{block.name!r}]). {credit}.")
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    if not link:
        _pack_images({slot.material for obj in imported for slot in getattr(obj, "material_slots", [])})
    rows = [object_summary(obj) for obj in imported]
    return ToolResult(_clip(f"Imported {entry.id_type.lower()} {block.name!r} from {library.name}: "
                            f"{len(imported)} object(s) at the origin. Place and scale it now. {credit}.\n"
                            + json.dumps(rows, separators=(",", ":"), default=str)))


DISPATCH = {"search_assets": search_assets, "import_asset": import_asset}
