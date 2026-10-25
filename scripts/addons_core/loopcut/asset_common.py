"""What the asset-library tools share: HTTP with readable errors, verified downloads into a cache,
keyword ranking, and a sheet of thumbnails the model picks from.

The sheet is one image a capture's width (four tiles across), each tile labelled with its row's pick
number (asset_picks): the model chooses by look, which is the point of a library.
"""

import hashlib
import http.client
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


def _broke_off(url: str, ex: BaseException) -> ToolError:
    """A body read that failed after the answer began (a read timeout, a reset, a reply cut short): the tool's own
    failed step. Uncaught, a library's thumbnail timing out ended a turn as "the connection to the model dropped"
    (2026-10-25)."""
    from . import llm
    return ToolError(f"Lost the connection to {url.split('/')[2]} while reading its answer ({llm.short_cause(ex)}); "
                     "try again")


def get_json(url: str, headers: dict | None = None):
    with open_url(url, headers) as response:
        try:
            return json.load(response)
        except ValueError as ex:
            raise ToolError(f"{url.split('/')[2]} sent something that is not JSON") from ex
        except (OSError, http.client.HTTPException) as ex:
            raise _broke_off(url, ex) from ex


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
    response = open_url(url, headers, timeout=max(TIMEOUT, size / 200_000))
    try:
        with response, open(partial, "wb") as out:
            while True:
                try:
                    chunk = response.read(1 << 20)
                except (OSError, http.client.HTTPException) as ex:
                    raise _broke_off(url, ex) from ex
                if not chunk:
                    break
                out.write(chunk)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
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


