"""Paint stats: what a painted colour map measures over each part's UV area, so a worker and the
orchestrator read "noise-like" or "accents pop" from the pixels instead of a model's "bold
painterly" (Arcane look pass 2 painted every surface with high-contrast numpy noise, saturated
violet and teal streaks and a camo floor, and nothing measured it).

A part is a material slot of a mesh: the faces with that material index, over the UV map and
through the Mapping its Base Color image node reads. The image is box-averaged by a whole step to
at most ANALYSIS texels a side, and no finer than about a screen px when the scene has a camera;
sizes are given back in the image's own texels. Colour is OKLab (lightness L 0-1, chroma, hue),
decoded to linear first when the image stores sRGB:

- values: the fewest lightness levels (1-D k-means, up to MAX_LEVELS) that explain LEVELS_VAR of the
  part's lightness variance, levels closer than LEVEL_STEP merged; and the lightest against the
  darkest (95th against 5th percentile lightness, 1:2.6);
- patches: the texel-weighted mean run of one level along rows and columns: the length of the patch
  a texel sits in. In screen px when the scene has a camera: its px per metre at the object
  (world_to_camera_view) over the part's texels per metre (UV area x image size over 3D area);
- noise: the share of lightness variance finer than a blur at stroke scale (sigma STROKE_PX screen
  px; STROKE_TEXELS analysis texels without a camera), of at least QUIET's variance so a nearly
  flat part is not all noise, and that fine part's size (its RMS);
- accents: coloured texels (chroma >= CHROMA) more than ACCENT_HUE degrees off the part's main hue
  (the chroma-weighted hue histogram's peak): their share, their chroma against the base's, their
  mean |L - the surroundings' L| (the base's lightness blurred around them) and how many sit in
  dark surroundings (L < DARK_L). Value-matched accents sit at the base's lightness and saturation.

A part is a material slot, not a face attribute: workers paint per material, and every calibration
mesh split its paint that way. verdict() turns the numbers into advisory words, never a gate:
noise-like, values far apart, accents pop, accents in darks, restrained; else reads designed.
The thresholds were set on (2026-09-30, harness/paint_stats_check.py --calibrate):
- Arcane pass 2 ("looks terrible"), 26 parts: lightest:darkest 1:1.0-3.6, fine noise RMS
  0.013-0.156, accents ΔL 0.09-0.59, chroma x0.2-3.4; 23 read a problem;
- Arcane pass 1 ("amazing but not Arcane enough"), 30 parts: 1:1.0-1.7, RMS 0.006-0.036, accents
  on 3 (the shelf's two-hue wood); 27 read restrained;
- crops of the reference still's painted brass (image space): 1:1.2-1.4, RMS 0.004-0.028, accents
  ΔL 0.05-0.12, chroma x0.9-1.2: designed, the two flat plate crops restrained.
"""

import math

ANALYSIS = 1024       # Texels a side the stats read at most.
MAX_LEVELS = 5
LEVELS_VAR = 0.9      # Share of lightness variance the levels explain.
LEVEL_STEP = 0.04     # OKLab L: levels closer than this are one value.
STROKE_PX = 4.0       # Screen px: the blur whose residual is the fine noise.
STROKE_TEXELS = 4.0   # The same blur in analysis texels without a camera.
QUIET = 0.03          # OKLab L: a part varying less than this (std) is noise-free, whatever its share.
CHROMA = 0.03         # OKLab chroma under which a texel's hue is not read.
ACCENT_HUE = 20.0     # Degrees off the part's main hue that make a texel an accent.
SURROUND = 3.0        # The surroundings' blur against the stroke blur.
MIN_TEXELS = 400      # Analysis texels a part needs to be measured.
SAMPLES = 50_000      # Lightness values the levels are fitted on at most.

