"""Poly Pizza (low-poly, stylised models; CC0 and CC-BY) as two tools: search and import.

Where Poly Haven is scanned realism, Poly Pizza is the rescued Google Poly archive plus new
uploads: light single-GLB props for game, stylised and social work. It needs a free API key
(poly.pizza > account > API), kept in Loopcut's credentials file like the model key, or given as
LOOPCUT_POLYPIZZA_KEY. About two thirds of the catalogue is CC-BY, which requires crediting the
creator: the attribution string is stored on every imported root object and returned to the model
so the user hears it.

The model files come from static.poly.pizza behind Cloudflare, which challenges datacenter and VPN
addresses with an HTML page instead of the GLB; that case gets its own message.
"""

import json
import os
import urllib.parse
from pathlib import Path

import bpy

from . import asset_common, credentials
from .asset_common import NotFound, download, get_json, sheet, valid_slug
from .tools import ToolError, ToolResult, _clip, object_summary

API = "https://api.poly.pizza/v1.1"
KEY_ID = "https://api.poly.pizza"   # The credentials-file entry the key is filed under.
ENV_KEY = "LOOPCUT_POLYPIZZA_KEY"
SITE = "https://poly.pizza/m/"
DEFAULT_LIMIT = 8
MAX_LIMIT = 12
CATEGORIES = {"Food & Drink": 0, "Clutter": 1, "Weapons": 2, "Transport": 3, "Furniture & Decor": 4,
              "Objects": 5, "Nature": 6, "Animals": 7, "Buildings": 8, "People & Characters": 9,
              "Scenes & Levels": 10, "Other": 11}
LICENCES = {"CC-BY": 0, "CC0": 1}
NO_KEY = ("Poly Pizza needs a free API key: ask the user to paste one from poly.pizza (account > API) into "
          "Loopcut's preferences under Asset libraries. Poly Haven and search_assets need no key.")

SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "search_polypizza",
        "description": (
            "Search Poly Pizza: light low-poly, stylised models (game props, characters, vehicles, "
            "plants, food), each one GLB, CC0 or CC-BY. Use it for a stylised or game look, or many "
            "props cheaply; Poly Haven for realism. Returns numbered matches with a thumbnail sheet; "
            "then import_polypizza(model_id)."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to match: 'low poly tree', 'robot'"},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "licence": {"type": "string", "enum": list(LICENCES),
                        "description": "CC0 needs no credit; default both"},
            "animated": {"type": "boolean", "description": "Only animated models"},
            "limit": {"type": "integer", "description": f"Default {DEFAULT_LIMIT}, max {MAX_LIMIT}"},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "import_polypizza",
        "description": (
            "Download and import a Poly Pizza model by id. It arrives at the origin; scale is "
            "arbitrary in this archive, so pass target_size (meters, largest side) or scale it after. "
            "The result names Poly Pizza with a link and any attribution due: repeat them to the user."),
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string", "description": "The id from search_polypizza"},
            "target_size": {"type": "number", "description": "Largest dimension in meters after import"},
        }, "required": ["model_id"]},
    }},
]


# ------------------------------------------------------------------ pure helpers (unit tested)

def api_key() -> str:
    return os.environ.get(ENV_KEY) or credentials.api_key(KEY_ID)


def search_params(category: str = "", licence: str = "", animated: bool = False, limit: int = DEFAULT_LIMIT) -> dict:
    """Query parameters as the API wants them: Capitalized keys and numeric ids; anything else is
    accepted with HTTP 200 and silently ignored."""
    params = {"Limit": max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))}
    if category:
        if category not in CATEGORIES:
            raise ToolError(f"category must be one of {', '.join(CATEGORIES)}")
        params["Category"] = CATEGORIES[category]
    if licence:
        if licence not in LICENCES:
            raise ToolError(f"licence must be one of {', '.join(LICENCES)}")
        params["License"] = LICENCES[licence]
    if animated:
        params["Animated"] = 1
    return params


def describe_model(index: int, model: dict) -> str:
    creator = model.get("Creator") or {}
    parts = [f"{index}. {model.get('ID')}: {model.get('Title', '')}"]
    if model.get("Tri Count"):
        parts.append(f"{model['Tri Count']} tris")
    parts.append(str(model.get("Licence") or "?"))
    if isinstance(creator, dict) and creator.get("Username"):
        parts.append(f"by {creator['Username']}")
    if model.get("Category"):
        parts.append(str(model["Category"]))
    if model.get("Animated"):
        parts.append("animated")
    return "; ".join(parts)


def credit_line(model: dict) -> str:
    """Poly Pizza's API terms: name Poly Pizza with a link every time, and pass on the attribution."""
    licence = str(model.get("Licence") or "")
    attribution = str(model.get("Attribution") or "")
    source = f"From Poly Pizza ({SITE}{model.get('ID')})"
    if licence.upper().startswith("CC0"):
        return f"{source}, {licence}: no credit required, but tell the user where it came from."
    return f"{source}, {licence or 'licence unstated'}: credit required. Attribution: {attribution or model.get('Title', '')}"


