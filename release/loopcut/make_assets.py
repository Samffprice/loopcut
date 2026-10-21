"""The Loopcut mark, and every file made from it.

The mark's polygons live in scripts/addons_core/loopcut/ui/brand.py (the panel draws them too), so every
size is drawn fresh instead of scaled. source/ holds the render it was traced from.

Needs numpy, which Blender's Python has:
    tools/Blender.app/Contents/Resources/5.2/python/bin/python3.13 release/loopcut/make_assets.py \
        tools/Blender.app/Contents/Resources/5.2/datafiles/fonts/Inter.woff2
Writes mark.svg, topbar-icon.svg, editor-icon.svg (copy it to release/datafiles/icons_svg/loopcut.svg),
mark-<size>.png, app-icon-1024.png, loopcut.ico and, on macOS only (they use iconutil and Quick Look),
loopcut.icns and splash.png next to this file. With --wix it writes only the Windows installer's
wix-dialog.png and wix-banner.bmp (macOS only, Quick Look again). The splash's wordmark is set in Inter,
the OFL-licensed font Blender ships, embedded from the path given as argv[1].
"""

import base64

import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core" / "loopcut" / "ui"))
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
<g transform="translate(36,0)"><!-- The lockup's middle on the canvas's middle (measured from splash.png). -->
{_mark_paths(2.3, 108, 124)}
<text x="372" y="282" font-family="LoopcutInter" font-weight="650" font-size="112" letter-spacing="-3" fill="#f2efe9">Loopcut</text>
<text x="378" y="332" font-family="LoopcutInter" font-weight="400" font-size="25" fill="#a3a3ab">An AI agent that works inside Blender</text>
</g>
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


def _font_face(name: str, font: Path) -> str:
    data = base64.b64encode(font.read_bytes()).decode("ascii")
    return f'@font-face {{ font-family: "{name}"; src: url("data:font/woff2;base64,{data}"); font-weight: 100 900; }}'


