"""Free mesh checks and advisory silhouette hints, without names or a model.

Clothes and stylized bodies can hide limbs. Only the returned skeleton can
establish supported anatomy; silhouette uncertainty must not block a quote.
"""
import math

MAX_TRIANGLES = 100_000
MAX_VERTICES = 300_000
# R5-D1: ar-dog-3's model promised a ready-made run before the rig, which then refused it after the purchase.
# R6-D4/B4: Fast repeats these word for word, so they are the user's words ("experimental rig" and "the bend check"
# reached the user unexplained), and the long coat's hidden leg gap alone does not warn: clothed people got person rigs.
SHAPE_WARNING = ("This shape may not get a person rig. Then there are no ready-made walks, and Loopcut animates it "
                 "with keyframes instead, for free.")
CLOTHING_WARNING = 'Long coats may stretch between the legs.'
# Measured on the 22 M2 eval meshes and the persona dog (out/autorig/m2/client-ui/r13/hints.json): a long coat or a
# clothed multi-mesh character (B7, R12, R13: standard rigs) sets only leg_gap; every shape that now gets an
# experimental rig but B5 (arm_separation) and B8 (none) sets upright_body or head_and_body; R16 (arms down, standard)
# sets arm_separation.
PERSON_HINTS = frozenset(('leg_gap',))                         # No warning on these alone.
EXPERIMENTAL_HINTS = frozenset(('upright_body', 'head_and_body'))  # The price note's "animated for free" path.
# ar-dog-1: a 3.9 m upload height and a 100x scale read as "resize first", and the model shrank the
# dog out of sight before rigging; the upload includes size and rotation (autorig.geometry).
HANDLED = 'Size and rotation are handled, so nothing needs resizing or rotating first.'
POSE_HANDLING = ('Animals and unusual shapes are rarely in a T-pose or A-pose; that is normal, so rig it as it is '
                 'and do not ask the user about the pose.')


class RigError(ValueError):
    def __init__(self, message, code="invalid_mesh"):
        super().__init__(message)
        self.code = code


def refuse(message, code="unsupported_anatomy"):
    raise RigError(message + " No credits used.", code)


def sections(points, triangles, z, epsilon):
    intervals = []
    for face in triangles:
        p = [points[i] for i in face]
        if min(v[2] for v in p) > z or max(v[2] for v in p) < z:
            continue
        hits = []
        for a, b in zip(p, p[1:] + p[:1]):
            if (a[2] <= z < b[2]) or (b[2] <= z < a[2]):
                hits.append(a[0] + (b[0] - a[0]) * (z - a[2]) / (b[2] - a[2]))
        if len(hits) == 2:
            intervals.append((min(hits), max(hits)))
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1] + epsilon:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return [span for span in merged if span[1] - span[0] > epsilon]


def geometry(points, triangles):
    """Points are upright preparation-space metres; no rig/gold facts enter this check."""
    if len(triangles) > MAX_TRIANGLES:
        refuse(f"This character has {len(triangles):,} triangles; the limit is 100,000. "
               "Make a lower-detail copy and rig that copy. Moving its rig back to the detailed "
               "character is not available yet.", "triangle_limit")
    if len(points) > MAX_VERTICES:
        refuse("This character has more than 300,000 points. Make a lower-detail copy and rig that copy.",
               "vertex_limit")
    if not 4 <= len(points) <= MAX_VERTICES or not triangles:
        refuse("Select a character mesh with faces (at most 300,000 points).", "invalid_mesh")
    if any(len(p) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in p) for p in points):
        refuse("The mesh contains unusable point positions. Repair the mesh first.", "invalid_mesh")
    if any(len(t) != 3 or any(type(i) is not int or not 0 <= i < len(points) for i in t) for t in triangles):
        refuse("The mesh contains invalid faces. Repair the mesh first.", "invalid_mesh")
    lo = [min(p[i] for p in points) for i in range(3)]
    hi = [max(p[i] for p in points) for i in range(3)]
    width, depth, height = [b - a for a, b in zip(lo, hi)]
    uncertain = []
    if height < 1e-5 or not .25 <= width / height <= 1.8 or depth > height * .75:
        uncertain.append('upright_body')
    sample = {q: sections(points, triangles, lo[2] + q * height, height * .003)
              for q in (.18, .28, .38, .52, .62, .72, .82, .92)}
    legs = [spans for q, spans in sample.items() if q in (.18, .28, .38) and len(spans) == 2
            and spans[1][0] - spans[0][1] > height * .012]
    if len(legs) < 2:
        uncertain.append('leg_gap')
    head = sample[.92]
    trunk = sample[.52]
    if len(head) != 1 or not trunk or head[0][1] - head[0][0] > width * .65:
        uncertain.append('head_and_body')
    centre = (head[0][0] + head[0][1]) / 2 if len(head) == 1 else (lo[0] + hi[0]) / 2
    trunk_span = min(trunk, key=lambda s: abs((s[0] + s[1]) / 2 - centre)) if trunk else (lo[0], hi[0])
    arms = [(q, spans) for q, spans in sample.items() if .52 < q < .9 and spans
            and spans[0][0] < trunk_span[0] - height * .065
            and spans[-1][1] > trunk_span[1] + height * .065]
    if not arms:
        uncertain.append('arm_separation')
    widest = max(arms, key=lambda pair: pair[1][-1][1] - pair[1][0][0]) if arms else None
    pose = ("T-pose" if widest[0] >= .72 else "A-pose") if widest else 'other'
    toes = [p[1] for p in points if p[2] <= lo[2] + height * .08]
    ankles = [p[1] for p in points if lo[2] + height * .10 <= p[2] <= lo[2] + height * .2]
    shift = ((max(toes) + min(toes)) / 2 - (max(ankles) + min(ankles)) / 2) if toes and ankles else 0
    facing = ('+Y' if shift > 0 else '-Y') if abs(shift) > height * .005 else 'ambiguous'
    warnings = ([SHAPE_WARNING] if set(uncertain) - PERSON_HINTS else []) + ([CLOTHING_WARNING] if 'leg_gap' in uncertain
                                                                              else [])
    return {"eligible": True, "pose": pose, "pose_handling": POSE_HANDLING, "up": "Z", "height_metres": height,
            "triangles": len(triangles), "vertices": len(points),
            "facing": facing, "facing_basis": "foot surface extent; confirmed against returned foot joints",
            "shape_hints": uncertain, "warnings": warnings,
            "message": f"Ready for a rigging quote: {len(triangles):,} triangles. Checking was free. {HANDLED}"
                       + (' ' + ' '.join(warnings) if warnings else '')}


def selection(rows):
    """Refuse spatially separate complete bodies, not overlapping clothing parts.

Each candidate must independently have clear head, body, arms and legs. The
remaining uncertain cases go to the worker rather than inferring anatomy here.
"""
    bodies = []
    for _, points, triangles in rows:
        if len(points) < 4 or not triangles:
            continue
        if not geometry(points, triangles)['shape_hints']:
            bodies.append(([min(p[k] for p in points) for k in range(3)],
                           [max(p[k] for p in points) for k in range(3)]))
    for i, (lo, hi) in enumerate(bodies):
        for other_lo, other_hi in bodies[:i]:
            margin = max(hi[2] - lo[2], other_hi[2] - other_lo[2]) * .05
            if any(lo[k] > other_hi[k] + margin or other_lo[k] > hi[k] + margin for k in (0, 1)):
                refuse('Several complete characters are selected. Select just one character, including its clothes, '
                       'hair and eyes.', 'ambiguous_selection')
