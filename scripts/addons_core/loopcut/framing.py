"""The math behind frame_camera: pure Python, no bpy, so it is tested without Blender.

A camera looks down its local -Z with +Y up. A basis here is (right, up, back): the camera's world
axes as unit vectors; it looks along -back. The frame is the render frame as Blender computes it
(BKE_camera_params_compute_viewplane): the sensor size applies to the width or the height by the
sensor fit, AUTO picking the longer side of the render, and shift moves the frame by a fraction of
that fitted side. A perspective frame is given at unit distance, so a point at depth d lands at x/d;
an orthographic one at ortho_scale 1, so a point lands at x/ortho_scale. Frame coordinates are
world_to_camera_view's: 0..1 from left to right and from bottom to top.

The fit is exact, not a bounding sphere, which leaves wide empty borders. With the rotation fixed,
each frame edge moved in by the margin is a plane through the camera, and every point must be on
the inner side of all four. That is linear in the camera position, so per axis the closest position
that keeps every point inside has a closed form. The tighter axis sets the distance, its two margins
are then exactly the margin, and the camera slides along the looser axis until its two are equal.

A region (x0, y0, x1, y1 in frame coordinates) narrows the frame the subject is fitted into: the
same planes, through the region's edges moved in by the margin, so the camera slides until the
subject sits there; the lens shift is kept and allowed for, never reset.

A close shot (fit_around) keeps the rotation too but pins one point, aim, to the middle of the
frame (of the region), so the camera moves only along one line. On that line a point lands at k / (f + d): k its
offset across the view, f its depth, both from aim, and d the camera's distance from aim. With
fill 1 the closest d that keeps every point inside is again closed form; with fill > 1 the camera
comes closer, until the subject spans fill times what the whole-subject fit gives it.
"""

import math
from dataclasses import dataclass

NAMED = {  # XYZ Euler of the camera in degrees: capture_viewport's angles, so both show the same view.
    "three_quarter": (62.0, 0.0, 38.0),
    "front": (90.0, 0.0, 0.0),
    "side": (90.0, 0.0, 90.0),
    "top": (0.0, 0.0, 0.0),
}
MAX_MARGIN = 0.4
MAX_FILL = 10.0       # A close shot: the subject at up to 10 times its whole-frame size.
MAX_ROWS = 8          # Objects the report lists one by one; the rest are counted.
MAX_FRAMES = 24       # report_only frames measured in one call.
FULL = (0.0, 0.0, 1.0, 1.0)  # The region that is the whole frame.
_CENTRE_STEPS = 60    # Halvings when centring along the looser axis: far below a pixel.
_INSIDE = 1e-9        # A point this close outside an edge is on it.
DIRECTION_HELP = 'keep, front, side, top, three_quarter, or "x,y,z" from the subject toward the camera'


def dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b) -> tuple:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(v) -> tuple:
    length = math.sqrt(dot(v, v))
    if not length > 1e-12:
        raise ValueError("a zero-length direction")
    return tuple(c / length for c in v)


def _combine(*terms) -> tuple:
    """Sum of scalar * vector terms."""
    return tuple(sum(s * v[i] for s, v in terms) for i in range(3))


# ------------------------------------------------------------------ rotation

def euler_basis(degrees) -> tuple:
    """(right, up, back) of a camera with this XYZ Euler rotation in degrees (R = Rz Ry Rx)."""
    cx, cy, cz = (math.cos(math.radians(a)) for a in degrees)
    sx, sy, sz = (math.sin(math.radians(a)) for a in degrees)
    rows = ((cy * cz, sx * sy * cz - cx * sz, cx * sy * cz + sx * sz),
            (cy * sz, sx * sy * sz + cx * cz, cx * sy * sz - sx * cz),
            (-sy, sx * cy, cx * cy))
    return tuple(tuple(rows[i][j] for i in range(3)) for j in range(3))


def look_basis(toward_camera) -> tuple:
    """(right, up, back) of a camera looking back along toward_camera (from the subject to the
    camera) with no roll: its right axis stays level, so world +Z is up in the picture. Straight
    down or up, +Y is up in the picture, as in Blender's top view."""
    back = _unit(toward_camera)
    forward = (-back[0], -back[1], -back[2])
    hint = (0.0, 0.0, 1.0) if abs(back[2]) < 0.9999 else (0.0, 1.0, 0.0)
    right = _unit(_cross(forward, hint))
    return right, _cross(right, forward), back


