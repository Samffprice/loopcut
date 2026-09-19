"""The Loopcut mark, and every file made from it.

The mark's polygons live in extension/loopcut/ui/brand.py (the panel draws them too), so every
size is drawn fresh instead of scaled. source/ holds the render it was traced from.

Needs numpy, which Blender's Python has:
    tools/Blender.app/Contents/Resources/5.2/python/bin/python3.13 branding/make_assets.py \
        tools/Blender.app/Contents/Resources/5.2/datafiles/fonts/Inter.woff2
Writes mark.svg, topbar-icon.svg, mark-<size>.png, app-icon-1024.png, loopcut.ico and, on macOS only (they use
iconutil and Quick Look), loopcut.icns and splash.png next to this file. The splash's wordmark is
set in Inter, the OFL-licensed font Blender ships, embedded from the path given as argv[1].
"""

import base64

import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension" / "loopcut" / "ui"))
import brand  # noqa: E402  (the mark's polygons and rasteriser, shared with the panel)
from brand import PALETTE, SHAPES, hex_rgb, square  # noqa: E402

TILE = "#1b1b1d"         # The app icon's rounded square, the panel's background colour.
TILE_BORDER = "#2e2e32"


def svg(size=1024, background=None):
    scale = 10 * size / 1024
    tx, ty = (size - 93.5 * scale) / 2 - 14 * scale, (size - 82 * scale) / 2 - 13.5 * scale
    def d(points):
        return "M" + " L".join(f"{x * scale + tx:.1f},{y * scale + ty:.1f}" for x, y in points) + " Z"
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">']
    if background:
        out.append(f'<rect width="{size}" height="{size}" fill="{background}"/>')
    for colour, polygon, hole in SHAPES:
        path = d(polygon) + (" " + d(hole) if hole else "")
        out.append(f'<path fill="{PALETTE[colour]}" fill-rule="evenodd" d="{path}"/>')
    return "\n".join(out + ["</svg>"]) + "\n"


def over(image, background):
    bg = np.array(hex_rgb(background))
    return np.dstack([image[..., :3] * image[..., 3:] + bg * (1 - image[..., 3:]), np.ones(image.shape[:2])])

def write_png(path, image):
    data = (np.clip(image, 0, 1) * 255 + 0.5).astype(np.uint8)
    h, w = data.shape[:2]
    raw = b"".join(b"\x00" + data[y].tobytes() for y in range(h))
    def chunk(kind, body): return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _rounded_square(size, inset, radius, ss=4):
    """Coverage of a rounded square, by signed distance."""
    n = size * ss
    ys, xs = np.mgrid[0:n, 0:n]
    half = size / 2 - inset
    qx = np.abs((xs + 0.5) / ss - size / 2) - half + radius
    qy = np.abs((ys + 0.5) / ss - size / 2) - half + radius
    distance = np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - radius
    return (distance < 0).reshape(size, ss, size, ss).mean(axis=(1, 3))


def app_icon(size):
    """macOS style: the mark on a dark rounded square that fills 824/1024 of the canvas."""
    k = size / 1024
    outer = _rounded_square(size, 100 * k, 185 * k)
    inner = _rounded_square(size, 100 * k + max(1.0, 3 * k), 185 * k - max(1.0, 3 * k))
    tile = np.dstack([np.ones((size, size, 3)) * hex_rgb(TILE_BORDER), outer])
    tile[..., :3] = np.where(inner[..., None] > 0, (np.array(hex_rgb(TILE)) * inner[..., None]
                             + np.array(hex_rgb(TILE_BORDER)) * (1 - inner[..., None])), tile[..., :3])
    mark = square(size, margin=0.26)
    a = mark[..., 3:]
    return np.dstack([mark[..., :3] * a + tile[..., :3] * (1 - a), np.maximum(tile[..., 3], mark[..., 3])])


def _png_bytes(image):
    with tempfile.NamedTemporaryFile(suffix=".png") as handle:
        write_png(handle.name, image)
        return Path(handle.name).read_bytes()


