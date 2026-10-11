"""Imports for indexed providers, retaining native IDs and JSON-only worker handoffs."""

import copy
import hashlib
import re
import shutil
import stat
import zipfile
from pathlib import Path

import bpy

from . import asset_common, asset_libraries, polyhaven
from .tools import ToolError

# Native (provider, id), not result position. Later searches do not invalidate earlier results.
_rows = {}
MAX_UNPACKED = 2 * 1024 * 1024 * 1024
MAP_ROLES = {"color": "base_color", "roughness": "roughness", "metalness": "metallic",
             "normalgl": "normal", "displacement": "displacement"}


def remember(row):
    _rows[(row["source"], row["source_id"])] = copy.deepcopy(row)


def remote_entry(row, cache_dir):
    """Use the published root (including slash) for Blender's shared cache identity."""
    remote = row["remote_library"]
    value = remote["entries"][0]
    library = asset_libraries.Library(remote["name"], Path(cache_dir) / "remote-assets" /
        asset_libraries.remote_cache_dirname(remote["url"]), remote["url"], value["import_method"] or "APPEND")
    entry = asset_libraries.Entry(library.name, value["file"], value["id_type"], value["name"],
        license=row["license"]["name"], author=", ".join(row["attribution"]["authors"]),
        thumbnail_url=value["thumbnail_url"] or "", files=value["files"], min_version=value["min_blender"] or "")
    return library, entry


def _download(item, dest):
    size = item["size"]
    if dest.exists() and size is not None and dest.stat().st_size != size:
        dest.unlink()
    result = asset_common.download(item["url"], dest, size=size or 0)
    if size is not None and result.stat().st_size != size:
        result.unlink(missing_ok=True)
        raise ToolError("Asset download did not match its published size; try again")
    return result


def _unpack(archive, folder):
    """Extract regular files only, bounded in count/size; never follow archive/cache symlinks."""
    try:
        with zipfile.ZipFile(archive) as zipped:
            entries = zipped.infolist()
            if len(entries) > 2048 or sum(e.file_size for e in entries) > MAX_UNPACKED:
                raise ToolError("Asset archive exceeds the unpacked size limit")
            for item in entries:
                target = folder / item.filename
                mode = item.external_attr >> 16
                if (not asset_libraries.safe_relative_path(item.filename)
                        or not target.resolve().is_relative_to(folder.resolve())
                        or stat.S_ISLNK(mode)):
                    raise ToolError("Asset archive contains an unsafe file path")
                if item.is_dir():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_suffix(target.suffix + ".part")
                try:
                    with zipped.open(item) as src, partial.open("wb") as out:
                        shutil.copyfileobj(src, out, 1 << 20)
                    partial.replace(target)
                finally:
                    partial.unlink(missing_ok=True)
    except (zipfile.BadZipFile, RuntimeError, OSError) as ex:
        raise ToolError("Could not unpack the asset archive") from ex


