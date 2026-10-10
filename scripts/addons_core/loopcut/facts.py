"""What a worker's deliverable measures, for the orchestrator to read instead of what the worker says
it built (m4_effort_replay_analysis §5.8, §6.5): low-effort workers reported "glass neck", "flat
shading" and "recessed lid" of props that had none.

measure(root, boxes) is the one place a deliverable is measured: the worker's Blender calls it at
report time (worker_child.finish, through deliverables()) and the advisory checks read it
(acceptance: no_modifiers, flat_shaded, inside_bounds, triangles). A deliverable is a root object
with everything parented under it: evaluated and base triangles, unapplied modifiers, the share of
smooth-shaded faces, non-manifold edges, faces per material, world bounds and size against the
brief's planned boxes, and the origin against its base (acceptance.base_point). deliverables() adds
each one's UV maps as uv_tools audits them (uv_facts): mapped, islands, outside 0-1, overlap,
stretch and texel density; and its paint per material slot (paint_stats.measured: values, patches,
noise, accents; the orchestrator reads paint_stats.summary). The rest is pure: planned() picks the
brief's box and says how far out the deliverable is, text() and uv_text() are the delegate result's
lines, flags() the claims in a worker's report that the numbers plainly contradict.
"""

import math
import re

GEOMETRY = ("MESH", "CURVE", "SURFACE", "FONT", "META")
FLAT_SHARE = 0.05     # Smooth faces up to this share still read as flat shaded.
BOUNDS_TOL = 0.02     # Metres a deliverable may stand outside its planned box and still be inside.
TRIS_OFF = 0.15       # A triangle count a report states this far from every measured one is a contradiction.
MAX_ROOTS = 6         # Deliverables measured per worker; the rest are counted.
MAX_FACES = 6         # Materials named per deliverable in text(), the most faces first.
MAX_UV_FACES = 20_000  # Faces whose UVs one deliverable audits (uv_tools.audit, about 40 us a face); past it, named.


# ------------------------------------------------------------------ measuring (bpy)

def measure(root, boxes=()) -> dict:
    """One deliverable, root and every object under it, evaluated as the scene shows it. boxes: the
    brief's planned boxes, [[x0, y0, z0], [x1, y1, z1]] each. Main thread."""
    import bpy
    import numpy as np
    from . import acceptance
    graph = bpy.context.evaluated_depsgraph_get()
    members = [root, *root.children_recursive]
    fact = {"tris": acceptance._triangles(root), "base_tris": 0, "modifiers": [], "faces": 0, "smooth": 0.0,
            "non_manifold": 0, "materials": {}}
    smooth = 0
    for ob in members:
        fact["modifiers"] += [m.name for m in getattr(ob, "modifiers", ()) if m.show_viewport or m.show_render]
        if ob.type not in GEOMETRY:
            continue
        if ob.type == "MESH":
            loops = np.empty(len(ob.data.polygons), dtype=np.int64)
            ob.data.polygons.foreach_get("loop_total", loops)
            fact["base_tris"] += int((loops - 2).sum())
        evaluated = ob.evaluated_get(graph)
        mesh = evaluated.to_mesh()
        try:
            count = len(mesh.polygons)
            if not count:
                continue
            flags = np.empty(count, dtype=bool)
            mesh.polygons.foreach_get("use_smooth", flags)
            smooth += int(flags.sum())
            fact["faces"] += count
            edges = np.empty(len(mesh.loops), dtype=np.int64)
            mesh.loops.foreach_get("edge_index", edges)
            fact["non_manifold"] += int((np.bincount(edges, minlength=len(mesh.edges)) != 2).sum())
            indices = np.empty(count, dtype=np.int64)
            mesh.polygons.foreach_get("material_index", indices)
            slots = [slot.material.name if slot.material else "(none)" for slot in ob.material_slots]
            for index, faces in enumerate(np.bincount(indices)):
                if faces:
                    name = slots[index] if index < len(slots) else "(none)"
                    fact["materials"][name] = fact["materials"].get(name, 0) + int(faces)
        finally:
            evaluated.to_mesh_clear()
    fact["smooth"] = round(smooth / fact["faces"], 3) if fact["faces"] else 0.0
    points = acceptance._points(root)
    origin = tuple(root.matrix_world.translation)
    fact["origin"] = [round(v, 3) for v in origin]
    if points is not None:
        low, high = points.min(axis=0), points.max(axis=0)
        fact["bounds"] = [[round(float(v), 3) for v in low], [round(float(v), 3) for v in high]]
        fact["size"] = [round(float(h - l), 3) for l, h in zip(low, high)]
        fact["origin_off"] = round(math.dist(origin, acceptance.base_point(points)), 4)
        fact["planned"] = planned(fact["bounds"], boxes)
    return fact