def dmg_background_svg(inter: Path, mono: Path, width: float) -> str:
    """The disk image window's background, 660 x 480 points drawn at 2x (1320 x 960) as the middle
    band of a 1320 square canvas (see write_dmg_background). Coordinates are 2x points and match
    dmg-layout.applescript: the app icon at (170, 150), Applications at (490, 150), the readme at
    (80, 330), all 96-point icons."""
    mark = 0.9  # Mark height 82 * 0.9 = 74px = 37 points.
    row1, band = 300, 524  # Icon row centre; top of the first-launch card.
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1320 1320" width="{width}" height="{width}">
<defs>
<style>{_font_face("LoopcutInter", inter)} {_font_face("LoopcutMono", mono)}</style>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#202024"/><stop offset="1" stop-color="#101012"/></linearGradient>
<radialGradient id="teal" cx="0.12" cy="0.1" r="0.7"><stop offset="0" stop-color="{PALETTE["teal_light"]}" stop-opacity="0.20"/><stop offset="1" stop-color="{PALETTE["teal_light"]}" stop-opacity="0"/></radialGradient>
<radialGradient id="orange" cx="0.95" cy="1.0" r="0.75"><stop offset="0" stop-color="{PALETTE["orange"]}" stop-opacity="0.22"/><stop offset="1" stop-color="{PALETTE["orange"]}" stop-opacity="0"/></radialGradient>
</defs>
<g transform="translate(0,180)" font-family="LoopcutInter" fill="#f2efe9">
<rect width="1320" height="960" fill="url(#bg)"/>
<rect width="1320" height="960" fill="url(#teal)"/>
<rect width="1320" height="960" fill="url(#orange)"/>
{_mark_paths(mark, 48 - brand.LEFT * mark, 40 - brand.TOP * mark)}
<text x="140" y="94" font-weight="650" font-size="40" letter-spacing="-1">Loopcut</text>
<text x="1272" y="92" font-weight="400" font-size="22" fill="#a3a3ab" text-anchor="end">An AI agent that works inside Blender</text>
<path d="M 500 {row1} L 800 {row1} M 760 {row1 - 34} L 800 {row1} L 760 {row1 + 34}" fill="none" stroke="#f2efe9" stroke-opacity="0.55" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
<text x="660" y="478" font-weight="500" font-size="28" text-anchor="middle"><tspan fill="{PALETTE["orange_light"]}">1</tspan>   Drag Loopcut into Applications</text>
<rect x="48" y="{band}" width="1224" height="392" rx="22" fill="#ffffff" fill-opacity="0.045" stroke="#ffffff" stroke-opacity="0.10" stroke-width="2"/>
<text x="296" y="{band + 62}" font-weight="500" font-size="28"><tspan fill="{PALETTE["orange_light"]}">2</tspan>   The first time, macOS says it can't verify Loopcut.</text>
<text x="296" y="{band + 118}" font-weight="400" font-size="24" fill="#c9c5be">Open System Settings › Privacy &amp; Security and click Open Anyway.</text>
<text x="296" y="{band + 164}" font-weight="400" font-size="24" fill="#c9c5be">Or paste this into Terminal once:</text>
<rect x="296" y="{band + 190}" width="880" height="60" rx="12" fill="#0c0c0e" fill-opacity="0.8" stroke="#ffffff" stroke-opacity="0.12" stroke-width="2"/>
<text x="322" y="{band + 230}" font-family="LoopcutMono" font-size="24" fill="#e8e4dc">xattr -dr com.apple.quarantine /Applications/Loopcut.app</text>
<text x="296" y="{band + 306}" font-weight="400" font-size="22" fill="#8f8b85">Read me first.txt says why. Updates Loopcut installs itself never ask again.</text>
</g>
</svg>
'''


def write_dmg_background(here: Path, inter: Path, mono: Path) -> None:
    """dmg-background.png: 1320 x 960 pixels at 144 dpi, so Finder shows it at 660 x 480 points."""
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "dmg.svg"
        source.write_text(dmg_background_svg(inter, mono, width=1320 / 1.3), encoding="utf-8")  # See write_splash.
        subprocess.run(["qlmanage", "-t", "-s", "1320", "-o", folder, str(source)], check=True, capture_output=True)
        rendered = Path(folder) / "dmg.svg.png"
        out = here / "dmg-background.png"
        subprocess.run(["sips", "-c", "960", "1320", str(rendered), "--out", str(out)], check=True, capture_output=True)
        subprocess.run(["sips", "-s", "dpiWidth", "144", "-s", "dpiHeight", "144", str(out)], check=True,
                       capture_output=True)


def _mark_at(x: float, y: float, scale: float, opacity: float = 1.0) -> str:
    """The mark with its top-left corner at (x, y)."""
    return _mark_paths(scale, x - brand.LEFT * scale, y - brand.TOP * scale, opacity)


def wix_dialog_svg(inter: Path, width: float) -> str:
    """The Windows installer's welcome and finish pages, 493 x 312 like Blender's WIX_UI_DIALOG.png,
    as the middle band of a 493 square canvas (see write_wix). Windows Installer draws the page text
    in black right of x = 164, so only the strip left of that carries the brand; the rest stays white."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 493 493" width="{width}" height="{width}">
<defs>
<style>{_font_face("LoopcutInter", inter)}</style>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#202024"/><stop offset="1" stop-color="#101012"/></linearGradient>
<radialGradient id="teal" cx="0.1" cy="0.08" r="0.9"><stop offset="0" stop-color="{PALETTE["teal_light"]}" stop-opacity="0.24"/><stop offset="1" stop-color="{PALETTE["teal_light"]}" stop-opacity="0"/></radialGradient>
<radialGradient id="orange" cx="0.95" cy="1.0" r="0.9"><stop offset="0" stop-color="{PALETTE["orange"]}" stop-opacity="0.26"/><stop offset="1" stop-color="{PALETTE["orange"]}" stop-opacity="0"/></radialGradient>
</defs>
<g transform="translate(0,90.5)" font-family="LoopcutInter">
<rect width="493" height="312" fill="#ffffff"/>
<rect width="164" height="312" fill="url(#bg)"/>
<rect width="164" height="312" fill="url(#teal)"/>
<rect width="164" height="312" fill="url(#orange)"/>
{_mark_at(-30, 40, 2.1, opacity=0.06)}
{_mark_at(22, 262, 0.34)}
<text x="58" y="285" font-weight="650" font-size="22" letter-spacing="-0.5" fill="#f2efe9">Loopcut</text>
</g>
</svg>
'''


