"""Asset search and import, with an optional source for search.

One schema pair instead of one per library, because every schema is paid for on every step. Each
explicit-source call goes to its library's module. Omitted source merges the user's libraries
and the managed index locally; indexed providers retain their own source and native ID.

A worker imports in two halves (07_m4_design §2.7): fetch_asset in the user's Blender, where the
network, the keys and the asset caches are, then load_fetched in the worker's child Blender, which
only reads the files it is handed. Every library's import is its fetch followed by its load.

Every search result carries a pick number (asset_picks), and import_asset takes it as `pick` (or
"#9" as `id`) in place of source and id: hosted Fast copied 36-character ids wrongly and built
nothing (2026-10-23). source and id still work as before; an id the library does not know but
that nearly matches one recent result is read as that result, and the result says so.
"""

import inspect
import re

from . import (asset_libraries, asset_picks, asset_search, blenderkit, indexed_assets, polyhaven,
               polypizza, tools)
from .tools import ToolError

# source: (module, its search function, its import function, the name that function gives `id`)
SOURCES = {
    "blenderkit": (blenderkit, "search_blenderkit", "import_blenderkit", "asset_id"),
    "polyhaven": (polyhaven, "search_polyhaven", "import_polyhaven", "asset_id"),
    "library": (asset_libraries, "search_assets", "import_asset", "ref"),
    "polypizza": (polypizza, "search_polypizza", "import_polypizza", "model_id"),
}
IMPORT_SOURCES = {**SOURCES, **{source: (indexed_assets, None, "import_indexed", "asset_id")
                              for source in ("ambientcg", "physicallybased")}}
# source: its import split in two, (fetch in the parent, load in the child).
SPLIT = {
    "blenderkit": ("fetch_blenderkit", "load_blenderkit"),
    "polyhaven": ("fetch_polyhaven", "load_polyhaven"),
    "library": ("fetch_asset", "load_asset"),
    "polypizza": ("fetch_polypizza", "load_polypizza"),
    "ambientcg": ("fetch_indexed", "load_indexed"),
    "physicallybased": ("fetch_indexed", "load_indexed"),
}
# The kinds search_assets names, in each library's own words; those words are accepted as well.
KINDS = {"polyhaven": {"model": "models", "material": "textures", "hdri": "hdris"},
         "blenderkit": {"model": "model", "material": "material", "hdri": "hdr"}}

SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "search_assets",
        "title": "Searching for assets",
        "description": (
            "Omit source to search the user's libraries and Loopcut's index together. "
            "Returns numbered matches (import_asset's pick) and a thumbnail sheet. Sources: blenderkit, "
            "the largest: models (furniture, props, vehicles, plants, characters), PBR materials and HDRIs, "
            "realistic and stylised; tens of thousands free with no account, a Full-plan key in preferences "
            "unlocks the rest. polyhaven: CC0 scanned models, PBR textures and HDRI skies, for realism. library: the "
            "user's Blender asset libraries: their own kits, Online Essentials (base meshes, materials, "
            "HDRI worlds), remote ones such as ambientCG (PBR materials). polypizza: light low-poly "
            "stylised GLB models (game props, characters, vehicles, plants, food), CC0 or CC-BY, for a "
            "stylised or game look or many props cheaply; needs the user's free key."),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "enum": list(SOURCES)},
            "query": {"type": "string", "description": "Words to match: 'low poly tree', 'oak floor', 'sunset'"},
            "type": {"type": "string", "enum": ["model", "material", "hdri", *asset_libraries.SEARCH_TYPES],
                     "description": "blenderkit, polyhaven (required): model, material or hdri. "
                                    "library (optional): a Blender ID type"},
            "limit": {"type": "integer", "description": f"Default {polyhaven.DEFAULT_LIMIT}, max {polyhaven.MAX_LIMIT}"},
            "category": {"type": "string", "enum": list(polypizza.CATEGORIES), "description": "polypizza only"},
            "licence": {"type": "string", "enum": list(polypizza.LICENCES),
                        "description": "polypizza only. CC0 needs no credit; default both"},
            "animated": {"type": "boolean", "description": "polypizza only: only animated models"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "import_asset",
        "title": "Adding an asset",
        "description": (
            "Import by pick number from search results. A model or collection arrives at the origin in "
            "meters (the result gives its bounds: place and scale it); a material is applied to `apply_to` "
            "objects; an HDRI or world becomes the scene world; a node group is added to the file. Files are "
            "cached. polypizza scale is arbitrary: pass target_size or scale it after; its result names Poly "
            "Pizza with a link and any attribution due: repeat them to the user."),
        "parameters": {"type": "object", "properties": {
            "pick": {"type": "integer", "description": "9 for #9"},
            "source": {"type": "string", "enum": list(IMPORT_SOURCES)},
            "id": {"type": "string", "description": "Without pick: its id (library: the ref)"},
            "resolution": {"type": "string", "enum": list(blenderkit.RESOLUTIONS),
                           "description": "blenderkit, polyhaven, ambientcg (1k-4k). Default 1k: previews, 2k final renders"},
            "apply_to": {"type": "array", "items": {"type": "string"},
                         "description": "Materials only: objects to give the material"},
            "target_size": {"type": "number", "description": "polypizza: largest dimension in meters after import"},
        }},
    }},
]


