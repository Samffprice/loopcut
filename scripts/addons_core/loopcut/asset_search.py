"""Local library discovery plus the free managed index, fused by rank on this client.

Only the query and contract filters leave for the server. Library registrations, entries,
rankings and cache paths never enter a cloud request. Search keeps scene reads/sheet rendering
on main; remote listings, managed jobs and thumbnails run in ToolResult.then on the caller.
"""

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import bpy

from . import asset_common, asset_libraries, asset_report, cloud, cloud_http, cloud_jobs, config, indexed_assets
from .tools import ToolError, ToolResult

SEARCH_SECONDS = 30
LOCAL_TYPES = {"model": ("OBJECT", "COLLECTION"), "material": ("MATERIAL",), "hdri": ("WORLD",)}
INDEX_TYPES = {"OBJECT": "model", "COLLECTION": "model", "MATERIAL": "material", "WORLD": "hdri"}


def _local_row(library, entry):
    keys = []
    if library.remote:
        try:
            keys.append(asset_libraries.remote_key(library.remote_url, entry.file, entry.id_type, entry.name))
        except ValueError:
            pass  # Still a usable library entry, just ineligible for cross-source identity.
    return {"entry": entry, "library": library, "server": None, "keys": keys,
            "cached": library.remote and (library.path / entry.file).is_file()}


def _server_row(row, cache_dir):
    cached = False
    if row.get("remote_library"):
        library, entry = indexed_assets.remote_entry(row, cache_dir)
        cached = all((library.path / f["path"]).is_file() for f in entry.files)
    return {"entry": None, "library": None, "server": row, "keys": row["dedupe_keys"], "cached": cached}


def sort_key(row):
    return (-row["score"], row["entry"] is None, not row["cached"], row["order"])


def fuse(local, server):
    """Union shared keys transitively; best rank per list contributes once (RRF k=60).

    Local folder rows have no remote key. Two variants/listings connected through a server
    row count once in each ranking, and keep the highest ranked user entry for import.
    """
    rows = [*local, *server]
    parents = list(range(len(rows)))
    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    owners = {}
    for i, row in enumerate(rows):
        for key in row["keys"]:
            if key in owners:
                parents[root(i)] = root(owners[key])
            owners[key] = i
    groups = {}
    for i, row in enumerate(rows):
        group = groups.setdefault(root(i), {"entry": None, "library": None, "server": None,
            "keys": set(), "cached": False, "ranks": {}, "order": i})
        group["keys"].update(row["keys"])
        group["cached"] |= row["cached"]
        if group["entry"] is None and row["entry"] is not None:
            group.update(entry=row["entry"], library=row["library"])
        if group["server"] is None and row["server"] is not None:
            group["server"] = row["server"]
        source, rank = ("local", i + 1) if i < len(local) else ("server", i - len(local) + 1)
        group["ranks"].setdefault(source, rank)
    for row in groups.values():
        row["score"] = sum(1 / (60 + r) for r in row["ranks"].values())
    return sorted(groups.values(), key=sort_key)


def _route():
    """Main-thread runtime snapshot, using the same availability/policy rules as managed tools."""
    cloud.sync_settings()
    runtime = cloud._runtime
    if runtime is None or not runtime.signed_in:
        return None, "signed out"
    if not runtime.supported:
        return None, "client update required"
    known = next((d for d in runtime.manifest["tools"] if isinstance(d, dict) and d.get("name") == "asset_search"), None)
    if known is None:
        return None, ""  # Older services have no index; the user's libraries still work.
    if known.get("status") == "coming_soon":
        return None, "asset_search is coming soon"
    if not runtime.manifest["flags"].get("services"):
        return None, "services are off"
    if runtime.private_mode() and (known.get("byok") or {}).get("data_class") in ("C", "D"):
        return None, "private mode refuses this route"
    descriptor = next((d for d in runtime.descriptors() if d["name"] == "asset_search"), None)
    if descriptor is None:
        reason = known.get("unavailable_reason") or "unavailable"
        return None, str(reason).replace("_", " ")
    cost = descriptor["cost"]
    if (descriptor["backend"] != "managed" or descriptor["kind"] != "assist"
            or cost["credits_estimate"] != 0 or cost["quote_required"] or descriptor["file_inputs"]
            or descriptor.get("bundle_input") or descriptor["apply"] != "attach_report"):
        return None, "free index search is unavailable"
    # Snapshot credentials on main just as cloud.execute does. No files or account data in input.
    from . import credentials
    source = runtime.client
    key = source.api_key if source.api_key is not None else credentials.api_key(source.base_url + "/v1")
    client = cloud_http.Client(base_url=source.base_url, api_key=key, transport=source.transport,
        timeout=min(source.timeout, SEARCH_SECONDS), client_headers=source.version_headers())
    return SimpleNamespace(runtime=runtime, descriptor=descriptor, client=client,
                           no_generation=runtime.no_generation()), ""