def uv_facts(root) -> dict | None:
    """Its meshes' render UV maps as uv_tools.audit measures them, summed for one line: None when no
    mesh has one and none of their materials reads an image (nothing to say)."""
    from . import uv_tools
    meshes = [o for o in (root, *root.children_recursive) if o.type == "MESH" and len(o.data.polygons)]
    images = any(n.type == "TEX_IMAGE" and n.image for o in meshes for s in o.material_slots
                 if s.material and s.material.node_tree for n in s.material.node_tree.nodes)
    fact = {"meshes": len(meshes), "mapped": 0, "images": images, "islands": 0, "outside": 0, "overlap": [],
            "area": 0.0, "angle": 0.0, "stretched": 0.0, "density": [], "skipped": []}
    left = MAX_UV_FACES
    for ob in meshes:
        if len(ob.data.polygons) > left:
            fact["skipped"].append(ob.name)
            fact["mapped"] += bool(ob.data.uv_layers)
            continue
        measured = uv_tools.measure(ob)
        if measured is None:
            continue
        left -= len(ob.data.polygons)
        stats = measured[1]
        fact["mapped"] += 1
        fact["islands"] += stats["islands"]
        fact["outside"] += len(stats["outside"])
        if stats["overlapping"] or stats["folded"]:
            fact["overlap"].append(ob.name)
        fact["area"] += stats["area"]
        fact["angle"] += stats["angle"] * stats["area"]
        fact["stretched"] += stats["angle_share"] * stats["area"]
        if stats["density"][1] > 0:
            fact["density"].append(round(stats["density"][1]))
    if not fact["mapped"] and not images:
        return None
    if fact["area"]:
        fact["angle"], fact["stretched"] = round(fact["angle"] / fact["area"], 2), round(fact["stretched"] / fact["area"], 3)
    return fact


def deliverables(objects, boxes=()) -> dict:
    """{root name: measure(root)} for the worker's own objects: each one whose parent is not its own
    too, with geometry somewhere under it (a helper empty alone is not a deliverable); at most
    MAX_ROOTS, the rest counted under "+more"."""
    import bpy
    from . import paint_stats
    own = {o.session_uid for o in objects}
    roots = [o for o in objects if (o.parent is None or o.parent.session_uid not in own)
             and any(m.type in GEOMETRY for m in (o, *o.children_recursive))]
    measured = {root.name: measure(root, boxes) for root in roots[:MAX_ROOTS]}
    for root in roots[:MAX_ROOTS]:
        try:
            measured[root.name]["uv"] = uv_facts(root)
        except Exception as ex:  # noqa: BLE001 - a UV audit bug costs the UV line, not the facts.
            measured[root.name]["uv"] = {"error": f"{type(ex).__name__}: {ex}"[:120]}
        try:
            measured[root.name]["paint"] = paint_stats.measured((root, *root.children_recursive), bpy.context.scene)
        except Exception as ex:  # noqa: BLE001 - and a paint stats bug the paint line.
            measured[root.name]["paint"] = {"error": f"{type(ex).__name__}: {ex}"[:120]}
    if len(roots) > MAX_ROOTS:
        measured["+more"] = len(roots) - MAX_ROOTS
    return measured


# ------------------------------------------------------------------ pure

def planned(bounds, boxes) -> dict | None:
    """The planned box a deliverable belongs to (the one it overlaps most, else the nearest) and how far
    it stands outside it per side ({"x-": 0.175}, metres, only sides out by more than a millimetre);
    None when the brief planned no box."""
    boxes = [b for b in boxes or () if isinstance(b, (list, tuple)) and len(b) == 2]
    if not boxes or not bounds:
        return None
    low, high = bounds

    def overlap(box):
        return math.prod(max(0.0, min(high[i], box[1][i]) - max(low[i], box[0][i])) for i in range(3))

    def distance(box):
        return math.dist([(low[i] + high[i]) / 2 for i in range(3)], [(box[0][i] + box[1][i]) / 2 for i in range(3)])

    index = max(range(len(boxes)), key=lambda n: (overlap(boxes[n]), -distance(boxes[n])))
    box = boxes[index]
    out = {}
    for i, axis in enumerate("xyz"):
        for side, excess in (("-", box[0][i] - low[i]), ("+", high[i] - box[1][i])):
            if excess > 0.001:
                out[f"{axis}{side}"] = round(excess, 3)
    return {"box": index + 1, "count": len(boxes), "out": out}


def outside(fact: dict, tol: float = BOUNDS_TOL) -> dict:
    """The sides a deliverable stands out of its planned box by more than tol."""
    plan = fact.get("planned") or {}
    return {side: excess for side, excess in (plan.get("out") or {}).items() if excess > tol}