def write_ico(path, sizes=(16, 24, 32, 48, 64, 128, 256)):
    """PNG-compressed entries, which Windows has read since Vista. The bare mark: Windows icons have no tile."""
    blobs = [_png_bytes(square(size, margin=0.04)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset, entries = 6 + 16 * len(sizes), b""
    for size, blob in zip(sizes, blobs):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    Path(path).write_bytes(header + entries + b"".join(blobs))


def write_icns(path):
    with tempfile.TemporaryDirectory() as folder:
        iconset = Path(folder) / "loopcut.iconset"
        iconset.mkdir()
        for points in (16, 32, 128, 256, 512):
            write_png(iconset / f"icon_{points}x{points}.png", app_icon(points))
            write_png(iconset / f"icon_{points}x{points}@2x.png", app_icon(points * 2))
        subprocess.run(["iconutil", "--convert", "icns", "--output", str(path), str(iconset)], check=True)


def _mark_paths(scale, tx, ty, opacity=1.0):
    def d(points):
        return "M" + " L".join(f"{x * scale + tx:.1f},{y * scale + ty:.1f}" for x, y in points) + " Z"
    return "\n".join(
        f'<path fill="{PALETTE[colour]}" fill-opacity="{opacity}" fill-rule="evenodd" '
        f'd="{d(polygon) + (" " + d(hole) if hole else "")}"/>' for colour, polygon, hole in SHAPES)


def splash_svg(font: Path, width: float = 1000) -> str:
    """1000x500, the size of Blender's splash, as the middle band of a square canvas (see
    write_splash). Blender draws the version over the top right corner."""
    inter = base64.b64encode(font.read_bytes()).decode("ascii")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000" width="{width}" height="{width}">
<defs>
<style>@font-face {{ font-family: "LoopcutInter"; src: url("data:font/woff2;base64,{inter}"); font-weight: 100 900; }}</style>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#202024"/><stop offset="1" stop-color="#101012"/></linearGradient>
<radialGradient id="teal" cx="0.12" cy="0.1" r="0.7"><stop offset="0" stop-color="{PALETTE["teal_light"]}" stop-opacity="0.20"/><stop offset="1" stop-color="{PALETTE["teal_light"]}" stop-opacity="0"/></radialGradient>
<radialGradient id="orange" cx="0.95" cy="1.0" r="0.75"><stop offset="0" stop-color="{PALETTE["orange"]}" stop-opacity="0.22"/><stop offset="1" stop-color="{PALETTE["orange"]}" stop-opacity="0"/></radialGradient>
</defs>
<g transform="translate(0,250)">
<rect width="1000" height="500" fill="url(#bg)"/>
<rect width="1000" height="500" fill="url(#teal)"/>
<rect width="1000" height="500" fill="url(#orange)"/>
{_mark_paths(7.6, 480, -130, opacity=0.05)}
{_mark_paths(2.3, 108, 124)}
<text x="372" y="282" font-family="LoopcutInter" font-weight="650" font-size="112" letter-spacing="-3" fill="#f2efe9">Loopcut</text>
<text x="378" y="332" font-family="LoopcutInter" font-weight="400" font-size="25" fill="#a3a3ab">An AI agent that works inside Blender</text>
</g>
</svg>
'''


def write_splash(here: Path, font: Path) -> None:
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "splash.svg"
        # Quick Look draws SVGs at 1.3x into a square thumbnail: ask for 1/1.3 of the size and keep the
        # middle band. Check splash.png by eye after regenerating; this is Quick Look's behaviour, not a contract.
        source.write_text(splash_svg(font, width=1000 / 1.3), encoding="utf-8")
        subprocess.run(["qlmanage", "-t", "-s", "1000", "-o", folder, str(source)], check=True, capture_output=True)
        rendered = Path(folder) / "splash.svg.png"
        subprocess.run(["sips", "-c", "500", "1000", str(rendered), "--out", str(here / "splash.png")],
                       check=True, capture_output=True)


# The mark as one colour, for Blender's icon set: the outline and the three openings.
SILHOUETTE = [
    [(20.5, 13.5), (101, 16), (107.5, 28), (65.5, 95.5), (52, 95.5), (14, 24.5)],
    [(40.5, 28), (84, 28), (57.5, 40.5)],
    [(35, 38), (55, 51), (54, 71)],
    [(85.5, 37.5), (66.5, 47.3), (65.8, 73)],
]


def topbar_icon_svg() -> str:
    """Replaces release/datafiles/icons_svg/blender.svg in the fork (the app menu's icon); same canvas."""
    scale = 15.5
    tx, ty = (1800 - brand.WIDTH * scale) / 2 - brand.LEFT * scale, (1500 - brand.HEIGHT * scale) / 2 - brand.TOP * scale
    path = " ".join("M" + " L".join(f"{x * scale + tx:.1f},{y * scale + ty:.1f}" for x, y in loop) + " Z"
                    for loop in SILHOUETTE)
    return ('<svg height="1500" viewBox="0 0 1800 1500" width="1800" xmlns="http://www.w3.org/2000/svg">'
            f'<path fill="#fff" fill-rule="evenodd" d="{path}"/></svg>\n')


def main():
    here = Path(__file__).resolve().parent
    (here / "topbar-icon.svg").write_text(topbar_icon_svg(), encoding="utf-8")
    fonts = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if sys.platform == "darwin" and fonts:
        write_splash(here, Path(fonts[0]))
    if "--quick" in sys.argv:  # Only the text files and the splash; the icon sizes take a minute.
        return
    (here / "mark.svg").write_text(svg(), encoding="utf-8")
    for size in (64, 256, 1024):
        write_png(here / f"mark-{size}.png", square(size))
    write_png(here / "app-icon-1024.png", app_icon(1024))
    write_ico(here / "loopcut.ico")
    if sys.platform == "darwin":
        write_icns(here / "loopcut.icns")


if __name__ == "__main__":
    main()
