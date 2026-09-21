"""What the asset-library tools share: HTTP with readable errors, verified downloads into a cache,
keyword ranking, and a sheet of thumbnails the model picks from.

The sheet is one image a capture's width (four tiles across), numbered by position to match the
text rows: the model chooses by look, which is the point of a library.
"""

import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from . import scratch
from .tools import ToolError, _pixels, _save_pixels

TIMEOUT = 30.0
USER_AGENT = "Loopcut (Blender add-on)"
TILE = 160           # A tile on the sheet: four across make a capture's width.
SHEET_COLUMNS = 4
SHEET_GAP = 4
SLUG = re.compile(r"^[A-Za-z0-9_-]+$")


class NotFound(ToolError):
    """HTTP 404: an id the model made up, or one the library has since removed."""


def valid_slug(value: str) -> bool:
    """Ids come from the model and become file names: letters, digits, _ and - only."""
    return bool(value) and bool(SLUG.match(value)) and len(value) <= 100


def open_url(url: str, headers: dict | None = None, timeout: float = TIMEOUT):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as ex:
        where = url.split("?")[0]
        if ex.code == 404:
            raise NotFound(f"Nothing at {where} (HTTP 404)") from ex
        if ex.code in (401, 403):
            raise ToolError(f"Refused (HTTP {ex.code}) at {where}") from ex
        if ex.code == 429:
            raise ToolError(f"Rate limited (HTTP 429) at {where}; wait a moment") from ex
        raise ToolError(f"HTTP {ex.code} at {where}") from ex
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise ToolError(f"Could not reach {url.split('/')[2]}: {getattr(ex, 'reason', ex)}") from ex


def get_json(url: str, headers: dict | None = None):
    with open_url(url, headers) as response:
        try:
            return json.load(response)
        except ValueError as ex:
            raise ToolError(f"{url.split('/')[2]} sent something that is not JSON") from ex


def digest_of(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def matches(path: Path, expected: str | None) -> bool:
    """`expected` is 'sha256:hex' or 'md5:hex' (case-insensitive), as the libraries publish it.
    Integrity against a truncated or swapped transfer, not security."""
    if not expected:
        return True
    algorithm, _, value = expected.partition(":")
    try:
        return digest_of(path, algorithm.lower()) == value.lower()
    except ValueError:
        return True  # An algorithm this Python lacks: trust the transfer.


def download(url: str, dest: Path, expected: str | None = None, size: int = 0,
             headers: dict | None = None) -> Path:
    """Fetch one file unless a verified copy is already at `dest`."""
    if dest.exists() and matches(dest, expected):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    with open_url(url, headers, timeout=max(TIMEOUT, size / 200_000)) as response, open(partial, "wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    if not matches(partial, expected):
        partial.unlink(missing_ok=True)
        raise ToolError(f"Download of {dest.name} did not match its published checksum; try again")
    partial.replace(dest)
    return dest


def thumbnail(url: str, dest: Path, headers: dict | None = None) -> Path | None:
    """A preview for the sheet, or None: a missing thumbnail must not fail a search."""
    try:
        return download(url, dest, headers=headers)
    except ToolError as ex:
        print(f"Loopcut: no thumbnail from {url.split('?')[0]}: {ex}")
        return None


def rank(query: str, items, text_of, name_of=None, weight_of=None, limit: int = 8) -> list:
    """Items by how many query words hit their text, then hits in the name, then weight.
    A word that matches nothing is ignored, so 'red brick wall' still finds bricks."""
    words = [w for w in re.split(r"[^a-z0-9]+", str(query).lower()) if len(w) > 1]
    scored = []
    for index, item in enumerate(items):
        text = text_of(item).lower()
        hits = sum(1 for w in words if w in text)
        if not hits:
            continue
        name = name_of(item).lower() if name_of else ""
        in_name = sum(1 for w in words if w in name)
        weight = weight_of(item) if weight_of else 0
        scored.append((-hits, -in_name, -weight, index, item))
    scored.sort(key=lambda s: s[:4])
    return [item for *_, item in scored[:limit]]


def sheet(paths: list, name: str = "sheet.png") -> Path | None:
    """Thumbnails as one image, numbered by position; None entries are blank tiles."""
    import numpy as np
    tiles = []
    for path in paths:
        try:
            tiles.append(_pixels(path, TILE) if path else None)
        except Exception as ex:  # A preview Blender cannot read is a blank tile, not a failed search.
            print(f"Loopcut: could not read thumbnail {path}: {ex}")
            tiles.append(None)
    if not any(t is not None for t in tiles):
        return None
    rows = (len(tiles) + SHEET_COLUMNS - 1) // SHEET_COLUMNS
    width = SHEET_COLUMNS * TILE + (SHEET_COLUMNS - 1) * SHEET_GAP
    height = rows * TILE + (rows - 1) * SHEET_GAP
    canvas = np.zeros((height, width, 4), dtype=np.float32)
    canvas[..., :3], canvas[..., 3] = 0.15, 1.0
    for index, tile in enumerate(tiles):
        if tile is None:
            continue
        row, column = divmod(index, SHEET_COLUMNS)
        tile = tile[:TILE, :TILE]
        y0 = height - (row + 1) * TILE - row * SHEET_GAP  # Pixel rows run bottom-up in Blender images.
        x0 = column * (TILE + SHEET_GAP)
        canvas[y0:y0 + tile.shape[0], x0:x0 + tile.shape[1]] = tile
    out = scratch.folder() / name
    out.parent.mkdir(exist_ok=True)
    _save_pixels(canvas, out)
    return out


def hdri_world(name: str, path: Path):
    """A new world lit by the image at `path`, packed so the .blend never depends on a cache
    folder, made the scene world. The previous world is left intact and unused."""
    import bpy
    world = bpy.data.worlds.new(name)
    world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    nodes.clear()
    coords = nodes.new("ShaderNodeTexCoord")
    coords.location = (-800, 0)
    mapping = nodes.new("ShaderNodeMapping")
    mapping.location = (-600, 0)
    env = nodes.new("ShaderNodeTexEnvironment")
    env.location = (-400, 0)
    env.image = bpy.data.images.load(str(path), check_existing=True)
    env.image.pack()
    background = nodes.new("ShaderNodeBackground")
    background.location = (-100, 0)
    output = nodes.new("ShaderNodeOutputWorld")
    output.location = (100, 0)
    links.new(coords.outputs["Generated"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    links.new(env.outputs["Color"], background.inputs["Color"])
    links.new(background.outputs["Background"], output.inputs["Surface"])
    bpy.context.scene.world = world
    return world


def cache_root(name: str) -> Path:
    from .checkpoints import data_root
    return data_root() / name