def _source(source: str) -> tuple:
    if not isinstance(source, str) or source not in IMPORT_SOURCES:
        raise ToolError(f"source must be one of {', '.join(IMPORT_SOURCES)}")
    return IMPORT_SOURCES[source]


def _native(source: str, arguments: dict) -> dict:
    kinds, kind = KINDS.get(source, {}), arguments.get("type")
    if isinstance(kind, str) and kind in kinds:
        return {**arguments, "type": kinds[kind]}
    return arguments


def search_assets(source: str | None = None, **arguments) -> tools.ToolResult:
    if source is None:
        return tools.forward(asset_search.search_assets, arguments, "combined asset search")
    if not isinstance(source, str) or source not in SOURCES:
        raise ToolError(f"search source must be one of {', '.join(SOURCES)}, or omitted")
    module, search, _, _ = _source(source)
    return tools.forward(getattr(module, search), _native(source, arguments), f"source {source}")


def _then(text: str, more: str) -> str:
    text = text.rstrip()
    return f"{text}{'' if text.endswith(('.', '!', '?')) else '.'} {more}"


# What a library or this module says of an id it has no such asset for, or that cannot be one, and
# nothing else: not a resolution it lacks, a file it cannot serve (NotFound) or the network.
_UNKNOWN = re.compile(
    r"(?:BlenderKit|Poly Haven) has no asset ['\"]|Poly Pizza has no model ['\"]"  # The three APIs' fetches.
    r"|id is the (?:id|ref) search_assets gave"                                    # Not an id at all.
    r"|No asset library named |.+ has no (?:" + "|".join(asset_libraries.ID_TYPES)  # asset_libraries._find
    + r") ['\"].*; search again\.$"
    r"|Unknown \w+ asset id; "                                                      # indexed_assets.fetch_indexed
    r"|source must be one of ")                                                     # _source


def _unknown_id(ex: Exception) -> bool:
    return bool(_UNKNOWN.match(str(ex)))


def _picked(source, asset_id, pick):
    """(source, id, how it was read, the pick's row or None) for a pick number, given as pick or as id
    ("#9", "9"); for an id without a known source, the one result shown with it; else as given. A pick
    and an id that name two different results are refused."""
    if pick is not None and pick != "":
        number = asset_picks.parse(pick)
        if number is None:
            raise ToolError("pick is a result's number from search_assets: 9 for #9")
    elif asset_id is None or asset_id == "":
        raise ToolError("Give pick, a result's number from search_assets (9 for #9), or source and id.")
    else:
        number = asset_picks.parse(asset_id)
    if number is None:
        known = isinstance(source, str) and source in IMPORT_SOURCES
        row = asset_picks.with_id(asset_id) if not known and isinstance(asset_id, str) else None
        return (row["source"], row["id"], "", None) if row else (source, asset_id, "", None)
    row = asset_picks.find(number)
    if row is None:
        raise ToolError(f"No result #{number}; search again.")
    if pick is not None and pick != "" and isinstance(asset_id, str) and asset_id and asset_id != row["id"]:
        other = asset_picks.with_id(asset_id)
        if other and other["n"] != row["n"]:
            raise ToolError(f"pick #{number} is {row['name']} but id {asset_id} is #{other['n']} {other['name']}: "
                            "give one of them.")
    other = source and source != row["source"]
    note = f"#{number} is a {row['source']} result, not {source}: imported it." if other else ""
    return row["source"], row["id"], note, row