# Advisory thresholds (verdict), set on the calibration set (harness/paint_stats_check.py).
NOISY_SHARE = 0.20    # Fine noise this share of the variance ...
NOISY_SIZE = 0.05     # ... and this big (RMS, OKLab L) reads noise-like.
HARSH = 2.0           # Lightest over darkest from here: values far apart.
RESTRAINED = 1.35     # Under this, with no accents: restrained.
ACCENT_MIN = 0.02     # Accents under this share of the part are not judged.
POP_DL = 0.15         # Accents this far from their surroundings' lightness pop ...
POP_CHROMA = 1.5      # ... and so do accents this many times as saturated as the base.
DARK_L = 0.4          # OKLab L of dark surroundings (about sRGB 72/255 grey) ...
DARK_SHARE = 0.5      # ... where this share of the accents sit: in the darks.


# ------------------------------------------------------------------ pixels (numpy, no bpy)

def linear(rgb):
    """sRGB-encoded values to linear."""
    import numpy as np
    rgb = np.clip(rgb, 0.0, None)
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def oklab(rgb):
    """Linear Rec.709 RGB (..., 3) to OKLab (..., 3), float32."""
    import numpy as np
    m1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                   [0.2119034982, 0.6806995451, 0.1073969566],
                   [0.0883024619, 0.2817188376, 0.6299787005]], dtype=np.float32)
    m2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                   [1.9779984951, -2.4285922050, 0.4505937099],
                   [0.0259040371, 0.7827717662, -0.8086757660]], dtype=np.float32)
    return np.cbrt(np.clip(rgb, 0.0, None).astype(np.float32) @ m1.T) @ m2.T


def shrink(pixels, step: int):
    """Pixels box-averaged by a whole step."""
    if step <= 1:
        return pixels
    h, w = pixels.shape[0] // step * step, pixels.shape[1] // step * step
    total = sum(pixels[y:h:step, x:w:step].astype("float32") for y in range(step) for x in range(step))
    return total / (step * step)


def _box(a, r: int):
    """The mean over 2r + 1 texels along axis 0, zeros past the edges."""
    import numpy as np
    c = np.cumsum(np.pad(a, ((r + 1, r), (0, 0))), axis=0, dtype=np.float32)
    return (c[2 * r + 1:] - c[:-2 * r - 1]) / (2 * r + 1)