# Signed out (or a sign-in the service no longer accepts): one line, which also says what still works, so the
# model searches those instead of asking the user to sign in.
SIGN_IN_NOTE = ("Loopcut index not searched: it needs a Loopcut sign-in. Sources blenderkit and polyhaven, and "
                "the user's libraries, work without one.")


def _index(inputs, is_cancelled, on_main):
    started = time.monotonic()
    route, reason = on_main(_route)
    if route is None:
        if reason in ("signed out", "sign in"):
            return [], [], [SIGN_IN_NOTE]
        return [], [], ["Loopcut index not searched: " + reason + "."] if reason else []
    cancel = cloud._Cancel(lambda: is_cancelled() or time.monotonic() - started >= SEARCH_SECONDS)
    try:
        # cloud_jobs owns submit/wait/download and verifies both the output size and SHA-256.
        # Contract 0.4 makes this free and file-free: no credit or file consent card is needed.
        job = cloud_jobs.run({**route.descriptor, "latency": "seconds"}, inputs, {},
            project={"id": "local", "no_generation": route.no_generation}, cancel=cancel, client=route.client)
        reports = [o for o in job["outputs"] if o["type"] == "report" and o["mime"] == "application/json"]
        if len(reports) != 1:
            raise ToolError("missing asset-search report")
        with Path(reports[0]["path"]).open("rb") as handle:
            raw = handle.read(asset_report.MAX_BYTES + 1)
        if len(raw) > asset_report.MAX_BYTES:
            raise ToolError("asset-search report exceeds its byte budget")
        return asset_report.validate_report(json.loads(raw), inputs)
    except cloud_http.CloudError as ex:
        # The job runtime retains diagnostic context. Keep the fallback to one short reason.
        reason = "timed out" if time.monotonic() - started >= SEARCH_SECONDS else ex.code.replace("_", " ")
        return [], [], ["Loopcut index not searched: " + reason + "."]
    except (ToolError, OSError, ValueError, TypeError, KeyError, RecursionError) as ex:
        print(f"Loopcut: invalid asset index response: {ex}")
        return [], [], ["Loopcut index not searched: invalid or unreadable report."]


def _about(entry, server):
    """What the libraries say an asset is, for choosing beside its thumbnail: size, polycount, texture
    resolution, description and tags. The index's free text is untrusted: one line each, clipped."""
    server = server or {}
    parts = []
    dims = server.get("dimensions_m")
    size = asset_common.size_text(dims)
    if size:
        parts.append(size + (" tile" if len(dims) == 2 else ""))
    polycount = server.get("polycount")
    if type(polycount) is int and polycount > 0:
        parts.append(f"{polycount} polys")
    resolution = server.get("max_resolution")
    if type(resolution) is int and resolution >= 1024:
        parts.append(f"{resolution // 1024}k maps")
    description = asset_common.clean(entry.description if entry is not None else "", 80) \
        or asset_common.clean(server.get("description"), 90)
    if description:
        parts.append(description)
    listed = server.get("tags")
    tags = [*(entry.tags if entry is not None else []), *(listed if isinstance(listed, list) else [])]
    tags = list(dict.fromkeys(t for t in (asset_common.clean(t, 30) for t in tags[:12]) if t))[:5]
    if tags:
        parts.append("tags: " + ", ".join(tags))
    return parts


def _line(number, row, kit=""):
    entry, server = row["entry"], row["server"]
    if entry is not None:
        label = entry.name
        origin = entry.library + (f" + {server['source']}" if server else "")
        source, asset_id = "library", entry.ref
    else:
        label, origin = server["title"], server["source"]
        source, asset_id = server["import"]["source"], server["import"]["id"]
    kind = server["kind"] if server else INDEX_TYPES.get(entry.id_type, entry.id_type)
    license_name = server["license"]["id"] if server else entry.license or "license unstated"
    about = "".join(f"{part}; " for part in [*_about(entry, server), kit] if part)
    line = (f"{number}. {label} ({origin}); kind: {kind}; {about}"
            f"import_asset(source={json.dumps(source)}, id={json.dumps(asset_id)}); "
            + license_name)
    if server and server["attribution"]["required"]:
        line += "; " + server["attribution"]["text"]
    return line