def _ambient_download(row, resolution):
    candidates = [d for d in row.get("downloads", []) if re.search(
        r"(?<![0-9])" + resolution + r"(?![0-9])", d["label"], re.I)]
    supported = {"zip", "hdr", "exr", "jpg", "png", "blend", "glb", "gltf"}
    item = next((d for d in candidates if d["format"] in supported), None)
    if item is None:
        raise ToolError(f"{row['source_id']} has no {resolution} download in this search result; search again or choose another resolution")
    # A refreshed download URL gets a fresh cache file, even when its native asset ID repeats.
    folder = asset_common.cache_root("ambientcg") / row["source_id"] / resolution / hashlib.sha256(
        item["url"].encode()).hexdigest()[:16]
    path = _download(item, folder / ("download." + item["format"]))
    info = {"provider": "ambientcg", "credit": row["attribution"]["text"],
            "authors": dict.fromkeys(row["attribution"]["authors"]),
            "dimensions": [v * 1000 for v in row["dimensions_m"]] if row.get("dimensions_m") else None}
    fetched = {"id": row["source_id"], "resolution": resolution, "info": info, "loader": "polyhaven"}
    if item["format"] == "zip":
        unpacked = folder / "maps"
        _unpack(path, unpacked)
        paths = sorted(p for p in unpacked.rglob("*") if p.is_file())
    else:
        paths = [path]
    if row["kind"] in {"material", "texture"}:
        maps = {}
        for path in paths:
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".exr"}:
                continue
            role = MAP_ROLES.get(path.stem.rsplit("_", 1)[-1].lower())
            if role:
                maps.setdefault(role, str(path))
        if not maps and len(paths) == 1 and paths[0].suffix.lower() in {".jpg", ".png"}:
            maps["base_color"] = str(paths[0])
        if not maps:
            raise ToolError("Asset archive has no supported PBR maps")
        return {**fetched, "kind": "textures", "maps": maps}
    if row["kind"] == "hdri":
        hdr = next((p for p in paths if p.suffix.lower() in {".hdr", ".exr"}), None)
        if hdr:
            return {**fetched, "kind": "hdris", "hdr": str(hdr)}
    if row["kind"] == "model":
        blend = next((p for p in paths if p.suffix.lower() == ".blend"), None)
        gltf = next((p for p in paths if p.suffix.lower() in {".glb", ".gltf"}), None)
        if gltf:
            from . import cloud_http, cloud_jobs
            try:
                cloud_jobs._check_gltf(gltf)
            except (cloud_http.CloudError, OSError, ValueError) as ex:
                raise ToolError("Asset has invalid or unsafe glTF data") from ex
        if blend or gltf:
            return {**fetched, "kind": "models", "blend": str(blend) if blend else None,
                    "gltf": str(gltf) if gltf else None}
    raise ToolError("Asset download has no supported scene data")


def fetch_indexed(asset_id: str, source: str, resolution: str = "1k", apply_to=None) -> dict:
    row = _rows.get((source, asset_id))
    if row is None:
        raise ToolError(f"Unknown {source} asset id; use an id from search_assets in this session")
    resolution = resolution or "1k"
    if resolution not in polyhaven.RESOLUTIONS:
        raise ToolError("resolution must be one of 1k, 2k, 4k")
    if ((source == "ambientcg" and (resolution != "1k" or not row.get("remote_library"))
         and not any(re.search(r"(?<![0-9])" + resolution + r"(?![0-9])", d["label"], re.I)
                     for d in row.get("downloads", [])))
            or source == "physicallybased" and not row.get("remote_library")):
        # The report byte budget can trim optional downloads. Ask for just this public ID,
        # using the same managed runtime; no local paths or registration details are sent.
        from . import asset_search
        rows, _, notes = asset_search._index({"query": asset_id, "kind": row["kind"], "sources": [source], "limit": 1},
                                             lambda: False, lambda fn: fn())
        refreshed = next((r for r in rows if r["id"] == row["id"]), None)
        if refreshed is None:
            raise ToolError("Import metadata was trimmed; search again. " + " ".join(notes))
        row = refreshed
        remember(row)
    if source == "ambientcg" and (resolution != "1k" or not row.get("remote_library")):
        fetched = _ambient_download(row, resolution)
    else:
        if not row.get("remote_library"):
            raise ToolError("This result has no remote-library import metadata; search again")
        library, entry = remote_entry(row, bpy.app.cachedir)
        fetched = {**asset_libraries.fetch_entry(library, entry), "loader": "library"}
    return {**fetched, "credit": row["attribution"]["text"] if row["attribution"]["required"] else "",
            "dimensions_m": row.get("dimensions_m") if row["kind"] == "model" else None}


def load_indexed(fetched: dict, apply_to=None):
    if fetched["loader"] == "library":
        result = asset_libraries.load_asset(fetched, apply_to)
    else:
        result = polyhaven.load_polyhaven(fetched, apply_to)
    if fetched.get("credit") and fetched["credit"] not in result.text:
        result.text += "\n" + fetched["credit"]
    return result


def import_indexed(asset_id: str, source: str, resolution: str = "1k", apply_to=None):
    asset_libraries._check_apply_to(apply_to)
    return load_indexed(fetch_indexed(asset_id, source, resolution, apply_to), apply_to)


def describe(name, arguments):
    return f"Import {arguments.get('source', 'indexed')} asset {arguments.get('asset_id', '?')}", ""
