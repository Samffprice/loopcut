"""check_contact: whether objects rest on, touch, clear or sit inside each other, measured on their
evaluated world-space geometry, where bounds and pictures let a few millimetres through.

Each object becomes one bmesh in world space and a BVH tree over it. An object with no geometry of
its own (a root empty) stands for all its visible child meshes, a collection instance for its
collection's. Sampling stops at SAMPLE_CAP vertices per object: past it every k-th, the same ones
each time.

rests_on is about gravity (-Z). a's vertices within a band above its lowest point are split into
supports: basins that start at a low point and meet another only higher up, so a lamp's three feet
are three supports whether they are three objects or one mesh. A basin with some of a lower inside
its footprint rests on a itself (a base on its feet) and is not one. Each support is measured along
vertical lines, a's vertices down onto b and b's vertices up into a, and b's solid along a line is
found by parity: a chair leg that passes through a 15 mm rug to the floor is 15 mm into it, not
touching its underside; an odd last crossing is a sheet, such as a ground plane.

touches and clear_of measure in any direction with find_nearest, both ways, from vertices and
face centres (which find two boxes that overlap with every corner on the other's surface). A point
is inside a watertight piece when rays from it cross that piece an odd number of times: parity, not
the normal of the nearest face, whose sign is noise when the nearest point is a rim (anything
beside a disc, level with its bottom) and wrong when faces wind inward (hand-built caps). Open
surfaces have no inside; a point straight behind one counts only when that very face crosses the
other object, so what stands behind a one-sided wall is not in the wall, and a strap loop wound
inward that dips 10 mm into a lens barrel is 10 mm in, not as deep as the barrel's far corner (a
meeting anywhere once counted every point in front of an inward face as behind it).

inside is against b's convex hull: what a bowl, a shelf or a glass holds.

Nothing is ever moved: a failure gives the move that would fix it. Pairs checked with watch=true are
measured again after every run_python (recheck), and a change of status joins its Scene changes.
Every turn also retains its measurements for the final reply: only changed geometry is re-measured,
and a pass invalidated by later edits can bounce the reply once (finish_gate).
The top half is pure and tested in tests/test_contact.py; harness/contact_check.py runs the rest.
"""

import difflib
import math
import re
from array import array
from bisect import bisect_left, bisect_right

RELATIONS = ("rests_on", "touches", "clear_of", "inside")
DEFAULT_TOLERANCE = 0.002
MAX_PAIRS = 12
MAX_WATCHED = 12
MAX_SUPPORTS = 32       # The lowest ones; a mesh with more is noise, not furniture.
SAMPLE_CAP = 100_000    # Vertices measured per object.
SUPPORT_SAMPLES = 2_000  # Vertices of one support measured vertically.
SUPPORT_LINES = 6       # Failing supports listed per pair.
MAX_CROSSINGS = 64      # Surfaces one vertical line may cross.
INSET = 1e-4            # Vertical lines move this far into a support, off b's edges and corners.
STEP = 1e-5             # How far past a crossed surface a vertical line goes on.
# The inside test's rays: off every axis and diagonal, so that no box or cylinder lines up with them.
RAYS = ((0.5129, 0.6203, 0.5933), (-0.7314, 0.2261, 0.6433), (0.1937, -0.8412, 0.5049))
STRAIGHT = 0.99         # Straight behind an open face: p - q this close to the face's inward normal.

SCHEMAS = [{"type": "function", "function": {
    "name": "check_contact",
    "title": "Checking object placement",
    "description": (
        "Measure placement on the evaluated geometry: gap or penetration in m per pair, every support "
        "(foot, leg) of rests_on separately, and the move that would fix a failure; moves nothing. An "
        "empty stands for its child meshes. Use it instead of printing bounds or judging from captures."),
    "parameters": {"type": "object", "properties": {
        "pairs": {"type": "array", "maxItems": MAX_PAIRS, "items": {"type": "object", "properties": {
            "a": {"type": "string"},
            "relation": {"type": "string", "enum": list(RELATIONS),
                         "description": "rests_on: a stands on b's top, every lowest part touching (gravity -Z). "
                                        "touches: in contact, any direction. clear_of: apart by at least gap. "
                                        "inside: within b's convex hull"},
            "b": {"type": "string"},
            "gap": {"type": "number", "description": "clear_of: minimum distance in m, default 0"},
        }, "required": ["a", "relation", "b"]}},
        "tolerance": {"type": "number", "description": "m, default 0.002"},
        "watch": {"type": "boolean", "description": "Measure these pairs again after every later run_python; "
                                                    "a change shows in its Scene changes"},
    }, "required": ["pairs"]},
}}]


# ------------------------------------------------------------------ pure