def blur(values, weights, sigma: float):
    """A near-Gaussian blur (three box passes each way) of values where weights > 0, normalised by
    the weights' own blur so nothing outside them bleeds in: (blurred values, blurred weights). A
    wide one (sigma 8 and up) runs on a grid a quarter of sigma coarser and is spread back."""
    import numpy as np
    num, den = (values * weights).astype(np.float32), weights.astype(np.float32)
    coarse = max(1, int(sigma // 4))
    if coarse > 1:
        num, den, sigma = shrink(num[..., None], coarse)[..., 0], shrink(den[..., None], coarse)[..., 0], sigma / coarse
    r = max(1, round(math.sqrt(sigma * sigma + 0.25) - 0.5))
    for _ in range(2):
        for _ in range(3):
            num, den = _box(num, r), _box(den, r)
        num, den = num.T, den.T
    if coarse > 1:
        pad = ((0, values.shape[0] - num.shape[0] * coarse), (0, values.shape[1] - num.shape[1] * coarse))
        num, den = (np.pad(np.repeat(np.repeat(a, coarse, 0), coarse, 1), pad, mode="edge") for a in (num, den))
    return num / np.maximum(den, 1e-6), den


def levels(values, most: int = MAX_LEVELS, explain: float = LEVELS_VAR, step: float = LEVEL_STEP) -> list[float]:
    """The fewest 1-D k-means centres (sorted) explaining `explain` of the values' variance, centres
    closer than `step` merged (weighted by their counts)."""
    import numpy as np
    values = np.sort(np.asarray(values, dtype=np.float64).ravel())
    if values.size > SAMPLES:
        values = values[:: math.ceil(values.size / SAMPLES)]
    total = values.var()
    centres, counts = np.array([values.mean()]), np.array([values.size])
    if total > 1e-8:
        for k in range(2, most + 1):
            centres = np.quantile(values, (np.arange(k) + 0.5) / k)
            for _ in range(30):
                label = np.searchsorted((centres[1:] + centres[:-1]) / 2, values)
                counts = np.bincount(label, minlength=k)
                moved = np.where(counts > 0, np.bincount(label, values, k) / np.maximum(counts, 1), centres)
                done = np.allclose(moved, centres, atol=1e-5)
                centres = np.sort(moved)
                if done:
                    break
            label = np.searchsorted((centres[1:] + centres[:-1]) / 2, values)
            counts = np.bincount(label, minlength=k)
            if 1 - ((values - centres[label]) ** 2).mean() / total >= explain:
                break
    merged = []  # [centre, count]
    for centre, count in zip(centres.tolist(), counts.tolist()):
        if not count:
            continue
        if merged and centre - merged[-1][0] < step:
            c, n = merged[-1]
            merged[-1] = [(c * n + centre * count) / (n + count), n + count]
        else:
            merged.append([centre, count])
    return [c for c, _ in merged]


def run_length(labels, mask) -> float:
    """The texel-weighted mean run of one label inside the mask along rows and columns: the length
    of the patch a random texel sits in (sum of squared runs over sum of runs)."""
    import numpy as np
    total, squares = 0.0, 0.0
    for grid, inside in ((labels, mask), (labels.T, mask.T)):
        flat = np.where(inside, grid, -1).ravel()
        change = np.ones(flat.size, dtype=bool)
        change[1:] = flat[1:] != flat[:-1]
        change[:: grid.shape[1]] = True  # A run ends at every row's end.
        run = np.cumsum(change) - 1
        sizes = np.bincount(run[flat >= 0]).astype(np.float64)
        total, squares = total + sizes.sum(), squares + (sizes * sizes).sum()
    return squares / total if total else 0.0


def main_hue(a, b, chroma) -> float:
    """The peak of the chroma-weighted hue histogram (10 degree bins, smoothed), refined to the
    weighted mean hue within ACCENT_HUE of it; radians."""
    import numpy as np
    hue = np.arctan2(b, a)
    bins = np.bincount(((hue + math.pi) / (2 * math.pi) * 36).astype(np.int64) % 36, chroma, 36)
    smooth = bins + 0.5 * (np.roll(bins, 1) + np.roll(bins, -1))
    peak = (np.argmax(smooth) + 0.5) / 36 * 2 * math.pi - math.pi
    near = np.abs((hue - peak + math.pi) % (2 * math.pi) - math.pi) <= math.radians(ACCENT_HUE)
    return math.atan2(float((b * near).sum()), float((a * near).sum()))


def measure(lab, mask, sigma: float = STROKE_TEXELS) -> dict | None:
    """One part's stats from OKLab pixels (h, w, 3) over a boolean mask, sizes in these texels,
    sigma the stroke-scale blur in these texels. None when the mask holds under MIN_TEXELS."""
    import numpy as np
    count = int(mask.sum())
    if count < MIN_TEXELS:
        return None
    rows, cols = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    box = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
    lab, mask = lab[box], mask[box]  # Blurs weigh by the mask: nothing outside it counts anyway.
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    inside = L[mask]
    low, high = (float(v) for v in np.quantile(inside, (0.05, 0.95)))
    centres = levels(inside)
    stats = {"texels": count, "levels": [round(c, 3) for c in centres], "ratio": round(high / max(low, 0.02), 2)}
    edges = np.array([(p + q) / 2 for p, q in zip(centres, centres[1:])])
    stats["patch"] = round(float(run_length(np.searchsorted(edges, L), mask)), 1)
    weights = mask.astype(np.float32)
    smooth, _ = blur(L, weights, sigma)
    fine = float(((L - smooth)[mask] ** 2).mean())
    variance = float(inside.var())
    stats["noise"] = round(min(1.0, fine / max(variance, QUIET ** 2)), 2)
    stats["noise_size"] = round(math.sqrt(fine), 3)
    chroma = np.hypot(a, b)
    coloured = mask & (chroma >= CHROMA)
    stats["accents"] = 0.0
    if coloured.sum() < MIN_TEXELS:
        return stats
    hue = main_hue(a[coloured], b[coloured], chroma[coloured])
    off = np.abs((np.arctan2(b, a) - hue + math.pi) % (2 * math.pi) - math.pi)
    accent = coloured & (off > math.radians(ACCENT_HUE))
    base = mask & ~accent
    stats["hue"] = round(math.degrees(hue) % 360)
    stats["accents"] = round(float(accent.sum()) / count, 3)
    if accent.sum() < MIN_TEXELS // 4 or base.sum() < MIN_TEXELS:
        return stats
    around, reach = blur(L, base.astype(np.float32), sigma * SURROUND)
    around = np.where(reach > 0.02, around, float(np.median(L[base])))
    stats["accent_dl"] = round(float(np.abs(L - around)[accent].mean()), 3)
    tinted = base & coloured
    base_chroma = float(np.median(chroma[tinted])) if tinted.any() else float(np.median(chroma[base]))
    stats["accent_chroma"] = round(float(np.median(chroma[accent])) / max(base_chroma, 1e-3), 2)
    stats["accent_dark"] = round(float((around[accent] < DARK_L).mean()), 2)
    return stats


# ------------------------------------------------------------------ words (pure)

def verdict(stats: dict) -> list[str]:
    """Advisory words for one part's stats; [] when nothing stands out (reads designed)."""
    words = []
    if stats.get("noise", 0.0) >= NOISY_SHARE and stats.get("noise_size", 0.0) >= NOISY_SIZE:
        words.append("noise-like")
    if stats.get("ratio", 1.0) >= HARSH:
        words.append("values far apart")
    judged = _judged(stats)
    if judged and (stats["accent_dl"] >= POP_DL or stats["accent_chroma"] >= POP_CHROMA):
        words.append("accents pop")
    if judged and stats["accent_dark"] >= DARK_SHARE:
        words.append("accents in darks")
    if not words and stats.get("ratio", 1.0) < RESTRAINED and stats.get("accents", 0.0) < ACCENT_MIN:
        words.append("restrained")
    return words


def _judged(stats: dict) -> bool:
    return stats.get("accents", 0.0) >= ACCENT_MIN and "accent_dl" in stats


def flagged(stats: dict) -> list[str]:
    """verdict's words that point at a problem (all but restrained)."""
    return [w for w in verdict(stats) if w != "restrained"]


def summary(name: str, parts: list[dict]) -> str:
    """A deliverable's paint for the orchestrator (facts.measured's "paint"): how many parts read
    each way, then the parts with the most problems, briefly; "" without parts."""
    if not parts:
        return ""
    tally = {}
    for part in parts:
        for word in verdict(part) or ["reads designed"]:
            tally[word] = tally.get(word, 0) + 1
    worst = sorted((p for p in parts if flagged(p)), key=lambda p: -len(flagged(p)))
    brief = [f"{p['part']} 1:{p['ratio']:.1f} noise {p['noise']:.0%}"
             + (f" ΔL {p['accent_dl']:.2f} C x{p['accent_chroma']:.1f}" if _judged(p) else "") for p in worst[:3]]
    return (f"{name} paint ({len(parts)} part{'s' * (len(parts) != 1)}, advisory): "
            + ", ".join(f"{word} {count}" for word, count in tally.items()) + "".join(f"; {b}" for b in brief))


def text_of(name: str, paint) -> str:
    """The deliverable's paint line from its facts ("paint": parts, or {"error"}), or ""."""
    if isinstance(paint, dict) and paint.get("error"):
        return f"{name} paint: not measured ({paint['error']})"
    return summary(name, paint) if isinstance(paint, list) else ""


def text(name: str, stats: dict, px_per_texel: float | None = None) -> str:
    """One part's line, e.g. `Lid: 3 values 1:1.4, patches ~40 px, noise 6%, accents 12% ΔL 0.03
    C x0.9 - reads designed`. px_per_texel: screen px per image texel, else sizes in texels."""
    count = len(stats["levels"])
    size = f"~{stats['patch'] * px_per_texel:.0f} px" if px_per_texel else f"~{stats['patch']:.0f} texels"
    parts = [f"{count} value{'s' * (count != 1)} 1:{stats['ratio']:.1f}", f"patches {size}",
             f"noise {stats['noise']:.0%}"]
    if _judged(stats):
        parts.append(f"accents {stats['accents']:.0%} ΔL {stats['accent_dl']:.2f} C x{stats['accent_chroma']:.1f}")
    words = verdict(stats)
    return f"{name}: " + ", ".join(parts) + f" - {', '.join(words) if words else 'reads designed'}"


# ------------------------------------------------------------------ Blender

def base_colour(bindings: list) -> dict | None:
    """The first image node binding whose path ends at a shader's Base Color or Color, on a UV map."""
    for binding in bindings:
        if binding.get("image") is not None and binding.get("uv_map") is not None and any(
                path.endswith((".Base Color", ".Color")) for path in binding.get("paths", ())):
            return binding
    return None


def _mapped(uv, transform):
    """UVs (n, 2) through a Mapping node's transform (texture_tools.mapping, for many points)."""
    import numpy as np
    from .texture_tools import _euler
    if not transform:
        return uv
    m = np.array(_euler(transform["rotation"]))
    location, scale = np.array(transform["location"]), np.array(transform["scale"], dtype=np.float64)
    p = np.column_stack([uv, np.zeros(len(uv))])
    if transform["kind"] == "TEXTURE":
        p = ((p - location) @ m) / np.where(scale == 0, np.inf, scale)
    else:
        p = (p * scale) @ m.T + (location if transform["kind"] == "POINT" else 0.0)
    return p[:, :2]


def raster(triangles, height: int, width: int):
    """A boolean (height, width) mask of the texels whose centres UV triangles (n, 3, 2) cover,
    repeating past the tile: scanlines, each row's span of every triangle marked at its ends and
    summed along the row. A triangle wider than a tile is cut at two."""
    import numpy as np
    mask = np.zeros((height, width), dtype=bool)
    if not len(triangles):
        return mask
    t = triangles * np.array([width, height], dtype=np.float64)
    t = t - np.floor(t.min(axis=1, keepdims=True) / [width, height]) * [width, height]  # Into the first tile.
    low, high = t[:, :, 1].min(axis=1), t[:, :, 1].max(axis=1)
    first = np.ceil(low - 0.5).astype(np.int64)
    rows = np.clip(np.floor(high - 0.5).astype(np.int64) - first + 1, 0, 2 * height - first)
    which = np.repeat(np.arange(len(t)), rows)
    row = first[which] + (np.arange(which.size) - np.repeat(np.cumsum(rows) - rows, rows))
    centre = row + 0.5
    left, right = np.full(which.size, np.inf), np.full(which.size, -np.inf)
    for a, b in ((0, 1), (1, 2), (2, 0)):
        p, q = t[which, a], t[which, b]
        slanted = p[:, 1] != q[:, 1]
        crosses = slanted & (np.minimum(p[:, 1], q[:, 1]) <= centre) & (centre <= np.maximum(p[:, 1], q[:, 1]))
        x = p[:, 0] + (centre - p[:, 1]) * (q[:, 0] - p[:, 0]) / np.where(slanted, q[:, 1] - p[:, 1], 1.0)
        left, right = np.where(crosses, np.minimum(left, x), left), np.where(crosses, np.maximum(right, x), right)
    start = np.clip(np.ceil(left - 0.5), 0, 2 * width).astype(np.int64)
    stop = np.clip(np.ceil(right - 0.5), 0, 2 * width).astype(np.int64)  # One past the last texel.
    keep = (stop > start) & (row < 2 * height)
    row, start, stop = row[keep], start[keep], stop[keep]
    span = 2 * width + 1
    marks = (np.bincount(row * span + start, minlength=2 * height * span)
             - np.bincount(row * span + stop, minlength=2 * height * span))
    canvas = np.cumsum(marks.reshape(2 * height, span), axis=1)[:, :2 * width] > 0
    for y in (0, height):
        for x in (0, width):
            mask |= canvas[y:y + height, x:x + width]
    return mask


def uv_parts(obj, uv_map: str, transform) -> dict:
    """{material index: (UV triangles (n, 3, 2), UV area in tiles, 3D area in m2)} of a mesh."""
    import numpy as np
    mesh = obj.data
    layer = mesh.uv_layers.get(uv_map) if uv_map else next((l for l in mesh.uv_layers if l.active_render), None)
    tris = mesh.loop_triangles
    n = len(tris)
    if layer is None or not n:
        return {}
    loops, verts, index = np.empty(n * 3, dtype=np.int32), np.empty(n * 3, dtype=np.int32), np.empty(n, dtype=np.int32)
    tris.foreach_get("loops", loops)
    tris.foreach_get("vertices", verts)
    tris.foreach_get("material_index", index)
    uv = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    layer.uv.foreach_get("vector", uv)
    uv = _mapped(uv.reshape(-1, 2).astype(np.float64)[loops], transform).reshape(n, 3, 2)
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    world = (co.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3])[verts].reshape(n, 3, 3)
    area3d = np.linalg.norm(np.cross(world[:, 1] - world[:, 0], world[:, 2] - world[:, 0]), axis=1) / 2
    e1, e2 = uv[:, 1] - uv[:, 0], uv[:, 2] - uv[:, 0]
    area2d = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) / 2
    return {slot: (uv[index == slot], float(area2d[index == slot].sum()), float(area3d[index == slot].sum()))
            for slot in np.unique(index).tolist()}