def _m(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def text(name: str, fact) -> str:
    """One deliverable as the delegate result shows it."""
    if not isinstance(fact, dict):
        return f"+{fact} more not measured"
    parts = [f"{fact['tris']:,} tris" + (f" (base {fact['base_tris']:,})" if fact["base_tris"] != fact["tris"] else ""),
             f"modifiers {', '.join(fact['modifiers'][:3])}" if fact["modifiers"] else "no modifiers",
             f"{fact['smooth']:.0%} smooth", f"{fact['non_manifold']} non-manifold edges"]
    if fact.get("size"):
        parts.append(" x ".join(_m(v) for v in fact["size"]) + " m")
    plan = fact.get("planned")
    if plan:
        box = f"box {plan['box']}" if plan["count"] > 1 else "its box"
        far = outside(fact, 0.001)
        parts.append(f"outside {box} " + ", ".join(f"{side} {v * 1000:.0f} mm" for side, v in far.items())
                     if far else f"inside {box}")
    if fact.get("origin_off") is not None:
        parts.append(f"origin {fact['origin_off'] * 1000:.0f} mm from its base centre")
    faces = sorted(fact["materials"].items(), key=lambda item: -item[1])
    if faces:
        shown = ", ".join(f"{material} {count}" for material, count in faces[:MAX_FACES])
        parts.append("faces " + shown + (f", +{len(faces) - MAX_FACES} more" if len(faces) > MAX_FACES else ""))
    return f"{name}: " + "; ".join(parts)


def uv_text(name: str, uv) -> str:
    """The deliverable's UV line (uv_facts), or "" when there is nothing to say."""
    if not isinstance(uv, dict):
        return ""
    if uv.get("error"):
        return f"{name} UVs: not measured ({uv['error']})"
    if not uv["mapped"]:
        return f"{name} UVs: none on its {uv['meshes']} mesh{'es' * (uv['meshes'] != 1)}, and its materials read images"
    parts = ["all meshes mapped" if uv["mapped"] == uv["meshes"] else f"{uv['mapped']} of {uv['meshes']} meshes mapped"]
    if uv["density"] or uv["area"]:
        low, high = min(uv["density"], default=0), max(uv["density"], default=0)
        parts += [f"{uv['islands']} islands",
                  f"{uv['outside']} faces outside 0-1" if uv["outside"] else "all inside 0-1",
                  f"overlap in {', '.join(uv['overlap'][:3])}" if uv["overlap"] else "no overlap",
                  f"stretch {uv['angle']:.2f} ({uv['stretched']:.0%} of area over 1.5x)",
                  (f"{low}-{high}" if high != low else f"{low}") + " px/m at 1024 px"]
    if uv["skipped"]:
        parts.append(f"not audited (over {MAX_UV_FACES:,} faces): {', '.join(uv['skipped'][:3])}")
    return f"{name} UVs: " + ", ".join(parts)


# A claim, not a limit or the brief's band echoed back: words before a number that make it one.
_LIMIT = re.compile(r"(?:under|below|less than|fewer than|at most|max(?:imum)?|budget|limit|cap|within|band|"
                    r"target|[<≤-]|to)\s*$", re.I)
_TRIS = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)\s*(?:evaluated\s+)?(?:tris|triangles)\b", re.I)
_FLAT = re.compile(r"\bflat[- ]shad(?:ed|ing)\b|\bshade_flat\b", re.I)
_DENIED = re.compile(r"\b(?:no|not|never|without|instead of|rather than|removed|dropped|replaced|lacks?|missing)\b"
                     r"[^.;]{0,25}$", re.I)


def _said_plainly(said: str, match) -> bool:
    """Whether a mention is a claim: nothing that denies it just before, in the same sentence."""
    return not _DENIED.search(said[max(0, match.start() - 40):match.start()])


def flags(said: str, measured: dict, materials=(), missing=()) -> list[str]:
    """Where the worker's own words plainly contradict what was measured, conservatively (a false flag
    costs the orchestrator a needless re-brief): its deliverable does not exist (`missing`, whatever
    it says); it says flat shading and a deliverable is more than FLAT_SHARE smooth; it names one of
    `materials` (the palette's, the brief's part materials) that no face of its deliverables wears;
    it states a triangle count (not a limit or a band) more than TRIS_OFF from every measured one and
    from their sum. [] when it says none of those."""
    said = " ".join(str(said or "").split())
    found = {name: f for name, f in (measured or {}).items() if isinstance(f, dict)}
    notes = [f"deliverable missing: no object named {name} in its copy at report time" for name in missing or ()]
    if not said or not found:
        return notes
    if any(_said_plainly(said, m) for m in _FLAT.finditer(said)):
        smooth = [f"{name} {f['smooth']:.0%}" for name, f in found.items() if f["faces"] and f["smooth"] > FLAT_SHARE]
        if smooth:
            notes.append(f"says flat shading; smooth faces measured: {', '.join(smooth)}")
    worn = {material for f in found.values() for material in f["materials"]}
    for material in dict.fromkeys(m for m in materials or () if isinstance(m, str) and m):
        mentions = list(re.finditer(rf"(?<![\w]){re.escape(material)}(?![\w])", said))
        if material not in worn and any(_said_plainly(said, m) for m in mentions):
            notes.append(f"names {material}; no face of its deliverables wears it")
    totals = [f["tris"] for f in found.values()]
    totals += [sum(totals)] if len(totals) > 1 else []
    for match in _TRIS.finditer(said):
        stated = int(match.group(1).replace(",", ""))
        if not stated or _LIMIT.search(said[max(0, match.start() - 20):match.start()]):
            continue
        if all(abs(stated - t) > TRIS_OFF * max(t, 1) for t in totals):
            notes.append(f"says {stated:,} triangles; measured {', '.join(f'{t:,}' for t in totals)}"
                         + (" (the last in all)" if len(totals) > 2 else ""))
    return list(dict.fromkeys(notes))