def clean(value, limit: int = 90) -> str:
    """A library's free text (description, tag) on one result row: one line, at most `limit` characters.
    Not a string, or nothing left: empty."""
    if not isinstance(value, str):
        return ""
    text = " ".join("".join(c if c.isprintable() else " " for c in value).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip(" ,.;:") + "…"


def size_text(dims) -> str:
    """'0.51x0.56x0.94 m' from two or three sizes in meters, '' when they are not that."""
    if not isinstance(dims, (list, tuple)) or len(dims) not in (2, 3):
        return ""
    if not all(isinstance(d, (int, float)) and not isinstance(d, bool) and 0 < d < 1e6 for d in dims):
        return ""
    return "x".join("%.2f" % d for d in dims) + " m"


def measured_line(bounds: list, listed=None) -> str:
    """What an imported model measures, all its objects' world bounds together, so its scale can be judged
    against the real thing without adding up per-object rows. `listed` is the size the library states,
    in meters; a size far from it is pointed out. '' when nothing has bounds."""
    boxes = [b for b in bounds if isinstance(b, dict)]
    if not boxes:
        return ""
    low = [min(b["min"][i] for b in boxes) for i in range(3)]
    high = [max(b["max"][i] for b in boxes) for i in range(3)]
    size = [h - l for l, h in zip(low, high)]
    line = "Measured: " + "x".join("%.2f" % s for s in size) + f" m (x, y, z), base at z {low[2]:.2f}."
    stated = size_text(listed)
    if stated:
        line += f" The library lists {stated}."
        ratio = max(size) / max(listed) if max(size) > 0 else 0
        if ratio and not 1 / 3 <= ratio <= 3:
            line += f" It arrived at {ratio:.3g}x that: check the units or the scale."
    return line


def measured(objects, listed=None) -> str:
    """measured_line for objects just imported. Main thread."""
    import bpy
    from .tools import _world_bounds
    bpy.context.view_layer.update()  # Parented children's matrix_world is stale until then.
    return measured_line([_world_bounds(obj) for obj in objects], listed)


def kit_notes(kits: list, numbers: list | None = None) -> dict:
    """{row index: 'kit: <file> (rows 1, 4)'} for results that share a kit, here the same .blend file in
    the same library: assets made to go together. `kits` holds one (library, file) or None per row;
    with `numbers`, each row's pick number (asset_picks), the note names those: 'kit: <file> (#9, #12)'."""
    rows = {}
    for index, kit in enumerate(kits):
        if kit:
            rows.setdefault(kit, []).append(index)
    notes = {}
    for (_, file), members in rows.items():
        if len(members) > 1:
            listed = (", ".join(f"#{numbers[m]}" for m in members) if numbers
                      else "rows " + ", ".join(str(m + 1) for m in members))
            notes.update(dict.fromkeys(members, f"kit: {Path(file).stem} ({listed})"))
    return notes


def fit_box(width: int, height: int, cell: tuple) -> tuple:
    """(width, height, dx, dy): an image of width x height scaled to fit whole inside cell (width, height),
    and its offset there to sit centred."""
    cell_w, cell_h = cell
    scale = min(cell_w / width, cell_h / height)
    w, h = max(1, min(cell_w, round(width * scale))), max(1, min(cell_h, round(height * scale)))
    return w, h, (cell_w - w) // 2, (cell_h - h) // 2


def _fitted(path, cell):
    """A thumbnail scaled to fit whole inside the cell, over the sheet's grey where it is transparent."""
    tile = _pixels(path, cell[1])
    if tile.shape[1] > cell[0]:  # Wider than the cell: scale again by width (one more small load).
        tile = _pixels(path, fit_box(tile.shape[1], tile.shape[0], cell)[1])
    tile = tile[:cell[1], :cell[0]].copy()
    alpha = tile[..., 3:4]
    tile[..., :3] = tile[..., :3] * alpha + 0.15 * (1 - alpha)
    tile[..., 3] = 1.0
    return tile


def sheet(paths: list, name: str = "sheet.png", cell: tuple = (TILE, TILE), columns: int = SHEET_COLUMNS,
          folder: Path | None = None, fit: bool = False, labels: list | None = None) -> Path | None:
    """Images as one, in rows of `columns` cells of cell (width, height) px, each scaled to the cell's
    height and cut to its width; None entries are blank cells. Search thumbnails (numbered by
    position in the text beside them) and a wave's contact sheet (waves.contact_sheet). Written to
    folder, else the scratch folder. Main thread.
    fit (the search sheets): each image whole and centred in its cell instead, on the sheet's grey. Cut to
    the cell's width, a 2:1 HDRI showed half its sky and a wide Poly Pizza or Poly Haven preview lost the
    model's right side (offline picking check, 2026-10-10).
    labels (the search sheets): each tile's pick number (asset_picks) in its top-left corner, so the
    thumbnail the model likes is the number it imports, not a position to count."""
    import numpy as np
    width_of, height_of = cell
    tiles = []
    for path in paths:
        try:
            tiles.append(None if not path else _fitted(path, cell) if fit else _pixels(path, height_of))
        except Exception as ex:  # A preview Blender cannot read is a blank tile, not a failed search.
            print(f"Loopcut: could not read thumbnail {path}: {ex}")
            tiles.append(None)
    if not any(t is not None for t in tiles):
        return None
    rows = (len(tiles) + columns - 1) // columns
    width = columns * width_of + (columns - 1) * SHEET_GAP
    height = rows * height_of + (rows - 1) * SHEET_GAP
    canvas = np.zeros((height, width, 4), dtype=np.float32)
    canvas[..., :3], canvas[..., 3] = 0.15, 1.0
    for index, tile in enumerate(tiles):
        if tile is None:
            continue
        row, column = divmod(index, columns)
        tile = tile[:height_of, :width_of]
        dx, dy = ((width_of - tile.shape[1]) // 2, (height_of - tile.shape[0]) // 2) if fit else (0, 0)
        y0 = height - (row + 1) * height_of - row * SHEET_GAP + dy  # Pixel rows run bottom-up in Blender images.
        x0 = column * (width_of + SHEET_GAP) + dx
        canvas[y0:y0 + tile.shape[0], x0:x0 + tile.shape[1]] = tile
    for index, label in enumerate(labels or ()):
        row, column = divmod(index, columns)
        stamp(canvas, str(label), column * (width_of + SHEET_GAP), height - row * (height_of + SHEET_GAP))
    out = (Path(folder) if folder else scratch.folder()) / name
    out.parent.mkdir(parents=True, exist_ok=True)
    _save_pixels(canvas, out)
    return out


# Digits 3x5, top row first: the search sheets' pick numbers, drawn with numpy alone (no font, no GPU here).
DIGITS = ("111101101101111", "010110010010111", "111001111100111", "111001111001111", "101101111001001",
          "111100111001111", "111100111101111", "111001001001001", "111101111101111", "111101111001111")
DIGIT_SCALE = 4  # A digit 12x20 px on a 160 px tile.


def stamp(canvas, text: str, left: int, top: int) -> None:
    """Digits in white on a black box whose top-left corner is column `left`, just under row `top` of
    canvas (rows run bottom-up, as Blender keeps them). Anything but digits draws nothing."""
    scale = pad = DIGIT_SCALE
    width, height = len(text) * 4 * scale - scale + 2 * pad, 5 * scale + 2 * pad
    if not text.isdigit() or not text.isascii() or top < height:
        return
    canvas[top - height:top, left:left + width, :3] = 0.0
    for place, digit in enumerate(text):
        for bit, on in enumerate(DIGITS[int(digit)]):
            if on == "1":
                row, column = divmod(bit, 3)
                y = top - pad - (row + 1) * scale
                x = left + pad + (place * 4 + column) * scale
                canvas[y:y + scale, x:x + scale, :3] = 1.0


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