def _meant(source, asset_id, pick, call):
    """call(source, id) for the asset the model means, and a line saying how its words were read when
    that was not as written. An id the library has no asset for (_unknown_id), shown by a search under
    another source or a near miss of one recent result (asset_picks.near: a mangled UUID, or a case or
    spacing slip), is retried as that result; nearly matching several, the error names them by number."""
    source, asset_id, note, row = _picked(source, asset_id, pick)
    if row is not None:
        try:
            return call(source, asset_id), note
        except ToolError as ex:
            if str(ex).startswith(f"Unknown {source} asset id"):  # The index's rows live in memory only.
                raise ToolError(f"#{row['n']} {row['name']} came from a search before Blender restarted; "
                                "search again to import it.") from ex
            raise
    try:
        return call(source, asset_id), ""
    except ToolError as ex:
        if not isinstance(asset_id, str) or not asset_id.strip() or not _unknown_id(ex):
            raise
        shown = asset_picks.with_id(asset_id)
        if shown and shown["source"] == source:
            raise  # The id is right; what failed was not the id.
        known = isinstance(source, str) and source in IMPORT_SOURCES
        candidates = [shown] if shown else asset_picks.near(asset_id, source if known else "")
        if len(candidates) == 1:
            row = candidates[0]
            note = f"Read id {asset_id!r} as #{row['n']} {row['name']} ({row['source']} id {row['id']})."
            try:
                return call(row["source"], row["id"]), note
            except ToolError as again:
                raise ToolError(f"{note} {again}") from again
        if candidates:
            closest = ", ".join(f"#{row['n']} {row['name']}" for row in candidates[:3])
            raise ToolError(_then(str(ex), f"Closest results: {closest}. {asset_picks.HINT}")) from ex
        if asset_picks.any_rows():
            raise ToolError(_then(str(ex), asset_picks.HINT)) from ex
        raise


def import_asset(source: str = "", id: str = "", pick=None, **arguments) -> tools.ToolResult:  # noqa: A002
    def call(source, asset_id):
        module, _, import_, id_name = _source(source)
        extra = {"source": source} if module is indexed_assets else {}
        return tools.forward(getattr(module, import_), {**arguments, **extra, id_name: asset_id}, f"source {source}")
    result, note = _meant(source, id, pick, call)
    if note:
        result.text = f"{note}\n{result.text}"
    return result


def fetch_asset(source: str = "", id: str = "", pick=None, **arguments) -> dict:  # noqa: A002 (the schema's word)
    """import_asset's first half, for a worker: downloads into this Blender's caches and returns what
    load_fetched needs, JSON-able (paths and credits, never a key). Takes import_asset's arguments
    and refuses what that source would refuse. Main thread: some libraries read the live scene or
    the preferences."""
    def call(source, asset_id):
        module, _, _, id_name = _source(source)
        extra = {"source": source} if module is indexed_assets else {}
        fetched = tools.forward(getattr(module, SPLIT[source][0]), {**arguments, **extra, id_name: asset_id},
                                f"source {source}")
        return {**fetched, "source": source}
    fetched, note = _meant(source, id, pick, call)
    return {**fetched, "note": note} if note else fetched


def load_fetched(fetched: dict, **arguments) -> tools.ToolResult:
    """import_asset's second half, in the worker's child: what fetch_asset downloaded, into this
    scene. Of import_asset's other arguments it uses those the source's load takes (apply_to,
    target_size); source and id came with `fetched`, and so did how a pick or a mistyped id was read."""
    source = fetched.get("source") if isinstance(fetched, dict) else None
    module = _source(source)[0]
    load = getattr(module, SPLIT[source][1])
    accepted = inspect.signature(load).parameters
    result = load(fetched, **{key: value for key, value in arguments.items()
                              if key in accepted and key != "fetched"})
    if fetched.get("note"):
        result.text = f"{fetched['note']}\n{result.text}"
    return result


DISPATCH = {"search_assets": search_assets, "import_asset": import_asset}


def describe(name: str, arguments: dict):
    """The step's label in the chat: the library's own, like job_tools.describe. A pick is labelled
    with its number and name."""
    source = arguments.get("source")
    number = asset_picks.parse(arguments.get("pick")) if name == "import_asset" else None
    if name == "import_asset" and number is None and isinstance(arguments.get("id"), (str, int)):
        number = asset_picks.parse(arguments.get("id"))
    if number is not None:
        row = asset_picks.find(number)
        if row is None:
            return f"Import asset #{number}", ""
        source = row["source"]
        arguments = {**arguments, "source": source, "id": f"#{number} {row['name']}"}
    if name == "search_assets" and source is None:
        return f"Search assets: {arguments.get('query', '')}", ""
    if name == "search_assets" and (not isinstance(source, str) or source not in SOURCES):
        return None
    if name not in DISPATCH or not isinstance(source, str) or source not in IMPORT_SOURCES:
        return None
    module, search, import_, id_name = _source(source)
    rest = {k: v for k, v in arguments.items() if k not in ("source", "id")}
    if module is indexed_assets:
        return module.describe(import_, {**rest, "source": source, id_name: arguments.get("id", "?")})
    if name == "search_assets":
        return module.describe(search, _native(source, rest))
    return module.describe(import_, {**rest, id_name: arguments.get("id", "?")})
