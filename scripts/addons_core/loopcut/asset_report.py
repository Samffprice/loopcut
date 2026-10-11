"""The untrusted asset-search report boundary (contract 0.5 / proposals 007–008).

Validate what the client displays, merges or imports. Bad rows are isolated; a bad envelope
is a local-only search. No schema dependency or copy of the server's schema in every prompt.
"""

import math
import re
from urllib.parse import urlsplit

from .asset_libraries import SEARCH_TYPES, remote_key, safe_relative_path
from .tools import ToolError

SCHEMA = "loopcut.asset_search/1"
MAX_BYTES = 60_000
PROVIDERS = ("polyhaven", "ambientcg", "physicallybased")
KINDS = ("model", "material", "hdri", "texture")
LICENSES = ("CC0-1.0", "CC-BY-4.0")
_ID = re.compile(r"[a-z][a-z0-9_]{1,31}:[A-Za-z0-9._-]{1,128}\Z")
_HASH = re.compile(r"SHA256:[0-9a-f]{64}\Z")


def require(value, message="Invalid asset metadata"):
    if not value:
        raise ValueError(message)


def text(value, maximum=1024, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def https(value):
    if not text(value, 8192) or any(c.isspace() for c in value):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.scheme == "https" and parts.hostname and not parts.username
                    and not parts.password and "\\" not in value and parts.port != 0)
    except ValueError:
        return False


def valid_key(value):
    if not text(value) or len(value) > 1024:
        return False
    if _ID.fullmatch(value):
        return True
    if not value.startswith("remote:"):
        return False
    parts = value[7:].split("|")
    return len(parts) == 4 and remote_key(*parts) == value


def requested_kinds(kind):
    """Validate a KindFilter and resolve its members in the contract's canonical order."""
    if isinstance(kind, str):
        require(kind in (*KINDS, "any"))
        return KINDS if kind == "any" else (kind,)
    require(isinstance(kind, list) and kind and all(k in KINDS for k in kind))
    require(len(set(kind)) == len(kind))
    return tuple(k for k in KINDS if k in kind)


def validate_row(row, filters):
    require(isinstance(row, dict))
    source, source_id = row["source"], row["source_id"]
    require(source in PROVIDERS and source in filters["sources"])
    require(text(source_id, 128) and _ID.fullmatch(row["id"]))
    require(row["id"] == f"{source}:{source_id}" and row["import"] == {"source": source, "id": source_id})
    keys = row["dedupe_keys"]
    require(isinstance(keys, list) and 1 <= len(keys) <= 9 and all(valid_key(k) for k in keys))
    require(keys[0] == row["id"] and len(set(keys)) == len(keys))
    require(row["kind"] in requested_kinds(filters["kind"]) and text(row["title"], 200))
    require(type(row["score"]) in (int, float) and math.isfinite(row["score"]) and row["score"] >= 0)
    require(https(row["page_url"]) and https(row["preview_url"]))
    require(re.search(r"\.(jpg|jpeg|png|webp)$", urlsplit(row["preview_url"]).path, re.I))
    lic, attr = row["license"], row["attribution"]
    require(lic["id"] in LICENSES and text(lic["name"]) and https(lic["url"]))
    require(filters["license"] != "cc0" or lic["id"] == "CC0-1.0")
    require(type(attr["required"]) is bool and text(attr["text"], 500))
    require(isinstance(attr["authors"], list) and all(text(a) for a in attr["authors"]))
    require(lic["id"] != "CC-BY-4.0" or attr["required"])
    for key in ("polycount", "max_resolution"):
        value = row.get(key)
        require(value is None or type(value) is int and value > 0)
    if filters["max_polycount"] is not None and row["kind"] == "model":
        require(row.get("polycount") is not None and row["polycount"] <= filters["max_polycount"])
    if filters["min_resolution"] is not None:
        require(row.get("max_resolution") is not None and row["max_resolution"] >= filters["min_resolution"])
    dims = row.get("dimensions_m")
    require(dims is None or isinstance(dims, list) and 2 <= len(dims) <= 3 and all(
        type(d) in (int, float) and math.isfinite(d) and d > 0 for d in dims))
    downloads = row.get("downloads", [])
    require(isinstance(downloads, list) and len(downloads) <= 8)
    for item in downloads:
        require(text(item["label"], 64) and https(item["url"]))
        require(item["format"] in {"zip", "blend", "gltf", "glb", "fbx", "obj", "usd", "exr", "hdr", "jpg", "png"})
        require(item["size"] is None or type(item["size"]) is int and item["size"] >= 0)
    remote = row.get("remote_library")
    if remote is not None:
        root = remote["url"]
        require(https(root) and root.endswith("/") and not urlsplit(root).query and not urlsplit(root).fragment)
        require(text(remote["name"]) and isinstance(remote["entries"], list) and 1 <= len(remote["entries"]) <= 4)
        for entry in remote["entries"]:
            require(entry["id_type"] in SEARCH_TYPES and safe_relative_path(entry["file"]))
            require(remote_key(root, entry["file"], entry["id_type"], entry["name"]) in keys)
            require(entry["file"].lower().endswith(".blend"))
            require(entry["thumbnail_url"] is None or https(entry["thumbnail_url"]))
            require(entry["min_blender"] is None or isinstance(entry["min_blender"], str)
                    and re.fullmatch(r"\d+\.\d+(?:\.\d+)?", entry["min_blender"]))
            require(entry["import_method"] in ("APPEND", "LINK", "APPEND_REUSE", None))
            files = entry["files"]
            require(isinstance(files, list) and 1 <= len(files) <= 8)
            require(entry["file"] in [f["path"] for f in files])
            for item in files:
                require(safe_relative_path(item["path"]) and https(item["url"]))
                require(isinstance(item["hash"], str) and _HASH.fullmatch(item["hash"]))
                require(type(item["size"]) is int and item["size"] >= 0)
    # The 0.5 byte budget protects top-ten downloads, but may still trim them if those
    # rows alone exceed the budget. Import can retrieve a missing variant with one result.
    return row


