"""The Loopcut mark: the one place its geometry lives. The panel draws it from here, and
branding/make_assets.py makes the app icons and splash from the same polygons.

The mark was traced by hand from the only surviving copy, a 95 px render in the old Blender
Copilot login mockup (branding/source/). If the original artwork turns up, replace SHAPES.
Needs numpy, which Blender's Python has; nothing else.
"""

import numpy as np

# Pixel coordinates of the 120x110 crop the mark was traced from.
PALETTE = {"teal": "#4a95a3", "teal_light": "#51a7ae", "blue_outer": "#437f97", "blue": "#37658b",
           "orange": "#df8b37", "orange_light": "#ebb074", "red": "#ad4336", "crimson": "#a03541", "white": "#ddd3c8"}
HOLE_RIGHT = [(85.5, 37.5), (66.5, 47.3), (65.8, 73)]
SHAPES = [  # (colour, polygon, optional hole)
    ("teal", [(20.5, 13.5), (101, 16), (100.5, 17), (99, 19.5), (84, 28), (40.5, 28), (57.5, 40.5), (55.5, 50),
              (35, 38), (16, 25.5), (14, 24.5)], None),
    ("teal_light", [(22, 14), (101, 16), (99.5, 17.8), (62, 22), (31, 23), (26, 18.5)], None),
    ("teal_light", [(46.5, 36.8), (57.5, 40.5), (55.5, 50), (52, 48)], None),
    ("blue_outer", [(14, 24.5), (52, 95.5), (53, 95.5), (54, 79), (24, 30)], None),
    ("blue", [(16, 25.5), (35, 38), (54, 71), (55, 51), (55.5, 50), (59.6, 63), (64.8, 95.5), (53, 95.5), (54, 79), (24, 30)], None),
    ("orange", [(66, 46), (100.5, 17), (101, 16), (107.5, 28), (65.5, 95.5), (64.8, 95.5), (59.6, 63), (55.5, 50)], HOLE_RIGHT),
    ("orange_light", [(92, 31.5), (106.8, 29.2), (66.5, 94.3), (67.8, 91.5)], None),
    ("orange_light", [(63.8, 48.2), (65.3, 47.6), (65.3, 73), (65.5, 95.5), (64.8, 95.5), (59.6, 63)], None),
    ("red", [(57.5, 40.5), (84, 28), (99, 19.5), (100.5, 17), (66, 46)], None),
    ("crimson", [(57.5, 40.5), (77, 35), (68, 44.5), (66, 46)], None),
    ("white", [(57.5, 40.5), (66, 46), (55.5, 50)], None),
]

WIDTH, HEIGHT, LEFT, TOP = 93.5, 82.0, 14.0, 13.5  # The mark's bounds in those coordinates.


def hex_rgb(c):
    return tuple(int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))

def _inside(xs, ys, polygon):
    inside = np.zeros(xs.shape, dtype=bool)
    n = len(polygon)
    for i in range(n):
        (x1, y1), (x2, y2) = polygon[i], polygon[(i + 1) % n]
        if y1 == y2:
            continue
        crosses = ((y1 > ys) != (y2 > ys)) & (xs < (x2 - x1) * (ys - y1) / (y2 - y1) + x1)
        inside ^= crosses
    return inside

def render(width, height, scale, tx, ty, ss=4):
    """RGBA float image; source coordinate (x, y) lands at (x * scale + tx, y * scale + ty)."""
    W, H = width * ss, height * ss
    ys, xs = np.mgrid[0:H, 0:W]
    xs = ((xs + 0.5) / ss - tx) / scale
    ys = ((ys + 0.5) / ss - ty) / scale
    rgb = np.zeros((H, W, 3)); alpha = np.zeros((H, W))
    for colour, polygon, hole in SHAPES:
        m = _inside(xs, ys, polygon)
        if hole:
            m &= ~_inside(xs, ys, hole)
        rgb[m] = hex_rgb(PALETTE[colour]); alpha[m] = 1
    rgb = (rgb * alpha[..., None]).reshape(height, ss, width, ss, 3).sum(axis=(1, 3))
    alpha = alpha.reshape(height, ss, width, ss).mean(axis=(1, 3))
    rgb = np.divide(rgb, (alpha * ss * ss)[..., None], out=np.zeros_like(rgb), where=alpha[..., None] > 0)
    return np.dstack([rgb, alpha])


def square(size, margin=0.085, ss=4):
    scale = size * (1 - 2 * margin) / WIDTH
    return render(size, size, scale, (size - WIDTH * scale) / 2 - LEFT * scale, (size - HEIGHT * scale) / 2 - TOP * scale, ss)