def direction_basis(direction):
    """(right, up, back) for a named view or a vector from the subject toward the camera, given
    as a list or as "x,y,z" text; None for keep. ValueError for anything else."""
    if direction is None or (isinstance(direction, str) and direction.strip().lower() in ("", "keep")):
        return None
    if isinstance(direction, str) and direction.strip().lower() in NAMED:
        return euler_basis(NAMED[direction.strip().lower()])
    parts = direction.strip().strip("[]()").split(",") if isinstance(direction, str) else direction
    try:
        vector = tuple(float(p) for p in parts)
    except (TypeError, ValueError):
        vector = ()
    if len(vector) != 3 or not all(math.isfinite(c) for c in vector) or not any(vector):
        raise ValueError(f"direction must be {DIRECTION_HELP}; got {direction!r}")
    return look_basis(vector)


# ------------------------------------------------------------------ the frame

@dataclass(frozen=True)
class Frame:
    """The render frame in camera space: at unit distance for a perspective camera, at
    ortho_scale 1 for an orthographic one. center is where the lens shift puts its middle."""
    width: float
    height: float
    center_x: float = 0.0
    center_y: float = 0.0
    ortho: bool = False
    fit: str = "HORIZONTAL"  # The side the sensor size or ortho_scale applies to.


def frame(resolution, sensor_fit: str = "AUTO", sensor_width: float = 36.0, sensor_height: float = 24.0,
          lens: float = 50.0, shift=(0.0, 0.0), pixel_aspect=(1.0, 1.0), ortho: bool = False) -> Frame:
    size_x, size_y = resolution[0] * pixel_aspect[0], resolution[1] * pixel_aspect[1]
    fit = sensor_fit if sensor_fit in ("HORIZONTAL", "VERTICAL") else "HORIZONTAL" if size_x >= size_y else "VERTICAL"
    # The sensor height counts only when the fit is set to VERTICAL; AUTO uses the width on either side.
    sensor = sensor_height if sensor_fit == "VERTICAL" else sensor_width
    side = 1.0 if ortho else sensor / lens
    width, height = (side, side * size_y / size_x) if fit == "HORIZONTAL" else (side * size_x / size_y, side)
    return Frame(width, height, shift[0] * side, shift[1] * side, ortho, fit)


def to_camera(point, location, basis) -> tuple:
    """(x, y, depth) of a world point: to the right, up, and in front of the camera."""
    d = (point[0] - location[0], point[1] - location[1], point[2] - location[2])
    return dot(d, basis[0]), dot(d, basis[1]), -dot(d, basis[2])


def project(point, location, basis, view: Frame, scale: float = 1.0) -> tuple:
    """(u, v, depth) like world_to_camera_view; u and v are None for a perspective point at or
    behind the camera. scale is the ortho_scale."""
    x, y, depth = to_camera(point, location, basis)
    if view.ortho:
        divisor = scale
    elif depth > 1e-12:
        divisor = depth
    else:
        return None, None, depth
    return (x / divisor - view.center_x) / view.width + 0.5, (y / divisor - view.center_y) / view.height + 0.5, depth


# ------------------------------------------------------------------ the fit

@dataclass
class Fit:
    location: tuple
    ortho_scale: float | None  # Orthographic only: the scale that fits.
    limit: str                 # "width" or "height": the side whose margins bind the fit.
    span: float | None = None  # fit_around: the share of the frame the subject spans on the limit side.
    edge: str | None = None    # fit_around at fill 1: the one margin that binds (left, right, bottom, top).


def region_of(value) -> tuple:
    """(x0, y0, x1, y1) from a list or "x0,y0,x1,y1" text, in frame coordinates (0..1, from the
    bottom left); FULL for none. ValueError for anything else."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return FULL
    parts = value.strip().strip("[]()").split(",") if isinstance(value, str) else value
    try:
        numbers = tuple(float(p) for p in parts if not isinstance(p, bool))
    except (TypeError, ValueError):
        numbers = ()
    if len(numbers) != 4 or not all(math.isfinite(n) for n in numbers):
        raise ValueError(f"region is [x0, y0, x1, y1] in frame coordinates (0..1 from the bottom left); got {value!r}")
    x0, y0, x1, y1 = numbers
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        raise ValueError(f"region needs 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1; got {list(numbers)}")
    return numbers


def usable(region, margin: float) -> tuple:
    """The share of the frame's width and height left inside the region once the margin is taken."""
    x0, y0, x1, y1 = region
    return x1 - x0 - 2.0 * margin, y1 - y0 - 2.0 * margin


