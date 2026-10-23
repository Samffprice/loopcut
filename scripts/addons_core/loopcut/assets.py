"""Asset search and import, with an optional source for search.

One schema pair instead of one per library, because every schema is paid for on every step. Each
explicit-source call goes to its library's module. Omitted source merges the user's libraries
and the managed index locally; indexed providers retain their own source and native ID.

A worker imports in two halves (07_m4_design §2.7): fetch_asset in the user's Blender, where the
network, the keys and the asset caches are, then load_fetched in the worker's child Blender, which
only reads the files it is handed. Every library's import is its fetch followed by its load.
"""

import inspect

from . import asset_libraries, asset_search, blenderkit, indexed_assets, polyhaven, polypizza, tools
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
            "Returns numbered matches, a thumbnail sheet and import IDs. Sources: blenderkit, the largest: models "
            "(furniture, props, vehicles, plants, characters), PBR materials and HDRIs, realistic and "
            "stylised; tens of thousands free with no account, a Full-plan key in preferences unlocks the "
            "rest. polyhaven: CC0 scanned models, PBR textures and HDRI skies, for realism. library: the "
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
            "Download and import an asset from search_assets, from the same source. A model or collection "
            "arrives at the origin in meters (the result gives its bounds: place and scale it); a material "
            "is applied to `apply_to` objects; an HDRI or world becomes the scene world; a node group is "
            "added to the file. Files are cached. polypizza scale is arbitrary: pass target_size or scale "
            "it after; its result names Poly Pizza with a link and any attribution due: repeat them to the user."),
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "enum": list(IMPORT_SOURCES)},
            "id": {"type": "string", "description": "The id from search_assets (library: the ref)"},
            "resolution": {"type": "string", "enum": list(blenderkit.RESOLUTIONS),
                           "description": "blenderkit, polyhaven, ambientcg (1k-4k). Default 1k: previews, 2k final renders"},
            "apply_to": {"type": "array", "items": {"type": "string"},
                         "description": "Materials only: objects to give the material"},
            "target_size": {"type": "number", "description": "polypizza: largest dimension in meters after import"},
        }, "required": ["source", "id"]},
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


def import_asset(source: str = "", id: str = "", **arguments) -> tools.ToolResult:  # noqa: A002 (the schema's word)
    module, _, import_, id_name = _source(source)
    if module is indexed_assets:
        arguments = {**arguments, "source": source}
    return tools.forward(getattr(module, import_), {**arguments, id_name: id}, f"source {source}")


def fetch_asset(source: str = "", id: str = "", **arguments) -> dict:  # noqa: A002 (the schema's word)
    """import_asset's first half, for a worker: downloads into this Blender's caches and returns what
    load_fetched needs, JSON-able (paths and credits, never a key). Takes import_asset's arguments
    and refuses what that source would refuse. Main thread: some libraries read the live scene or
    the preferences."""
    module, _, _, id_name = _source(source)
    if module is indexed_assets:
        arguments = {**arguments, "source": source}
    fetched = tools.forward(getattr(module, SPLIT[source][0]), {**arguments, id_name: id}, f"source {source}")
    return {**fetched, "source": source}


def load_fetched(fetched: dict, **arguments) -> tools.ToolResult:
    """import_asset's second half, in the worker's child: what fetch_asset downloaded, into this
    scene. Of import_asset's other arguments it uses those the source's load takes (apply_to,
    target_size); source and id came with `fetched`."""
    source = fetched.get("source") if isinstance(fetched, dict) else None
    module = _source(source)[0]
    load = getattr(module, SPLIT[source][1])
    accepted = inspect.signature(load).parameters
    return load(fetched, **{key: value for key, value in arguments.items() if key in accepted and key != "fetched"})


DISPATCH = {"search_assets": search_assets, "import_asset": import_asset}


def describe(name: str, arguments: dict):
    """The step's label in the chat: the library's own, like job_tools.describe."""
    source = arguments.get("source")
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
