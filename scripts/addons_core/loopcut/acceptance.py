"""Acceptance checks: what a brief says must be true of the scene, measured by Loopcut instead of
claimed by a model.

A check is {"kind", "quote", ...}: the kind says what is measured and how (KINDS), the quote is
the user's own words it comes from. validate() is pure, for the orchestrator's delegate call: it
names every malformed field, and with the user's messages it refuses a quote the user never
wrote (after normalise(): case, quotes and dashes, punctuation and spacing do not count; "..."
skips words). run() measures on the open scene, the same code in a worker's child Blender (its
check_brief and report) and in the parent (after a merge, and in the finish gate). Each result
is {"id", "kind", "quote", "status": "pass" | "fail" | "unavailable" | "deferred", "detail"}: the
detail has the numbers and, where the measuring tool gives one, the fix. In a worker's copy, a
check that names an object the copy does not have and the worker does not make (another worker's
armchair) is "deferred": it runs on the live scene after the merge. verified_block() is the text
the finish gate appends to the orchestrator's final reply.

A brief's advisory list holds the brief's own targets rather than the user's words: any kind without
a quote, and ADVISORY_KINDS (no_modifiers, flat_shaded, inside_bounds, triangles), measured through
facts.measure. run(advisory=True) marks each result "advisory": a miss is shown to the worker and
the orchestrator, and never fails a worker, refuses its report or holds up the finish.

Measuring reuses the tools the models use: contact.evaluate for contact, overlap and a light
inside its shade, camera.coverage for framing, camera._geometry (evaluated vertices, children
and instances included) for sizes and origins, ownership.token for unchanged. The relation kinds
(suspended, attached_to, above, below) measure contact's shapes, whole objects or a region of one
(a_part, b_part: the faces of a joined mesh with a material, a vertex group or a face attribute), so
"a hanging sign" is checked as hanging: its board clear of the ground and hung from its chains.
"""

import itertools
import math
import os
import re
import unicodedata

MAX_NAMES = 5          # no_overlap: at most 10 pairs.
MAX_LIST = 12          # in_frame, unchanged and preserved names.
MAX_SNAPSHOT = 40      # Objects one snapshot records.
# preserved: what a snapshot records per object, each of which a check may exclude (a look pass
# excludes materials, a remodel vertices and topology); and how far a value may drift.
ASPECTS = ("origin", "transform", "dims", "bounds", "topology", "vertices", "materials")
KEEP_TOL, TURN_TOL, SCALE_TOL = 0.0005, 0.05, 0.0005  # m, degrees, factor.
DIMS_TOL, DIMS_SHARE = 0.005, 0.02  # dims: 5 mm or 2 % of the size, whichever is larger.
ORIGIN_TOL = 0.005
BASE_BAND = 0.01      # origin_base: the lowest geometry is what lies within this of the lowest point.
REGION_TOL = 0.002
OBJECT_TYPES = ("MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GREASEPENCIL",
                "ARMATURE", "LATTICE", "EMPTY", "LIGHT", "LIGHT_PROBE", "CAMERA", "SPEAKER")
RELATIONS = ("rests_on", "touches", "clear_of", "inside")
PARTS = ("material", "group", "attribute")  # a_part / b_part: "material:Wood", a region of a joined mesh.
ATTACH_TOL = 0.005    # suspended, attached_to: this close counts as attached.
CLEAR = 0.05          # suspended: the hanging part's lowest point at least this far above the ground.
LIGHT_TYPES = ("POINT", "SUN", "SPOT", "AREA")
COLOR_TOL = 0.05      # light: per channel.
# setting: the scene values a check may read; everything else about the scene is the orchestrator's.
SETTINGS = {
    "render.engine": lambda s: s.render.engine,
    "render.resolution_x": lambda s: s.render.resolution_x,
    "render.resolution_y": lambda s: s.render.resolution_y,
    "render.resolution_percentage": lambda s: s.render.resolution_percentage,
    "render.fps": lambda s: s.render.fps / s.render.fps_base,
    "frame_start": lambda s: s.frame_start,
    "frame_end": lambda s: s.frame_end,
    "render.image_settings.file_format": lambda s: s.render.image_settings.file_format,
    "cycles.samples": lambda s: s.cycles.samples,
    "eevee.taa_render_samples": lambda s: s.eevee.taa_render_samples,
    "camera": lambda s: s.camera.name if s.camera else None,
    "world": lambda s: s.world.name if s.world else None,
}
_TEXT_SETTINGS = {"render.engine", "render.image_settings.file_format", "camera", "world"}

# kind: (required fields, optional fields). Every check also has kind and quote, and may have id.
KINDS = {
    "exists": (("name",), ("type",)),
    "count": ((), ("prefix", "collection", "type", "min", "max")),
    "dims": (("name", "size"), ("tol",)),
    "contact": (("a", "relation", "b"), ("gap", "tol")),
    # Spatial relations, a and b whole objects or regions of one (a_part, b_part): a hangs from b,
    # clear of the ground below; a is fixed to b; a is higher than b and over it (below: lower, under).
    "suspended": (("a", "b"), ("a_part", "b_part", "ground", "clear", "tol")),
    "attached_to": (("a", "b"), ("a_part", "b_part", "tol")),
    "above": (("a", "b"), ("a_part", "b_part", "gap")),
    "below": (("a", "b"), ("a_part", "b_part", "gap")),
    "no_overlap": (("names",), ("tol",)),
    "in_frame": (("camera", "names"), ("min_inside", "region", "frame")),
    "faces_max": (("name", "max"), ()),
    "origin_base": (("name",), ("tol", "origin_xy")),
    "material_on": (("name", "material"), ()),
    "light_inside": (("light", "container"), ()),
    "light": (("name",), ("type", "energy", "color", "tol", "spot_size")),
    "setting": (("path",), ("equals", "min", "max")),
    "animated": (("name", "path", "frames"), ()),
    "unchanged": (("names",), ("tokens",)),
    "preserved": (("names", "snapshot"), ("exclude", "baseline")),  # baseline: the snapshot's records, Loopcut's.
    "file": (("path",), ()),
}
# Advisory kinds: targets the brief itself states (its budget, flat shading, modifiers applied, the
# planned box), not the user's words. They go only in a brief's advisory list, which any kind may
# join without a quote: measured (facts.measure) and shown to the worker and the orchestrator, they
# never fail a worker, block its report or hold up the finish (owner decision, 2026-09-29).
ADVISORY_KINDS = {
    "no_modifiers": (("name",), ()),
    "flat_shaded": (("name",), ("max_smooth",)),
    "inside_bounds": (("name",), ("bounds", "tol")),
    "triangles": (("name",), ("min", "max")),
}
MAX_ADVISORY = 8

_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                         "“": '"', "”": '"', "„": '"', "″": '"',
                         "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"})
_WORD = re.compile(r"[^\W_]+(?:['.][^\W_]+)*")
_GAP = re.compile(r"\.\.\.+|…|\[\.\.\.\]")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")


# ------------------------------------------------------------------ quotes (pure)

def normalise(text: str) -> str:
    """Words only, lower case: curly quotes, dashes, punctuation, spacing and width don't count.
    Apostrophes and decimal points inside a word stay ("don't", "0.45"); a decimal comma is a
    point ("0,45" is "0.45")."""
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_QUOTES).casefold()
    return " ".join(_WORD.findall(_DECIMAL_COMMA.sub(".", text)))


def quote_found(quote: str, user_texts) -> bool:
    """Whether the quote is in one of the user's messages, word for word after normalise(); an
    ellipsis in the quote skips words, the pieces in order."""
    pieces = [normalise(piece) for piece in _GAP.split(str(quote or ""))]
    pieces = [piece for piece in pieces if piece]
    if not pieces:
        return False
    for text in [user_texts] if isinstance(user_texts, str) else user_texts or ():
        haystack, start = f" {normalise(text)} ", 0
        for piece in pieces:
            at = haystack.find(f" {piece} ", start)
            if at < 0:
                break
            start = at + len(piece) + 1
        else:
            return True
    return False


# ------------------------------------------------------------------ validation (pure)

