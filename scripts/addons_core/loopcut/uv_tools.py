"""inspect_uv and uv_unwrap: UV layouts measured instead of judged from a picture, and made in a
worker's headless Blender.

Eval 20260929-235231 (a hand-painted look pass on seven props) had every worker write its own UV
audit in run_python, each measuring something else; a sign painted black because its UVs sat
outside the image, found only after painting; an 8192 atlas 15 % full. In the chair runs a bad
unwrap read as plaid and was dismissed as the preview. inspect_uv is the one audit, per object and
UV map, on the base mesh in world units (what uv_unwrap edits; modifiers are named, not applied):

- islands: faces joined through shared UV corners (the same vertex at the same UV, to 1e-6);
- outside 0-1: faces with a corner outside the first tile; UDIM tiles past it are named;
- zero-area faces (no UV area on a face that has 3D area) and flipped ones (UV winding against the
  face's own: mirrored paint);
- overlap: each island's triangles rasterized on a GRID x GRID raster of the tile by texel centre.
  A cell two islands cover overlaps; an island whose own cells fall well short of its UV area
  folds onto itself. Stacked or mirrored islands count: that is what they are;
- stretch, per triangle, from the 2x2 map of its 3D plane onto UV: angle distortion is the ratio
  of its singular values (1: squares stay square), area distortion its texel density against the
  object's median; both weighted by 3D area;
- texel density: sqrt(UV area / 3D area) x resolution in px per metre, area-weighted; min and max
  leave out the extreme TRIM of area at each end, where slivers live;
- gutters: the closest boundary edges of two islands in px, through a hash grid, searched up to
  GAP_SEARCH x the padding; border: the closest UV corner to the tile's edge.

plan (atlas_plan) sizes an atlas instead: texels = area x density^2, plus a padding/2 band along every
island's boundary (its measured 3D length; ASSUMED_ISLANDS cube-like islands before any unwrap),
at PACKING efficiency.

checker: the objects wearing a temporary checker on that map, rendered, then everything put back.
Workbench's texture colour (it reads the active UV map, whatever a UV Map node says, measured in
Blender 5.2) needs no shader compile, which a fresh material in a material-preview draw would.
The parent draws offscreen with tools' capture helpers; a worker's child (any background Blender)
renders through worker_capture.look(angle="final"), whose final setup leaves Workbench's colour
type alone.

uv_unwrap works on the objects in Edit Mode (Blender's UV operators run in background Blender): a
projection or an unwrap, the UVs scaled into the tile, islands evened to one density
(average_islands_scale), then pack_islands with margin = padding / (2 x resolution) (FRACTION
margins leave twice the margin between islands and one at the border, measured), rotating by
quarter turns only and packing bounding boxes: rotate ANY with CONCAVE shapes took 8 s for six
squares. Cube, cylinder and sphere are projected here (project), in metres about the object's own
Z through its bounds' centre, winding kept: Blender's cube projection maps opposite faces onto one
another and joins neighbours into islands that overlap, and its cylinder maps height at a fixed
v = z / 2 whatever the radius (both measured in 5.2). A box face goes to the plane its normal
points at most; a cylinder's side unrolls to arc length by height and its caps (faces along Z) get
a plane of their own; a sphere unrolls to arc length by latitude arc. Each group is moved STEP
apart, so neighbours in different groups never share a UV and become one island. A density target
scales the packed islands down to it and packs them again without scaling; one that does not fit
keeps the fitted layout and says what would hold it. An orchestrator unwraps only its own
objects: not what a worker built (that goes back to the worker) nor what a running worker owns.
The pure top half is tested in tests/test_uv_tools.py; harness/uv_check.py runs the rest.
"""

import math

MAX_OBJECTS = 8
MAX_FACES = 300_000      # Per object: the audit is pure Python, about 30 us a face.
RESOLUTION = 1024
PADDING = 8              # px between two islands; the tile's border gets half.
GRID = 256               # The overlap raster's cells per side: 4 px at 1024.
ANGLE_LIMIT = 1.5        # A triangle whose singular values differ by more than this is stretched.
AREA_LIMIT = 1.5         # Density off the object's median by more than this factor is area distortion.
TRIM = 0.01              # Share of 3D area left out at each end for min and max density.
WORST = 5                # Face ids listed per problem.
ZERO_UV = 1e-10          # A face with less UV area than this (tile units) has none: 1e-4 px2 at 1024.
UV_KEY = 1e6             # Corners join an island when their UVs agree to 1 / UV_KEY.
EDGE = 1e-6              # UVs this far past 0 or 1 still count as inside.
FOLD = 0.9               # An island whose raster cells cover less than this share of its UV area folds.
FOLD_MIN_CELLS = 50      # Smaller islands are too coarse on the raster to tell.
GAP_SEARCH = 4           # Gutters are measured up to this many times the padding.
NARROW = 0.5             # px: a gutter counts as under the padding only past this; packing lands on it exactly.
PACKING = 0.75           # Share of a tile pack_islands fills with rotation, typically.
SIZES = (256, 512, 1024, 2048, 4096, 8192)
TEXEL_BYTES = 4 * 4 / 3  # RGBA8 with mipmaps.
ASSUMED_ISLANDS = 6
METHODS = ("smart", "cube", "cylinder", "sphere", "seams", "angle_seams")
SMART_ANGLE = 66.0       # smart_project's own default, degrees.
SEAM_ANGLE = 30.0
CHECKER = "lc_uv_checker"
CAP = 0.7                # A cylinder's face whose normal is at least this much along Z is a cap.
POLE = 0.999             # A sphere's corner this close to its axis takes its longitude from the others.

# Offered to the single loop only: the orchestrator and workers keep their fixed-token caps, and an orchestrator
# does not rework a worker's prop (a worker re-UVing a painted import would need it added there too).
SINGLE_ONLY = {"transfer_textures"}
SCHEMAS = [  # Sent with every request: every word here is paid for on every step.
    {"type": "function", "function": {
        "name": "inspect_uv",
        "title": "Checking texture mapping",
        "description": (
            "UV audit, read-only: per object islands, faces outside 0-1, zero-area and flipped faces, overlaps, "
            "stretch, texel density (px/m at resolution), gutters, worst face ids. Before painting or blaming a "
            "picture. checker: plus a checker image. plan: atlas size, coverage, memory for a density instead."),
        "parameters": {"type": "object", "properties": {
            "objects": {"type": "array", "items": {"type": "string"}},
            "uv_map": {"type": "string", "description": "Default: the render map"},
            "resolution": {"type": "integer", "description": "Texture px, default 1024"},
            "padding": {"type": "integer", "description": "px between islands, default 8"},
            "density": {"type": "number", "description": "Target px/m"},
            "checker": {"type": "boolean"},
            "plan": {"type": "boolean"},
        }, "required": ["objects"]},
    }},
    {"type": "function", "function": {
        "name": "uv_unwrap",
        "title": "Preparing texture mapping",
        "description": (
            "Unwrap meshes you own into 0-1, one texel density, packed with padding; returns inspect_uv's audit. "
            "method: smart (by angle), cube/cylinder/sphere (about local Z), seams (marked ones), angle_seams (mark "
            "where faces meet above angle, then unwrap)."),
        "parameters": {"type": "object", "properties": {
            "objects": {"type": "array", "items": {"type": "string"}},
            "method": {"type": "string", "enum": list(METHODS)},
            "angle": {"type": "number", "description": "Degrees: smart 66, angle_seams 30"},
            "uv_map": {"type": "string", "description": "Made if missing"},
            "resolution": {"type": "integer"},
            "padding": {"type": "integer"},
            "density": {"type": "number", "description": "px/m, if it fits"},
            "atlas": {"type": "boolean", "description": "One layout for all (one texture)"},
        }, "required": ["objects", "method"]},
    }},
    {"type": "function", "function": {
        "name": "transfer_textures",
        "title": "Copying textures",
        "description": (
            "Images follow the UV map they read: a new layout moves the artwork. Bakes the images the materials "
            "read through from_uv (the painted layout) onto to_uv, same size and colour space, normal maps for "
            "to_uv's tangents; repoints the materials; keeps from_uv; skips tiling ones."),
        "parameters": {"type": "object", "properties": {
            "objects": {"type": "array", "items": {"type": "string"}},
            "from_uv": {"type": "string"}, "to_uv": {"type": "string"},
        }, "required": ["objects", "from_uv", "to_uv"]},
    }},
]


# ------------------------------------------------------------------ pure

def triangle(p0, p1, p2, t0, t1, t2) -> tuple[float, float, float, float]:
    """(3D area, signed UV area, larger and smaller singular value of the map from the triangle's
    plane onto UV, in tile units per metre). The UV area is positive when the UV winding follows
    the 3D one. A triangle with no 3D area gives (0, its UV area, 0, 0)."""
    e1 = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
    e2 = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
    d1 = (t1[0] - t0[0], t1[1] - t0[1])
    d2 = (t2[0] - t0[0], t2[1] - t0[1])
    uv_area = (d1[0] * d2[1] - d2[0] * d1[1]) / 2
    n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
    twice = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
    length = math.sqrt(e1[0] * e1[0] + e1[1] * e1[1] + e1[2] * e1[2])
    if length == 0 or twice < 1e-18:
        return 0.0, uv_area, 0.0, 0.0
    # The triangle in its own plane: q1 = (length, 0), q2 = (x, y) with y > 0; J maps q1, q2 onto d1, d2.
    x = (e2[0] * e1[0] + e2[1] * e1[1] + e2[2] * e1[2]) / length
    y = twice / length
    a, c = d1[0] / length, d1[1] / length
    b, d = (d2[0] - a * x) / y, (d2[1] - c * x) / y
    q, r = math.hypot((a + d) / 2, (c - b) / 2), math.hypot((a - d) / 2, (c + b) / 2)
    return twice / 2, uv_area, q + r, abs(q - r)


def quantile(pairs: list, q: float) -> float:
    """The value at share q of the total weight, pairs (value, weight) sorted by value."""
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        return 0.0
    reach, running = q * total, 0.0
    for value, weight in pairs:
        running += weight
        if running >= reach:
            return value
    return pairs[-1][0]