def wix_banner_svg(inter: Path, width: float) -> str:
    """The top banner of the installer's other pages, 493 x 58 like Blender's WIX_UI_BANNER.bmp.
    Windows Installer draws each page's title and description in black over the left two thirds,
    so the background stays light and the brand sits right of x = 330."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 493 493" width="{width}" height="{width}">
<defs>
<style>{_font_face("LoopcutInter", inter)}</style>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#ffffff"/><stop offset="0.55" stop-color="#ffffff"/><stop offset="1" stop-color="#eeedea"/></linearGradient>
<radialGradient id="teal" cx="0.78" cy="0" r="0.35"><stop offset="0" stop-color="{PALETTE["teal_light"]}" stop-opacity="0.16"/><stop offset="1" stop-color="{PALETTE["teal_light"]}" stop-opacity="0"/></radialGradient>
<radialGradient id="orange" cx="1" cy="1" r="0.3"><stop offset="0" stop-color="{PALETTE["orange"]}" stop-opacity="0.18"/><stop offset="1" stop-color="{PALETTE["orange"]}" stop-opacity="0"/></radialGradient>
</defs>
<g transform="translate(0,217.5)" font-family="LoopcutInter">
<rect width="493" height="58" fill="url(#bg)"/>
<rect width="493" height="58" fill="url(#teal)"/>
<rect width="493" height="58" fill="url(#orange)"/>
{_mark_at(370, 16.5, 0.3)}
<text x="477" y="36" font-weight="650" font-size="20" letter-spacing="-0.4" text-anchor="end" fill="#1b1b1d">Loopcut</text>
</g>
</svg>
'''


def write_wix(here: Path, inter: Path) -> None:
    """wix-dialog.png and wix-banner.bmp for the Windows installer (build_files/cmake/packaging.cmake).
    They live here rather than over Blender's files in release/windows/installer_wix/, which are Git LFS
    objects this repository can't store. Windows Installer wants the banner as a 24-bit BMP."""
    with tempfile.TemporaryDirectory() as folder:
        for name, make, height in (("wix-dialog", wix_dialog_svg, 312), ("wix-banner", wix_banner_svg, 58)):
            source = Path(folder) / f"{name}.svg"
            source.write_text(make(inter, width=493 / 1.3), encoding="utf-8")  # See write_splash.
            subprocess.run(["qlmanage", "-t", "-s", "493", "-o", folder, str(source)], check=True, capture_output=True)
            rendered = Path(folder) / f"{name}.svg.png"
            cropped = Path(folder) / f"{name}.png"
            subprocess.run(["sips", "-c", str(height), "493", str(rendered), "--out", str(cropped)],
                           check=True, capture_output=True)
            if name == "wix-dialog":
                (here / "wix-dialog.png").write_bytes(cropped.read_bytes())
            else:
                write_bmp24(here / "wix-banner.bmp", read_png_rgb(cropped))