def _window(view: Frame, margin: float, region) -> tuple:
    """Per image axis, (centre, half) at unit distance (ortho_scale 1 for an orthographic camera):
    the part of the frame every point must stay in, the region moved in by the margin."""
    if not 0.0 <= margin <= MAX_MARGIN:
        raise ValueError(f"margin must be between 0 and {MAX_MARGIN}")
    x0, y0, x1, y1 = region
    wide, tall = usable(region, margin)
    if not (wide > 1e-6 and tall > 1e-6):
        raise ValueError(f"region {[round(v, 3) for v in region]} leaves no room inside margin {margin:g} on each side")
    return ((view.center_x + ((x0 + x1) / 2.0 - 0.5) * view.width, wide / 2.0 * view.width),
            (view.center_y + ((y0 + y1) / 2.0 - 0.5) * view.height, tall / 2.0 * view.height))


def _hull(pairs: list) -> list:
    """Convex hull of 2D points (monotone chain). The extremes of (s - c) / (f - cf) over the
    points, which the centring looks for many times, are always hull vertices."""
    pairs = sorted(set(pairs))
    if len(pairs) < 3:
        return pairs

    def half(sequence):
        chain = []
        for p in sequence:
            while len(chain) >= 2 and ((chain[-1][0] - chain[-2][0]) * (p[1] - chain[-2][1])
                                       - (chain[-1][1] - chain[-2][1]) * (p[0] - chain[-2][0])) <= 0:
                chain.pop()
            chain.append(p)
        return chain[:-1]
    return half(pairs) + half(reversed(pairs))


def _axis_fit(pairs: list, center: float, half: float) -> tuple:
    """For one image axis, with (s, f) the points' coordinates along it and along the view: the
    largest camera coordinate along the view that keeps every point within center +- half of
    the frame, and the two bounds that then pin the camera along the axis."""
    high = max(s - (center + half) * f for s, f in pairs)  # The camera must be at least here...
    low = min(s - (center - half) * f for s, f in pairs)   # ...and at most here, both plus a slope times cf.
    return (low - high) / (2.0 * half), high, low


def _centre(pairs: list, cf: float, center: float, half: float, high: float, low: float) -> float:
    """The camera coordinate along one image axis that leaves equal room on both sides."""
    lo, hi = high + (center + half) * cf, low + (center - half) * cf
    if hi - lo <= 1e-12 * max(1.0, abs(lo)):
        return (lo + hi) / 2.0

    def room(c: float) -> float:  # Room below minus room above; falls as c grows.
        spots = [(s - c) / (f - cf) for s, f in pairs]
        return (min(spots) - (center - half)) - ((center + half) - max(spots))
    for _ in range(_CENTRE_STEPS):
        middle = (lo + hi) / 2.0
        lo, hi = (middle, hi) if room(middle) > 0 else (lo, middle)
    return (lo + hi) / 2.0


def fit(points, basis, view: Frame, margin: float, region=FULL) -> Fit:
    """Where to put a camera with this rotation so that all points are in the frame (in region)
    with at least `margin` (a fraction of the frame) on every side, exactly `margin` on the two
    sides of the tighter axis, and equal room on the two of the other; for an orthographic camera
    also the ortho_scale that does it."""
    (center_x, half_w), (center_y, half_h) = _window(view, margin, region)
    right, up, back = basis
    forward = (-back[0], -back[1], -back[2])
    coords = [(dot(p, right), dot(p, up), dot(p, forward)) for p in points]
    if not coords:
        raise ValueError("no points to frame")
    size = max(max(c[i] for c in coords) - min(c[i] for c in coords) for i in range(3))
    if size < 1e-9:
        raise ValueError("the objects have no size to frame")
    if view.ortho:
        spans = [max(c[i] for c in coords) - min(c[i] for c in coords) for i in (0, 1)]
        by_width, by_height = spans[0] / (2.0 * half_w), spans[1] / (2.0 * half_h)
        scale = max(by_width, by_height, size * 1e-3)
        cx = (max(c[0] for c in coords) + min(c[0] for c in coords)) / 2.0 - center_x * scale
        cy = (max(c[1] for c in coords) + min(c[1] for c in coords)) / 2.0 - center_y * scale
        cf = min(c[2] for c in coords) - scale  # A frame's size in front of the nearest point: clear of it.
        return Fit(_combine((cx, right), (cy, up), (cf, forward)), scale, "width" if by_width >= by_height else "height")
    xs, ys = _hull([(c[0], c[2]) for c in coords]), _hull([(c[1], c[2]) for c in coords])
    depth_x, high_x, low_x = _axis_fit(xs, center_x, half_w)
    depth_y, high_y, low_y = _axis_fit(ys, center_y, half_h)
    # Only a subject with no extent across the view would put the camera on its nearest point.
    cf = min(depth_x, depth_y, min(c[2] for c in coords) - 1e-3 * size)
    cx = _centre(xs, cf, center_x, half_w, high_x, low_x)
    cy = _centre(ys, cf, center_y, half_h, high_y, low_y)
    return Fit(_combine((cx, right), (cy, up), (cf, forward)), None, "width" if depth_x <= depth_y else "height")