def px_per_metre(scene, obj) -> float | None:
    """Screen px a metre spans at the object's bounds centre through the scene camera; None without
    a camera or with the object behind it or well out of frame."""
    camera = scene.camera if scene is not None else None
    if camera is None:
        return None
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector
    centre = obj.matrix_world @ (sum((Vector(c) for c in obj.bound_box), Vector()) / 8)
    right = (camera.matrix_world.to_3x3() @ Vector((1.0, 0.0, 0.0))).normalized()
    a, b = world_to_camera_view(scene, camera, centre), world_to_camera_view(scene, camera, centre + right)
    if a.z <= 0 or not (-0.25 <= a.x <= 1.25 and -0.25 <= a.y <= 1.25):
        return None
    scale = scene.render.resolution_percentage / 100
    return math.hypot((b.x - a.x) * scene.render.resolution_x * scale, (b.y - a.y) * scene.render.resolution_y * scale)


class Lab:
    """Images in OKLab near an analysis step, read once per call. pixels: texture_tools._Pixels,
    whose reduced() reads an image past its MAX_TEXELS at a stride."""

    def __init__(self, pixels):
        self.pixels, self.done = pixels, {}

    def of(self, image, step: int):
        """(OKLab pixels, the step they are at in the image's own texels: a whole number of strides,
        at least `step`), or None when the image can't be read."""
        read = self.pixels.reduced(image)
        if read is None:
            return None
        data, stride = read
        step = max(1, math.ceil(step / stride))
        key = (image.name, step)
        if key not in self.done:
            small = shrink(data[..., :3], step)
            if image.colorspace_settings.name == "sRGB" and not image.is_float:
                small = linear(small)
            self.done[key] = oklab(small)
        return self.done[key], step * stride


