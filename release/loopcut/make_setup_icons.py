"""The first-run setup's icons (scripts/addons_core/loopcut/media/icons): Lucide outline icons drawn by Quick Look at
960 px, then reduced to 64 px in the setup's palette colours. On a Mac, with Blender's Python (it has numpy):

    Blender -b --factory-startup --python release/loopcut/make_setup_icons.py -- <lucide-static icons folder>

The SVGs are lucide-static 0.460.0's (npm); their licence is in the add-on's THIRD-PARTY-NOTICES.txt. Each
icon's 24-unit artboard fills 7/8 of the image's height, centred, with its 2-unit margin cut on the left: the
strokes start at the image's left edge, so the icon of a setup row lines up with the checkboxes below it
(onboarding.ICON_UNITS)."""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import bpy
import numpy as np

TONES = {"fg": 0xF2EFE9, "muted": 0x9C9CA3, "danger": 0xD4574A, "success": 0x6FBF8A}  # onboarding.py's palette.
ICONS = (("search", "fg"), ("sparkles", "fg"), ("message-circle", "fg"), ("user", "fg"), ("key-round", "fg"),
         ("cloud", "fg"), ("laptop", "fg"), ("circle-arrow-up", "fg"), ("loader-circle", "muted"),
         ("circle-alert", "danger"), ("check", "success"), ("check", "fg"), ("brain", "fg"), ("minus", "muted"),
         ("circle-help", "fg"),
         # Centred, not at the left: they stand over captions (How Loopcut works' cloud tools).
         ("box", "fg", "centre"), ("person-standing", "fg", "centre"), ("mic", "fg", "centre"),
         ("audio-lines", "fg", "centre"), ("aperture", "fg", "centre"), ("list-checks", "fg", "centre"),
         ("hammer", "fg", "centre"), ("scan-eye", "fg", "centre"), ("wrench", "fg", "centre"))
RENDER = 960  # Quick Look's size; a whole multiple of the glyph's.
SIZE = 64
GLYPH = 56
LEFT_MARGIN = RENDER // 12  # Lucide's 2-unit margin, which a row's icon does without at the left.


def render(source: Path, folder: Path) -> Path:
    """Black strokes on white at RENDER px: Quick Look draws an SVG at 1.3 times its size (see make_assets.py)."""
    text = re.sub(r"<!--.*?-->", "", source.read_text(), flags=re.S).strip()
    size = RENDER / 1.3 / 1.3
    text = text.replace('width="24"', f'width="{size:.3f}"', 1).replace('height="24"', f'height="{size:.3f}"', 1)
    text = text.replace('stroke="currentColor"', 'stroke="#000000"')
    text = re.sub(r"(<svg[^>]*>)", r'\1<rect width="24" height="24" fill="#ffffff" stroke="none"/>', text, count=1)
    svg = folder / source.name
    svg.write_text(text)
    subprocess.run(["qlmanage", "-t", "-s", str(RENDER), "-o", str(folder), str(svg)], check=True,
                   capture_output=True)
    return folder / f"{source.name}.png"


def write_icon(rendered: Path, tone: str, out: Path, centre: bool = False) -> None:
    image = bpy.data.images.load(str(rendered))
    w, h = image.size
    if (w, h) != (RENDER, RENDER):
        raise RuntimeError(f"{rendered} is {w}x{h}, not {RENDER}")
    ink = 1.0 - np.array(image.pixels[:], dtype=np.float32).reshape(h, w, 4)[..., :3].mean(axis=2)
    step = RENDER // GLYPH
    used = GLYPH * step  # A few pixels of the artboard's empty margin are cut at the bottom.
    shift = 0 if centre else LEFT_MARGIN
    ink = np.pad(ink, ((0, 0), (0, LEFT_MARGIN)))
    small = ink[h - used:, shift:shift + used].reshape(GLYPH, step, GLYPH, step).mean(axis=(1, 3))
    alpha = np.zeros((SIZE, SIZE), dtype=np.float32)
    top = (SIZE - GLYPH) // 2
    left = (SIZE - GLYPH) // 2 if centre else 0
    alpha[top:top + GLYPH, left:left + GLYPH] = np.clip(small, 0.0, 1.0)
    pixels = np.zeros((SIZE, SIZE, 4), dtype=np.float32)
    pixels[..., :3] = [((TONES[tone] >> shift) & 255) / 255 for shift in (16, 8, 0)]
    pixels[..., 3] = alpha
    result = bpy.data.images.new(out.stem, SIZE, SIZE, alpha=True)
    result.pixels.foreach_set(pixels.ravel())
    result.filepath_raw = str(out)
    result.file_format = "PNG"
    result.save()


def main() -> None:
    lucide = Path(sys.argv[sys.argv.index("--") + 1])
    out = Path(__file__).resolve().parents[2] / "scripts" / "addons_core" / "loopcut" / "media" / "icons"
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        for name, tone, *centre in ICONS:
            write_icon(render(lucide / f"{name}.svg", Path(folder)), tone, out / f"{name}-{tone}.png", bool(centre))
            print(f"{name}-{tone}.png")


main()