def _checked_fill(fill) -> float:
    if isinstance(fill, bool) or not isinstance(fill, (int, float)) or not 1.0 <= fill <= MAX_FILL:
        raise ValueError(f"fill must be from 1 (all of it) to {MAX_FILL:g}; got {fill!r}")
    return float(fill)


def _spread(pairs: list, d: float) -> float:
    """Max minus min of k / (f + d): the extremes of a slope from (-d, 0), so hull vertices suffice."""
    values = [k / (f + d) for k, f in pairs]
    return max(values) - min(values)


def _binding(extents: list) -> tuple:
    """(limit, edge) of the largest of [(need, axis, k)]: the side and the one margin it binds."""
    _, axis, k = max(extents, key=lambda e: e[0])
    return ("width", "right" if k > 0 else "left") if axis == 0 else ("height", "top" if k > 0 else "bottom")


def fit_around(points, basis, view: Frame, margin: float, aim, fill: float = 1.0, region=FULL) -> Fit:
    """Where to put a camera with this rotation so that aim lands in the middle of the frame (of
    region). fill 1: the closest such camera with every point inside and at least `margin` on
    every side; limit and edge name the margin that binds. fill > 1: closer, until the subject
    spans fill x the room inside the margins on its tighter side, which is fill times its size in
    fit(), so it is cropped around aim; an orthographic camera gets fit()'s scale / fill. The
    camera never goes into the subject: when even the closest spot in front of every point shows
    less, it stops there and span says how much."""
    (center_x, half_w), (center_y, half_h) = _window(view, margin, region)
    fill = _checked_fill(fill)
    right, up, back = basis
    forward = (-back[0], -back[1], -back[2])
    rel = [(dot(d, right), dot(d, up), dot(d, forward))
           for d in ((p[0] - aim[0], p[1] - aim[1], p[2] - aim[2]) for p in points)]
    if not rel:
        raise ValueError("no points to frame")
    size = max(max(c[i] for c in rel) - min(c[i] for c in rel) for i in range(3))
    if size < 1e-9:
        raise ValueError("the objects have no size to frame")
    room = (2.0 * half_w / view.width, 2.0 * half_h / view.height)  # Inside the margins, a share of the frame.
    spans = [max(c[i] for c in rel) - min(c[i] for c in rel) for i in (0, 1)]

    def settle(shown: tuple, edge=None) -> tuple:
        """(limit, span, edge): at fill 1 the binding margin, else the side that reaches the fill."""
        if edge is not None:
            limit, edge = edge
        else:
            limit = "width" if shown[0] / room[0] >= shown[1] / room[1] else "height"
        return limit, shown[0] if limit == "width" else shown[1], edge

    if view.ortho:
        binding = None
        if fill == 1.0:
            extents = [(abs(c[0]) / half_w, 0, c[0]) for c in rel] + [(abs(c[1]) / half_h, 1, c[1]) for c in rel]
            scale = max(max(e[0] for e in extents), size * 1e-3)
            binding = _binding(extents) if scale > size * 1e-3 else None
        else:
            scale = max(spans[0] / (2.0 * half_w), spans[1] / (2.0 * half_h), size * 1e-3) / fill
        shown = (spans[0] / (scale * view.width), spans[1] / (scale * view.height))
        location = _combine((1.0, aim), (-center_x * scale, right), (-center_y * scale, up),
                            (min(c[2] for c in rel) - scale, forward))
        limit, span, edge = settle(shown, binding)
        return Fit(location, scale, limit, span, edge)
    # Per image axis, (k, f): the offset across the view that the window's middle leaves, and the depth.
    xs = _hull([(c[0] - center_x * c[2], c[2]) for c in rel])
    ys = _hull([(c[1] - center_y * c[2], c[2]) for c in rel])
    near = -min(c[2] for c in rel) + 1e-3 * size  # Every point in front of the camera.

    def shown(d: float) -> tuple:
        return _spread(xs, d) / view.width, _spread(ys, d) / view.height

    def reached(d: float) -> float:  # How many times the room inside the margins the subject spans.
        across = shown(d)
        return max(across[0] / room[0], across[1] / room[1])
    extents = [(abs(k) / half_w - f, 0, k) for k, f in xs] + [(abs(k) / half_h - f, 1, k) for k, f in ys]
    far = max([near] + [e[0] for e in extents])
    d, binding = far, (_binding(extents) if far > near else None)
    if fill > 1.0:
        binding = None
        if reached(near) <= fill:
            d = near
        else:
            lo, hi = near, far  # The subject spans at least fill at lo, less at hi.
            for _ in range(_CENTRE_STEPS):
                middle = (lo + hi) / 2.0
                lo, hi = (middle, hi) if reached(middle) >= fill else (lo, middle)
            d = (lo + hi) / 2.0
    location = _combine((1.0, aim), (-d, forward), (-d * center_x, right), (-d * center_y, up))
    limit, span, edge = settle(shown(d), binding)
    return Fit(location, None, limit, span, edge)