def read_png_rgb(path: Path) -> np.ndarray:
    """An 8-bit RGB or RGBA, non-interlaced PNG (what sips writes) as uint8 RGB rows, top first."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", path
    pos, idat, width = 8, b"", 0
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and colour in (2, 6) and interlace == 0, (depth, colour, interlace)
            channels = 4 if colour == 6 else 3
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * channels
    rows, previous = [], np.zeros(stride, dtype=np.int32)
    for y in range(height):
        kind, line = raw[y * (stride + 1)], np.frombuffer(raw, np.uint8, stride, y * (stride + 1) + 1).astype(np.int32)
        out = np.zeros(stride, dtype=np.int32)
        for x in range(stride):  # Small images only: a plain loop keeps the five filters readable.
            a = out[x - channels] if x >= channels else 0
            b = previous[x]
            c = previous[x - channels] if x >= channels else 0
            if kind == 0: predictor = 0
            elif kind == 1: predictor = a
            elif kind == 2: predictor = b
            elif kind == 3: predictor = (a + b) // 2
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                predictor = a if pa <= pb and pa <= pc else b if pb <= pc else c
            out[x] = (line[x] + predictor) & 0xFF
        rows.append(out)
        previous = out
    return np.array(rows, dtype=np.uint8).reshape(height, width, channels)[..., :3]


def write_bmp24(path: Path, rgb: np.ndarray) -> None:
    """A bottom-up 24-bit BMP with a BITMAPINFOHEADER, the format Windows Installer reads."""
    height, width = rgb.shape[:2]
    row = width * 3
    pad = (4 - row % 4) % 4
    pixels = b"".join(rgb[y, :, ::-1].tobytes() + b"\x00" * pad for y in range(height - 1, -1, -1))
    header = struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    Path(path).write_bytes(header + info + pixels)


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


def _signed_area(points) -> float:
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])) / 2


# The mark's three faces, as groups of SHAPES colours.
EDITOR_ICON_FACES = (("blue_outer blue", 0.5), ("orange orange_light", 1.0),
                     ("teal teal_light red crimson white", 1.0))


def editor_icon_svg() -> str:
    """The Loopcut editor's icon in the Editor Type menu: release/datafiles/icons_svg/loopcut.svg (ICON_LOOPCUT).
    Blender's editor icons are white on a 1600 canvas (100 units = 1 px at 16 px) with secondary parts at
    partial opacity, so the mark's faces become three white shapes and the left face is dimmed. Each face is
    one path with nonzero filling, so its overlapping polygons don't double up; holes wind the other way."""
    scale = 15.0  # 93.5 wide -> 14 px, like the other editor icons.
    tx, ty = 800 - scale * (brand.LEFT + brand.WIDTH / 2), 800 - scale * (brand.TOP + brand.HEIGHT / 2)
    def d(points, clockwise):
        if (_signed_area(points) > 0) != clockwise:
            points = list(reversed(points))
        return "M" + " L".join(f"{x * scale + tx:.0f},{y * scale + ty:.0f}" for x, y in points) + " Z"
    paths = []
    for names, opacity in EDITOR_ICON_FACES:
        parts = []
        for colour, polygon, hole in SHAPES:
            if colour in names.split():
                parts.append(d(polygon, True))
                if hole:
                    parts.append(d(hole, False))
        alpha = "" if opacity == 1 else f' opacity="{opacity}"'
        paths.append(f'<path fill="#fff"{alpha} d="{" ".join(parts)}"/>')
    return ('<svg height="1600" viewBox="0 0 1600 1600" width="1600" xmlns="http://www.w3.org/2000/svg">'
            + "".join(paths) + "</svg>\n")


def main():
    here = Path(__file__).resolve().parent
    (here / "topbar-icon.svg").write_text(topbar_icon_svg(), encoding="utf-8")
    (here / "editor-icon.svg").write_text(editor_icon_svg(), encoding="utf-8")
    fonts = [arg for arg in sys.argv[1:] if not arg.startswith("--")]
    if "--wix" in sys.argv:  # Only the Windows installer's images, which the Mac app doesn't ship.
        write_wix(here, Path(fonts[0]))
        return
    if sys.platform == "darwin" and fonts:
        write_splash(here, Path(fonts[0]))
        write_dmg_background(here, Path(fonts[0]), Path(fonts[0]).with_name("DejaVuSansMono.woff2"))
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