def _present(query, rows, paths, sources, notes):
    # Local files only: a remote library's file can hold one material's variants, which is no kit.
    kits = asset_common.kit_notes([(row["entry"].library, row["entry"].file)
                                   if row["entry"] is not None and not row["entry"].files else None for row in rows])
    lines = [_line(i, row, kits.get(i - 1, "")) for i, row in enumerate(rows, 1)]
    for row in rows:
        if row["server"]:
            indexed_assets.remember(row["server"])
    credits = list(dict.fromkeys(s["credit"] for s in sources))
    if any(row["server"] and row["server"]["source"] == "polyhaven" for row in rows):
        if "Powered by Poly Haven" not in credits:
            credits.append("Powered by Poly Haven")
    image = asset_common.sheet(paths, "assets_sheet.png", fit=True) if rows else None
    text = f"{len(rows)} asset(s) for {query!r}, numbered as on the sheet (left to right, top to bottom):\n"
    text += "\n".join([*lines, *credits, *dict.fromkeys(notes)])
    if image:
        text += "\nImage attached: thumbnails in that order (blank: no preview)."
    # Do not _clip: that could remove an import ID, required attribution, or provider credit.
    return ToolResult(text, image_path=image)


def search_assets(query: str, type: str = "", limit: int = asset_libraries.DEFAULT_LIMIT):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise ToolError("Give 1–1000 characters to search for.")
    if not isinstance(type, str) or type and type not in (*LOCAL_TYPES, *asset_libraries.SEARCH_TYPES):
        raise ToolError("type must be model, material, hdri or a Blender asset ID type")
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ToolError("limit must be an integer")
    limit = max(1, min(int(limit or asset_libraries.DEFAULT_LIMIT), asset_libraries.MAX_LIMIT))
    registered = asset_libraries.libraries()
    # Local .blend listing uses bpy. Snapshot it now; public remote listing I/O happens below.
    local = [(lib, entry) for lib in registered if not lib.remote for entry in asset_libraries.local_entries(lib)]
    cache_dir = str(bpy.app.cachedir)
    thumbs = asset_common.cache_root("assets") / "thumbs"
    def finish(is_cancelled, on_main):
        entries, notes = list(local), []
        for library in registered:
            if library.remote:
                try:
                    entries.extend((library, e) for e in asset_libraries.remote_entries(library))
                except (ToolError, OSError, ValueError, KeyError, TypeError) as ex:
                    notes.append(f"{library.name}: could not read library ({str(ex)[:100]}).")
        allowed = LOCAL_TYPES.get(type, (type,) if type else asset_libraries.SEARCH_TYPES)
        pool = [(lib, e) for lib, e in entries if e.id_type in allowed]
        ranked = asset_common.rank(query, pool, text_of=lambda p: p[1].text(), name_of=lambda p: p[1].name,
                                   limit=len(pool))
        inputs = {"query": query, "kind": INDEX_TYPES.get(type, type or "any"), "limit": limit}
        if inputs["kind"] in (*asset_report.KINDS, "any"):
            try:
                results, sources, index_notes = _index(inputs, is_cancelled, on_main)
            except (config.ConfigError, cloud_http.CloudError) as ex:
                results, sources, index_notes = [], [], ["Loopcut index not searched: account configuration unavailable."]
                print(f"Loopcut: asset index configuration: {ex}")
        else:
            results, sources, index_notes = [], [], [f"Loopcut index not searched: no {type} category."]
        # A semantic server match may be owned even when its local keywords did not match.
        # Prefer that registration without inventing a rank in the local search list.
        owned = {}
        for lib, entry in entries:
            local_row = _local_row(lib, entry)
            for key in local_row["keys"]:
                owned.setdefault(key, local_row)
        server_rows = [_server_row(r, cache_dir) for r in results]
        for row in server_rows:
            match = next((owned[k] for k in row["keys"] if k in owned), None)
            if match:
                row.update(entry=match["entry"], library=match["library"], cached=row["cached"] or match["cached"])
        rows = fuse([_local_row(lib, e) for lib, e in ranked], server_rows)[:limit]
        paths = []
        for row in rows:
            entry, server = row["entry"], row["server"]
            url = (entry.thumbnail_url if entry else "") or (server["preview_url"] if server else "")
            suffix = Path(urlsplit(url).path).suffix or ".png"
            dest = thumbs / (hashlib.sha256(url.encode()).hexdigest() + suffix)
            paths.append(asset_common.thumbnail(url, dest) if url and not is_cancelled() else None)
        return on_main(lambda: _present(query, rows, paths, sources, notes + index_notes))
    return ToolResult("Searching asset libraries and Loopcut's index…", then=finish)