def object_parts(obj, lab: Lab, bindings_of, scene=None) -> list[tuple[str, dict, float | None]]:
    """[(material name, stats, screen px per image texel or None)] for a mesh's material slots whose
    material paints its Base Color from an image on a UV map; parts under MIN_TEXELS are left out.
    bindings_of(material): texture_tools._bindings. Edit Mode is skipped (its UV layers read empty)."""
    if obj.type != "MESH" or obj.mode == "EDIT" or not len(obj.data.polygons):
        return []
    if scene is not None and scene.camera is not None:
        import bpy
        bpy.context.view_layer.update()  # Matrices a script just set are stale until then.
    per_metre = px_per_metre(scene, obj)
    found, shapes = [], {}
    for slot_index, slot in enumerate(obj.material_slots):
        binding = base_colour(bindings_of(slot.material)) if slot.material else None
        if binding is None:
            continue
        key = (binding["uv_map"], repr(binding["transform"]))
        if key not in shapes:
            shapes[key] = uv_parts(obj, binding["uv_map"], binding["transform"])
        if slot_index not in shapes[key]:
            continue
        triangles, uv_area, area = shapes[key][slot_index]
        width, height = binding["image"].size
        px = per_metre / math.sqrt(uv_area * width * height / area) if per_metre and area > 0 and uv_area > 0 else None
        # Analysis texels no finer than a screen px, and at most ANALYSIS a side.
        read = lab.of(binding["image"], max(math.ceil(max(width, height) / ANALYSIS), int(1 / px) if px else 1, 1))
        if read is None:
            continue
        data, step = read
        mask = raster(triangles, *data.shape[:2])
        sigma = max(1.0, STROKE_PX / (px * step)) if px else STROKE_TEXELS
        stats = measure(data, mask, sigma)
        if stats:
            stats["patch"] = round(stats["patch"] * step, 1)  # In the image's own texels.
            found.append((slot.material.name, stats, px))
    return found