def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _name(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _names(value, low: int, high: int) -> bool:
    return (isinstance(value, list) and low <= len(value) <= high and all(_name(v) for v in value)
            and len(set(value)) == len(value))


def _part(value) -> bool:
    kind, _, name = value.partition(":") if isinstance(value, str) else ("", "", "")
    return kind in PARTS and bool(name.strip())


def _range(value) -> bool:
    """[min, max], either null for open, not both; 0 or more; min <= max."""
    return (isinstance(value, list) and len(value) == 2 and all(v is None or _number(v) and v >= 0 for v in value)
            and any(v is not None for v in value) and (None in value or value[0] <= value[1]))


def _field_errors(check: dict) -> list[str]:
    """(field, problem) for one check's own fields, the kind known."""
    kind, c = check["kind"], check
    problems = []

    def need(field: str, ok: bool, what: str) -> None:
        if field in c and not ok:
            problems.append(f"{field}: {what}")

    for field in ("name", "a", "b", "light", "container", "material", "camera", "prefix", "collection", "ground"):
        need(field, _name(c.get(field)), "a name")
    for field in ("a_part", "b_part"):
        need(field, _part(c.get(field)), "material:NAME, group:NAME or attribute:NAME (faces of a joined mesh)")
    types = LIGHT_TYPES if kind == "light" else OBJECT_TYPES
    need("type", isinstance(c.get("type"), str) and c.get("type", "").upper() in types,
         f"a light type: {', '.join(LIGHT_TYPES)}" if kind == "light" else f"an object type: {', '.join(OBJECT_TYPES[:6])}, ...")
    for field in ("tol", "gap"):
        need(field, _number(c.get(field)) and 0 <= c.get(field) <= 1,
             "a colour difference per channel, 0 to 1" if kind == "light" else "a distance in m, 0 to 1")
    need("frame", isinstance(c.get("frame"), int) and not isinstance(c.get("frame"), bool), "a whole frame number")
    if kind == "count":
        if ("prefix" in c) == ("collection" in c):
            problems.append("prefix or collection: exactly one of them")
        if "min" not in c and "max" not in c:
            problems.append("min or max: at least one")
        for field in ("min", "max"):
            need(field, isinstance(c.get(field), int) and not isinstance(c.get(field), bool) and c.get(field) >= 0,
                 "a whole number, 0 or more")
        if isinstance(c.get("min"), int) and isinstance(c.get("max"), int) and c["min"] > c["max"]:
            problems.append("min: more than max")
    elif kind == "dims":
        size = c.get("size")
        if not (isinstance(size, list) and len(size) == 3 and all(v is None or _number(v) and v > 0 for v in size)
                and any(v is not None for v in size)):
            problems.append("size: [x, y, z] in m, null for any, at least one number")
    elif kind == "contact":
        need("relation", c.get("relation") in RELATIONS, f"one of {', '.join(RELATIONS)}")
        if c.get("a") == c.get("b"):
            problems.append("b: the same object as a")
    elif kind in ("suspended", "attached_to", "above", "below"):
        if c.get("a") == c.get("b") and c.get("a_part") == c.get("b_part"):
            problems.append("b: the same as a; for two regions of one mesh give a_part and b_part")
        need("clear", _number(c.get("clear")) and 0 <= c.get("clear") <= 10, "a distance in m, 0 to 10")
    elif kind == "no_overlap":
        need("names", _names(c.get("names"), 2, MAX_NAMES), f"2 to {MAX_NAMES} different names")
    elif kind == "in_frame":
        need("names", _names(c.get("names"), 1, MAX_LIST), f"1 to {MAX_LIST} different names")
        need("min_inside", _number(c.get("min_inside")) and 0 < c.get("min_inside") <= 1, "a share, above 0 up to 1")
        region = c.get("region")
        need("region", isinstance(region, list) and len(region) == 4 and all(_number(v) and 0 <= v <= 1 for v in region)
             and region[0] < region[2] and region[1] < region[3], "[x0, y0, x1, y1] in frame coordinates 0..1, x0 < x1, y0 < y1")
    elif kind == "light":
        need("energy", _range(c.get("energy")), "[min, max] in W (sun: W/m²), null for open")
        need("spot_size", _range(c.get("spot_size")), "[min, max] cone angle in degrees, null for open")
        color = c.get("color")
        need("color", isinstance(color, list) and len(color) == 3 and all(_number(v) and 0 <= v <= 1 for v in color),
             "[r, g, b], each 0 to 1")
    elif kind == "origin_base":
        xy = c.get("origin_xy")
        need("origin_xy", isinstance(xy, list) and len(xy) == 2 and all(_number(v) for v in xy), "[x, y] in m, world")
    elif kind == "flat_shaded":
        need("max_smooth", _number(c.get("max_smooth")) and 0 <= c.get("max_smooth") <= 1, "a share of faces, 0 to 1")
    elif kind == "inside_bounds":
        need("bounds", _planned(c.get("bounds")), "[[x0, y0, z0], [x1, y1, z1]] in m, or a list of such boxes")
    elif kind == "triangles":
        if "min" not in c and "max" not in c:
            problems.append("min or max: at least one")
        for field in ("min", "max"):
            need(field, isinstance(c.get(field), int) and not isinstance(c.get(field), bool) and c.get(field) >= 0,
                 "a whole number of triangles, 0 or more")
        if isinstance(c.get("min"), int) and isinstance(c.get("max"), int) and c["min"] > c["max"]:
            problems.append("min: more than max")
    elif kind == "faces_max":
        need("max", isinstance(c.get("max"), int) and not isinstance(c.get("max"), bool) and c.get("max") > 0,
             "a whole number of triangles above 0")
    elif kind == "setting":
        path = _setting_path(c.get("path"))
        need("path", path in SETTINGS, f"one of {', '.join(SETTINGS)}")
        bounds = [f for f in ("min", "max") if f in c]
        if ("equals" in c) == bool(bounds):
            problems.append("equals, or min and/or max: one or the other")
        for field in bounds:
            need(field, _number(c.get(field)) and path not in _TEXT_SETTINGS, "a number, for a numeric setting")
        need("equals", isinstance(c.get("equals"), str) or _number(c.get("equals")) or c.get("equals") is None,
             "a number, a name or null")
    elif kind == "animated":
        need("path", _name(c.get("path")), 'a property path such as "location" or "data.lens"')
        frames = c.get("frames")
        need("frames", isinstance(frames, list) and len(frames) == 2 and all(isinstance(f, int) and not isinstance(f, bool)
                                                                            for f in frames) and frames[0] != frames[1],
             "[a, b], two different whole frames")
    elif kind == "preserved":
        need("names", _names(c.get("names"), 1, MAX_LIST), f"1 to {MAX_LIST} different names")
        need("snapshot", _name(c.get("snapshot")), "a snapshot's id, from the checks tool's snapshot")
        exclude = c.get("exclude")
        need("exclude", isinstance(exclude, list) and all(a in ASPECTS for a in exclude) and len(set(exclude)) < len(ASPECTS),
             f"a list of aspects to leave out, some of {', '.join(ASPECTS)}")
        need("baseline", isinstance(c.get("baseline"), dict), "the snapshot's records (added by Loopcut)")
    elif kind == "unchanged":
        need("names", _names(c.get("names"), 1, MAX_LIST), f"1 to {MAX_LIST} different names")
        need("tokens", isinstance(c.get("tokens"), dict), "the spawn tokens (added by Loopcut)")
    elif kind == "file":
        path = c.get("path")
        need("path", _name(path) and not os.path.isabs(str(path)[2:] if str(path).startswith("//") else str(path)),
             "a path relative to the .blend file")
    return problems


def validate(checks, user_texts=None, where: str = "checks", advisory: bool = False) -> list[str]:
    """Every problem with a list of checks, each naming the field: "checks[1] (contact) relation:
    one of ...". user_texts: the user's own messages; when given, every quote must be in them.
    advisory: a brief's advisory list, where ADVISORY_KINDS are allowed too and a quote is optional
    and never looked up. An empty list is a valid one."""
    kinds = {**KINDS, **ADVISORY_KINDS} if advisory else KINDS
    if not isinstance(checks, list):
        return [f"{where}: a list of checks"]
    errors = []
    for n, check in enumerate(checks):
        at = f"{where}[{n}]"
        if not isinstance(check, dict):
            errors.append(f'{at}: a check is {{"kind": ..., "quote": ..., ...}}')
            continue
        kind = check.get("kind")
        if kind in ADVISORY_KINDS and not advisory:
            errors.append(f"{at} kind: {kind!r} is advisory only (a brief's advisory list)")
            continue
        if kind not in kinds:
            errors.append(f"{at} kind: {kind!r} is not one of {', '.join(kinds)}")
            continue
        at = f"{at} ({kind})"
        required, optional = kinds[kind]
        quote = check.get("quote")
        if advisory:
            if quote is not None and not isinstance(quote, str):
                errors.append(f"{at} quote: text, or leave it out")
        elif not _name(quote):
            errors.append(f"{at} quote: the user's words this check comes from")
        elif user_texts is not None and not quote_found(quote, user_texts):
            errors.append(f"{at} quote: {quote!r} is not in the user's messages; quote them word for word, "
                          f"or put it in advisory")
        errors += [f"{at} {field}: missing" for field in required if field not in check]
        unknown = sorted(set(check) - set(required) - set(optional) - {"kind", "quote", "id"})
        errors += [f"{at} {field}: not a field of {kind} (it has {', '.join(required + optional)})" for field in unknown]
        errors += [f"{at} {problem}" for problem in _field_errors(check)]
    return errors


def _planned(value) -> bool:
    """One box [[x0, y0, z0], [x1, y1, z1]] (low <= high), or a list of them."""
    def box(b):
        return (isinstance(b, list) and len(b) == 2 and all(isinstance(c, list) and len(c) == 3 and all(map(_number, c))
                                                             for c in b) and all(lo <= hi for lo, hi in zip(*b)))
    return box(value) or isinstance(value, list) and bool(value) and all(box(b) for b in value)


def boxes_of(value) -> list:
    """Planned bounds, one box or several, as a list of boxes ([] when malformed)."""
    if not _planned(value):
        return []
    return [value] if isinstance(value[0][0], (int, float)) else list(value)


def normalised(checks: list, prefix: str = "c") -> list[dict]:
    """The checks with ids (c1, c2, ... in order, unless they have one; a1, a2, ... for an advisory
    list) and types in upper case."""
    out = []
    for n, check in enumerate(checks or (), 1):
        check = dict(check) if isinstance(check, dict) else {"kind": None}
        check.setdefault("id", f"{prefix}{n}")
        if isinstance(check.get("type"), str):
            check["type"] = check["type"].upper()
        out.append(check)
    return out


def prefix_rule(prefix: str) -> str:
    """What count's prefix matches, said the same way to the worker and in the result: the name
    itself, and every name that starts with it (Prefix_Part, Prefix.001, PrefixPart), as
    ownership.prefixed reads a worker's prefix."""
    return f"named {prefix} or starting with {prefix}"


def title(check: dict) -> str:
    """A check in a few words: "contact Armchair rests_on Rug"."""
    kind, c = check.get("kind"), check
    if kind == "contact":
        return f"contact {c.get('a')} {c.get('relation')} {c.get('b')}"
    if kind in ("suspended", "attached_to", "above", "below"):
        word = {"suspended": "from", "attached_to": "to", "above": "over", "below": "under"}[kind]
        return f"{kind} {_label(c.get('a'), c.get('a_part'))} {word} {_label(c.get('b'), c.get('b_part'))}"
    if kind in ("no_overlap", "unchanged"):
        return f"{kind} {', '.join(map(str, c.get('names') or []))}"
    if kind == "preserved":
        return (f"preserved {', '.join(map(str, c.get('names') or []))} as {c.get('snapshot')}"
                + (f" except {', '.join(c['exclude'])}" if c.get("exclude") else ""))
    if kind == "in_frame":
        return (f"in_frame {c.get('camera')}: {', '.join(map(str, c.get('names') or []))}"
                + (f" at frame {c['frame']}" if c.get("frame") is not None else ""))
    if kind == "count":
        return f"count {c.get('prefix') or c.get('collection')}"
    if kind == "light_inside":
        return f"light_inside {c.get('light')} in {c.get('container')}"
    if kind == "material_on":
        return f"material_on {c.get('name')}: {c.get('material')}"
    if kind in ("setting", "file"):
        return f"{kind} {c.get('path')}"
    return f"{kind} {c.get('name', '')}".strip()


def _label(name, part) -> str:
    """An object, or a region of it: "HangingSign[material:Wood]"."""
    return f"{name}[{part}]" if part else str(name)


def line(result: dict) -> str:
    """One line for a report or the Verified block: "c2 contact Armchair rests_on Rug: FAIL, ..."."""
    status = {"pass": "pass", "fail": "FAIL", "deferred": "deferred"}.get(result["status"], "could not measure")
    return f"{result['id']} {result.get('title') or result['kind']}: {status}, {result['detail']}"


def _shift(before, after) -> float:
    return max((abs(a - b) for a, b in zip(before, after)), default=0.0)


def preservation(before: dict, after: dict, exclude=()) -> list[str]:
    """What changed between two of one object's snapshot records (record()), aspect by aspect with
    the numbers, leaving out the excluded aspects. [] when it is preserved."""
    out, skip = [], set(exclude)
    if "origin" not in skip and _shift(before["origin"], after["origin"]) > KEEP_TOL:
        moved = math.dist(before["origin"], after["origin"])
        out.append(f"origin {_vec(before['origin'])} -> {_vec(after['origin'])} ({_mm(moved)})")
    if "transform" not in skip:
        if _shift(before["rotation"], after["rotation"]) > TURN_TOL:
            out.append(f"rotation {_vec(before['rotation'])}° -> {_vec(after['rotation'])}°")
        if _shift(before["scale"], after["scale"]) > SCALE_TOL:
            out.append(f"scale {_vec(before['scale'])} -> {_vec(after['scale'])}")
    geometry = before.get("dims") is not None and after.get("dims") is not None
    if before.get("dims") is None and after.get("dims") is not None:
        out.append("has geometry now, none before")
    elif before.get("dims") is not None and after.get("dims") is None:
        out.append("has no geometry now")
    if geometry and "dims" not in skip and _shift(before["dims"], after["dims"]) > KEEP_TOL:
        axes = [f"{axis} {'+' if b > a else '-'}{_mm(abs(b - a))}" for axis, a, b in zip("xyz", before["dims"], after["dims"])
                if abs(b - a) > KEEP_TOL]
        out.append(f"size {_vec(before['dims'])} -> {_vec(after['dims'])} m ({', '.join(axes)})")
    if geometry and "bounds" not in skip:
        shift = max(_shift(before["bounds"][0], after["bounds"][0]), _shift(before["bounds"][1], after["bounds"][1]))
        if shift > KEEP_TOL:
            out.append(f"bounds {_vec(before['bounds'][0])}..{_vec(before['bounds'][1])} -> "
                       f"{_vec(after['bounds'][0])}..{_vec(after['bounds'][1])} (up to {_mm(shift)})")
    same_topology = before.get("topology") == after.get("topology")
    if geometry and "topology" not in skip and not same_topology:
        out.append(f"topology {before['verts']:,} vertices, {before['faces']:,} faces -> {after['verts']:,}, "
                   f"{after['faces']:,}" + (" (same counts, other faces)" if (before["verts"], before["faces"])
                                            == (after["verts"], after["faces"]) else ""))
    if geometry and "vertices" not in skip and before.get("vertices") != after.get("vertices"):
        if same_topology and before.get("sample") and len(before["sample"]) == len(after.get("sample") or ()):
            moved = max(math.dist(p, q) for p, q in zip(before["sample"], after["sample"]))
            out.append(f"vertices moved, up to {_mm(moved)} at the sampled ones" if moved > KEEP_TOL
                       else "vertices moved (not at the sampled ones)")
        elif "topology" in skip or same_topology:
            out.append("vertices changed")
    if "materials" not in skip and before.get("materials") != after.get("materials"):
        was, now = before.get("materials") or {}, after.get("materials") or {}
        parts = [f"{part}: {', '.join(was.get(part) or ['none'])} -> {', '.join(now.get(part) or ['none'])}"
                 for part in sorted(set(was) | set(now)) if was.get(part) != now.get(part)]
        out.append("materials " + "; ".join(parts[:4]) + (f" (+{len(parts) - 4} more)" if len(parts) > 4 else ""))
    return out


def verified_block(results: list) -> str:
    """The finish gate's block for the final reply: how many checks hold, then each that doesn't."""
    passed = sum(r["status"] == "pass" for r in results)
    lines = [f"Verified by Loopcut on this scene: {passed} of {len(results)} checks."]
    return "\n".join(lines + [f"- {line(r)}" for r in results if r["status"] != "pass"])


# ------------------------------------------------------------------ measuring (bpy)

class _Fail(Exception):
    """A measured failure with its detail."""


class _Unavailable(Exception):
    """Can't be measured, with the reason."""


def _m(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _mm(value: float) -> str:
    return f"{value * 1000:.1f} mm" if abs(value) < 1 else f"{_m(value)} m"


def _vec(values) -> str:
    return "(" + ", ".join(_m(v) for v in values) + ")"


def _object(name: str):
    import bpy
    ob = bpy.data.objects.get(name)
    if ob is None:
        import difflib
        close = difflib.get_close_matches(name, bpy.data.objects.keys(), 1)
        raise _Fail(f"no object named {name!r}" + (f" (did you mean {close[0]!r}?)" if close else ""))
    return ob


def _points(ob):
    """Evaluated world vertices of an object, its children and what it instances; None: no geometry."""
    import bpy
    from . import camera
    rows, _notes = camera._geometry([ob], bpy.context.evaluated_depsgraph_get())
    return rows[ob.name]


def _exists(c, _spawn):
    import bpy
    from .ownership import QUARANTINE
    ob = _object(c["name"])
    if c.get("type") and ob.type != c["type"]:
        raise _Fail(f"{ob.name} is {ob.type.lower()}, not {c['type'].lower()}")
    if ob.get(QUARANTINE):
        raise _Fail(f"{ob.name} is in quarantine (outside its worker's role); accept_quarantine keeps it")
    if ob.name not in bpy.context.scene.objects:
        raise _Fail(f"{ob.name} exists but is not in the scene")
    return f"{ob.name} ({ob.type.lower()}) is in the scene"


def _count(c, _spawn):
    import bpy
    from .ownership import QUARANTINE
    if c.get("collection"):
        collection = bpy.data.collections.get(c["collection"])
        if collection is None:
            raise _Fail(f"no collection named {c['collection']!r}")
        found, where = list(collection.all_objects), f"in collection {collection.name}"
    else:
        found, where = [o for o in bpy.context.scene.objects if o.name.startswith(c["prefix"])], prefix_rule(c["prefix"])
    found = [o for o in found if not o.get(QUARANTINE) and (not c.get("type") or o.type == c["type"])]
    low, high = c.get("min", 0), c.get("max")
    want = f"{low}..{high}" if high is not None else f"at least {low}"
    names = sorted(o.name for o in found)
    shown = f" ({', '.join(names[:5])}{f' and {len(names) - 5} more' if len(names) > 5 else ''})" if names else ""
    text = f"{len(found)} {c.get('type', '').lower() + ' ' if c.get('type') else ''}objects {where}{shown}; want {want}"
    if len(found) < low or high is not None and len(found) > high:
        raise _Fail(text)
    return text


def _dims(c, _spawn):
    ob = _object(c["name"])
    points = _points(ob)
    if points is None:
        raise _Unavailable(f"{ob.name} has no geometry to measure")
    size = points.max(axis=0) - points.min(axis=0)
    off = []
    for axis, have, want in zip("xyz", size, c["size"]):
        if want is None:
            continue
        tol = c["tol"] if "tol" in c else max(DIMS_TOL, DIMS_SHARE * want)
        if abs(have - want) > tol:
            off.append(f"{axis} {_mm(abs(have - want))} {'over' if have > want else 'under'} (tolerance {_mm(tol)})")
    wanted = "(" + ", ".join("any" if w is None else _m(w) for w in c["size"]) + ")"
    text = f"size {_vec(size)} m, want {wanted}"
    if off:
        raise _Fail(text + ": " + "; ".join(off))
    return text


def _contact_result(pair: dict, tol: float) -> dict:
    from . import contact
    result = contact.evaluate([pair], tol)[0]
    if result["status"] == "unavailable" and result["line"].startswith("no object named"):
        raise _Fail(result["line"])
    return result


def _contact(c, _spawn):
    from . import contact
    tol = c.get("tol", contact.DEFAULT_TOLERANCE)
    result = _contact_result({"a": c["a"], "relation": c["relation"], "b": c["b"], "gap": c.get("gap", 0)}, tol)
    text = result["line"] + "".join(f"; {d}" for d in result.get("details", ()))
    if result["status"] == "unavailable":
        raise _Unavailable(text)
    if result["status"] == "fail":
        raise _Fail(text)
    return text


def _faces(part: str):
    """contact._shape's polygon filter for a region: the faces using a material (or a worker's copy
    of it, ownership.VARIANT), all of whose vertices are in a vertex group, or whose face attribute
    (or every vertex's point attribute) is set. Joined meshes keep their materials, so a material is
    what usually tells a sign's board from its chains."""
    from .ownership import VARIANT
    kind, _, value = part.partition(":")

    def faces(evaluated, mesh) -> list:
        polygons = mesh.polygons
        if kind == "material":
            slots = {i for i, slot in enumerate(evaluated.material_slots) if slot.material is not None
                     and value in (slot.material.name, slot.material.get(VARIANT))}
            return [p.index for p in polygons if p.material_index in slots] if slots else []
        if kind == "group":
            group = evaluated.vertex_groups.get(value)
            if group is None:
                return []
            members = {v.index for v in mesh.vertices if any(g.group == group.index and g.weight > 0 for g in v.groups)}
            return [p.index for p in polygons if all(v in members for v in p.vertices)]
        attribute = mesh.attributes.get(value)
        if (attribute is None or attribute.domain not in ("FACE", "POINT")
                or attribute.data_type not in ("BOOLEAN", "INT", "INT8", "FLOAT")):
            return []
        on = [bool(d.value) for d in attribute.data]
        if attribute.domain == "FACE":
            return [i for i, yes in enumerate(on) if yes]
        return [p.index for p in polygons if all(on[v] for v in p.vertices)]
    return faces


def _region(name: str, part, depsgraph):
    """A contact.Shape of an object (its children and instances included) or of a region of it. A
    missing object or a region with no faces is a failure that says what there is."""
    import bpy
    from . import contact
    ob = _object(name)
    if ob.name not in bpy.context.scene.objects:
        raise _Fail(f"{ob.name} exists but is not in the scene")
    try:
        return contact._shape(ob.name, depsgraph, _faces(part) if part else None)
    except contact.Unavailable as ex:
        if not part:
            raise _Unavailable(str(ex)) from ex
        kind, _, value = part.partition(":")
        parts = [ob, *ob.children_recursive]
        has = {"material": sorted({s.material.name for p in parts for s in p.material_slots if s.material}),
               "group": sorted({g.name for p in parts for g in getattr(p, "vertex_groups", ())}),
               "attribute": sorted({a.name for p in parts if p.type == "MESH" for a in p.data.attributes
                                    if a.domain in ("FACE", "POINT") and not a.name.startswith(".")})}[kind]
        raise _Fail(f"no faces of {ob.name} in {part}; its {kind}s: {', '.join(has[:8]) or 'none'}") from ex


def _regions(c, *fields):
    """The regions a relation check names ("a", "b", "ground"), and the Shapes to free after."""
    import bpy
    depsgraph, shapes = bpy.context.evaluated_depsgraph_get(), []
    try:
        for field in fields:
            shapes.append(_region(c[field], c.get(f"{field}_part"), depsgraph) if c.get(field) else None)
    except Exception:
        for shape in shapes:
            if shape is not None:
                shape.bm.free()
        raise
    return shapes


def _gap(a, b) -> tuple[float, dict]:
    """How far a region is from another (0 where their surfaces meet or cross), and contact's measure."""
    from . import contact
    measure = contact._nearest(a, b)
    return (0.0 if measure["meet"] or measure["depth"] > 0 else measure["distance"]), measure


def _attached_at(a, b, tol: float):
    """The height of a's highest point within tol of b: its probes (vertices, face centres, as
    contact measures) near b's surface, the points of its surface near b's probes, and where its
    faces cross b's (the top of the heights both faces span); None if none."""
    from . import contact
    zs = [p.z for p in contact._probes(a) if b.tree.find_nearest(p, tol)[0] is not None]
    zs += [near.z for near in (a.tree.find_nearest(p, tol)[0] for p in contact._probes(b)) if near is not None]
    for i, j in a.tree.overlap(b.tree):
        za, zb = [v.co.z for v in a.bm.faces[i].verts], [v.co.z for v in b.bm.faces[j].verts]
        zs.append(max(min(max(za), max(zb)), max(min(za), min(zb))))
    return max(zs, default=None)


def _suspended(c, _spawn):
    """a hangs from b: attached to it (within tol, crossing allowed: a hook through a ring) in a's
    top half, and a's lowest point at least clear above the ground (z 0, or the top of the object
    named ground). a and b may be regions of one joined mesh: a sign's board and its chains."""
    from . import contact
    tol, clear = c.get("tol", ATTACH_TOL), c.get("clear", CLEAR)
    hung, held = _label(c["a"], c.get("a_part")), _label(c["b"], c.get("b_part"))
    a, b, ground = _regions(c, "a", "b", "ground")
    try:
        (low, high), floor = a.bounds(), ground.bounds()[1][2] if ground is not None else 0.0
        gap, measure = _gap(a, b)
        at = _attached_at(a, b, tol) if gap <= tol else None
    finally:
        for shape in (a, b, ground):
            if shape is not None:
                shape.bm.free()
    clearance, middle = low[2] - floor, (low[2] + high[2]) / 2
    under = f"{c['ground']}'s top (z {_m(floor)})" if ground is not None else "the ground (z 0)"
    height = f"{_mm(clearance)} above" if clearance >= 0 else f"{_mm(-clearance)} below"
    parts, fixes = [f"{hung}'s lowest point {height} {under}, want at least {_mm(clear)}"], []
    whole = not c.get("a_part") and c["a"] != c["b"]
    if clearance < clear - 1e-9:
        need = clear - clearance
        fixes.append(f"move {c['a']} by (0, 0, {_m(need)}) m, {c['b']} with it" if whole else
                     f"raise {hung} {_mm(need)} within {c['a']} (a remodel for its builder: shorter hangers, a higher arm)")
    if gap > tol:
        parts.append(f"not attached to {held}: gap {_mm(gap)} (want at most {_mm(tol)})")
        fixes.append(f"move {c['a'] if whole else hung} by {contact.vector(measure['toward'])} m")
    elif at is not None and at < middle - 1e-9:
        parts.append(f"attached to {held} only at z {_m(at)}, below its middle (z {_m(middle)}): it leans, not hangs")
        fixes.append(f"hang {hung} from its top")
    else:
        parts.append(f"attached to {held}" + (f" at z {_m(at)}, its top half" if at is not None else ""))
    text = "; ".join(parts)
    if fixes:
        raise _Fail(f"{text}. Fix: " + "; ".join(fixes))
    return text


def _attached_to(c, _spawn):
    """a fixed to b: their surfaces within tol, crossing allowed (a handle sunk into a door)."""
    from . import contact
    tol = c.get("tol", ATTACH_TOL)
    a, b = _regions(c, "a", "b")
    try:
        gap, measure = _gap(a, b)
    finally:
        a.bm.free()
        b.bm.free()
    one, other = _label(c["a"], c.get("a_part")), _label(c["b"], c.get("b_part"))
    if gap > tol:
        what = c["a"] if not c.get("a_part") and c["a"] != c["b"] else one
        raise _Fail(f"{one} is {_mm(gap)} from {other} (want at most {_mm(tol)}). Fix: move {what} by "
                    f"{contact.vector(measure['toward'])} m")
    return f"{one} attached to {other}" + (f" ({_mm(measure['depth'])} into it)" if measure["depth"] >= 5e-5 else "")


def _above(c, _spawn):
    """above: a's lowest point at least gap over b's highest, and a over b (their XY bounds overlap);
    below: the other way up."""
    from . import contact
    up, gap, tol = c["kind"] == "above", c.get("gap", 0.0), contact.DEFAULT_TOLERANCE
    a, b = _regions(c, "a", "b")
    try:
        (alo, ahi), (blo, bhi) = a.bounds(), b.bounds()
    finally:
        a.bm.free()
        b.bm.free()
    one, other = _label(c["a"], c.get("a_part")), _label(c["b"], c.get("b_part"))
    apart = alo[2] - bhi[2] if up else blo[2] - ahi[2]
    over = all(alo[k] <= bhi[k] + tol and blo[k] <= ahi[k] + tol for k in (0, 1))
    ends = (("lowest", alo[2]), ("highest", bhi[2])) if up else (("highest", ahi[2]), ("lowest", blo[2]))
    text = (f"{one}'s {ends[0][0]} point z {_m(ends[0][1])}, {other}'s {ends[1][0]} z {_m(ends[1][1])}: "
            + (f"{_mm(apart)} apart" if apart >= 0 else f"{_mm(-apart)} {'below' if up else 'above'} it")
            + (f", want at least {_mm(gap)}" if gap else ""))
    move = [0.0, 0.0, 0.0]
    if apart < gap - tol:
        move[2] = (gap - apart) * (1 if up else -1)
    if not over:
        text += f"; not {'over' if up else 'under'} {other} (their XY bounds do not overlap)"
        move[0], move[1] = ((blo[k] + bhi[k] - alo[k] - ahi[k]) / 2 for k in (0, 1))
    if any(move):
        what = c["a"] if not c.get("a_part") and c["a"] != c["b"] else one
        raise _Fail(f"{text}. Fix: move {what} by {contact.vector(move)} m")
    return text


def _no_overlap(c, _spawn):
    """Each pair measured as check_contact's clear_of measures it, so the two never disagree."""
    from . import contact
    tol = c.get("tol", contact.DEFAULT_TOLERANCE)
    overlaps, missing = [], []
    shapes = contact._Shapes()
    try:
        for a, b in itertools.combinations(c["names"], 2):
            sa, sb = shapes.of(a), shapes.of(b)
            gone = [s for s in (sa, sb) if isinstance(s, Exception)]
            if gone:
                if str(gone[0]).startswith("no object named"):
                    raise _Fail(str(gone[0]))
                missing.append(f"{a}/{b}: {gone[0]}")
                continue
            measure = contact._nearest(sa, sb)
            depth, move = measure["depth"], measure["out"]
            if depth > tol:
                overlaps.append(f"{a} {_mm(depth)} into {b}. Fix: move {a} by {_vec(contact._away(move, depth + tol))} m")
    finally:
        shapes.free()
    pairs = len(c["names"]) * (len(c["names"]) - 1) // 2
    if overlaps:
        raise _Fail(f"{len(overlaps)} of {pairs} pairs overlap: " + "; ".join(overlaps))
    if missing:
        raise _Unavailable("; ".join(missing))
    return f"none of {pairs} pairs overlap (tolerance {_mm(tol)})"


class _AtFrame:
    """The scene at a frame for a measure, then back where it was (frame and subframe)."""

    def __init__(self, frame):
        self.frame = frame

    def __enter__(self):
        import bpy
        scene = bpy.context.scene
        self.back = (scene.frame_current, scene.frame_subframe)
        if self.frame is not None:
            scene.frame_set(int(self.frame))
        return self

    def __exit__(self, *_exc):
        import bpy
        if self.frame is not None:
            bpy.context.scene.frame_set(self.back[0], subframe=self.back[1])


def _in_frame(c, _spawn):
    from . import camera, framing
    from .tools import ToolError
    try:
        with _AtFrame(c.get("frame")):
            measured = camera.coverage(c["camera"], c["names"])
    except ToolError as ex:
        raise _Fail(str(ex)) from ex
    least, region, parts, bad = c.get("min_inside", 1.0), c.get("region"), [], False
    for name in c["names"]:
        row = measured["rows"][name]
        if row.get("empty"):
            raise _Unavailable(f"{name} has no geometry")
        text = framing.row(name, row)[2:]
        if row["inside"] < least - 1e-9:
            bad = True
            text += f" (want {least:.0%})"
        if region and row["bounds"]:
            left, right, bottom, top = row["bounds"]
            outside = [f"{edge} by {_m(amount)}" for edge, amount in
                       (("left", region[0] - left), ("right", right - region[2]),
                        ("bottom", region[1] - bottom), ("top", top - region[3])) if amount > REGION_TOL]
            if outside:
                bad = True
                text += f"; outside region {_vec(region)}: " + ", ".join(outside)
        parts.append(text)
    at = f" at frame {c['frame']}" if c.get("frame") is not None else ""
    text = f"camera {measured['camera']}{at}: " + "; ".join(parts)
    if bad:
        raise _Fail(text)
    return text


def _triangles(ob) -> int:
    import bpy
    import numpy as np
    depsgraph = bpy.context.evaluated_depsgraph_get()
    members = {o.name for o in (ob, *ob.children_recursive)}
    total = 0
    for instance in depsgraph.object_instances:
        owner = (instance.parent if instance.is_instance else instance.object).original
        evaluated = instance.object
        if owner.name not in members or evaluated.type not in ("MESH", "CURVE", "SURFACE", "FONT", "META"):
            continue
        mesh = evaluated.data if evaluated.type == "MESH" else evaluated.to_mesh()
        try:
            loops = np.empty(len(mesh.polygons), dtype=np.int64)
            mesh.polygons.foreach_get("loop_total", loops)
            total += int((loops - 2).sum())
        finally:
            if evaluated.type != "MESH":
                evaluated.to_mesh_clear()
    return total


def _faces_max(c, _spawn):
    ob = _object(c["name"])
    count = _triangles(ob)
    text = f"{count:,} triangles (evaluated, children included); max {c['max']:,}"
    if count > c["max"]:
        raise _Fail(text)
    return text


def base_point(points, origin_xy=None) -> tuple:
    """Where an origin at the base goes (origin_base, and facts.measure's origin): origin_xy at the
    lowest point, else the middle of the points within BASE_BAND of the lowest one. points: Nx3."""
    low = float(points[:, 2].min())
    if origin_xy is not None:
        return float(origin_xy[0]), float(origin_xy[1]), low
    foot = points[points[:, 2] <= low + BASE_BAND][:, :2]
    middle = (foot.min(axis=0) + foot.max(axis=0)) / 2
    return float(middle[0]), float(middle[1]), low


def _origin_base(c, _spawn):
    """The origin at the middle of what the object stands on: the XY centre of its lowest geometry
    (within BASE_BAND of its lowest point), at that height, so a lock plate or a bung sticking out of
    one side, or a sign's board hanging beside its post, does not move it; or at the check's
    origin_xy (the post's foot) at the lowest point, when the brief names one."""
    ob = _object(c["name"])
    points = _points(ob)
    if points is None:
        raise _Unavailable(f"{ob.name} has no geometry to measure")
    base = base_point(points, c.get("origin_xy"))
    label = "the brief's base point" if "origin_xy" in c else "bottom centre"
    origin = tuple(ob.matrix_world.translation)
    apart = math.dist(origin, base)
    tol = c.get("tol", ORIGIN_TOL)
    text = f"origin {_vec(origin)}, {label} {_vec(base)}: {_mm(apart)} apart"
    if apart > tol:
        raise _Fail(text + f". Fix: move the origin by {_vec(b - o for b, o in zip(base, origin))} m, the geometry staying put")
    return text


def _material_on(c, _spawn):
    """The material, or a worker's own copy of it (tagged ownership.VARIANT when it was copied: a
    finer walnut on one chair is still the walnut the user asked for), on the object or a child."""
    import bpy
    from .ownership import VARIANT
    ob = _object(c["name"])
    material = bpy.data.materials.get(c["material"])
    if material is None:
        raise _Fail(f"no material named {c['material']!r}")
    parts = [ob, *ob.children_recursive]
    having = [p.name for p in parts if any(slot.material == material for slot in p.material_slots)]
    copies = sorted({slot.material.name for p in parts for slot in p.material_slots if slot.material is not None
                     and slot.material != material and slot.material.get(VARIANT) == material.name})
    if copies:
        having += [p.name for p in parts if p.name not in having and any(
            slot.material is not None and slot.material.name in copies for slot in p.material_slots)]
    if having:
        shown = ", ".join(having[:4]) + (f" and {len(having) - 4} more" if len(having) > 4 else "")
        return f"{material.name} on {shown}" + (f" (as its copy {', '.join(copies[:3])})" if copies else "")
    used = sorted({slot.material.name for p in parts for slot in p.material_slots if slot.material})
    raise _Fail(f"{material.name} is not on {ob.name} or its {len(parts) - 1} children; they use "
                f"{', '.join(used[:5]) or 'no material'}")


def _fact(c) -> dict:
    """The deliverable's facts (facts.measure: the one place they are measured), for the advisory kinds."""
    from . import facts
    ob = _object(c["name"])
    if not any(m.type in facts.GEOMETRY for m in (ob, *ob.children_recursive)):
        raise _Unavailable(f"{ob.name} has no geometry to measure")
    return facts.measure(ob, boxes_of(c.get("bounds")))


def _no_modifiers(c, _spawn):
    fact = _fact(c)
    if fact["modifiers"]:
        raise _Fail(f"{len(fact['modifiers'])} unapplied: {', '.join(fact['modifiers'][:4])}. Fix: apply them "
                    f"(bpy.ops.object.modifier_apply per modifier, the object active)")
    return "no unapplied modifiers"


def _flat_shaded(c, _spawn):
    from . import facts
    fact, most = _fact(c), c.get("max_smooth", facts.FLAT_SHARE)
    text = f"{fact['smooth']:.0%} of {fact['faces']:,} faces smooth shaded; flat wants at most {most:.0%}"
    if fact["smooth"] > most:
        raise _Fail(text + ". Fix: mesh.shade_flat() on each part")
    return text


def _inside_bounds(c, _spawn):
    from . import facts
    if not boxes_of(c.get("bounds")):
        raise _Unavailable("no planned bounds to compare with")
    fact, tol = _fact(c), c.get("tol", facts.BOUNDS_TOL)
    far = facts.outside(fact, tol)
    plan = fact["planned"]
    where = f"box {plan['box']}" if plan["count"] > 1 else "the planned box"
    if far:
        raise _Fail(f"outside {where} by " + ", ".join(f"{side} {_mm(v)}" for side, v in far.items())
                    + f" (tolerance {_mm(tol)})")
    return f"inside {where} (tolerance {_mm(tol)})"


def _triangles_band(c, _spawn):
    count = _fact(c)["tris"]
    low, high = c.get("min"), c.get("max")
    want = " to ".join(f"{v:,}" for v in (low, high) if v is not None)
    text = f"{count:,} triangles (evaluated, children included); want " + (
        want if low is not None and high is not None else f"at least {low:,}" if low is not None else f"at most {high:,}")
    if low is not None and count < low or high is not None and count > high:
        raise _Fail(text)
    return text


class _Point:
    """A light's position standing in for a shape in contact's inside measure."""

    def __init__(self, co):
        self.co, self.count = tuple(co), 1

    def point(self, _i):
        return self.co

    def bounds(self):
        return self.co, self.co


def _light_inside(c, _spawn):
    from . import contact
    light = _object(c["light"])
    if light.type != "LIGHT":
        raise _Fail(f"{light.name} is {light.type.lower()}, not a light")
    _object(c["container"])
    shapes = contact._Shapes()
    try:
        shape = shapes.of(c["container"])
        if isinstance(shape, Exception):
            raise _Unavailable(str(shape))
        pair = {"a": light.name, "relation": "inside", "b": c["container"], "gap": 0.0, "error": ""}
        result = contact._measure_inside(pair, _Point(light.matrix_world.translation), shape, contact.DEFAULT_TOLERANCE)
    finally:
        shapes.free()
    if result["status"] == "unavailable":
        raise _Unavailable(result["line"])
    text = f"{light.name} at {_vec(light.matrix_world.translation)}: " + result["line"].replace(
        f"100% of {light.name}'s vertices", light.name)
    if result["status"] == "fail":
        raise _Fail(text)
    return text


def _within(value: float, bounds) -> bool:
    low, high = bounds
    return (low is None or value >= low - 1e-9) and (high is None or value <= high + 1e-9)


def _bounds(bounds, unit: str) -> str:
    low, high = bounds
    return f"{'' if low is None else _m(low)}..{'' if high is None else _m(high)}{unit}"


def _light(c, _spawn):
    """A light's type, power, colour and cone, as the brief asks for them."""
    from .ownership import QUARANTINE
    ob = _object(c["name"])
    if ob.type != "LIGHT":
        raise _Fail(f"{ob.name} is {ob.type.lower()}, not a light")
    if ob.get(QUARANTINE):
        raise _Fail(f"{ob.name} is in quarantine (outside its worker's role); accept_quarantine keeps it")
    light, off = ob.data, []
    unit = " W/m²" if light.type == "SUN" else " W"
    parts = [f"{light.type.lower()}", f"energy {_m(light.energy)}{unit}", f"color {_vec(light.color)}"]
    if c.get("type") and light.type != c["type"]:
        off.append(f"a {light.type.lower()} light, want {c['type'].lower()}")
    if "energy" in c and not _within(light.energy, c["energy"]):
        off.append(f"energy {_m(light.energy)}{unit}, want {_bounds(c['energy'], unit)}")
    if "color" in c:
        tol = c.get("tol", COLOR_TOL)
        if max(abs(have - want) for have, want in zip(light.color, c["color"])) > tol + 1e-9:
            off.append(f"color {_vec(light.color)}, want {_vec(c['color'])} within {_m(tol)}")
    if "spot_size" in c:
        if light.type != "SPOT":
            off.append(f"spot_size: a {light.type.lower()} light has no cone")
        else:
            degrees = math.degrees(light.spot_size)
            parts.append(f"spot_size {degrees:.1f}°")
            if not _within(degrees, c["spot_size"]):
                off.append(f"spot_size {degrees:.1f}°, want {_bounds(c['spot_size'], '°')}")
    text = f"{ob.name}: " + ", ".join(parts)
    if off:
        raise _Fail(text + "; " + "; ".join(off))
    return text


def _setting_path(path) -> str:
    path = str(path or "").strip()
    return path[len("scene."):] if path.startswith("scene.") else path


def _engine(value) -> str:
    return re.sub(r"^BLENDER_|_NEXT$", "", str(value).upper())


def _setting(c, _spawn):
    import bpy
    path = _setting_path(c["path"])
    try:
        value = SETTINGS[path](bpy.context.scene)
    except AttributeError as ex:
        raise _Unavailable(f"{path} is not available in this Blender ({ex})") from ex
    shown = f"{path} = {value!r}" if isinstance(value, str) or value is None else f"{path} = {_m(value)}"
    if "equals" in c:
        want = c["equals"]
        if path == "render.engine":
            same = _engine(value) == _engine(want)
        elif isinstance(want, str) or want is None or isinstance(value, str) or value is None:
            same = str(value).casefold() == str(want).casefold() if want is not None else value is None
        else:
            same = abs(float(value) - float(want)) <= 1e-6 * max(1.0, abs(float(want)))
        if not same:
            raise _Fail(f"{shown}, want {want!r}")
        return shown
    low, high = c.get("min"), c.get("max")
    if isinstance(value, str) or value is None:
        raise _Unavailable(f"{shown}: not a number")
    if low is not None and value < low - 1e-9 or high is not None and value > high + 1e-9:
        raise _Fail(f"{shown}, want {'' if low is None else _m(low)}..{'' if high is None else _m(high)}")
    return shown


def _value(ob, path: str):
    value = ob.path_resolve(path)
    try:
        return tuple(value)
    except TypeError:
        return value


def _animated(c, _spawn):
    import bpy
    ob = _object(c["name"])
    scene = bpy.context.scene
    frame, subframe = scene.frame_current, scene.frame_subframe
    values = []
    try:
        for f in c["frames"]:
            scene.frame_set(f)
            try:
                values.append(_value(ob, c["path"]))
            except ValueError as ex:
                raise _Fail(f"{ob.name} has no property {c['path']!r}") from ex
    finally:
        scene.frame_set(frame, subframe=subframe)
    a, b = values
    shown = [_vec(v) if isinstance(v, tuple) else _m(v) if isinstance(v, (int, float)) else repr(v) for v in values]
    text = f"{ob.name}.{c['path']} at frame {c['frames'][0]}: {shown[0]}, at {c['frames'][1]}: {shown[1]}"
    if isinstance(a, tuple) and isinstance(b, tuple):
        same = all(abs(float(x) - float(y)) <= 1e-6 for x, y in zip(a, b))
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
        same = abs(a - b) <= 1e-6
    else:
        same = a == b
    if same:
        raise _Fail(text + ": the same")
    return text


def _unchanged(c, spawn):
    """Each named datablock, an object with its data, materials and children, against the spawn
    tokens: the check's own "tokens", else the Spawn's (parent), else the child's stamp_all ones."""
    import bpy
    from . import ownership
    baseline = c.get("tokens") or (spawn.tokens if spawn is not None else ownership.SPAWN_TOKENS)
    if not baseline:
        raise _Unavailable("no spawn tokens to compare with")
    uid2spawn = spawn.uid2spawn() if spawn is not None else None
    uid2key = {uid: key for key, uid in spawn.ids.items()} if spawn is not None else {}
    by_uid = {i.session_uid: i for i in ownership.all_ids()} if spawn is not None else {}
    stamped = {ownership.key_of(i): i for i in ownership.all_ids() if i.get(ownership.STAMP)}

    def live(key: str):
        if spawn is not None:
            return by_uid.get(spawn.ids.get(key))
        return stamped.get(key) if stamped else ownership.find(key)

    def key_of(idb) -> str | None:  # The key it had at spawn, whatever it is called now.
        return uid2key.get(idb.session_uid) if spawn is not None else ownership.key_of(idb)

    changed, checked = [], 0
    for name in c["names"]:
        if ":" in name:
            root, keys = live(name), [name]
        elif bpy.data.objects.get(name) is not None or f"OBJECT:{name}" not in baseline:
            root = bpy.data.objects.get(name) or ownership.find(name)
            keys = [(key_of(root) if root is not None else None) or f"OBJECT:{name}"]
        else:  # The object it was at spawn: renamed since (found by identity), or gone.
            root, keys = live(f"OBJECT:{name}"), [f"OBJECT:{name}"]
        if root is not None and root.id_type == "OBJECT":
            for part in (root, *root.children_recursive):
                for idb in (part, part.data, *(slot.material for slot in part.material_slots)):
                    k = key_of(idb) if idb is not None and idb.library is None else None
                    if k and k not in keys:
                        keys.append(k)
        for k in keys:
            if k not in baseline:
                continue
            checked += 1
            idb = live(k)
            if idb is None:
                changed.append(f"{k.split(':', 1)[1]} is gone")
            elif ownership.token(idb, uid2spawn) != baseline[k]:
                changed.append(f"{idb.name} ({idb.id_type.lower()}) changed")
    if not checked:
        raise _Unavailable(f"none of {', '.join(c['names'])} existed at spawn")
    if changed:
        raise _Fail("; ".join(changed))
    return f"{checked} datablock{'s' if checked != 1 else ''} as they were"


def record(ob) -> dict:
    """One object as a snapshot keeps it, for preserved: its origin, rotation (degrees) and scale;
    the evaluated geometry of it, its children and what it instances, as its world size and bounds,
    counts, a hash of the topology and one of the vertices in the object's own space (a move is not
    a vertex change) with a few of them sampled, for a distance; and the materials of each part. At
    0.1 mm. Evaluated in a view layer that includes it (view_layers.home), the active one first; one
    every layer excludes is _Unavailable. Main thread."""
    from . import view_layers
    layer = view_layers.home(ob)
    if layer is None:
        raise _Unavailable(view_layers.unavailable([ob.name]))
    with view_layers.evaluating(layer):
        return _record(ob)


def _record(ob) -> dict:
    import bpy
    import numpy as np
    from .ownership import _hash
    depsgraph = bpy.context.evaluated_depsgraph_get()
    members = {o.name for o in (ob, *ob.children_recursive)}
    inverse = np.array(ob.matrix_world.inverted())
    parts = []
    for instance in depsgraph.object_instances:
        owner, evaluated = (instance.parent if instance.is_instance else instance.object).original, instance.object
        if owner.name not in members or evaluated.type not in ("MESH", "CURVE", "SURFACE", "FONT", "META"):
            continue
        mesh = evaluated.data if evaluated.type == "MESH" else evaluated.to_mesh()
        try:
            co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
            mesh.vertices.foreach_get("co", co)
            loops = np.empty(len(mesh.loops), dtype=np.int32)
            mesh.loops.foreach_get("vertex_index", loops)
            totals = np.empty(len(mesh.polygons), dtype=np.int32)
            mesh.polygons.foreach_get("loop_total", totals)
        finally:
            if evaluated.type != "MESH":
                evaluated.to_mesh_clear()
        world = np.array(instance.matrix_world)
        points = co.reshape(-1, 3).astype(np.float64) @ world[:3, :3].T + world[:3, 3]
        local = points @ inverse[:3, :3].T + inverse[:3, 3]
        parts.append((evaluated.name, tuple(np.round(local.mean(axis=0), 3)) if len(local) else (), points, local,
                      np.concatenate([[len(co) // 3, len(totals)], totals, loops]).astype(np.int64)))
    parts.sort(key=lambda p: (p[0], p[1]))  # The depsgraph's order is not the file's: the same each time.
    location, rotation, scale = ob.matrix_world.decompose()
    entry = {"type": ob.type, "origin": [round(v, 4) for v in location],
             "rotation": [round(math.degrees(a), 2) for a in rotation.to_euler()], "scale": [round(v, 4) for v in scale],
             "materials": {p.name: [s.material.name if s.material else "" for s in p.material_slots]
                           for p in (ob, *ob.children_recursive) if p.material_slots},
             "dims": None, "bounds": None, "verts": 0, "faces": 0, "topology": "", "vertices": "", "sample": []}
    points = np.vstack([p[2] for p in parts]) if parts else np.empty((0, 3))
    if len(points):
        local = np.round(np.vstack([p[3] for p in parts]), 4) + 0.0  # + 0.0: -0.0 and 0.0 alike.
        low, high = points.min(axis=0), points.max(axis=0)
        index = np.unique(np.linspace(0, len(local) - 1, min(len(local), 16)).round().astype(int))
        entry.update(dims=[round(float(v), 4) for v in high - low],
                     bounds=[[round(float(v), 4) for v in low], [round(float(v), 4) for v in high]],
                     verts=int(len(points)), faces=sum(int(p[4][1]) for p in parts),
                     topology=_hash(np.concatenate([p[4] for p in parts])), vertices=_hash(local),
                     sample=[[float(v) for v in local[i]] for i in index])
    return entry


def snapshot(names: list) -> dict:
    """{name: record} for the named objects; a name not in the file raises _Fail. Main thread."""
    return {name: record(_object(name)) for name in names}


def _preserved(c, _spawn):
    """Each named object against its record in the check's snapshot (baseline, filled in by Loopcut
    from the checks tool's snapshot), less the excluded aspects: what changed, with the numbers."""
    import bpy
    baseline, exclude = c.get("baseline") or {}, c.get("exclude") or []
    if not baseline:
        raise _Unavailable(f"no records for snapshot {c['snapshot']}: take one with the checks tool's snapshot")
    changed, missing, same = [], [], 0
    for name in c["names"]:
        if name not in baseline:
            missing.append(name)
            continue
        ob = bpy.data.objects.get(name)
        diffs = ["is gone"] if ob is None else preservation(baseline[name], record(ob), exclude)
        if diffs:
            changed.append(f"{name}: {'; '.join(diffs)}")
        else:
            same += 1
    compared = ", ".join(a for a in ASPECTS if a not in exclude)
    if changed:
        raise _Fail(" | ".join(changed) + (f" | not in {c['snapshot']}: {', '.join(missing)}" if missing else ""))
    if missing:
        raise _Unavailable(f"not in snapshot {c['snapshot']}: {', '.join(missing)}")
    return f"{same} object{'s' if same != 1 else ''} as snapshot {c['snapshot']} recorded ({compared})"


def _file(c, _spawn):
    import bpy
    import glob
    path = str(c["path"])[2:] if str(c["path"]).startswith("//") else str(c["path"])
    if not bpy.data.filepath:
        raise _Unavailable("the .blend is not saved, so a relative path has no folder")
    full = os.path.join(os.path.dirname(bpy.data.filepath), path)
    if any(ch in path for ch in "*?#"):
        pattern = re.sub(r"#+", lambda m: "[0-9]" * len(m.group()), full)
        found = [f for f in glob.glob(pattern) if os.path.isfile(f) and os.path.getsize(f) > 0]
        if not found:
            raise _Fail(f"no file matches {path}")
        return f"{len(found)} file{'s' if len(found) != 1 else ''} matching {path}"
    if not os.path.isfile(full):
        raise _Fail(f"{path} does not exist")
    size = os.path.getsize(full)
    if not size:
        raise _Fail(f"{path} is empty")
    return f"{path}: {size:,} bytes"


def _named(check: dict) -> list[str]:
    """The object names a check measures (not unchanged's: those existed at spawn)."""
    names = [check.get(field) for field in ("name", "a", "b", "light", "container", "camera", "ground")]
    if check.get("kind") in ("no_overlap", "in_frame"):
        names += list(check.get("names") or [])
    return [n for n in names if isinstance(n, str) and n]


def elsewhere(check: dict, scope: dict) -> list[str]:
    """The objects a check names that this copy does not have and that are not the worker's to
    make (its deliverable, its prefix, what it owns): another worker's, so the check is deferred.
    scope: {"prefix", "deliverable", "owned": keys}. Main thread."""
    import bpy
    from .ownership import prefixed
    prefix, owned = scope.get("prefix") or "", set(scope.get("owned") or ())
    theirs = []
    for name in dict.fromkeys(_named(check)):
        if bpy.data.objects.get(name) is not None or name == scope.get("deliverable") or f"OBJECT:{name}" in owned:
            continue
        if not (prefix and prefixed(name, prefix)):
            theirs.append(name)
    return theirs


MEASURE = {"exists": _exists, "count": _count, "dims": _dims, "contact": _contact, "no_overlap": _no_overlap,
           "suspended": _suspended, "attached_to": _attached_to, "above": _above, "below": _above,
           "in_frame": _in_frame, "faces_max": _faces_max, "origin_base": _origin_base, "material_on": _material_on,
           "light_inside": _light_inside, "light": _light, "setting": _setting, "animated": _animated, "unchanged": _unchanged,
           "file": _file, "no_modifiers": _no_modifiers, "flat_shaded": _flat_shaded, "inside_bounds": _inside_bounds,
           "triangles": _triangles_band, "preserved": _preserved}


def run(checks: list, spawn=None, scope: dict | None = None, advisory: bool = False) -> list[dict]:
    """Measure each check on the open scene (main thread). spawn: the merge.Spawn whose tokens
    `unchanged` compares with, in the parent; the child uses its own stamp_all tokens. A check
    that is malformed or can't be measured is "unavailable", with the reason. scope: a worker's
    {"prefix", "deliverable", "owned"}, in its copy: a check on another worker's objects is
    "deferred" (elsewhere). advisory: a brief's advisory list, each result marked "advisory": a
    miss there is shown, never counted as a failure."""
    results = []
    for check in normalised(checks, "a" if advisory else "c"):
        result = {"id": check["id"], "kind": check.get("kind"), "quote": check.get("quote", ""), "title": title(check)}
        if advisory:
            result["advisory"] = True
        errors = validate([check], where="check", advisory=advisory)
        if errors:
            detail = "; ".join(re.sub(r"^check\[\d+\]( \(\w+\))? ", "", e) for e in errors)
            results.append({**result, "status": "unavailable", "detail": detail})
            continue
        theirs = elsewhere(check, scope) if scope is not None else []
        if theirs:
            results.append({**result, "status": "deferred", "detail": (
                f"{', '.join(theirs)} {'is' if len(theirs) == 1 else 'are'} not in this copy (another worker's): "
                f"runs on the live scene after the merge")})
            continue
        note = ""
        try:
            from . import view_layers
            layer, note = _layer(check)
            with view_layers.evaluating(layer):
                result.update(status="pass", detail=MEASURE[check["kind"]](check, spawn))
        except _Fail as ex:
            result.update(status="fail", detail=str(ex))
        except _Unavailable as ex:
            result.update(status="unavailable", detail=str(ex))
        except Exception as ex:  # noqa: BLE001 - a measuring bug must not take the report down.
            result.update(status="unavailable", detail=f"could not be measured: {type(ex).__name__}: {ex}")
        if note and result["status"] != "unavailable":
            result["detail"] += f" ({note[0].lower()}{note[1:]})"
        results.append(result)
    return results


# Kinds that read evaluated placement or geometry: measured in a view layer that includes their objects.
EVALUATED = {"dims", "contact", "no_overlap", "suspended", "attached_to", "above", "below", "in_frame", "faces_max",
             "origin_base", "light_inside", "inside_bounds", "triangles", "preserved", "animated"}


def _layer(check: dict) -> tuple:
    """(view layer, note) a check measures in: the active one when it includes every object the check
    names, else one that does (view_layers.common; a camera's first), the note saying which. Objects
    every view layer excludes have no evaluated placement: _Unavailable says so instead of a number
    read from their stale matrices (Arcane run W0047: six preserved props failed while the staging
    layer excluded them)."""
    import bpy
    from . import view_layers
    if check.get("kind") not in EVALUATED:
        return None, ""
    names = _named(check) + (list(check.get("names") or []) if check.get("kind") == "preserved" else [])
    objects = [o for o in (bpy.data.objects.get(n) for n in dict.fromkeys(names)) if o is not None]
    lost = [o.name for o in objects if view_layers.home(o) is None]
    if lost:
        raise _Unavailable(view_layers.unavailable(lost))
    camera = bpy.data.objects.get(check["camera"]) if isinstance(check.get("camera"), str) else None
    layer, _missing = view_layers.common(objects, required=[camera] if camera is not None else ())
    return layer, view_layers.elsewhere(layer, objects)