def validate_report(report, inputs):
    try:
        require(isinstance(report, dict) and report["schema"] == SCHEMA)
        require(report["query"] == inputs["query"] and report["mode"] in ("hybrid", "keyword"))
        require(isinstance(report["generated_at"], str))
        filters = report["filters"]
        kind = inputs.get("kind", "model")
        kinds = requested_kinds(kind)
        expected = {"kind": list(kinds) if isinstance(kind, list) else kind, "license": inputs.get("license", "cc0"),
                    "sources": inputs.get("sources", list(PROVIDERS)), "limit": inputs.get("limit", 10),
                    "max_polycount": inputs.get("max_polycount") if "model" in kinds else None,
                    "min_resolution": inputs.get("min_resolution")}
        require(filters == expected)
        require(isinstance(report["results"], list) and len(report["results"]) <= 50)
        require(isinstance(report["sources"], list) and len(report["sources"]) <= len(PROVIDERS))
        require(isinstance(report["notices"], list) and len(report["notices"]) <= 10
                and all(text(n, 500, empty=True) for n in report["notices"]))
    except (KeyError, TypeError, ValueError) as ex:
        raise ToolError("invalid asset-search report") from ex
    rows, sources, notes, dropped = [], [], list(report["notices"]), 0
    for row in report["results"][:filters["limit"]]:
        try:
            rows.append(validate_row(row, filters))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            dropped += 1
    dropped += max(0, len(report["results"]) - filters["limit"])
    if dropped:
        notes.append(f"Dropped {dropped} invalid index result(s).")
    for source in report["sources"]:
        try:
            require(source["id"] in filters["sources"] and text(source["name"]) and https(source["url"]))
            require(text(source["credit"], 300) and type(source["credit_required"]) is bool)
            require(source["license_ids"] and all(v in LICENSES for v in source["license_ids"]))
            require(source.get("remote_library_url") is None or https(source["remote_library_url"]))
            if source["id"] not in {s["id"] for s in sources}:
                sources.append(source)
        except (KeyError, TypeError, ValueError):
            notes.append("Dropped invalid index source metadata.")
    return rows, sources, list(dict.fromkeys(notes))


def importable_rows(report):
    """The rows of a report that did not come through search_assets: the raw managed tool
    (loopcut_cloud__asset_search) attaches it as text, and the model imports its ids. Each row is
    checked as validate_report checks it, against the report's own resolved filters, since there is
    no search_assets request to compare with; a bad envelope gives none."""
    try:
        require(isinstance(report, dict) and report["schema"] == SCHEMA)
        filters = report["filters"]
        require(isinstance(filters, dict))
        requested_kinds(filters["kind"])
        require(isinstance(report["results"], list))
    except (KeyError, TypeError, ValueError):
        return []
    rows = []
    for row in report["results"][:50]:
        try:
            rows.append(validate_row(row, filters))
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            pass
    return rows