def object_lines(obj, pixels, scene=None) -> list[str]:
    """inspect_texture's block for a mesh: a header and a line per measured part; [] when no
    material of it paints its Base Color from an image. pixels: the call's texture_tools._Pixels."""
    from .texture_tools import _bindings
    parts = object_parts(obj, Lab(pixels), _bindings, scene)
    if not parts:
        return []
    unit = "screen px through the scene camera" if any(px for _, _, px in parts) else "image texels (no camera)"
    return ([f"Paint stats per material slot (advisory; OKLab lightness over its UV area; sizes in {unit}):"]
            + [f"  {text(name, stats, px)}" for name, stats, px in parts])


def measured(objects, scene=None) -> list[dict]:
    """Every part of these objects as one dict each ({"part", "px", **stats}), for facts and the
    worker's check_brief."""
    from .texture_tools import _Pixels, _bindings
    lab = Lab(_Pixels())
    return [{"part": name, "px": round(px, 3) if px else None, **stats}
            for ob in objects for name, stats, px in object_parts(ob, lab, _bindings, scene)]


def brief_lines(parts: list[dict], most: int = 4) -> list[str]:
    """The worker's check_brief block: the parts whose paint reads a problem (at most `most`), then
    the rest named briefly; [] without parts."""
    if not parts:
        return []
    bad = [p for p in parts if flagged(p)]
    lines = ["Paint stats (measured, advisory: never blocks report_back):"]
    lines += [f"- {text(p['part'], p, p.get('px'))}" for p in bad[:most]]
    if len(bad) > most:
        lines.append(f"- +{len(bad) - most} more reading a problem: inspect_texture on the object lists every part")
    for word, label in (("restrained", f"restrained (lightest:darkest under 1:{RESTRAINED}, no accents)"),
                        ("", "reads designed")):
        names = [p["part"] for p in parts if not flagged(p) and (word in verdict(p) if word else not verdict(p))]
        if names:
            lines.append(f"- {label}: " + ", ".join(names[:8]) + (f" +{len(names) - 8}" if len(names) > 8 else ""))
    return lines