def sample(count: int, cap: int = SAMPLE_CAP) -> range:
    """Every index up to cap, else every k-th: the same ones each time, so a recheck compares like with like."""
    return range(0, count, max(1, -(-count // cap)))


def relation_of(name) -> str:
    """The relation as RELATIONS spells it ("Rests on" is rests_on), or "" when it is none of them."""
    spelled = str(name or "").strip().lower().replace(" ", "_").replace("-", "_")
    return spelled if spelled in RELATIONS else ""


def parse(pairs: list) -> list[dict]:
    """Each pair as {a, relation, b, gap, error}. One that cannot be checked says why in error, and
    the others still run."""
    parsed = []
    for entry in pairs:
        pair = {"a": "", "relation": "", "b": "", "gap": 0.0, "error": ""}
        parsed.append(pair)
        if not isinstance(entry, dict):
            pair["error"] = 'not a pair: use {"a": ..., "relation": ..., "b": ...}'
            continue
        raw, gap = entry.get("relation"), entry.get("gap", 0)
        pair.update(a=str(entry.get("a") or ""), b=str(entry.get("b") or ""), relation=relation_of(raw) or str(raw))
        if not pair["a"] or not pair["b"]:
            pair["error"] = "needs both a and b"
        elif not relation_of(raw):
            pair["error"] = f"unknown relation {raw!r}: use {', '.join(RELATIONS)}"
        elif isinstance(gap, bool) or not isinstance(gap, (int, float)) or gap < 0:
            pair["error"] = "gap is a distance in m, 0 or more"
        else:
            pair["gap"] = float(gap)
    return parsed


def band(height: float) -> float:
    """How far above a's lowest point its supports are looked for: a foot that hangs 8 mm is still
    one of them, a chair's seat is not."""
    return min(max(0.05 * height, 0.01), 0.03)


def supports(heights: dict, edges, persistence: float) -> list[list[int]]:
    """a's lowest vertices ({index: z}, joined by mesh edges) split into supports, lowest first, each
    lowest vertex first. Sweeping upwards, a vertex with no lower neighbour starts a basin; where two
    meet, the younger stays a support of its own if it is deeper than `persistence`, else it is noise
    in the elder. Separate objects share no edges, so they never meet."""
    neighbours = {i: [] for i in heights}
    for i, j in edges:
        neighbours[i].append(j)
        neighbours[j].append(i)
    root, birth, members, done = {}, {}, {}, []

    def find(i):
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i

    for vertex in sorted(heights, key=lambda i: (heights[i], i)):
        basins = sorted({find(n) for n in neighbours[vertex] if n in root}, key=lambda r: (birth[r], r))
        if not basins:
            root[vertex], birth[vertex], members[vertex] = vertex, heights[vertex], [vertex]
            continue
        elder = basins[0]
        root[vertex] = elder
        members[elder].append(vertex)
        for younger in basins[1:]:
            root[younger] = elder
            if heights[vertex] - birth[younger] > persistence:
                done.append(members.pop(younger))
            else:
                members[elder] += members.pop(younger)
    return sorted(done + list(members.values()), key=lambda group: (heights[group[0]], group[0]))


def lowest_parts(groups: list, points: dict, tolerance: float, limit: int = MAX_SUPPORTS) -> list[list[int]]:
    """The first `limit` supports with nothing of a lower inside their footprint. points: {index: (x, y, z)}."""
    by_height = sorted(points.values(), key=lambda p: p[2])
    heights = [p[2] for p in by_height]
    kept = []
    for group in groups:
        xs, ys = [points[i][0] for i in group], [points[i][1] for i in group]
        lower = by_height[:bisect_left(heights, points[group[0]][2] - tolerance)]
        if not any(min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys) for x, y, _ in lower):
            kept.append(group)
            if len(kept) == limit:
                break
    return kept


def _solids(crossings: list) -> list:
    """Solid along a vertical line from its surface crossings [(z, face)], top down: by parity every
    two crossings bound solid, and an odd last one is a sheet with no thickness (a plane)."""
    return [(crossings[i], crossings[min(i + 1, len(crossings) - 1)]) for i in range(0, len(crossings), 2)]


def offset_below(z: float, crossings: list, tolerance: float):
    """How far a point of a at height z is above b's top surface under it, from b's crossings along
    that vertical: (offset, face, how), negative when the point is into b or through it (a leg that
    reaches the bottom of a rug, or past it). None when the line misses b."""
    if not crossings:
        return None
    solids = _solids(crossings)
    for (top, face), (bottom, _) in solids:
        if bottom - tolerance <= z < top - tolerance:
            return z - top, face, "through" if z <= bottom + tolerance else "into"
    for (top, face), _ in solids:
        if top < z + tolerance:
            return z - top, face, "gap"
    (top, face), _ = solids[-1]
    return z - top, face, "through"


def offset_above(z: float, crossings: list, tolerance: float):
    """The same from b's side: how far a's underside is above a point of b at height z, from a's
    crossings along that vertical. The face is a's, so the result can be put down to a support."""
    if not crossings:
        return None
    solids = _solids(crossings)
    for (top, _), (bottom, face) in solids:
        if bottom + tolerance < z <= top + tolerance:
            return bottom - z, face, "through" if z >= top - tolerance else "into"
    for _, (bottom, face) in reversed(solids):
        if bottom > z - tolerance:
            return bottom - z, face, "gap"
    _, (bottom, face) = solids[0]
    return bottom - z, face, "through"


def _m(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def vector(v) -> str:
    return f"({', '.join(_m(c) for c in v)})"


def _mm(value: float) -> str:
    return f"{value * 1000:.1f} mm" if abs(value) < 1 else f"{_m(round(value, 3))} m"


def _fix(moves: list) -> str:
    return (". Fix: " + "; ".join(f"move {name} by {vector(v)} m" for name, v in moves)) if moves else ""


def _what(offset: float, how: str, b: str) -> str:
    if how == "gap" or offset > 0:
        return f"gap {_mm(max(offset, 0.0))}"
    if how == "through":
        return f"through {b}, {_mm(-offset)} below its top"
    return f"{_mm(-offset)} into {b}"


def rests_on(pair: dict, found: list, tolerance: float, whole: str, over=None) -> dict:
    """The verdict on measured supports: found is [{"part", "xy", "offset", "how", "alone"}], offset
    None where the support is not over b; alone when its part can move by itself (a foot of a lamp
    made of parts). whole: what to move for all of a. over: the move that brings a over b, used when
    no support is."""
    a, b = pair["a"], pair["b"]
    measured = [s for s in found if s["offset"] is not None]
    result = {"status": "fail", "supports": found, "details": [], "fixes": []}
    if not measured:
        result["fixes"] = [(whole, over)] if over else []
        result["line"] = f"no part of {a} is over {b}" + _fix(result["fixes"])
        return result
    low = min(s["offset"] for s in measured)
    lift = -low if abs(low) > tolerance else 0.0
    moves, still = ([(whole, (0.0, 0.0, lift))] if lift else []), []
    counts = {}
    for s in found:
        if s["offset"] is None:
            word = "not over " + b
        elif abs(s["offset"]) <= tolerance:
            word = "touching"
        else:
            word = "floating" if s["offset"] > 0 else f"{s['how']} {b}"
        counts[word] = counts.get(word, 0) + 1
        if word != "touching":
            what = word if s["offset"] is None else _what(s["offset"], s["how"], b)
            result["details"].append(f"{s['part']} at {vector(s['xy'])}: {what}")
        residual = None if s["offset"] is None else s["offset"] + lift
        if residual is not None and residual > tolerance and s["alone"]:
            moves.append((s["part"], (0.0, 0.0, -residual)))
        elif residual is not None and residual > tolerance:
            still.append((s, residual))
    result["fixes"] = moves
    plural = "s" if len(found) > 1 else ""
    if len(counts) == 1 and "touching" in counts:
        result["status"] = "pass"
        largest = max(abs(s["offset"]) for s in found)
        result["line"] = f"{len(found)} support{plural} touching" + (f" (within {_mm(largest)})" if largest >= 5e-5 else "")
        return result
    result["line"] = f"{len(found)} support{plural}: " + ", ".join(f"{n} {word}" for word, n in counts.items())
    result["line"] += _fix(moves)
    if still:
        s, residual = max(still, key=lambda item: item[1])
        result["line"] += f". Then {s['part']} at {vector(s['xy'])} would still float {_mm(residual)}: {a} is tilted or uneven"
    if len(result["details"]) > SUPPORT_LINES:
        extra = len(result["details"]) - SUPPORT_LINES
        result["details"] = result["details"][:SUPPORT_LINES] + [f"(+{extra} more)"]
    return result


def touches(pair: dict, measure: dict, tolerance: float) -> dict:
    """measure: {"distance", "toward", "depth", "out", "meet"}: the closest approach outside and the
    move of a that closes it, the deepest point inside and the move of a that clears it, and whether
    the surfaces meet."""
    a, b = pair["a"], pair["b"]
    if measure["depth"] > tolerance:
        moves = [(a, measure["out"])]
        return {"status": "fail", "failure_reason": "overlap",
                "line": f"{_mm(measure['depth'])} into {b}" + _fix(moves), "fixes": moves}
    distance = 0.0 if measure["meet"] else measure["distance"]  # Surfaces that meet are 0 apart, whatever the vertices say.
    if distance <= tolerance:
        note = (f" ({_mm(measure['depth'])} into)" if measure["depth"] >= 5e-5
                else f" (gap {_mm(distance)})" if distance >= 5e-5 else "")
        return {"status": "pass", "line": "in contact" + note, "fixes": []}
    moves = [(a, measure["toward"])]
    return {"status": "fail", "failure_reason": "too_far",
            "line": f"gap {_mm(measure['distance'])}" + _fix(moves), "fixes": moves}


def _away(direction, length: float) -> tuple:
    """direction scaled to length, rounded up to 0.1 mm so the move clears rather than touches."""
    size = math.sqrt(sum(c * c for c in direction)) or 1.0
    length = math.ceil(length * 10000 - 0.01) / 10000  # Up, but not for float noise.
    return tuple(c / size * length for c in direction)


def clear_of(pair: dict, measure: dict, tolerance: float, apart=(0.0, 0.0, 1.0)) -> dict:
    """As touches; apart is the direction from b to a, for when they touch at a point with no
    direction of its own."""
    a, b = pair["a"], pair["b"]
    needed = max(pair["gap"], tolerance)
    if measure["depth"] > 1e-6:
        moves = [(a, _away(measure["out"], measure["depth"] + needed))]
        return {"status": "fail", "failure_reason": "overlap",
                "line": f"{_mm(measure['depth'])} into {b}" + _fix(moves), "fixes": moves}
    distance = 0.0 if measure["meet"] else measure["distance"]
    if distance > tolerance and distance >= pair["gap"]:
        return {"status": "pass", "line": f"clear by {_mm(distance)}", "fixes": []}
    toward = measure["toward"] if distance > 0 and any(measure["toward"]) else tuple(-c for c in apart)
    moves = [(a, _away(tuple(-c for c in toward), needed - distance))]
    what = "touching" if distance <= tolerance else f"{_mm(distance)} apart, needs {_mm(needed)}"
    return {"status": "fail", "failure_reason": "too_close",
            "line": what + _fix(moves), "fixes": moves}


def inside(pair: dict, measure: dict, tolerance: float) -> dict:
    """measure: {"outside": share of a's vertices outside b's hull, "worst": the farthest out, "back":
    the move that brings it in, "larger": axes along which a is bigger than b}."""
    a, b = pair["a"], pair["b"]
    if measure["worst"] <= tolerance:
        return {"status": "pass", "line": f"inside {b}'s convex hull", "fixes": []}
    moves = [] if measure["larger"] else [(a, measure["back"])]
    line = f"{measure['outside']:.0%} of {a}'s vertices outside {b}'s convex hull, up to {_mm(measure['worst'])}"
    if measure["larger"]:
        line += f"; {a} is larger than {b} along {', '.join(measure['larger'])}"
    return {"status": "fail", "line": line + _fix(moves), "fixes": moves}


def unavailable(pair: dict, reason: str) -> dict:
    return {**pair, "status": "unavailable", "line": reason, "fixes": [], "fingerprint": None}


def title(pair: dict) -> str:
    return f"{pair['a'] or '?'} {pair['relation'] or '?'} {pair['b'] or '?'}"


def pair_line(result: dict) -> str:
    """One line: status, the pair, what was found and the fix."""
    status = "FAIL" if result["status"] == "fail" else result["status"]
    notes = "".join(f" ({note})" for note in result.get("notes", ()))
    return f"{status}: {title(result)}{notes}: {result['line']}."


def report(results: list, tolerance: float) -> str:
    counts = {status: sum(r["status"] == status for r in results) for status in ("pass", "fail", "unavailable")}
    lines = [f"Contact: {counts['pass']} pass, {counts['fail']} fail, {counts['unavailable']} unavailable "
             f"(tolerance {_mm(tolerance)}; moves are world-space, nothing was moved)."]
    for result in results:
        lines.append(pair_line(result))
        if result["status"] == "fail":
            lines += [f"  {detail}" for detail in result.get("details", ())]
    return "\n".join(lines)


def change_line(old: dict, new: dict) -> str:
    """A watched pair whose status changed, for run_python's Scene changes."""
    old_status = "FAIL" if old["status"] == "fail" else old["status"]
    return f"contact {old_status} -> {pair_line(new)}"


def describe(name: str, arguments: dict) -> tuple[str, str] | None:
    """(summary, code) for the tool card."""
    if name != "check_contact":
        return None
    pairs = arguments.get("pairs")
    pairs = pairs if isinstance(pairs, list) else []
    named = [title(p) for p in parse(pairs[:2])]
    more = f", +{len(pairs) - 2} more" if len(pairs) > 2 else ""
    return "Check contact: " + ", ".join(named) + more, ""


# ------------------------------------------------------------------ Blender

_GEOMETRY = {"MESH", "CURVE", "SURFACE", "FONT", "META"}


class Unavailable(Exception):
    """Why an object cannot be measured."""


class Shape:
    """One object's geometry in world space: its parts' evaluated meshes in one bmesh."""

    def __init__(self, name: str, note: str, bm, co: array, starts: list, labels: list):
        self.name, self.note, self.bm, self.co = name, note, bm, co
        self.starts, self.labels = starts, labels
        self.count = len(co) // 3
        self._tree = self._piece = self._open = self._solids = self._box = None
        for elements in (bm.verts, bm.faces):  # from_mesh appends: indices must run on across parts.
            elements.index_update()
            elements.ensure_lookup_table()

    @property
    def tree(self):
        from mathutils.bvhtree import BVHTree
        if self._tree is None:
            self._tree = BVHTree.FromBMesh(self.bm)
        return self._tree

    def point(self, i: int) -> tuple:
        return self.co[3 * i], self.co[3 * i + 1], self.co[3 * i + 2]

    def part(self, vertex: int) -> str:
        return self.labels[bisect_right(self.starts, vertex) - 1]

    def _pieces(self) -> tuple:
        """Each face's connected piece (a root vertex), and the pieces that are not watertight: some
        edge not between exactly two faces. Pieces, not parts: a joined mesh is often shells that
        pass through each other, and parity over two of them reads their overlap as outside."""
        if self._piece is None:
            up = list(range(self.count))

            def root(i):
                while up[i] != i:
                    up[i] = up[up[i]]
                    i = up[i]
                return i

            for e in self.bm.edges:
                i, j = root(e.verts[0].index), root(e.verts[1].index)
                if i != j:
                    up[i] = j
            self._piece = [root(f.verts[0].index) for f in self.bm.faces]
            self._open = {root(e.verts[0].index) for e in self.bm.edges if not e.is_manifold}
        return self._piece, self._open

    def closed(self, face: int) -> bool:
        """Whether the piece holding this face is watertight."""
        piece, unclosed = self._pieces()
        return piece[face] not in unclosed

    def bounds(self) -> tuple:
        return tuple(min(self.co[k::3]) for k in range(3)), tuple(max(self.co[k::3]) for k in range(3))

    def solids(self) -> list:
        """Each watertight piece as (lowest corner, highest corner, a tree over that piece alone)."""
        if self._solids is None:
            from mathutils.bvhtree import BVHTree
            piece, unclosed = self._pieces()
            faces = {}
            for f in self.bm.faces:
                if piece[f.index] not in unclosed:
                    faces.setdefault(piece[f.index], []).append([v.index for v in f.verts])
            self._solids = []
            for polygons in faces.values():
                used = sorted({i for face in polygons for i in face})
                local = {i: k for k, i in enumerate(used)}
                points = [self.point(i) for i in used]
                lo = tuple(min(p[axis] for p in points) for axis in range(3))
                hi = tuple(max(p[axis] for p in points) for axis in range(3))
                tree = BVHTree.FromPolygons(points, [[local[i] for i in face] for face in polygons], all_triangles=False)
                self._solids.append((lo, hi, tree))
        return self._solids

    def holds(self, p) -> bool:
        """Whether p is inside one of the watertight pieces."""
        x, y, z = p
        if self._box is None:
            self._box = self.bounds()
        (x0, y0, z0), (x1, y1, z1) = self._box
        if not (x0 <= x <= x1 and y0 <= y <= y1 and z0 <= z <= z1):
            return False
        for lo, hi, tree in self.solids():
            if lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1] and lo[2] <= z <= hi[2] and _enclosed(tree, p):
                return True
        return False


def _enclosed(tree, p) -> bool:
    """An odd number of crossings along two of three rays. Parity does not care which way faces
    wind, so a part with its caps wound inward is as solid as any; the third ray settles a ray that
    grazes an edge."""
    from mathutils import Vector
    rays = [Vector(d).normalized() for d in RAYS]
    votes = [_odd(tree, p, rays[0]), _odd(tree, p, rays[1])]
    if votes[0] != votes[1]:
        votes.append(_odd(tree, p, rays[2]))
    return sum(votes) >= 2


def _odd(tree, p, direction) -> bool:
    """Whether a ray from p crosses tree's surface an odd number of times. Going on STEP past each
    crossing counts a ray through a shared edge once, not once per face."""
    crossings, origin = 0, p
    while crossings < MAX_CROSSINGS:
        location = tree.ray_cast(origin, direction)[0]
        if location is None:
            break
        crossings += 1
        origin = location + direction * STEP
    return crossings % 2 == 1


def _own(obj, depsgraph, matrix=None, prefix: str = "", depth: int = 0) -> list:
    """obj's own geometry as (label, evaluated object, world matrix): itself, or what it instances."""
    from mathutils import Matrix
    evaluated = obj.evaluated_get(depsgraph)
    world = evaluated.matrix_world if matrix is None else matrix @ evaluated.matrix_world
    if obj.type in _GEOMETRY:
        return [(prefix + obj.name, evaluated, world.copy())]
    collection = obj.instance_collection if obj.instance_type == "COLLECTION" else None
    if collection is None or depth > 3:
        return []
    base = world @ Matrix.Translation(-collection.instance_offset)
    return [part for member in collection.all_objects
            for part in _own(member, depsgraph, base, f"{prefix}{obj.name}/", depth + 1)]


def _parts(obj, depsgraph) -> tuple[list, str]:
    """What obj stands for, and a note when that is not obj itself."""
    own = _own(obj, depsgraph)
    if own:
        return own, "" if obj.type in _GEOMETRY else f"{obj.name}: {len(own)} instanced mesh{'es' * (len(own) > 1)}"
    children = [part for child in obj.children_recursive if child.visible_get() for part in _own(child, depsgraph)]
    many = "es" * (len(children) > 1)
    return children, f"{obj.name}: union of its {len(children)} child mesh{many}" if children else ""


def _geometry(name: str, depsgraph) -> tuple[list, str]:
    """Find the evaluated parts without constructing measurement geometry."""
    import bpy
    objects = bpy.context.scene.objects
    obj = objects.get(name)
    if obj is None:
        close = difflib.get_close_matches(name, objects.keys(), 1)
        raise Unavailable(f'no object named "{name}" in this scene' + (f' (did you mean "{close[0]}"?)' if close else ""))
    parts, note = _parts(obj, depsgraph)
    if not parts:
        kind = obj.type.lower().replace("_", " ")
        raise Unavailable(f"{name} is {'an' if kind[0] in 'aeiou' else 'a'} {kind} with no mesh and no mesh children")
    return parts, note


def _fingerprint(name: str, depsgraph) -> int:
    """Read evaluated mesh data directly; only non-mesh geometry needs temporary conversion.

    Local coordinates plus world matrices detect the same edits without transforming, validating
    or copying meshes into BMeshes. Materials are deliberately absent from the fingerprint.
    """
    parts, _ = _geometry(name, depsgraph)
    prints = []
    for label, evaluated, matrix in parts:
        convert = evaluated.type != "MESH"
        try:
            mesh = evaluated.to_mesh() if convert else evaluated.data
        except RuntimeError:
            prints.append((label, "unavailable"))
            continue
        try:
            coords = array("f", bytes(12 * len(mesh.vertices)))
            mesh.vertices.foreach_get("co", coords)
            prints.append(hash(_mesh_fingerprint(label, matrix, mesh, coords)))
        finally:
            if convert:
                evaluated.to_mesh_clear()
    return hash(tuple(prints))


def _shape(name: str, depsgraph, faces=None) -> Shape:
    """faces(evaluated, mesh) -> the polygon indices of that part to keep: a region of a joined mesh
    (acceptance's a_part, b_part), built face by face; None keeps every face."""
    import bmesh
    parts, note = _geometry(name, depsgraph)
    bm, co, starts, labels, skipped = bmesh.new(), array("f"), [], [], []
    for label, evaluated, matrix in parts:
        try:
            mesh = evaluated.to_mesh()
        except RuntimeError:
            skipped.append(f"{label} ({evaluated.type.lower()}) could not be evaluated as a mesh")
            continue
        try:
            if not mesh.polygons:
                skipped.append(f"{label} has no faces after evaluation")
                continue
            mesh.transform(matrix)
            if matrix.determinant() < 0:  # A mirror turns the faces inside out; the normal sign needs them out.
                mesh.flip_normals()
            # A hand-built mesh (bmesh.to_mesh with no validate(), a bad boolean, a botched modifier)
            # can carry a polygon with an out-of-range or repeated vertex index. bm.from_mesh below
            # trusts that indexing and crashes Blender itself on it; validate() repairs or drops the
            # offending polygons first, so a malformed prop degrades to "fewer faces", not a segfault.
            mesh.validate(clean_customdata=False)
            keep = None if faces is None else list(faces(evaluated, mesh))
            if keep is not None and not keep:
                skipped.append(f"{label} has none of the faces asked for")
                continue
            if keep is not None and len(keep) < len(mesh.polygons):
                coords = _region_into(bm, mesh, keep)
            else:
                keep = None
                coords = array("f", bytes(12 * len(mesh.vertices)))
                mesh.vertices.foreach_get("co", coords)
            starts.append(len(co) // 3)
            labels.append(label)
            co.extend(coords)
            if keep is None:
                bm.from_mesh(mesh)
        finally:
            evaluated.to_mesh_clear()
    if not labels:
        bm.free()
        raise Unavailable("; ".join(skipped))
    notes = [note] if note else []
    if skipped:
        notes.append("left out: " + "; ".join(skipped))
    return Shape(name, "; ".join(notes), bm, co, starts, labels)


def _mesh_fingerprint(label, matrix, mesh, coords, keep=None) -> tuple:
    """Evaluated transforms, coordinates and topology, including rewired faces of equal size."""
    topology = []
    for collection, field, width in ((mesh.loops, "vertex_index", 1),
                                     (mesh.polygons, "loop_total", 1), (mesh.edges, "vertices", 2)):
        values = array("i", [0]) * (len(collection) * width)
        collection.foreach_get(field, values)
        topology.append(values.tobytes())
    return (label, tuple(tuple(row) for row in matrix), coords.tobytes(), *topology, tuple(keep or ()))


def _region_into(bm, mesh, keep: list) -> array:
    """The kept polygons of a mesh appended to bm with only the vertices they use, in order, and
    those vertices' coordinates. New elements go at the end (nothing is deleted from bm), so the
    vertex ranges of the parts before stay as Shape's starts say."""
    polygons, vertices = mesh.polygons, mesh.vertices
    used = sorted({v for i in keep for v in polygons[i].vertices})
    made = dict(zip(used, (bm.verts.new(vertices[v].co) for v in used)))
    for i in keep:
        try:
            bm.faces.new([made[v] for v in polygons[i].vertices])
        except ValueError:  # A face that already exists: one of a doubled pair.
            pass
    return array("f", [c for v in used for c in vertices[v].co])


def _crossings(tree, x: float, y: float, top: float) -> list:
    """Every surface a vertical line at (x, y) crosses from top down, as [(z, face)]."""
    from mathutils import Vector
    down, found = Vector((0.0, 0.0, -1.0)), []
    origin = Vector((x, y, top))
    while len(found) < MAX_CROSSINGS:
        location, _, face, _ = tree.ray_cast(origin, down)
        if location is None:
            break
        found.append((location.z, face))
        origin = Vector((x, y, location.z - STEP))
    return found


def _inset(x: float, y: float, cx: float, cy: float) -> tuple:
    dx, dy = cx - x, cy - y
    size = math.hypot(dx, dy)
    return (x, y) if size <= INSET else (x + dx / size * INSET, y + dy / size * INSET)


def _measure_rests_on(pair: dict, a: Shape, b: Shape, tolerance: float) -> dict:
    zs = a.co[2::3]
    lowest = min(zs)
    reach = lowest + band(max(zs) - lowest)
    low = sorted((i for i in range(a.count) if zs[i] <= reach), key=zs.__getitem__)[:SAMPLE_CAP]
    heights = {i: zs[i] for i in low}
    verts = a.bm.verts
    edges = [(i, j) for i in low for j in (e.other_vert(verts[i]).index for e in verts[i].link_edges)
             if i < j and j in heights]
    points = {i: a.point(i) for i in low}
    groups = lowest_parts(supports(heights, edges, tolerance), points, tolerance)
    top_b = max(b.co[2::3]) + 1.0
    owner, found = {}, []
    for k, group in enumerate(groups):
        owner.update((i, k) for i in group)
        cx = sum(points[i][0] for i in group) / len(group)
        cy = sum(points[i][1] for i in group) / len(group)
        hits = []
        for i in group[::max(1, -(-len(group) // SUPPORT_SAMPLES))]:
            x, y = _inset(points[i][0], points[i][1], cx, cy)
            hit = offset_below(points[i][2], _crossings(b.tree, x, y, top_b), tolerance)
            if hit:
                hits.append(hit)
        found.append({"part": a.part(group[0]), "xy": (round(cx, 3), round(cy, 3)), "hits": hits})
    # b's vertices under a's lowest parts, measured upwards: a table top's underside over its legs,
    # which its own vertices, all out at the corners, never are.
    xs, ys = [p[0] for p in points.values()], [p[1] for p in points.values()]
    box = (min(xs) - tolerance, max(xs) + tolerance, min(ys) - tolerance, max(ys) + tolerance)
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    top_a = max(zs) + 1.0
    for j in sample(b.count):
        x, y, z = b.point(j)
        if z > reach + tolerance or not (box[0] <= x <= box[1] and box[2] <= y <= box[3]):
            continue
        hit = offset_above(z, _crossings(a.tree, *_inset(x, y, cx, cy), top_a), tolerance)
        if hit:
            k = next((owner[v.index] for v in a.bm.faces[hit[1]].verts if v.index in owner), None)
            if k is not None:
                found[k]["hits"].append(hit)
    parts = [s["part"] for s in found]
    for s in found:
        offset, _, how = min(s.pop("hits"), default=(None, None, ""), key=lambda h: h[0])
        s.update(offset=offset, how=how, alone=len(a.labels) > 1 and parts.count(s["part"]) == 1)
    (b_lo, b_hi) = b.bounds()
    over = ((b_lo[0] + b_hi[0]) / 2 - cx, (b_lo[1] + b_hi[1]) / 2 - cy, b_hi[2] - lowest)
    return rests_on(pair, found, tolerance, pair["a"], over)


# A point this close to a surface is on it, not in it, whichever side float noise puts it.
SIDE = 1e-6


def _probes(shape: Shape) -> list:
    from mathutils import Vector
    faces = shape.bm.faces
    return ([Vector(shape.point(i)) for i in sample(shape.count)]
            + [faces[i].calc_center_median() for i in sample(len(faces))])


def _within(target: Shape, p, q, normal, face: int, distance: float, crossing: set) -> bool:
    """Whether p is in target: inside a watertight piece, or straight behind a face of an open piece
    (q the foot of the perpendicular from p, not a rim or an edge) that itself crosses the other
    object: crossing holds target's faces that do."""
    if target.holds(p):
        return True
    return face in crossing and (p - q).dot(normal) < -STRAIGHT * distance and not target.closed(face)


def _nearest(a: Shape, b: Shape) -> dict:
    """Both ways: a's probes against b's surface, b's against a's. Vectors are moves of a. meet:
    some triangle of a's crosses or touches one of b's."""
    pairs = a.tree.overlap(b.tree)
    meet = bool(pairs)
    crossing = {id(a): {i for i, _ in pairs}, id(b): {j for _, j in pairs}}  # Each one's faces that meet the other.
    measure = {"distance": math.inf, "toward": (0.0, 0.0, 0.0), "depth": 0.0, "out": (0.0, 0.0, 0.0), "meet": meet}
    for source, target, sign in ((a, b, 1.0), (b, a, -1.0)):
        tree = target.tree
        for p in _probes(source):
            q, normal, face, distance = tree.find_nearest(p)
            if q is None or measure["distance"] <= distance <= measure["depth"]:
                continue  # Neither closer outside nor deeper inside than what is already found.
            if distance > SIDE and _within(target, p, q, normal, face, distance, crossing[id(target)]):
                if distance > measure["depth"]:
                    measure["depth"], measure["out"] = distance, tuple(sign * c for c in (q - p))
            elif distance < measure["distance"]:
                measure["distance"], measure["toward"] = distance, tuple(sign * c for c in (q - p))
    return measure


def _measure_inside(pair: dict, a: Shape, b: Shape, tolerance: float) -> dict:
    import bmesh
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    hull = bmesh.new()
    try:
        for j in sample(b.count):
            hull.verts.new(b.point(j))
        try:
            bmesh.ops.convex_hull(hull, input=list(hull.verts))
        except (RuntimeError, ValueError):
            pass
        if not hull.faces:
            return unavailable(pair, f"{pair['b']} is flat: it has no volume to be inside of")
        tree = BVHTree.FromBMesh(hull)
    finally:
        hull.free()
    worst, back, out, taken = 0.0, (0.0, 0.0, 0.0), 0, sample(a.count)
    for i in taken:
        p = Vector(a.point(i))
        q, normal, _, distance = tree.find_nearest(p)
        if q is not None and (p - q).dot(normal) > 0:
            out += distance > tolerance
            if distance > worst:
                worst, back = distance, tuple(q - p)
    (a_lo, a_hi), (b_lo, b_hi) = a.bounds(), b.bounds()
    larger = [axis for k, axis in enumerate("xyz") if a_hi[k] - a_lo[k] > b_hi[k] - b_lo[k] + tolerance]
    return inside(pair, {"outside": out / len(taken), "worst": worst, "back": back, "larger": larger}, tolerance)


def _measure(pair: dict, a: Shape, b: Shape, tolerance: float) -> dict:
    if pair["relation"] == "rests_on":
        return _measure_rests_on(pair, a, b, tolerance)
    if pair["relation"] == "inside":
        return _measure_inside(pair, a, b, tolerance)
    measure = _nearest(a, b)
    if pair["relation"] == "touches":
        return touches(pair, measure, tolerance)
    (a_lo, a_hi), (b_lo, b_hi) = a.bounds(), b.bounds()
    apart = tuple((a_lo[k] + a_hi[k] - b_lo[k] - b_hi[k]) / 2 for k in range(3))
    return clear_of(pair, measure, tolerance, apart if any(apart) else (0.0, 0.0, 1.0))


class _Shapes(dict):
    """Fingerprints cached separately from lazily constructed measurement shapes; freed after."""

    def __init__(self):
        import bpy
        super().__init__()
        self.depsgraph = bpy.context.evaluated_depsgraph_get()
        self.prints = {}

    def _read(self, name: str, read):
        """Read in a view layer that includes the object (the active one first): one the
        active layer excludes is otherwise read from its stale matrices; one every layer excludes is
        Unavailable, not a number."""
        import bpy
        from . import view_layers
        obj = bpy.context.scene.objects.get(name)
        layer = view_layers.home(obj) if obj is not None else None
        try:
            if obj is not None and layer is None:
                raise Unavailable(view_layers.unavailable([name]))
            if layer is None or layer == self.depsgraph.view_layer:
                return read(name, self.depsgraph)
            with view_layers.evaluating(layer):
                return read(name, bpy.context.evaluated_depsgraph_get())
        except Unavailable as ex:
            return ex

    def fingerprint(self, name: str):
        if name not in self.prints:
            self.prints[name] = self._read(name, _fingerprint)
        return self.prints[name]

    def of(self, name: str):
        if name not in self:
            self[name] = self._read(name, _shape)
        return self[name]

    def free(self) -> None:
        for shape in self.values():
            if isinstance(shape, Shape):
                shape.bm.free()


def _evaluate(pair: dict, shapes: _Shapes, tolerance: float, previous: dict | None = None) -> dict:
    """One pair's result; previous is returned as it was when neither object's geometry changed."""
    if pair["error"]:
        return unavailable(pair, pair["error"])
    prints = [shapes.fingerprint(pair[key]) for key in ("a", "b")]
    for value in prints:
        if isinstance(value, Unavailable):
            return unavailable(pair, str(value))
    fingerprint = (*prints, tolerance)
    if previous is not None and previous.get("fingerprint") == fingerprint:
        return previous
    a, b = shapes.of(pair["a"]), shapes.of(pair["b"])
    for side in (a, b):
        if isinstance(side, Unavailable):
            return unavailable(pair, str(side))
    if a is b:
        return unavailable(pair, "a and b are the same object")
    shared = sorted(set(a.labels) & set(b.labels))
    if shared:
        return unavailable(pair, f"{shared[0]} is part of both {a.name} and {b.name}")
    try:
        result = _measure(pair, a, b, tolerance)
    except (RuntimeError, ValueError) as ex:  # bmesh and the BVH trees on geometry they cannot take.
        return unavailable(pair, f"could not be measured: {ex}")
    notes = [shape.note for shape in (a, b) if shape.note]
    return {**pair, **result, "notes": notes, "fingerprint": fingerprint if result["status"] != "unavailable" else None}


def evaluate(pairs: list, tolerance: float) -> list[dict]:
    shapes = _Shapes()
    try:
        return [_evaluate(pair, shapes, tolerance) for pair in parse(pairs)]
    finally:
        shapes.free()


def _watch(results: list, tolerance: float) -> str:
    """Keeps the checked pairs for recheck, for the rest of the conversation."""
    from . import state
    watched = state.session().setdefault("contact_watch", [])
    for result in results:
        if result["error"]:
            continue
        pair = {key: result[key] for key in ("a", "relation", "b", "gap", "error")}
        watched[:] = [entry for entry in watched if entry["pair"] != pair]
        watched.append({"pair": pair, "tolerance": tolerance, "result": result})
    del watched[:-MAX_WATCHED]
    return (f"Watching {len(watched)} pair{'s' if len(watched) != 1 else ''}: measured again after each "
            f"run_python, a change of status listed in its Scene changes.")


def recheck() -> list[str]:
    """After a run_python: the watched pairs measured again, one line for each whose status changed.
    Nothing is read when nothing is watched, and a pair whose objects' geometry and transforms are
    unchanged keeps its result without being measured."""
    from . import state
    watched = state.session().get("contact_watch")
    if not watched:
        return []
    shapes, lines = _Shapes(), []
    try:
        for entry in watched:
            result = _evaluate(entry["pair"], shapes, entry["tolerance"], entry["result"])
            if result["status"] != entry["result"]["status"]:
                lines.append(change_line(entry["result"], result))
            entry["result"] = result
    except Exception as ex:  # Never at the cost of the step's own result; shown, not swallowed.
        lines.append(f"contact recheck failed: {ex!r}")
    finally:
        shapes.free()
    return lines


def check_contact(pairs: list, tolerance: float = DEFAULT_TOLERANCE, watch: bool = False):
    from .tools import ToolError, ToolResult
    if not isinstance(pairs, list) or not pairs:
        raise ToolError('pairs: a list of {"a": ..., "relation": ..., "b": ...}')
    if len(pairs) > MAX_PAIRS:
        raise ToolError(f"At most {MAX_PAIRS} pairs per call.")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 0.1:
        raise ToolError("tolerance is in m, from 0 to 0.1")
    results = evaluate(pairs, float(tolerance))
    _remember(results, float(tolerance))
    text = report(results, float(tolerance))
    if watch:
        text += "\n" + _watch(results, float(tolerance))
    return ToolResult(text)


def _remember(results: list, tolerance: float) -> None:
    """Every live turn's checks, even without watch; a later explicit check replaces the old one."""
    from . import state
    turn = state.session().get("turn")
    if turn is None:
        return
    if not hasattr(turn, "contact_checks"):  # A turn already running when the add-on was reloaded.
        turn.contact_checks = []
    for result in results:
        if result.get("error"):
            continue
        pair = {key: result[key] for key in ("a", "relation", "b", "gap", "error")}
        turn.contact_checks[:] = [entry for entry in turn.contact_checks if entry["pair"] != pair]
        turn.contact_checks.append({"pair": pair, "tolerance": tolerance, "result": result})


def _plain_name(name: str) -> str:
    """Object names as words, including CamelCase and separated names."""
    name = re.sub(r"([A-Z])([A-Z][a-z])", r"\1 \2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return " ".join(re.sub(r"[_./-]+", " ", name).lower().split())


def _plain_check(result: dict) -> str:
    a, b = (_plain_name(result[key]) for key in ("a", "b"))
    relation = result["relation"]
    verb = {"touches": "touches", "rests_on": "rests on", "clear_of": "clears", "inside": "sits inside"}
    if result["status"] == "unavailable":
        return f"Could not check: whether {a} {verb[relation]} {b}."
    if result["status"] == "fail":
        failure = {"touches": "does not meet the required contact with",
                   "rests_on": "does not have the required support from",
                   "clear_of": "does not have the required clearance from",
                   "inside": "does not sit fully inside"}
        text = f"Failed: {a} {failure[relation]} {b}."
        reason = result.get("failure_reason")
        if reason == "overlap":
            text += (f" It should be clear of {b} but overlaps it." if relation == "clear_of"
                     else f" It extends too far into {b}.")
        elif reason == "too_far":
            text += f" It is too far from {b}."
        elif reason == "too_close":
            text += f" It is too close to {b}."
        return text
    return f"Passed: {a} {verb[relation]} {b}."


def rechecked_line(result: dict) -> str:
    """A plain panel notice; raw names, measurements and corrective moves stay in the details."""
    return "Re-checked after later edits: " + _plain_check(result)


def refresh(turn) -> tuple[list, list]:
    """On the main thread at a final reply. Reuse unchanged measurements; never capture or render.

    Fail open per pair (including missing objects), but never count an old pass as fresh on error.
    Failures that replace passes are returned separately for the turn's one correction bounce.
    """
    entries = turn.contact_checks
    results, regressed, shapes = [], [], None
    if not entries:
        return results, regressed
    try:
        shapes = _Shapes()
        for entry in entries:
            previous = entry["result"]
            try:
                result = _evaluate(entry["pair"], shapes, entry["tolerance"], previous)
            except Exception as ex:
                result = unavailable(entry["pair"], f"could not re-check: {ex}")
            results.append(result)
            if previous["status"] == "pass" and result["status"] == "fail":
                regressed.append(result)
            entry["result"] = result
    except Exception as ex:
        results = [unavailable(e["pair"], f"could not re-check: {ex}") for e in entries]
        regressed = []
    finally:
        if shapes is not None:
            try:
                shapes.free()
            except Exception:
                pass
    return results, regressed


def finish_gate(session: dict, turn, completion, run_refresh):
    """One bounce per turn for stale passes; every final attempt still gets fresh measurements."""
    from . import orchestrator, state
    if not getattr(turn, "contact_checks", None) or turn.cancel.is_set():
        return None
    try:
        results, regressed = run_refresh()
    except Exception as ex:  # Scheduling/evaluation errors must not prevent the reply.
        results = [unavailable(e["pair"], f"could not re-check: {ex}") for e in turn.contact_checks]
        regressed = []
    if turn.cancel.is_set():
        return None
    for result in regressed:
        session["items"].append(state.item_notice(rechecked_line(result)))
    if regressed and not getattr(turn, "contact_bounced", False) and turn.outcome == "running":
        turn.contact_bounced = True
        return ("[Loopcut] Re-checked after later edits. Earlier passing checks no longer hold:\n"
                + "\n".join(pair_line(r) for r in regressed)
                + "\nUse these fresh measurements. Correct the placement and check it, or clearly state what "
                  "still fails; do not claim it is connected or verified. This reminder runs once per turn.")
    passed = sum(r["status"] == "pass" for r in results)
    lines = [f"Verified by Loopcut on this scene: {passed} of {len(results)} checks."]
    if results:
        extra = len(results) - 3
        more = f" Plus {extra} other check{'s' if extra != 1 else ''}." if extra > 0 else ""
        lines.extend(_plain_check(r) for r in results[:3])
        if more:
            lines.append(more.strip())
    orchestrator._append_to_reply(session, completion, "\n".join(lines))
    return None


DISPATCH = {"check_contact": check_contact}