# ------------------------------------------------------------------ the report

EDGES = ("left", "right", "bottom", "top")


def coverage(points, location, basis, view: Frame, scale: float = 1.0, clip=None) -> dict:
    """Where points land: bounds (left, right, bottom, top) in frame coordinates of those in front
    of the camera, or None; inside, the share in the frame (and within clip, (start, end), when
    given); margins per edge, negative past it; clips, what cuts points off: edges, near, far,
    behind; offset of the bounds' middle from the frame's."""
    us, vs, inside, clips, count = [], [], 0, set(), 0
    for point in points:
        count += 1
        u, v, depth = project(point, location, basis, view, scale)
        if u is None:
            clips.add("behind")
            continue
        us.append(u)
        vs.append(v)
        out = {edge for edge, past in zip(EDGES, (u < -_INSIDE, u > 1 + _INSIDE, v < -_INSIDE, v > 1 + _INSIDE)) if past}
        if clip and depth < clip[0]:
            out.add("near")
        if clip and depth > clip[1]:
            out.add("far")
        clips |= out
        inside += not out
    result = {"inside": inside / count if count else 0.0, "bounds": None, "margins": None, "offset": None,
              "clips": [c for c in EDGES + ("near", "far", "behind") if c in clips]}
    if us:
        bounds = (min(us), max(us), min(vs), max(vs))
        result.update(bounds=bounds, margins=(bounds[0], 1.0 - bounds[1], bounds[2], 1.0 - bounds[3]),
                      offset=((bounds[0] + bounds[1]) / 2.0 - 0.5, (bounds[2] + bounds[3]) / 2.0 - 0.5))
    return result


def _share(inside: float) -> str:
    if inside >= 1.0:
        return "100%"
    return f"{min(inside * 100.0, 99.9):.1f}%" if inside > 0 else "0%"


def _n(value: float, digits: int = 3, sign: str = "") -> str:
    return f"{round(value, digits) + 0.0:{sign}.{digits}f}"  # + 0.0: no "-0.000".


def row(name: str, measured: dict) -> str:
    if measured.get("empty"):
        return f"- {name}: no geometry"
    if measured["bounds"] is None:
        return f"- {name}: not in view ({', '.join(measured['clips'])})"
    (left, right, bottom, top), margins, offset = measured["bounds"], measured["margins"], measured["offset"]
    text = (f"- {name}: x {_n(left)}..{_n(right)} y {_n(bottom)}..{_n(top)}, {_share(measured['inside'])} inside, "
            f"margins L {_n(margins[0])} R {_n(margins[1])} B {_n(margins[2])} T {_n(margins[3])}, "
            f"offset {_n(offset[0], sign='+')} {_n(offset[1], sign='+')}")
    return text + (f", clipped: {', '.join(measured['clips'])}" if measured["clips"] else "")


def _vector(values, digits: int) -> str:
    return "[" + ", ".join(_n(v, digits) for v in values) + "]"