def is_glb(head: bytes) -> bool:
    return head[:4] == b"glTF"


def describe(name: str, arguments: dict):
    if name == "search_polypizza":
        return f"Search Poly Pizza: {arguments.get('query', '')}", ""
    if name == "import_polypizza":
        return f"Import Poly Pizza model {arguments.get('model_id', '?')}", ""
    return None


# ------------------------------------------------------------------ tools

def _headers() -> dict:
    key = api_key()
    if not key:
        raise ToolError(NO_KEY)
    return {"x-auth-token": key}


def cache_root() -> Path:
    return asset_common.cache_root("polypizza")


def search_polypizza(query: str, category: str = "", licence: str = "", animated: bool = False,
                     limit: int = DEFAULT_LIMIT) -> ToolResult:
    if not str(query).strip():
        raise ToolError("Give a few words to search for.")
    headers = _headers()
    params = search_params(category, licence, animated, limit)
    url = f"{API}/search/{urllib.parse.quote(str(query).strip(), safe='')}?{urllib.parse.urlencode(params)}"
    try:
        data = get_json(url, headers)
    except ToolError as ex:
        if "Refused" in str(ex):
            raise ToolError("Poly Pizza refused the API key; ask the user to check it in Loopcut's preferences.") from ex
        raise
    results = [m for m in (data.get("results") or []) if isinstance(m, dict) and m.get("ID")]
    if not results:
        raise ToolError(f"No Poly Pizza models match {query!r}. Try fewer or more general words.")
    lines = [describe_model(i, model) for i, model in enumerate(results, 1)]
    image = sheet([_thumbnail(model) for model in results], "polypizza_sheet.png")
    text = (f"{len(results)} of {data.get('total', len(results))} Poly Pizza models for {query!r}, numbered as on "
            f"the sheet (left to right, top to bottom):\n" + "\n".join(lines))
    if image:
        text += "\n\nImage attached: thumbnails in that order. Import with import_polypizza(model_id)."
    return ToolResult(_clip(text), image_path=image)


def _thumbnail(model: dict) -> Path | None:
    url = str(model.get("Thumbnail") or "")
    if not url or not valid_slug(str(model.get("ID"))):
        return None
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".png"
    return asset_common.thumbnail(url, cache_root() / "thumbs" / f"{model['ID']}{suffix}")


def import_polypizza(model_id: str, target_size: float = 0.0) -> ToolResult:
    if not valid_slug(str(model_id)):
        raise ToolError("model_id is the id from search_polypizza: letters, digits, _ and - only.")
    headers = _headers()
    try:
        model = get_json(f"{API}/model/{urllib.parse.quote(str(model_id), safe='')}", headers)
    except NotFound as ex:
        raise ToolError(f"Poly Pizza has no model {model_id!r}. Search first and use the id it gives.") from ex
    url = str(model.get("Download") or "")
    if not url:
        raise ToolError(f"Poly Pizza lists no download for {model_id!r}")
    path = download(url, cache_root() / f"{model_id}.glb")
    with open(path, "rb") as f:
        head = f.read(4)
    if not is_glb(head):
        path.unlink(missing_ok=True)
        raise ToolError("Poly Pizza's file server answered with a web page instead of the model (its Cloudflare "
                        "bot check blocks datacenter and VPN addresses). Try again from a normal connection, or "
                        "the user can download the GLB at " + SITE + str(model_id) + " and import it.")
    before = {obj.name for obj in bpy.data.objects}
    try:
        bpy.ops.import_scene.gltf(filepath=str(path))
    except RuntimeError as ex:
        raise ToolError(f"Blender could not import the GLB: {ex}") from ex
    imported = [obj for obj in bpy.data.objects if obj.name not in before]
    if not imported:
        raise ToolError(f"{model_id} downloaded but nothing arrived in the scene")
    roots = [obj for obj in imported if obj.parent is None or obj.parent.name in before]
    scaled = ""
    if target_size and target_size > 0:
        bpy.context.view_layer.update()
        corners = [obj.matrix_world @ v.co for obj in imported if obj.type == "MESH" for v in obj.data.vertices]
        if corners:
            largest = max(max(c[i] for c in corners) - min(c[i] for c in corners) for i in range(3))
            if largest > 0:
                factor = float(target_size) / largest
                for root in roots:
                    root.scale = [s * factor for s in root.scale]
                scaled = f" Scaled by {factor:.3g} so the largest side is {float(target_size):g} m."
    attribution = str(model.get("Attribution") or "")
    for root in roots:
        root["polypizza_id"] = str(model_id)
        root["polypizza_licence"] = str(model.get("Licence") or "")
        root["polypizza_attribution"] = attribution
    bpy.context.view_layer.update()
    rows = [object_summary(obj) for obj in roots]
    return ToolResult(_clip(f"Imported {model.get('Title', model_id)!r} ({model_id}) as {len(imported)} object(s) "
                            f"at the origin.{scaled} {credit_line(model)}\n"
                            + json.dumps(rows, separators=(",", ":"), default=str)))


DISPATCH = {"search_polypizza": search_polypizza, "import_polypizza": import_polypizza}