def _key(vertex: int, uv) -> tuple:
    return vertex, round(uv[0] * UV_KEY), round(uv[1] * UV_KEY)


def islands(faces: list, uvs: list) -> tuple[list[int], int]:
    """(island per face, count): faces that share a UV corner (a vertex at the same UV) are one
    island, as Blender's UV editor selects linked. Islands are numbered in face order."""
    parent = list(range(len(faces)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first = {}
    for index, (face, uv) in enumerate(zip(faces, uvs)):
        for vertex, corner in zip(face, uv):
            other = first.setdefault(_key(vertex, corner), index)
            if other != index:
                a, b = root(index), root(other)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    labels, numbers = [], {}
    for index in range(len(faces)):
        labels.append(numbers.setdefault(root(index), len(numbers)))
    return labels, len(numbers)


def cover(t0, t1, t2, grid: int = GRID):
    """The cells of a grid x grid raster of the 0-1 tile whose centres the UV triangle covers,
    edges included; outside the tile nothing."""
    xs, ys = (t0[0], t1[0], t2[0]), (t0[1], t1[1], t2[1])
    i0, i1 = max(0, math.ceil(min(xs) * grid - 0.5)), min(grid - 1, math.floor(max(xs) * grid - 0.5))
    j0, j1 = max(0, math.ceil(min(ys) * grid - 0.5)), min(grid - 1, math.floor(max(ys) * grid - 0.5))
    area = (t1[0] - t0[0]) * (t2[1] - t0[1]) - (t2[0] - t0[0]) * (t1[1] - t0[1])
    if i0 > i1 or j0 > j1 or area == 0:
        return
    sign = 1.0 if area > 0 else -1.0
    edges = [(a, b) for a, b in ((t0, t1), (t1, t2), (t2, t0))]
    for j in range(j0, j1 + 1):
        v = (j + 0.5) / grid
        for i in range(i0, i1 + 1):
            u = (i + 0.5) / grid
            if all(sign * ((b[0] - a[0]) * (v - a[1]) - (b[1] - a[1]) * (u - a[0])) >= 0 for a, b in edges):
                yield j * grid + i


def overlap(triangles_by_island: dict, grid: int = GRID) -> dict:
    """{cells: covered by any island, shared: covered by two or more, pairs: [(a, b)] islands that
    share a cell, folded: [islands whose cells fall short of their UV area]} on the raster."""
    owners, pairs, folded = {}, set(), []
    for island, tris in triangles_by_island.items():
        mine = set()
        for t0, t1, t2 in tris:
            mine.update(cover(t0, t1, t2, grid))
        for cell in mine:
            present = owners.setdefault(cell, [])
            pairs.update((min(other, island), max(other, island)) for other in present)
            present.append(island)
        expected = sum(abs((t1[0] - t0[0]) * (t2[1] - t0[1]) - (t2[0] - t0[0]) * (t1[1] - t0[1])) / 2
                       for t0, t1, t2 in tris if _inside(t0) and _inside(t1) and _inside(t2)) * grid * grid
        if expected >= FOLD_MIN_CELLS and len(mine) < FOLD * expected:
            folded.append(island)
    return {"cells": len(owners), "shared": sum(1 for present in owners.values() if len(present) > 1),
            "pairs": sorted(pairs), "folded": folded}


def _inside(uv) -> bool:
    return -EDGE <= uv[0] <= 1 + EDGE and -EDGE <= uv[1] <= 1 + EDGE


def _point_segment(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segment_distance(a, b, c, d) -> float:
    """The shortest distance between segments ab and cd in the plane; 0 when they cross or touch."""
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return 0.0  # They cross; touching ends and overlapping lines come out 0 below.
    return min(_point_segment(a, c, d), _point_segment(b, c, d), _point_segment(c, a, b), _point_segment(d, a, b))


def boundary(co: list, faces: list, uvs: list, labels: list) -> tuple[list, float]:
    """([(island, uv_a, uv_b)] the UV edges only one face uses, the total 3D length of those edges in
    m): each island's outline, holes and seams included."""
    count, where = {}, {}
    for index, (face, uv) in enumerate(zip(faces, uvs)):
        n = len(face)
        for k in range(n):
            a, b = _key(face[k], uv[k]), _key(face[(k + 1) % n], uv[(k + 1) % n])
            key = (a, b) if a <= b else (b, a)
            count[key] = count.get(key, 0) + 1
            where[key] = (labels[index], uv[k], uv[(k + 1) % n], face[k], face[(k + 1) % n])
    segments, length = [], 0.0
    for key, n in count.items():
        if n == 1:
            island, ua, ub, va, vb = where[key]
            segments.append((island, ua, ub))
            length += math.dist(co[va], co[vb])
    return segments, length


def gutters(segments: list, limit: float) -> dict:
    """{(island a, island b): the closest their outlines come, when under limit}, in UV units. The
    segments go into a hash grid of limit-sized cells, each into every cell its box grown by
    limit / 2 touches, so two within limit always share one."""
    if limit <= 0:
        return {}
    cells = {}
    for index, (_, a, b) in enumerate(segments):
        half = limit / 2
        x0, x1 = math.floor((min(a[0], b[0]) - half) / limit), math.floor((max(a[0], b[0]) + half) / limit)
        y0, y1 = math.floor((min(a[1], b[1]) - half) / limit), math.floor((max(a[1], b[1]) + half) / limit)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 4096:
            continue  # An edge longer than 64 cells is not where islands come close; skip it rather than stall.
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                cells.setdefault((x, y), []).append(index)
    closest, seen = {}, set()
    for members in cells.values():
        for i, first in enumerate(members):
            island_a, a0, a1 = segments[first]
            for second in members[i + 1:]:
                island_b, b0, b1 = segments[second]
                if island_a == island_b or (first, second) in seen:
                    continue
                seen.add((first, second))
                distance = segment_distance(a0, a1, b0, b1)
                pair = (min(island_a, island_b), max(island_a, island_b))
                if distance < limit and distance < closest.get(pair, limit):
                    closest[pair] = distance
    return closest


def audit(co: list, faces: list, uvs: list, resolution: int = RESOLUTION, padding: float = PADDING,
          grid: int = GRID) -> dict:
    """Everything inspect_uv says about one object's UV map: co are world-space vertex positions,
    faces their indices per face, uvs each face's corners' UVs. See the module docstring."""
    labels, count = islands(faces, uvs)
    first_face = {}
    for index, island in enumerate(labels):
        first_face.setdefault(island, index)
    triangles, zero, flipped, outside, tiles = {}, [], [], [], set()
    rows = []  # (density in tile units per metre, 3D area, angle ratio, face)
    area3_total = uv_total = 0.0
    worst = []
    for index, (face, uv) in enumerate(zip(faces, uvs)):
        face_area3 = face_uv = face_abs = 0.0
        face_worst, face_rows = 1.0, []
        for k in range(1, len(face) - 1):
            t = (uv[0], uv[k], uv[k + 1])
            area3, signed, high, low = triangle(co[face[0]], co[face[k]], co[face[k + 1]], *t)
            triangles.setdefault(labels[index], []).append(t)
            face_area3 += area3
            face_uv += signed
            face_abs += abs(signed)
            if area3 > 0:
                ratio = high / low if low > 0 else math.inf
                face_rows.append((math.sqrt(abs(signed) / area3), area3, ratio, index))
                face_worst = max(face_worst, ratio)
        area3_total += face_area3
        uv_total += face_abs
        if face_area3 > 0 and face_abs <= ZERO_UV:
            zero.append(index)  # Said once as zero-area, not again as endless stretch.
        else:
            rows += face_rows
            if face_uv < 0:
                flipped.append(index)
        if not all(_inside(c) for c in uv):
            outside.append(index)
        cu, cv = sum(c[0] for c in uv) / len(uv), sum(c[1] for c in uv) / len(uv)
        if not (0 <= cu < 1 and 0 <= cv < 1):
            tiles.add(1001 + math.floor(cu) + 10 * math.floor(cv) if 0 <= cu < 10 and cv >= 0 else "outside the UDIM range")
        if face_area3 > 0 and face_abs > ZERO_UV:
            worst.append((face_worst, index))
    density = sorted((d, w) for d, w, _, _ in rows if d > 0)
    median = quantile(density, 0.5)
    weight = sum(w for _, w, _, _ in rows) or 1.0
    angle_mean = sum(min(r, 10.0) * w for _, w, r, _ in rows) / weight
    angle_share = sum(w for _, w, r, _ in rows if r > ANGLE_LIMIT) / weight
    area_share = sum(w for d, w, _, _ in rows if median > 0 and not 1 / AREA_LIMIT <= d / median <= AREA_LIMIT) / weight
    raster = overlap(triangles, grid)
    segments, boundary_length = boundary(co, faces, uvs, labels)
    limit = max(1.0, padding) * GAP_SEARCH / resolution
    closest = gutters(segments, limit)
    corners = [c for uv in uvs for c in uv]
    inside = corners and not outside
    border = min(min(c[0], 1 - c[0], c[1], 1 - c[1]) for c in corners) * resolution if inside else None
    worst.sort(reverse=True)
    return {
        "faces": len(faces), "islands": count, "area": area3_total, "uv_area": uv_total, "boundary": boundary_length,
        "used": raster["cells"] / (grid * grid), "outside": outside, "tiles": sorted(tiles, key=str),
        "bounds": [min(c[0] for c in corners), min(c[1] for c in corners), max(c[0] for c in corners),
                   max(c[1] for c in corners)] if corners else None,
        "zero": zero, "flipped": flipped,
        "overlap": raster["shared"] / raster["cells"] if raster["cells"] else 0.0,
        "overlapping": [(first_face[a], first_face[b]) for a, b in raster["pairs"]],
        "folded": [first_face[i] for i in raster["folded"]],
        "angle": angle_mean, "angle_share": angle_share, "area_share": area_share,
        "worst": [(face, ratio) for ratio, face in worst[:WORST] if ratio > ANGLE_LIMIT],
        "density": [quantile(density, TRIM) * resolution, median * resolution, quantile(density, 1 - TRIM) * resolution],
        "gutter": min(((d * resolution, first_face[a], first_face[b]) for (a, b), d in closest.items()), default=None),
        "narrow": sum(1 for d in closest.values() if d * resolution < padding - NARROW),
        "search": limit * resolution, "border": border,
    }


def _faces(ids: list) -> str:
    shown = ", ".join(str(i) for i in ids[:WORST])
    return shown + (f" (+{len(ids) - WORST} more)" if len(ids) > WORST else "")


def _pct(share: float) -> str:
    return f"{share * 100:.0f}%" if share >= 0.01 or share == 0 else f"{share * 100:.1f}%"


def object_lines(name: str, uv_map: str, stats: dict, padding: float, density: float | None = None,
                 note: str = "") -> list[str]:
    """One object's audit as the model reads it: a head line, then one line per measure."""
    s = stats
    head = f"{name} ({uv_map}{note}): {s['faces']:,} faces, {s['islands']} island{'s' * (s['islands'] != 1)}, " \
           f"{_pct(s['used'])} of the tile covered."
    if s["outside"]:
        u0, v0, u1, v1 = s["bounds"]
        tiles = f"; tiles {', '.join(map(str, s['tiles']))}" if s["tiles"] else ""
        placed = f"outside 0-1: {len(s['outside'])} faces (u {u0:.2f}..{u1:.2f}, v {v0:.2f}..{v1:.2f}{tiles}): " \
                 f"{_faces(s['outside'])}"
    else:
        placed = "all inside 0-1"
    lines = [head, f"  {placed} | zero-area: {_faces(s['zero']) or 'none'} | flipped: {_faces(s['flipped']) or 'none'}"]
    if s["overlapping"] or s["folded"]:
        pairs = ", ".join(f"{a}/{b}" for a, b in s["overlapping"][:WORST])
        more = f" (+{len(s['overlapping']) - WORST} more)" if len(s["overlapping"]) > WORST else ""
        folded = f"; folded onto itself: islands at faces {_faces(s['folded'])}" if s["folded"] else ""
        shared = (f"{_pct(s['overlap'])} of covered cells, islands at faces {pairs}{more}" if s["overlapping"]
                  else "none between islands")
        lines.append(f"  overlap: {shared}{folded}")
    else:
        lines.append("  overlap: none")
    worst = ", ".join(f"{face} ({ratio:.1f}x)" if ratio < 100 else f"{face} (collapsed)" for face, ratio in s["worst"])
    lines.append(f"  stretch: angle {s['angle']:.2f} mean (1.00 = none), {_pct(s['angle_share'])} of area over "
                 f"{ANGLE_LIMIT:g}x; area {_pct(s['area_share'])} of area {AREA_LIMIT:g}x off the median density"
                 + (f"; worst faces {worst}" if worst else ""))
    low, median, high = s["density"]
    target = f"; median {median / density:.2f}x the {density:g} target" if density else ""
    lines.append(f"  density px/m: {low:.0f} / {median:.0f} / {high:.0f} (min / median / max){target}")
    if s["gutter"] is None:
        gutter = f"no islands within {s['search']:.0f} px of each other"
    else:
        distance, a, b = s["gutter"]
        gutter = f"min {distance:.1f} px (islands at faces {a}/{b}), {s['narrow']} pair{'s' * (s['narrow'] != 1)} " \
                 f"under {padding:g} px"
    border = f"; border {s['border']:.1f} px" if s["border"] is not None else ""
    lines.append(f"  gutter: {gutter}{border}" if s["islands"] > 1 else f"  gutter: one island{border}")
    return lines


def across(medians: dict) -> str:
    """How far apart the objects' median densities are: one texture set should read alike."""
    medians = {name: m for name, m in medians.items() if m > 0}
    if len(medians) < 2:
        return ""
    high, low = max(medians, key=medians.get), min(medians, key=medians.get)
    return (f"Across objects: median density {medians[high]:.0f} px/m ({high}) to {medians[low]:.0f} ({low}), "
            f"{medians[high] / medians[low]:.1f}x apart.")


def surface(co: list, faces: list) -> float:
    """World-space area in m2 of faces over co, for an object with no UV map yet."""
    return sum(triangle(co[f[0]], co[f[k]], co[f[k + 1]], (0, 0), (0, 0), (0, 0))[0]
               for f in faces for k in range(1, len(f) - 1))


def texels(area: float, boundary_length: float, density: float, padding: float) -> tuple[float, float]:
    """(texels the islands cover, texels their gutters take) at density px/m: each island gets half
    the padding along its outline."""
    return area * density * density, boundary_length * density * padding / 2


def max_density(area: float, boundary_length: float, size: int, padding: float) -> float:
    """The most px/m that area (m2) with this much island outline fits into one size x size texture."""
    if area <= 0:
        return 0.0
    b = boundary_length * padding / 2
    return (-b + math.sqrt(b * b + 4 * area * PACKING * size * size)) / (2 * area)


def _smallest(needed: float) -> int | None:
    return next((size for size in SIZES if needed <= PACKING * size * size), None)


def _mb(size: int) -> str:
    megabytes = size * size * TEXEL_BYTES / 2 ** 20
    return f"{megabytes:.0f} MB" if size >= 512 else f"{megabytes:.1f} MB"


def atlas_plan(items: list[dict], density: float | None, padding: float, resolution: int) -> list[str]:
    """The planner's lines. items: {name, area (m2), boundary (m of island outline, None before any
    unwrap), islands}. No density: the most the objects hold together at resolution."""
    items = [dict(item) for item in items]
    for item in items:
        if item.get("boundary") is None:  # Cube-like: ASSUMED_ISLANDS squares of equal area.
            item.update(boundary=4 * ASSUMED_ISLANDS * math.sqrt(item["area"] / ASSUMED_ISLANDS),
                        islands=ASSUMED_ISLANDS, assumed=True)
    area = sum(i["area"] for i in items)
    outline = sum(i["boundary"] for i in items)
    if area <= 0:
        return ["Nothing to plan: these objects have no surface area."]
    lines = []
    if not density:
        density = max_density(area, outline, resolution, padding)
        lines.append(f"At {resolution} the most they hold together: {density:.0f} px/m.")
    lines.append(f"Plan for {density:.0f} px/m with {padding:g} px padding (pack_islands fills about "
                 f"{PACKING:.0%} of a tile):")
    for item in items:
        cover_, gutter = texels(item["area"], item["boundary"], density, padding)
        alone = _smallest(cover_ + gutter)
        where = f"{alone} alone ({_pct(cover_ / alone ** 2)} covered)" if alone else f"more than {SIZES[-1]} alone"
        islands_text = f"{item['islands']} islands" + (" assumed (no UVs yet)" if item.get("assumed") else "")
        lines.append(f"- {item['name']}: {item['area']:.2f} m2, {islands_text}: {cover_ / 1e6:.2f} Mpx of islands + "
                     f"{gutter / 1e6:.2f} of gutters; {where}")
    cover_, gutter = texels(area, outline, density, padding)
    fits = _smallest(cover_ + gutter)
    if len(items) > 1 or fits is None:
        if fits is None:
            lines.append(f"Shared atlas: more than one {SIZES[-1]} holds; split it into several textures, or lower "
                         f"the density ({SIZES[-1]} holds {max_density(area, outline, SIZES[-1], padding):.0f} px/m).")
        else:
            options = [f"{fits} fits ({_pct(cover_ / fits ** 2)} covered, gutters {_pct(gutter / fits ** 2)}), {_mb(fits)}"]
            if fits > SIZES[0]:
                below = fits // 2
                options.append(f"{below} holds {max_density(area, outline, below, padding):.0f} px/m at most")
            if fits < SIZES[-1]:
                options.append(f"{fits * 2} would be {_pct(cover_ / (fits * 2) ** 2)} covered, {_mb(fits * 2)}")
            lines.append("Shared atlas: " + "; ".join(options) + " (RGBA8 with mips, per map).")
    if gutter > 0.25 * cover_:
        count = sum(i["islands"] for i in items)
        lines.append(f"Fragmented: gutters take {gutter / cover_:.0%} of the islands' texels ({count} islands). Fewer, "
                     f"larger islands (smart with a larger angle, or angle_seams) give them back.")
    return lines


_BOX = {  # Direction the normal points at most -> (u, v) from (x, y, z), seen from outside.
    (0, 1): lambda p: (p[1], p[2]), (0, -1): lambda p: (-p[1], p[2]), (1, 1): lambda p: (-p[0], p[2]),
    (1, -1): lambda p: (p[0], p[2]), (2, 1): lambda p: (p[0], p[1]), (2, -1): lambda p: (p[0], -p[1])}


def project(method: str, corners: list, normal, centre, step: float) -> list[tuple[float, float]]:
    """A face's UVs, in metres, for cube, cylinder or sphere: corners and normal in the object's
    frame (scaled), about Z through centre. Each group (a box direction, a cap, the side) sits step
    apart in u. See the module docstring."""
    local = [(c[0] - centre[0], c[1] - centre[1], c[2] - centre[2]) for c in corners]
    if method == "cube" or (method == "cylinder" and abs(normal[2]) >= CAP * math.sqrt(sum(n * n for n in normal))):
        axis = 2 if method == "cylinder" else max(range(3), key=lambda k: abs(normal[k]))
        side = 1 if normal[axis] >= 0 else -1
        group = 2 * axis + (side < 0) + 1
        return [(u + group * step, v) for u, v in map(_BOX[axis, side], local)]
    if method == "sphere":
        spans = [math.sqrt(x * x + y * y + z * z) for x, y, z in local]
        polar = [s == 0 or abs(z) / s >= POLE for (_, _, z), s in zip(local, spans)]
    else:
        spans = [math.hypot(x, y) for x, y, _ in local]
        polar = [s == 0 for s in spans]
    turns = [math.atan2(y, x) for x, y, _ in local]
    known = [t for t, p in zip(turns, polar) if not p] or [0.0]
    if max(known) - min(known) > math.pi:  # The face straddles the back seam: one side goes round once more.
        turns = [t + 2 * math.pi if t < 0 else t for t in turns]
        known = [t + 2 * math.pi if t < 0 else t for t in known]
    turns = [sum(known) / len(known) if p else t for t, p in zip(turns, polar)]
    if method == "sphere":
        return [(t * s, math.asin(max(-1.0, min(1.0, z / s))) * s if s else 0.0)
                for t, s, (_, _, z) in zip(turns, spans, local)]
    return [(t * s, z) for t, s, (_, _, z) in zip(turns, spans, local)]


def describe(name: str, arguments: dict) -> tuple[str, str] | None:
    """(summary, code) for the tool card."""
    if name not in ("inspect_uv", "uv_unwrap", "transfer_textures"):
        return None
    objects = arguments.get("objects")
    subject = ", ".join(map(str, objects[:3])) + (f", +{len(objects) - 3} more" if len(objects) > 3 else "") \
        if isinstance(objects, list) and objects else "?"
    if name == "uv_unwrap":
        return f"UV unwrap {subject} ({arguments.get('method', '?')})", ""
    if name == "transfer_textures":
        return f"Move the textures of {subject} onto {arguments.get('to_uv', '?')}", ""
    if arguments.get("plan"):
        return f"Plan a texture atlas for {subject}", ""
    return f"Measure the UVs of {subject}" + (" with a checker" if arguments.get("checker") else ""), ""


# ------------------------------------------------------------------ Blender

def _named(objects) -> list:
    """The objects named, in order, once each."""
    import bpy
    from .tools import ToolError
    if isinstance(objects, str):
        objects = [objects]
    if not isinstance(objects, list) or not objects or not all(isinstance(n, str) for n in objects):
        raise ToolError("objects: a list of object names")
    if len(objects) > MAX_OBJECTS:
        raise ToolError(f"At most {MAX_OBJECTS} objects per call.")
    missing = [n for n in objects if n not in bpy.data.objects]
    if missing:
        raise ToolError(f"No such object(s): {', '.join(missing)}")
    return list(dict.fromkeys(bpy.data.objects[n] for n in objects))


def _settings(resolution, padding, density) -> tuple[int, float, float | None]:
    from .tools import ToolError
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    if not number(resolution) or not 16 <= resolution <= 16384:
        raise ToolError("resolution is the texture's size in px, 16 to 16384")
    if not number(padding) or not 0 <= padding <= 256:
        raise ToolError("padding is in px between islands, 0 to 256")
    if density is not None and (not number(density) or density <= 0):
        raise ToolError("density is a target in px per metre, above 0")
    return int(resolution), float(padding), float(density) if density else None


def _layer(obj, uv_map: str = ""):
    """The UV map asked for, else the one that renders, else the first; None when there is none."""
    layers = obj.data.uv_layers
    if uv_map:
        return layers.get(uv_map)
    return next((layer for layer in layers if layer.active_render), None) or (layers[0] if len(layers) else None)


def _gather(obj, uv_map: str = "") -> tuple[list, list, list]:
    """(world-space vertex positions, each face's vertex indices, each face's corner UVs on the map of
    that name) of the base mesh; UVs all 0 without one. By name: a mode switch rebuilds the mesh's
    layers, and a UV layer held across one reads freed memory (it crashed Blender 5.2 here). An
    object in Edit Mode is read from its edit mesh: its data's UV layers are empty meanwhile, even
    after update_from_editmode (5.2, measured), and the user's mode is theirs."""
    import numpy as np
    mesh = obj.data
    if obj.mode == "EDIT":
        import bmesh
        edit = bmesh.from_edit_mesh(mesh)
        edit.verts.index_update()
        layer = edit.loops.layers.uv.get(uv_map) if uv_map else None
        matrix = obj.matrix_world
        world = [tuple(matrix @ v.co) for v in edit.verts]
        faces = [[loop.vert.index for loop in face.loops] for face in edit.faces]
        return world, faces, [[tuple(loop[layer].uv) if layer else (0.0, 0.0) for loop in face.loops]
                              for face in edit.faces]
    count, corners, polygons = len(mesh.vertices), len(mesh.loops), len(mesh.polygons)
    co = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    world = (co.reshape(count, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]).tolist()
    vertex = np.empty(corners, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", vertex)
    uv = np.zeros(corners * 2, dtype=np.float32)
    if uv_map:
        mesh.uv_layers[uv_map].uv.foreach_get("vector", uv)
    starts, totals = np.empty(polygons, dtype=np.int32), np.empty(polygons, dtype=np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    mesh.polygons.foreach_get("loop_total", totals)
    vertex, uv = vertex.tolist(), [tuple(pair) for pair in uv.reshape(corners, 2).tolist()]
    spans = list(zip(starts.tolist(), totals.tolist()))
    return world, [vertex[s:s + t] for s, t in spans], [uv[s:s + t] for s, t in spans]


def measure(obj) -> tuple[str, dict] | None:
    """(map name, audit) of a mesh's render UV map at RESOLUTION and PADDING, for facts.py; None
    without one. Read by name, in the object's own mode (_gather)."""
    if obj.mode == "EDIT":
        import bmesh
        layer = bmesh.from_edit_mesh(obj.data).loops.layers.uv.active  # Its data's layers read empty.
    else:
        layer = _layer(obj)
    if layer is None:
        return None
    name = layer.name
    return name, audit(*_gather(obj, name))


def _combined(objects, layers: dict) -> tuple[list, list, list]:
    """Several objects' meshes as one, for a layout they share. layers: object -> UV map name."""
    co, faces, uvs = [], [], []
    for obj in objects:
        world, own, corners = _gather(obj, layers[obj])
        faces += [[v + len(co) for v in face] for face in own]
        co += world
        uvs += corners
    return co, faces, uvs


def _note(obj, uv_map: str) -> str:
    """What the head line says beside the map's name: the other maps, the modifiers not measured."""
    others = [m.name for m in obj.data.uv_layers if m.name != uv_map]
    modifiers = [m.name for m in obj.modifiers if m.show_render]
    return (f"; also {', '.join(others)}" if others else "") + (
        f"; base mesh, before {', '.join(modifiers[:3])}" if modifiers else "")


def inspect_uv(objects: list, uv_map: str = "", resolution: int = RESOLUTION, padding: int = PADDING,
               density: float | None = None, checker: bool = False, plan: bool = False):
    """The tool (see SCHEMAS and the module docstring)."""
    from .tools import ToolError, ToolResult
    targets = _named(objects)
    resolution, padding, density = _settings(resolution, padding, density)
    lines, medians, items, shown = [], {}, [], []
    for obj in targets:
        if obj.type != "MESH":
            lines.append(f"{obj.name}: not a mesh ({obj.type}).")
            continue
        layer = _layer(obj, uv_map)
        if len(obj.data.polygons) > MAX_FACES:
            lines.append(f"{obj.name}: {len(obj.data.polygons):,} faces, more than {MAX_FACES:,} to measure in one "
                         f"call; maps: {', '.join(m.name for m in obj.data.uv_layers) or 'none'}.")
            continue
        if layer is None:
            maps = [m.name for m in obj.data.uv_layers]
            if plan and not maps:  # Planned with islands assumed, which its line says.
                items.append({"name": obj.name, "area": surface(*_gather(obj)[:2]), "boundary": None, "islands": 0})
                continue
            lines.append(f"{obj.name}: " + (f"no UV map named {uv_map!r} (it has {', '.join(maps)})" if maps else
                                             "no UV map; uv_unwrap makes one") + ".")
            continue
        stats = audit(*_gather(obj, layer.name), resolution, padding)
        medians[obj.name] = stats["density"][1]
        shown.append((obj, layer.name))
        if plan:
            items.append({"name": obj.name, "area": stats["area"], "boundary": stats["boundary"],
                          "islands": stats["islands"]})
        else:
            lines += object_lines(obj.name, layer.name, stats, padding, density, _note(obj, layer.name))
    if plan:
        lines += atlas_plan(items, density, padding, resolution) if items else []
    else:
        lines.insert(0, f"UV audit at {resolution} px, padding {padding:g} px. Overlap: islands rasterized on a "
                        f"{GRID}x{GRID} grid by texel centre; stacked or mirrored islands count. Face ids are the "
                        f"base mesh's.")
        spread = across(medians)
        if spread:
            lines.append(spread)
    result = ToolResult("\n".join(lines))
    if checker:
        if not shown:
            raise ToolError("No checker: none of these objects has that UV map.")
        result.image_path = _checker(shown)
        maps = sorted({name for _, name in shown})
        result.text += (f"\n\nImage attached: {', '.join(o.name for o, _ in shown)} wearing a temporary checker on "
                        f"{', '.join(maps)} (Workbench texture colour, studio light), three-quarter view. Squares "
                        f"should stay square and alike in size and letters read the right way round; a jump in the "
                        f"pattern is a seam. Nothing of the scene changed.")
    return result


def _checker(pairs: list):
    """The objects wearing a temporary checker on their UV map, rendered; every slot, active map and
    datablock put back. pairs: (object, UV map name)."""
    import bpy
    from .ownership import TEMP
    image = bpy.data.images.new(CHECKER, 1024, 1024)
    image.generated_type = "COLOR_GRID"
    image[TEMP] = True
    material = bpy.data.materials.new(CHECKER)
    material[TEMP] = True
    tree = material.node_tree
    shader = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image, texture.interpolation = image, "Closest"
    tree.links.new(texture.outputs["Color"], shader.inputs["Base Color"])
    tree.nodes.active = texture  # Workbench's texture colour shows the active image node.
    slots, active, appended = [], [], []
    try:
        for obj, name in pairs:
            mesh = obj.data
            active.append((mesh, mesh.uv_layers.active_index))
            mesh.uv_layers.active = mesh.uv_layers[name]  # Workbench reads the active map, not the render one.
            if not obj.material_slots:
                mesh.materials.append(material)
                appended.append(mesh)
            for slot in obj.material_slots:
                slots.append((slot, slot.material))
                slot.material = material
        bpy.context.view_layer.update()  # An offscreen draw shows the evaluated scene, which does not update itself.
        return _checker_look([obj for obj, _ in pairs])
    finally:  # In reverse: a mesh two objects share was saved twice, the second time already changed.
        for slot, previous in reversed(slots):
            slot.material = previous
        for mesh in appended:
            mesh.materials.clear()  # pop() leaves the object's slot behind; clear() takes it (Blender 5.2).
        for mesh, index in reversed(active):
            mesh.uv_layers.active_index = index
        bpy.data.materials.remove(material)
        bpy.data.images.remove(image)
        bpy.context.view_layer.update()


def _checker_look(objects):
    """The image's path. A background Blender (a worker's child) renders a Workbench look through
    worker_capture.look(angle="final"), which frames the focus and leaves the colour type alone; the
    user's Blender draws offscreen with capture_viewport's helpers, the viewport untouched."""
    import bpy
    from . import scratch, tools
    from .tools import ToolError
    scene = bpy.context.scene
    if bpy.app.background:
        from . import worker_capture
        if not worker_capture.configured():
            raise ToolError("No checker here: a background Blender draws it only in a worker's Blender.")
        shading = scene.display.shading
        settings = [(scene.render, "engine", "BLENDER_WORKBENCH"), (shading, "light", "STUDIO"),
                    (shading, "color_type", "TEXTURE"), (shading, "show_cavity", False),
                    (shading, "show_shadows", False), (shading, "show_object_outline", False)]
        saved = [(owner, attr, getattr(owner, attr)) for owner, attr, _ in settings]
        try:
            for owner, attr, value in settings:
                setattr(owner, attr, value)
            return worker_capture.look(angle="final", focus=[o.name for o in objects]).image_path
        finally:
            for owner, attr, value in reversed(saved):
                setattr(owner, attr, value)
    with tools._editor_context("VIEW_3D"):
        override = tools._view3d_override()
        region, space = override["region"], override["area"].spaces.active
        shading, overlay = space.shading, space.overlay
        saved = (shading.type, shading.color_type, shading.light, overlay.show_overlays)
        try:
            shading.type, shading.color_type, shading.light = "SOLID", "TEXTURE", "STUDIO"
            overlay.show_overlays = False
            width, height = tools.CAPTURE_WIDTH, tools.CAPTURE_WIDTH * 3 // 4
            view, projection, _ = tools._framed_view(space, objects, "three_quarter", width, height)
            # No names, as in a worker's final look: they would cover the checker being judged.
            pixels = tools._draw_offscreen(scene, space, region, view, projection, width, height, [])
        finally:
            shading.type, shading.color_type, shading.light, overlay.show_overlays = saved
    path = scratch.folder() / "uv_checker.png"
    tools._save_pixels(pixels, path)
    return path


def _not_yours(objects, tool: str = "uv_unwrap") -> str:
    """Why an orchestrator may not unwrap these, or "": what a worker built goes back to that worker
    (the orchestrator fixing a worker's prop is a failure), and a running worker's objects are its
    own until they merge. A single agent and a worker's child have no workers to ask."""
    from . import orchestrator, state, waves
    session = state.session()
    turn = session.get("turn")
    roles = getattr(turn, "roles", None)
    if roles is None or not getattr(roles, "orchestrate", False):
        return ""
    built = orchestrator.worker_built(session)
    running = {name: wid for wid, brief in waves.busy(getattr(turn, "job", None)).items()
               for name in [brief["deliverable"], *brief["owns"]]}
    problems = []
    for obj in objects:
        names = [o.name for o in (obj, *_ancestors(obj))]
        wid = next((running[n] for n in names if n in running), None)
        if wid:
            problems.append(f"{obj.name} is running worker {wid}'s until it merges")
        elif obj.name in built:
            problems.append(f"worker {built[obj.name]} built {obj.name}: send the {'unwrap' if tool == 'uv_unwrap' else 'job'} "
                            f"back to it (continues {built[obj.name]})")
    return (f"{tool} refused: " + "; ".join(problems) + ".") if problems else ""


def _ancestors(obj) -> list:
    found = []
    while obj.parent is not None:
        obj = obj.parent
        found.append(obj)
    return found


def _angle(angle, method: str) -> float:
    from .tools import ToolError
    if angle is None:
        return SMART_ANGLE if method == "smart" else SEAM_ANGLE
    if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not 1 <= angle <= 89:
        raise ToolError("angle is in degrees, 1 to 89")
    return float(angle)


def uv_unwrap(objects: list, method: str, angle: float | None = None, uv_map: str = "", resolution: int = RESOLUTION,
              padding: int = PADDING, density: float | None = None, atlas: bool = False):
    """The tool (see SCHEMAS and the module docstring). One undo step before it; the mode, selection
    and active object put back after."""
    import bpy
    from . import scene_diff, tools
    from .camera import _undo_push
    from .tools import ToolError, ToolResult
    targets = _named(objects)
    if method not in METHODS:
        raise ToolError(f"method must be one of {', '.join(METHODS)}")
    resolution, padding, density = _settings(resolution, padding, density)
    angle = _angle(angle, method)
    problems = [f"{o.name} is not a mesh ({o.type})" for o in targets if o.type != "MESH"]
    problems += [f"{o.name}'s mesh is linked from a library" for o in targets
                 if o.type == "MESH" and (o.library is not None or o.data.library is not None)]
    problems += [f"{o.name} is not in this view layer" for o in targets if o.name not in bpy.context.view_layer.objects]
    if problems:
        raise ToolError("uv_unwrap refused: " + "; ".join(problems) + ".")
    refusal = _not_yours(targets)
    if refusal:
        raise ToolError(refusal)
    unique, meshes = [], set()
    for obj in targets:  # A mesh two objects share is unwrapped once.
        if obj.data.session_uid not in meshes:
            meshes.add(obj.data.session_uid)
            unique.append(obj)
    before = scene_diff.snapshot()
    _undo_push("Before Loopcut: UV unwrap")
    notes, layers = [], {}
    margin = padding / (2 * resolution)
    with tools._editor_context("VIEW_3D"), _ObjectMode():
        for obj in unique:
            layer = _layer(obj, uv_map)
            if layer is None:
                layer = obj.data.uv_layers.new(name=uv_map or "UVMap")
                if layer is None:
                    raise ToolError(f"{obj.name} has as many UV maps as Blender allows; name one of them as uv_map.")
                notes.append(f"Made UV map {layer.name} on {obj.name}.")
            else:
                name = layer.name
                notes += _keep_painted_layout(obj, name)
                layer = obj.data.uv_layers[name]  # Adding a map reallocates the layers: never hold one across it.
            obj.data.uv_layers.active = layer  # The operators write to the active map.
            layers[obj] = layer.name
        for group in ([unique] if atlas else [[o] for o in unique]):
            with _Editing(group):
                notes += _project(group, method, angle, margin)
            if density:
                notes += _to_density(group, layers, density, resolution, margin)
    shared = sorted({o.name for o in bpy.data.objects if o.data is not None and o.data.session_uid in meshes
                     and o not in targets})
    if shared:
        notes.append(f"{', '.join(shared)} share{'s' * (len(shared) == 1)} a mesh with these and changed too.")
    how = f"{method} ({angle:g} deg)" if method in ("smart", "angle_seams") else method
    lines = [f"Unwrapped {', '.join(o.name for o in unique)} with {how} into "
             f"{'one shared' if atlas else 'each its own'} 0-1 layout, {padding:g} px padding at {resolution} px.",
             *notes]
    medians, trouble = {}, False
    for obj in unique:
        stats = audit(*_gather(obj, layers[obj]), resolution, padding)
        medians[obj.name] = stats["density"][1]
        trouble = trouble or bool(stats["folded"] or stats["overlapping"] or stats["zero"]) or stats["angle_share"] > 0.25
        lines += object_lines(obj.name, layers[obj], stats, padding, density, _note(obj, layers[obj]))
    if atlas and len(unique) > 1:
        stats = audit(*_combined(unique, layers), resolution, padding)
        overlap_ = f"{_pct(stats['overlap'])} of covered cells overlap" if stats["overlapping"] else "no overlap"
        lines.append(f"Shared layout: {_pct(stats['used'])} of the tile covered, {overlap_}, "
                     + (f"min gutter {stats['gutter'][0]:.1f} px." if stats["gutter"] else "gutters wide."))
    spread = across(medians)
    if spread:
        lines.append(spread)
    if trouble:
        lines.append("Folded, overlapping, zero-area or badly stretched faces: a closed tube or ball needs a seam "
                     "along it (mark one and use seams), or try smart or a projection that fits the shape.")
    return ToolResult("\n".join(lines), scene_before=before, scene_after=scene_diff.snapshot())


class _ObjectMode:
    """Object Mode for the call and the active object's mode put back after it: Edit Mode keeps the
    mesh in a copy that would overwrite, on leaving, a UV map made on the data meanwhile."""

    def __enter__(self):
        import bpy
        self.active = bpy.context.view_layer.objects.active
        self.mode = self.active.mode if self.active is not None else "OBJECT"
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        return self

    def __exit__(self, *exc):
        import bpy
        if self.active is not None and self.mode != "OBJECT" and bpy.context.view_layer.objects.active == self.active:
            try:
                bpy.ops.object.mode_set(mode=self.mode)
            except RuntimeError:
                pass
        return False


class _Editing:
    """In Object Mode: the objects in Edit Mode together, every face selected and UV selection
    synced to it; then Object Mode again, the selection, active object and hidden state put back."""

    def __init__(self, objects):
        self.objects = objects

    def __enter__(self):
        import bpy
        from .tools import ToolError
        context = bpy.context
        self.view_layer, self.tool = context.view_layer, context.scene.tool_settings
        self.active = self.view_layer.objects.active
        self.selected = [o for o in self.view_layer.objects if o.select_get()]
        self.hidden = [o for o in self.objects if o.hide_get()]
        self.sync = self.tool.use_uv_select_sync
        for obj in self.hidden:
            obj.hide_set(False)
        unseen = [o.name for o in self.objects if not o.visible_get()]
        if unseen:
            self.__exit__()
            raise ToolError(f"uv_unwrap refused: {', '.join(unseen)} hidden in the viewport (hide_viewport or its "
                            f"collection); show it first.")
        for obj in self.selected:
            obj.select_set(False)
        for obj in self.objects:
            obj.select_set(True)
        self.view_layer.objects.active = self.objects[0]
        bpy.ops.object.mode_set(mode="EDIT")
        self.tool.use_uv_select_sync = True
        bpy.ops.mesh.select_all(action="SELECT")
        return self

    def __exit__(self, *exc):
        import bpy
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        self.tool.use_uv_select_sync = self.sync
        for obj in self.objects:
            obj.select_set(False)
        for obj in self.selected:
            obj.select_set(True)
        for obj in self.hidden:
            obj.hide_set(True)
        self.view_layer.objects.active = self.active
        return False


def _project(group, method: str, angle: float, margin: float) -> list[str]:
    """In Edit Mode: the method, the UVs scaled into the tile, islands evened and packed."""
    import bmesh
    import bpy
    ops, notes = bpy.ops.uv, []
    if method == "smart":
        ops.smart_project(angle_limit=math.radians(angle), island_margin=0.0, area_weight=0.0, correct_aspect=True,
                          scale_to_bounds=False)
    elif method in ("cube", "cylinder", "sphere"):
        for obj in group:
            _projected(obj, method)
    else:
        for obj in group:
            edit = bmesh.from_edit_mesh(obj.data)
            if method == "angle_seams":
                limit = math.radians(angle)
                marked = 0
                for edge in edit.edges:
                    if len(edge.link_faces) == 2 and not edge.seam and edge.calc_face_angle(0.0) > limit:
                        edge.seam = True
                        marked += 1
                bmesh.update_edit_mesh(obj.data)
                notes.append(f"Marked {marked} seams on {obj.name}.")
            elif not any(edge.seam for edge in edit.edges):
                notes.append(f"{obj.name} has no seams marked: each connected part unwraps as one island.")
        ops.unwrap(method="ANGLE_BASED", fill_holes=True, correct_aspect=True, margin_method="FRACTION", margin=margin)
    _into_tile(group)
    ops.average_islands_scale()
    _pack(scale=True, margin=margin)
    return notes


def _pack(scale: bool, margin: float) -> None:
    """In Edit Mode: pack_islands into the first tile, quarter turns and bounding boxes (fast; see the
    module docstring)."""
    import bpy
    bpy.ops.uv.pack_islands(udim_source="CLOSEST_UDIM", rotate=True, rotate_method="CARDINAL", scale=scale,
                            margin_method="FRACTION", margin=margin, shape_method="AABB")


def _projected(obj, method: str) -> None:
    """In Edit Mode: project() for every face of obj onto its active UV map, in the object's frame
    with its scale, about Z through the centre of its bounds."""
    import bmesh
    from mathutils import Vector, geometry
    mesh = obj.data
    edit = bmesh.from_edit_mesh(mesh)
    layer = edit.loops.layers.uv.active
    scale = obj.matrix_world.to_scale()
    points = {v: (v.co.x * scale.x, v.co.y * scale.y, v.co.z * scale.z) for v in edit.verts}
    if not points:
        return
    low = [min(p[k] for p in points.values()) for k in range(3)]
    high = [max(p[k] for p in points.values()) for k in range(3)]
    centre = [(a + b) / 2 for a, b in zip(low, high)]
    step = 8 * max(max(b - a for a, b in zip(low, high)), 1e-3)
    for face in edit.faces:
        corners = [points[loop.vert] for loop in face.loops]
        normal = geometry.normal([Vector(c) for c in corners])
        for loop, uv in zip(face.loops, project(method, corners, tuple(normal), centre, step)):
            loop[layer].uv = uv
    bmesh.update_edit_mesh(mesh)


def _into_tile(group) -> None:
    """In Edit Mode: every UV of the group scaled uniformly into 0-1, so packing picks the first tile."""
    import bmesh
    edits = [(bmesh.from_edit_mesh(obj.data), obj.data) for obj in group]
    corners = []
    for edit, _ in edits:
        layer = edit.loops.layers.uv.active
        corners += [loop[layer] for face in edit.faces for loop in face.loops]
    if not corners:
        return
    u0, v0 = min(c.uv.x for c in corners), min(c.uv.y for c in corners)
    size = max(max(c.uv.x for c in corners) - u0, max(c.uv.y for c in corners) - v0)
    if size <= 0:
        return
    for corner in corners:
        corner.uv = ((corner.uv.x - u0) / size, (corner.uv.y - v0) / size)
    for _, mesh in edits:
        bmesh.update_edit_mesh(mesh)


def _to_density(group, layers: dict, target: float, resolution: int, margin: float) -> list[str]:
    """The group's packed islands scaled down to target px/m and packed again without scaling; a
    target the tile cannot hold keeps the fitted layout and says what would."""
    import numpy as np
    current = audit(*_combined(group, layers), resolution)["density"][1]
    if current <= 0:
        return []
    k = target / current
    if k > 1.02:
        needed = resolution * k
        size = next((s for s in SIZES if s >= needed), None)
        holds = f"a {size} texture would hold it" if size else f"more than {SIZES[-1]} px would hold it: split it"
        return [f"{target:g} px/m does not fit one {resolution} px tile with these islands: they fill it at "
                f"{current:.0f} px/m; {holds} (plan with inspect_uv)."]
    if k > 0.98:
        return []
    for obj in group:
        layer = obj.data.uv_layers[layers[obj]]
        uv = np.empty(len(obj.data.loops) * 2, dtype=np.float32)
        layer.uv.foreach_get("vector", uv)
        layer.uv.foreach_set("vector", uv * k)
        obj.data.update()
    with _Editing(group):
        _pack(scale=False, margin=margin)
    corners = [c for obj in group for uvs in _gather(obj, layers[obj])[2] for c in uvs]
    if all(_inside(c) for c in corners):
        return [f"Islands scaled to {target:g} px/m."]
    with _Editing(group):
        _pack(scale=True, margin=margin)
    return [f"{target:g} px/m did not pack into one tile after all; packed to fill it instead."]


def _keep_painted_layout(obj, layer_name: str) -> list[str]:
    """Before an unwrap rewrites a map that image textures read, a copy of it keeps the layout they
    were painted for, so transfer_textures can move them across (the unwrap alone scrambles them)."""
    import numpy as np
    from .texture_tools import _bindings
    mesh = obj.data
    layer = mesh.uv_layers[layer_name]
    renders = layer.active_render
    images = sorted({b["image"].name for slot in obj.material_slots if slot.material is not None
                     for b in _bindings(slot.material) if "groups" not in b and b["image"] is not None
                     and (b["uv_map"] == layer.name or (b["uv_map"] == "" and renders))})
    if not images:
        return []
    uv = np.empty(len(obj.data.loops) * 2, np.float32)
    layer.uv.foreach_get("vector", uv)
    if not len(uv) or np.ptp(uv) == 0:
        return []  # No layout to keep.
    name = f"{layer_name} before unwrap"[:63]
    if mesh.uv_layers.get(name) is not None:
        return [f"{obj.name}'s images ({', '.join(images[:4])}) were painted for {name}, kept from an earlier unwrap."]
    kept = mesh.uv_layers.new(name=name, do_init=False)
    if kept is not None:  # Copied by name: do_init does not copy the active map once there are several.
        kept_name = kept.name
        mesh.uv_layers[kept_name].uv.foreach_set("vector", uv)
    if kept is None:
        return [f"{obj.name}'s materials read {', '.join(images[:4])} through {layer_name}: this unwrap moves that "
                f"artwork on the surface, and its old layout could not be kept (too many UV maps)."]
    mesh.uv_layers[kept_name].active_render = False
    mesh.uv_layers[layer_name].active_render = renders
    mesh.uv_layers.active = mesh.uv_layers[layer_name]
    return [f"{obj.name}'s materials read {', '.join(images[:4])} through {layer_name}, so its old layout is kept as "
            f"{kept_name}: transfer_textures from {kept_name} to {layer_name} moves the artwork onto the new layout "
            f"(until then it no longer lines up)."]


# ------------------------------------------------------------------ Blender: transfer_textures

TRANSFER_SAMPLES = 4   # Per texel: a little antialiasing where the new layout shrinks an island.
TILING = 1.02          # Source UVs reaching past 0-1 by this much, or a Mapping scale above it: a texture that tiles.


def _feeds_tangent_normal(node) -> bool:
    """The image's Color goes (through reroutes) into a tangent-space Normal Map node."""
    stack, seen = [link.to_node for link in node.outputs["Color"].links], set()
    while stack:
        target = stack.pop()
        if target.as_pointer() in seen:
            continue
        seen.add(target.as_pointer())
        if target.type == "NORMAL_MAP":
            return target.space == "TANGENT"
        if target.type == "REROUTE":
            stack += [link.to_node for link in target.outputs[0].links]
    return False


def _tangent_normal_maps(node) -> list:
    found, stack, seen = [], [link.to_node for link in node.outputs["Color"].links], set()
    while stack:
        target = stack.pop()
        if target.as_pointer() in seen:
            continue
        seen.add(target.as_pointer())
        if target.type == "NORMAL_MAP" and target.space == "TANGENT":
            found.append(target)
        elif target.type == "REROUTE":
            stack += [link.to_node for link in target.outputs[0].links]
    return found


def uv_reach(obj, uv_map: str, slot: int) -> float:
    """How far past 0-1 the slot's faces reach on a UV map (0 inside the tile)."""
    import numpy as np
    mesh = obj.data
    layer = mesh.uv_layers.get(uv_map)
    if layer is None or not len(mesh.loops):
        return 0.0
    uv = np.empty(len(mesh.loops) * 2, np.float32)
    layer.uv.foreach_get("vector", uv)
    materials = np.empty(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("material_index", materials)
    starts = np.empty(len(mesh.polygons), np.int32)
    totals = np.empty(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    mesh.polygons.foreach_get("loop_total", totals)
    mine = np.zeros(len(mesh.loops), bool)
    for start, total in zip(starts[materials == slot], totals[materials == slot]):
        mine[start:start + total] = True
    if not mine.any():
        return 0.0
    points = uv.reshape(-1, 2)[mine]
    return float(max(0.0, -points.min(), points.max() - 1.0))


def _transfer_plan(targets, from_uv: str, to_uv: str) -> tuple[dict, list, list, list]:
    """({image name: job}, skipped lines, skipped images that read the rendering map, problems). A job is
    one image to bake onto to_uv: its kind (color or normal), whether its alpha is used, and every
    (object, slot, material, node) that reads it through the old layout."""
    import bpy
    from .texture_tools import _bindings
    jobs, skipped, skipped_render, problems, seen = {}, [], [], [], set()
    users = {}
    for obj in targets:
        for index, slot in enumerate(obj.material_slots):
            if slot.material is not None:
                users.setdefault(slot.material.name, []).append((obj, index))
    for name, places in users.items():
        material = bpy.data.materials[name]
        for binding in _bindings(material):
            if "groups" in binding:
                skipped.append(f"{name} has node groups ({', '.join(binding['groups'])}): images inside them are not moved")
                skipped_render.append(f"images in {name}'s node groups")
                continue
            node, image = binding["node"], binding["image"]
            if image is None or node.as_pointer() in seen:
                continue
            seen.add(node.as_pointer())
            where = f"{image.name} in {name}"
            uv_map, transform = binding["uv_map"], binding["transform"]
            if uv_map is None:
                skipped.append(f"{where}: not read through a UV map{binding['note']}; left as it is")
                continue
            if uv_map not in ("", from_uv, to_uv):
                skipped.append(f"{where} reads through {uv_map}; left as it is")
                continue
            if image.source == "TILED" or not all(image.size):
                skipped.append(f"{where} " + ("is a UDIM image" if image.source == "TILED" else
                                              "has no pixels (is its file missing?)") + "; not moved")
                if uv_map == "":
                    skipped_render.append(image.name)
                continue
            scale = max(abs(v) for v in transform["scale"]) if transform else 1.0
            reach = max(uv_reach(obj, from_uv, index) for obj, index in places)
            if node.extension == "REPEAT" and (scale > TILING or reach > TILING - 1):
                skipped.append(f"{where} repeats (" + (f"Mapping scale {scale:g}" if scale > TILING else
                               f"its UVs reach {1 + reach:.2g} on {from_uv}") + "): left to tile on the new layout")
                continue
            kind = "normal" if _feeds_tangent_normal(node) else "color"
            transform = {k: v for k, v in transform.items() if k != "name"} if transform else None
            job = jobs.setdefault(image.name, {"image": image, "kind": kind, "alpha": False, "uses": [],
                                               "transform": transform})
            if job["kind"] != kind or job["transform"] != transform:
                problems.append(f"{image.name} is read two ways (as colour and as a normal map, or through different "
                                f"Mapping nodes): one image can't be moved for both")
                continue
            job["alpha"] = job["alpha"] or node.outputs["Alpha"].is_linked
            job["uses"] += [(obj, index, material, node) for obj, index in places]
    moving = {m.name for job in jobs.values() for _, _, m, _ in job["uses"]}
    for name in sorted(moving):  # Repointing a material changes it everywhere it is used.
        others = sorted({o.name for o in bpy.data.objects if o not in targets and o.users_scene
                         and any(s.material is not None and s.material.name == name for s in o.material_slots)})
        if others:
            problems.append(f"material {name} is also on {', '.join(others)}, which would show the moved images on "
                            f"their old layout: add {'them' if len(others) > 1 else 'it'} to objects (same layouts), "
                            f"or give {'them' if len(others) > 1 else 'it'} a copy of {name} first")
    return jobs, skipped, skipped_render, problems


class _TransferBake:
    """The objects alone selected and visible, to_uv active and rendering (the bake writes into the active
    map, and tangent-space normals follow the rendering one), Cycles: put back after, with the slots'
    materials and every temporary image and material removed."""

    def __init__(self, targets, to_uv: str):
        self.targets, self.to_uv = targets, to_uv

    def __enter__(self):
        import bpy
        scene, layer = bpy.context.scene, bpy.context.view_layer
        self.scene, self.layer = scene, layer
        self.selected = [o for o in layer.objects if o.select_get()]
        self.active = layer.objects.active
        bake = scene.render.bake
        self.render = (scene.render.engine, scene.cycles.samples, bake.margin, bake.margin_type, bake.use_clear,
                       bake.target, bake.use_selected_to_active, bake.normal_space)
        self.uv, self.shown, self.slots, self.temporary = {}, [], [], []
        for obj in self.targets:
            self.shown.append((obj, obj.hide_get(), obj.hide_viewport, obj.hide_select))
            obj.hide_viewport = obj.hide_select = False
            if obj.hide_get():
                obj.hide_set(False)
            mesh = obj.data
            if mesh not in self.uv:
                render = next((l.name for l in mesh.uv_layers if l.active_render), None)
                self.uv[mesh] = (mesh.uv_layers.active.name if mesh.uv_layers.active else None, render)
                mesh.uv_layers.active = mesh.uv_layers[self.to_uv]
                mesh.uv_layers[self.to_uv].active_render = True
            for index, slot in enumerate(obj.material_slots):
                self.slots.append((obj, index, slot.material, slot.link))
        for obj in self.selected:
            obj.select_set(False)
        scene.render.engine = "CYCLES"
        scene.cycles.samples = TRANSFER_SAMPLES
        bake.target, bake.use_selected_to_active, bake.margin_type = "IMAGE_TEXTURES", False, "EXTEND"
        return self

    def material(self, name: str):
        import bpy
        material = bpy.data.materials.new(name)
        self.temporary.append(material)
        return material

    def image(self, name: str, width: int, height: int):
        import bpy
        image = bpy.data.images.new(name, width, height, alpha=False, float_buffer=True)
        image.colorspace_settings.name = "Non-Color"
        self.temporary.append(image)
        return image

    def __exit__(self, *exc):
        import bpy
        scene, layer = self.scene, self.layer
        for obj, index, material, link in reversed(self.slots):
            obj.material_slots[index].link = link
            obj.material_slots[index].material = material
        for item in self.temporary:
            (bpy.data.materials if isinstance(item, bpy.types.Material) else bpy.data.images).remove(item)
        for mesh, (active, render) in self.uv.items():
            if active is not None and mesh.uv_layers.get(active) is not None:
                mesh.uv_layers.active = mesh.uv_layers[active]
            if render is not None and mesh.uv_layers.get(render) is not None:
                mesh.uv_layers[render].active_render = True
        for obj in layer.objects:
            obj.select_set(obj in self.selected)
        for obj, hidden, viewport, unselectable in self.shown:
            obj.hide_viewport, obj.hide_select = viewport, unselectable
            if hidden:
                obj.hide_set(True)
        layer.objects.active = self.active
        bake = scene.render.bake
        (scene.render.engine, scene.cycles.samples, bake.margin, bake.margin_type, bake.use_clear, bake.target,
         bake.use_selected_to_active, bake.normal_space) = self.render
        return False


def _bake_source(tree, job: dict, from_uv: str, alpha: bool = False):
    """Old image read through the old layout (and its Mapping): the socket to bake, and the Alpha socket
    when the material uses it (an image node's Color is premultiplied unless its Alpha is used)."""
    from .paint_tools import _node
    coords = _node(tree, "ShaderNodeUVMap")
    coords.uv_map = from_uv
    vector = coords.outputs["UV"]
    transform = job["transform"]
    if transform:
        mapping = _node(tree, "ShaderNodeMapping")
        mapping.vector_type = transform["kind"]
        for socket in ("Location", "Rotation", "Scale"):
            if socket in mapping.inputs:
                mapping.inputs[socket].default_value = transform[socket.lower()]
        tree.links.new(vector, mapping.inputs["Vector"])
        vector = mapping.outputs["Vector"]
    texture = _node(tree, "ShaderNodeTexImage")
    texture.image, texture.interpolation, texture.extension = job["image"], job["uses"][0][3].interpolation, \
        job["uses"][0][3].extension
    tree.links.new(vector, texture.inputs["Vector"])
    return texture.outputs["Alpha" if alpha else "Color"], texture.outputs["Alpha"]


def _bake_job(bake: "_TransferBake", job: dict, target, from_uv: str, alpha: bool = False) -> None:
    """One bake of one image: every slot that reads it gets a material sampling it on the old layout,
    every other slot of those objects bakes black into a throwaway image."""
    import bpy
    from .paint_tools import _node
    from .tools import ToolError
    reading = {(obj.name, index) for obj, index, _, _ in job["uses"]}
    objects = list(dict.fromkeys(obj for obj, _, _, _ in job["uses"]))
    spare = bake.image("lc_transfer_spare", 8, 8)
    for obj in objects:
        for index, slot in enumerate(obj.material_slots):
            material = bake.material("lc_transfer_bake")
            material.use_nodes = True
            tree = material.node_tree
            tree.nodes.clear()
            output = _node(tree, "ShaderNodeOutputMaterial")
            image = _node(tree, "ShaderNodeTexImage")
            if (obj.name, index) in reading:
                source, alpha_out = _bake_source(tree, job, from_uv, alpha)
                if job["kind"] == "normal" and not alpha:
                    normal = _node(tree, "ShaderNodeNormalMap")
                    normal.space, normal.uv_map = "TANGENT", from_uv
                    tree.links.new(source, normal.inputs["Color"])
                    shader = _node(tree, "ShaderNodeBsdfDiffuse")
                    tree.links.new(normal.outputs["Normal"], shader.inputs["Normal"])
                    tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
                else:
                    emission = _node(tree, "ShaderNodeEmission")
                    tree.links.new(source, emission.inputs["Color"])
                    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
                    if job["alpha"] and not alpha:
                        # Use the Alpha (strength max(alpha, 1) is 1) so the Color stays straight, as the material reads it.
                        one = _node(tree, "ShaderNodeMath", operation="MAXIMUM")
                        one.inputs[1].default_value = 1.0
                        tree.links.new(alpha_out, one.inputs[0])
                        tree.links.new(one.outputs["Value"], emission.inputs["Strength"])
                image.image = target
            else:
                image.image = spare
            tree.nodes.active = image
            slot.material = material
        obj.select_set(True)
    unselected = [o.name for o in objects if not o.select_get()]
    if unselected:  # A bake leaves out what is not selected: its islands would stay empty.
        raise ToolError(f"{', '.join(unselected)} can't be selected here (in a hidden or excluded collection?); "
                        f"show it, then move the textures again")
    bpy.context.view_layer.objects.active = objects[0]
    size = max(target.size)
    try:
        bpy.ops.object.bake(type="NORMAL" if job["kind"] == "normal" and not alpha else "EMIT",
                            normal_space="TANGENT", normal_r="POS_X", normal_g="POS_Y", normal_b="POS_Z",
                            margin=max(4, size // 128), margin_type="EXTEND", use_clear=True,
                            target="IMAGE_TEXTURES", use_selected_to_active=False)
    except RuntimeError as ex:
        raise ToolError(f"The bake of {job['image'].name} failed: {str(ex).strip()}") from None
    finally:
        for obj in objects:
            obj.select_set(False)


def _new_image(job: dict, to_uv: str):
    import bpy
    old = job["image"]
    width, height = old.size
    image = bpy.data.images.new(f"{old.name} {to_uv}"[:63], width, height, alpha=job["alpha"],
                                float_buffer=old.is_float)
    image.colorspace_settings.name = old.colorspace_settings.name
    # The baked colour is straight whatever the source stored (a premultiplied source would be divided again).
    image.alpha_mode = "CHANNEL_PACKED" if old.alpha_mode == "CHANNEL_PACKED" else "STRAIGHT"
    return image


def _repoint(job: dict, image, to_uv: str) -> None:
    """The materials read the new image through to_uv; the old Mapping is baked in, so it is bypassed."""
    from .paint_tools import _node
    done = set()
    for _, _, material, node in job["uses"]:
        if node.as_pointer() in done:
            continue
        done.add(node.as_pointer())
        tree = material.node_tree
        coords = _node(tree, "ShaderNodeUVMap", location=(node.location.x - 220, node.location.y - 120))
        coords.uv_map = to_uv
        tree.links.new(coords.outputs["UV"], node.inputs["Vector"])
        node.image = image
        for normal in _tangent_normal_maps(node):
            normal.uv_map = to_uv


def _layout_clashes(jobs: dict, to_uv: str) -> list[str]:
    """Several meshes baking one image must keep apart on to_uv: islands of two meshes in the same place
    would bake over each other, and one of them would show the other's artwork."""
    problems = []
    for name, job in jobs.items():
        by_mesh = {}
        for obj, index, _, _ in job["uses"]:
            by_mesh.setdefault(obj.data.name, (obj.data, set()))[1].add(index)
        if len(by_mesh) < 2:
            continue
        triangles = {}
        for mesh_name, (mesh, slots) in by_mesh.items():
            mesh.calc_loop_triangles()
            uv = mesh.uv_layers[to_uv].uv
            triangles[mesh_name] = [tuple(tuple(uv[loop].vector) for loop in t.loops) for t in mesh.loop_triangles
                                    if t.material_index in slots]
        found = overlap(triangles, grid=128)
        if found["pairs"] and found["shared"] > 0.01 * max(1, found["cells"]):
            a, b = found["pairs"][0]
            problems.append(f"{name} would be baked by {a} and {b}, whose {to_uv} layouts overlap: unwrap them into "
                            f"one atlas (uv_unwrap atlas) first, or give each its own copy of the material")
    return problems


def _settle_maps(targets, from_uv: str, to_uv: str, jobs: dict, skipped_render: list) -> list[str]:
    """to_uv becomes the active map; also the rendering one unless an image left as it is still reads the
    rendering map. A copy uv_unwrap kept ('<to_uv> before unwrap') is spent once moved from: renamed, so the
    next unwrap of to_uv keeps the layout these new images were baked for."""
    notes = []
    for mesh in {o.data for o in targets}:
        mesh.uv_layers.active = mesh.uv_layers[to_uv]
        if not skipped_render:
            mesh.uv_layers[to_uv].active_render = True
        kept = mesh.uv_layers.get(from_uv)
        if jobs and kept is not None and from_uv == f"{to_uv} before unwrap"[:63]:
            # A free name: renaming a map onto an existing map's name replaces that map (Blender 5.2, measured).
            base, number = f"{to_uv} painted"[:60], 1
            name = base
            while mesh.uv_layers.get(name) is not None:
                number += 1
                name = f"{base} {number}"
            kept.name = name
            notes.append(f"{from_uv} is renamed {name} on {mesh.name}: the next unwrap of {to_uv} keeps its "
                         f"current layout instead.")
    if skipped_render:
        notes.append(f"The rendering map stays as it was: {', '.join(skipped_render[:3])} read it and were left as "
                     f"they are.")
    return notes


def transfer_textures(objects: list, from_uv: str, to_uv: str):
    """The tool (see SCHEMAS). In Object Mode for the call (Edit Mode keeps UV maps out of the mesh data),
    one undo step before it; selection, engine, slots, hiding and active maps put back after the bakes,
    then to_uv made the active map, and the rendering one when nothing left behind reads that."""
    import time
    import bpy
    import numpy as np
    from . import scene_diff, tools
    from .camera import _undo_push
    from .tools import ToolError, ToolResult
    targets = _named(objects)
    if not all(isinstance(v, str) and v for v in (from_uv, to_uv)) or from_uv == to_uv:
        raise ToolError("from_uv and to_uv: the names of two different UV maps, the old layout and the new one")
    problems = [f"{o.name} is not a mesh ({o.type})" for o in targets if o.type != "MESH"]
    problems += [f"{o.name}'s mesh is linked from a library" for o in targets
                 if o.type == "MESH" and (o.library is not None or o.data.library is not None)]
    problems += [f"{o.name} is not in this view layer" for o in targets if o.name not in bpy.context.view_layer.objects]
    if problems:
        raise ToolError("transfer_textures refused: " + "; ".join(problems) + ".")
    refusal = _not_yours(targets, "transfer_textures")
    if refusal:
        raise ToolError(refusal)
    with tools._editor_context("VIEW_3D"), _ObjectMode():
        for obj in targets:
            missing = [n for n in (from_uv, to_uv) if obj.data.uv_layers.get(n) is None]
            if missing:
                have = ", ".join(l.name for l in obj.data.uv_layers) or "none"
                problems.append(f"{obj.name} has no UV map {' or '.join(missing)} (its maps: {have})")
        if not problems:
            jobs, skipped, skipped_render, problems = _transfer_plan(targets, from_uv, to_uv)
            problems += _layout_clashes(jobs, to_uv)
        if problems:
            raise ToolError("transfer_textures refused: " + "; ".join(problems) + ".")
        before = scene_diff.snapshot()
        _undo_push("Before Loopcut: transfer textures")
        started, made = time.perf_counter(), {}
        with _TransferBake(targets, to_uv) as bake:
            for name, job in jobs.items():
                image = _new_image(job, to_uv)
                made[name] = image
                _bake_job(bake, job, image, from_uv)
                if job["alpha"]:
                    width, height = image.size
                    alpha = bake.image("lc_transfer_alpha", width, height)
                    _bake_job(bake, job, alpha, from_uv, alpha=True)
                    pixels = np.empty(width * height * 4, np.float32)
                    image.pixels.foreach_get(pixels)
                    values = np.empty(width * height * 4, np.float32)
                    alpha.pixels.foreach_get(values)
                    pixels[3::4] = values[0::4]
                    if image.is_float:  # Float buffers hold premultiplied colour; byte ones straight.
                        for channel in range(3):
                            pixels[channel::4] *= pixels[3::4]
                    image.pixels.foreach_set(pixels)
        for name, job in jobs.items():
            image = made[name]
            image.file_format = "OPEN_EXR" if image.is_float else "PNG"
            image.pack()
            _repoint(job, image, to_uv)
        notes = _settle_maps(targets, from_uv, to_uv, jobs, skipped_render)
        for obj in {o.data: o for o in targets}.values():  # Stacked islands share texels: one face's paint shows.
            stacked = audit(*_gather(obj, to_uv))["overlapping"]
            if stacked and jobs:
                notes.append(f"{obj.name}'s {to_uv} has {len(stacked)} pair{'s' * (len(stacked) != 1)} of overlapping "
                             f"islands (faces {stacked[0][0]}/{stacked[0][1]}...): where islands share texels only one "
                             f"face's paint can show. Unwrap without overlaps to keep the look everywhere.")
    seconds = time.perf_counter() - started
    names = ", ".join(o.name for o in targets)
    if jobs:
        renders = "the active and rendering map" if not skipped_render else "the active map"
        lines = [f"Moved {len(jobs)} image{'s' * (len(jobs) != 1)} of {names} from {from_uv} onto {to_uv} in "
                 f"{seconds:.1f} s; {to_uv} is now {renders}, {from_uv} is kept (it can go once the result looks "
                 f"right). New images are packed into the .blend:"]
        for name, job in jobs.items():
            old, new = job["image"], made[name]
            what = "normal map, re-baked for the new tangents" if job["kind"] == "normal" else \
                old.colorspace_settings.name + (", with alpha" if job["alpha"] else "")
            places = sorted({m.name for _, _, m, _ in job["uses"]})
            lines.append(f"- {old.name} -> {new.name} ({old.size[0]}x{old.size[1]}, {what}) in {', '.join(places)}")
    else:
        lines = [f"Nothing to move: no image on {names}'s materials reads through {from_uv}."]
    if skipped:
        lines.append("Left as they are: " + "; ".join(skipped) + ".")
    lines += notes
    return ToolResult("\n".join(lines), scene_before=before, scene_after=scene_diff.snapshot())


DISPATCH = {"inspect_uv": inspect_uv, "uv_unwrap": uv_unwrap, "transfer_textures": transfer_textures}