def _optics(camera: dict) -> str:
    return f"ortho_scale {camera['ortho_scale']:.3f}" if camera.get("ortho_scale") else f"lens {camera['lens']:.1f} mm"


def frame_numbers(value) -> list:
    """Whole frame numbers from a list, one number or "1,50,100" text, each once, in order.
    ValueError for anything else or more than MAX_FRAMES."""
    parts = value.strip().strip("[]()").split(",") if isinstance(value, str) else (
        value if isinstance(value, (list, tuple)) else [value])
    numbers = []
    for part in parts:
        try:
            number = float(part) if not isinstance(part, bool) else math.nan
        except (TypeError, ValueError):
            number = math.nan
        if not (math.isfinite(number) and number == int(number)):
            raise ValueError(f"frames are whole frame numbers; got {value!r}")
        if int(number) not in numbers:
            numbers.append(int(number))
    if not 1 <= len(numbers) <= MAX_FRAMES:
        raise ValueError(f"frames: 1 to {MAX_FRAMES} whole frame numbers; got {len(numbers)}")
    return numbers


def frames_report(camera: dict, frames: list, back: int, notes: list) -> str:
    """report_only at several frames, a line each: the subject's coverage (all together, or the
    one object's), where the camera is when it moves, and the objects not wholly inside.
    camera: name, lens or ortho_scale, resolution, fit; frames: (frame, camera location, rows,
    subject) each; back: the frame the scene is back on."""
    lines = [f"Camera \"{camera['name']}\" as it is, at frames {', '.join(str(f[0]) for f in frames)} (the scene "
             f"is back on frame {back}): {_optics(camera)}; {camera['resolution'][0]}x{camera['resolution'][1]}, "
             f"sensor fit {camera['fit']}."]
    lines += notes
    lines.append("In frame coordinates (0..1, x left to right, y bottom to top); a negative margin is past that edge:")
    first = frames[0][1]
    moves = any(max(abs(a - b) for a, b in zip(f[1], first)) > 1e-6 for f in frames)
    for number, location, rows, subject in frames:
        whole = subject if subject is not None and len(rows) > 1 else rows[0][1]
        line = row(f"frame {number}", whole) + (f"; camera at {_vector(location, 3)}" if moves else "")
        cut = [(name, m) for name, m in rows if not m.get("empty") and m["inside"] < 1.0] if len(rows) > 1 else []
        if cut:
            listed = [f"{name} {_share(m['inside'])}" for name, m in cut[:MAX_ROWS]]
            line += f"; not all inside: {', '.join(listed)}{' ...' if len(cut) > MAX_ROWS else ''}"
        lines.append(line)
    return "\n".join(lines)


def report(camera: dict, rows: list, subject: dict | None, notes: list) -> str:
    """The tool's result. camera: name, what (its first words), location, rotation (degrees),
    lens or ortho_scale, distance, resolution, fit; rows: (name, coverage) per object; subject:
    the coverage of them all, shown when there are several."""
    optics = _optics(camera)
    lines = [f"Camera \"{camera['name']}\" {camera['what']}: at {_vector(camera['location'], 3)}, rotation "
             f"{_vector(camera['rotation'], 1)} deg (world XYZ), {optics}, {camera['distance']:.3f} m from the "
             f"subject's centre; {camera['resolution'][0]}x{camera['resolution'][1]}, sensor fit {camera['fit']}."]
    lines += notes
    lines.append("In frame coordinates (0..1, x left to right, y bottom to top); a negative margin is past that edge:")
    whole = [n for n, (_, measured) in enumerate(rows) if measured.get("inside", 1.0) >= 1.0]
    shown = set(sorted(range(len(rows)), key=lambda n: n in whole)[:MAX_ROWS])  # Clipped ones first.
    lines += [row(name, measured) for n, (name, measured) in enumerate(rows) if n in shown]
    rest = [n for n in range(len(rows)) if n not in shown]
    if rest:
        cut = [rows[n][0] for n in rest if n not in whole]
        clipped = f"; clipped: {', '.join(cut[:MAX_ROWS])}{' ...' if len(cut) > MAX_ROWS else ''}" if cut else ""
        lines.append(f"- {len(rest)} more objects: {len(rest) - len(cut)} fully inside{clipped}.")
    if subject is not None and len(rows) > 1:
        lines.append(row("All together", subject))
    return "\n".join(lines)
