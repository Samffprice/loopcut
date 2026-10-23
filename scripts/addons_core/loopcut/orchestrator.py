"""The orchestrator: the agent's own loop with one more power, handing deliverables to workers.

Its prompt text is served by Loopcut (contract 0.3).

delegate(wave) is a tool like any other and runs on the main thread: it validates the wave against
the user's words, the live scene and the workers still running, dry-runs the planned layout, saves
the scene copy the workers start from, and returns a result whose `then` starts the wave
(waves.run_wave) on the agent thread and returns once one of the turn's workers has finished and
merged; delegate wait (no briefs) returns the next ones, and the finish gate waits for the rest.
Checks are written by the orchestrator but only from quotes of the user, and Loopcut, not any model,
runs them on the live scene before the turn may end (finish_gate). Those rules come from M0: a spec
that invents scope, moves what the user said to keep, or plans numbers nobody measured makes a
builder worse, and checks written from the spec's own numbers pass its own mistakes.

Everything that reads Blender is in the functions marked "Main thread"; the rest is pure.
"""

import fnmatch
import functools
import json
import re

from . import acceptance, config, context, glb, state

ROLES = ["modeling", "materials", "lighting", "camera", "animation"]
ASPECTS = ("lighting", "camera", "materials")
MAX_CHECKS = 8
MAX_NOTES = 600      # Characters of shared.notes: one short paragraph.
MAX_STYLE = 800      # Characters of a worker's style (art direction): look, parts, detail, finish.
MAX_BOXES = 8        # A worker's planned bounds: one box, or one per prop of a scattered set.
MAX_REFERENCES = 3   # The user's images one brief shows its worker (context.KEEP_ATTACHED): each is paid every request.
TOLERANCE = 0.005    # Metres: planned boxes may touch or overlap this much; a support's top may be off this much.
Z0_SNAP = 0.05       # Metres: a bounds.z0 off its support's top_z by up to this much is snapped, not refused.
OVERLAP_WARN = 0.02  # Metres: two modeling boxes overlapping by less than this is a warning, not a refusal.
MAX_BOUNCES = 2
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")
FLOOR = "floor"  # rests_on "floor" with no object of that name: z = 0.


def _setting_paths() -> str:
    render = [p[len("render."):] for p in acceptance.SETTINGS if p.startswith("render.")]
    rest = [p for p in acceptance.SETTINGS if not p.startswith("render.")]
    return ", ".join([f"render.{{{','.join(render)}}}", *rest])


# Every kind's fields, ? optional: the limits delegate refuses by (acceptance.validate), said up front.
CHECKS_TEXT = (
    "{kind, quote, ...}, ? = optional: exists name type?; count prefix|collection type? min? max?; dims name "
    "size [x,y,z] m (null = any) tol?; contact a relation (rests_on|touches|clear_of|inside) b gap? tol?; suspended a "
    "b a_part? b_part? (material:M|group:G|attribute:A) ground? clear? tol?; attached_to a b a_part? b_part? tol?; "
    "above a b a_part? b_part? gap?; below a b a_part? b_part? gap?; no_overlap "
    f"names (2-{acceptance.MAX_NAMES}) tol?; in_frame camera names min_inside? region? [x0,y0,x1,y1] (0..1) "
    "frame?; faces_max name max; origin_base name tol? origin_xy? [x,y]; material_on name material; light_inside light container; "
    "light name type? energy? [min,max] W color? [r,g,b] tol? spot_size? [min,max] deg; setting path "
    f"({_setting_paths()}) equals|min|max; animated name path frames [a,b]; unchanged names; preserved names snapshot "
    "exclude?; file path (relative)")

# The advisory list's kinds (acceptance.ADVISORY_KINDS), and that every kind above may join it.
ADVISORY_TEXT = ("Any check kind, or no_modifiers name; flat_shaded name max_smooth?; inside_bounds name tol?; "
                 "triangles name min? max?")

SCHEMAS = [
    {"type": "function", "function": {
        "name": "delegate",
        "title": "Planning background helpers",
        "description": (
            "Start a wave of workers, any number (the rest queue); returns once any finish: merged, reported, "
            "and who still runs or waits."),
        "parameters": {"type": "object", "properties": {
            "dry_run": {"type": "boolean"},
            "wait": {"type": "boolean", "description": "No wave: the running workers' next results"},
            "wave": {"type": "object", "required": ["workers"], "properties": {
                "shared": {"type": "object", "description": (
                    "keep: {names, quote}; palette: material names; supports: "
                    f"[{{name, top_z}}] measured; units; notes: at most {MAX_NOTES} characters")},
                "workers": {"type": "array", "items": {
                    "type": "object", "required": ["id", "deliverable", "role", "prefix", "requirements"],
                    "properties": {
                        "id": {"type": "string"},
                        "deliverable": {"type": "string", "description": "Root object's name"},
                        "role": {"type": "string", "enum": ROLES},
                        "prefix": {"type": "string", "description": "Unique letters and digits naming its "
                                                                    "materials and helpers (Prefix_Name); no live "
                                                                    "name may start with it unless it owns it or "
                                                                    "shared lists it"},
                        "owns": {"type": "array", "items": {"type": "string"},
                                 "description": "Existing names it may change"},
                        "aspects": {"type": "array", "items": {"type": "string", "enum": list(ASPECTS)}},
                        "requirements": {"type": "array", "items": {"type": "object"}, "description": "{quote, do}"},
                        "defaults": {"type": "array", "items": {"type": "string"}},
                        "style": {"type": "string", "description": f"Art direction, at most {MAX_STYLE} "
                                                                   "characters: look, parts, detail, finish"},
                        "continues": {"type": "string", "description": "An earlier worker's id"},
                        "references": {"type": "array", "items": {"type": "string"},
                                       "description": "Attached images it sees, by name"},
                        "bounds": {"type": "array", "description": "Planned world box [[x0,y0,z0],[x1,y1,z1]], "
                                                                   f"or up to {MAX_BOXES} (one per prop)"},
                        "rests_on": {"type": "string", "description": "A support; bounds z0 = its top_z"},
                        "checks": {"type": "array", "maxItems": MAX_CHECKS, "items": {"type": "object"},
                                   "description": CHECKS_TEXT},
                        "advisory": {"type": "array", "items": {"type": "object"}, "description": ADVISORY_TEXT}}}}}}}}}},
    {"type": "function", "function": {
        "name": "checks",
        "title": "Checking the results",
        "description": (
            "No arguments: run every check now. add: as delegate's checks, run now and before your reply "
            "ends. retire: [{id, reason}], the reason quoting the user or what you measured. snapshot: names "
            "to record for preserved."),
        "parameters": {"type": "object", "properties": {
            "snapshot": {"type": "array", "items": {"type": "string"}},
            "add": {"type": "array", "items": {"type": "object"}},
            "retire": {"type": "array", "items": {"type": "object"}}}}}},
    {"type": "function", "function": {
        "name": "accept_quarantine",
        "title": "Accepting the changes kept aside",
        "description": "Move a wave's quarantined items into the scene.",
        "parameters": {"type": "object", "required": ["wave"], "properties": {
            "wave": {"type": "string"}, "names": {"type": "array", "items": {"type": "string"}}}}}},
    *glb.SCHEMAS,
]

LATE = ("[Loopcut] You replied while workers were still running; Loopcut collected their outcomes. Review "
        "their results as after any delegate, then reply again:\n{report}")
BOUNCE = ("[Loopcut] These checks fail on the live scene:\n{lines}\nSend a fix to a worker's object back to that "
          "worker (delegate, continues), fix your own work yourself, or say plainly that they are not done. A check the user has since overridden, or one that is wrong, you retire "
          "with the checks tool (its id, and the user's words or what you measured); never to hide a real failure.")


# ------------------------------------------------------------------ the user's words

# One matcher for every quote a wave carries, the checks' (acceptance.validate) and the rest.
normalise = acceptance.normalise
quote_found = acceptance.quote_found


def _quote_error(quote, words: str) -> str:
    if not isinstance(quote, str) or len(normalise(quote).replace(" ", "")) < 3:
        return "needs a quote: the user's own words (at least a word) that ask for it"
    if not quote_found(quote, words):
        return f"quote {quote[:80]!r} is not in the user's messages; quote their exact words, or make it a default"
    return ""


def user_words(session: dict) -> str:
    """What the user wrote this conversation, with no <scene_context>: their messages as the chat shows
    them, plus the reference card of an image they attached (it stands for their picture)."""
    parts = [item["text"] for item in session["items"] if item.get("kind") == "user"]
    for message in session["messages"]:
        if message.get(context.REFERENCE_CARD):
            content = message["content"]
            text = content[0]["text"] if isinstance(content, list) else str(content)
            parts += re.findall(r"<reference_card[^>]*>(.*?)</reference_card>", text, re.S)
    return "\n".join(parts)


# ------------------------------------------------------------------ the wave, pure

def _names(value) -> list:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


_KEEP_LIST_SHAPE = "_keep_list_shape"  # Internal marker: shared.keep arrived as a list, not {names, quote};
# repair_wave notes it and strips it, so it never reaches a worker or a saved wave.


def _keep_list_names(value: list) -> list:
    """Names read from shared.keep given as a list: of plain names, or of {"name": ...} objects."""
    names = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def normalise_wave(wave: dict) -> dict:
    """The wave with every optional field present and each worker's checks numbered c1, c2... in
    brief order. Malformed values are kept as they are, for validate_wave to name. shared.keep given
    as a list (live_facts and validate_wave need it as {names, quote}) is reshaped here, marked
    _KEEP_LIST_SHAPE so repair_wave can still see and note the mistake."""
    wave = json.loads(json.dumps(wave))  # A copy the caller's arguments do not share.
    shared = wave.get("shared") if isinstance(wave.get("shared"), dict) else {}
    keep = shared.get("keep")
    if isinstance(keep, list):
        shared_keep = {"names": _keep_list_names(keep), "quote": "", _KEEP_LIST_SHAPE: True}
    else:
        keep = keep if isinstance(keep, dict) else {}
        shared_keep = {"names": keep.get("names", []), "quote": keep.get("quote", "")}
    shared.update(keep=shared_keep, palette=shared.get("palette", []), supports=shared.get("supports", []),
                  units=str(shared.get("units") or "metres, Z up, floor z = 0"), notes=str(shared.get("notes") or ""))
    wave["shared"] = shared
    workers = wave.get("workers") if isinstance(wave.get("workers"), list) else []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        for key in ("owns", "aspects", "requirements", "defaults", "checks", "advisory"):
            worker.setdefault(key, [])
        worker.setdefault("bounds", None)
        worker.setdefault("rests_on", "")
        worker.setdefault("style", "")
        worker.setdefault("continues", "")
        worker.setdefault("references", None)  # None: not given, so a continues brief keeps its worker's.
        worker.pop("steps", None)  # Older orchestrator prompts may still send it; budgets now bound the run.
        for key, prefix in (("checks", "c"), ("advisory", "a")):
            for number, check in enumerate(worker[key] if isinstance(worker[key], list) else [], 1):
                if isinstance(check, dict):
                    check["id"] = f"{prefix}{number}"
    wave["workers"] = workers
    return wave


def _box(value):
    """((x0,y0,z0), (x1,y1,z1)) from planned bounds, or None when malformed."""
    try:
        low, high = value
        low, high = tuple(float(v) for v in low), tuple(float(v) for v in high)
    except (TypeError, ValueError):
        return None
    if len(low) != 3 or len(high) != 3 or any(a > b for a, b in zip(low, high)):
        return None
    return low, high


def _boxes(value):
    """Planned bounds as a list of boxes: one box, or a list of up to MAX_BOXES (one per prop of a
    scattered set: one box around props spread over a room overlapped everything); None when malformed."""
    one = _box(value)
    if one is not None:
        return [one]
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BOXES:
        return None
    boxes = [_box(v) for v in value]
    return None if any(b is None for b in boxes) else boxes


def _box_text(box) -> str:
    (x0, y0, z0), (x1, y1, z1) = box
    return f"x {x0:g}..{x1:g}, y {y0:g}..{y1:g}, z {z0:g}..{z1:g}"


def _support_tops(shared: dict, live: dict) -> dict:
    """name -> top z the brief declares, for the supports that are well formed."""
    tops = {}
    for support in shared["supports"] if isinstance(shared["supports"], list) else []:
        if isinstance(support, dict) and isinstance(support.get("name"), str) and isinstance(support.get("top_z"), (int, float)):
            tops[support["name"]] = float(support["top_z"])
    return tops


def _owned_names(worker: dict, live: dict) -> set:
    """The live names a worker will own: owns, the single-user data of those objects, and its
    aspects' lights and cameras (with their data) and world (ownership.expand_owns, by name)."""
    objects = live.get("objects", {})
    owned = set(_names(worker.get("owns"))) if isinstance(worker.get("owns"), list) else set()
    aspects = worker.get("aspects") if isinstance(worker.get("aspects"), list) else []
    kinds = {kind for aspect, kind in (("lighting", "LIGHT"), ("camera", "CAMERA")) if aspect in aspects}
    owned |= {name for name, facts in objects.items() if facts.get("type") in kinds}
    owned |= {objects[name]["data"] for name in list(owned) if (objects.get(name) or {}).get("data")}
    if "lighting" in aspects and live.get("world"):
        owned.add(live["world"])
    return owned


# ------------------------------------------------------------------ repairs: small mistakes fixed, not refused
#
# M4 lesson (GLM as orchestrator, 2026-09-28): most refused delegate calls were small, mechanical
# mistakes a repair can fix with no loss of the M0 guarantees (scope from the user's own words, kept
# objects owned by nobody, planned numbers that add up). A refusal is now kept only for what would
# break the wave or that scope lock; everything else here is repaired or dropped, and said so in the
# result ("Repairs: ...") so the orchestrator can see what changed.

_CHECK_ALIASES = {  # kind -> {right field: [wrong names tried, in order]}; no_overlap's a/b is special below
    "contact": {"relation": ("contact", "type")},
    "light_inside": {"container": ("in", "b")},
    "count": {"prefix": ("name",)},
    "dims": {"size": ("dimensions",)},
    "animated": {"path": ("property", "channel", "data_path")},
    "setting": {"path": ("property", "channel", "data_path")},
}

# Kinds whose one clear identifying field is "name" (round 4: GLM wrote object/target for it, or
# the kind's own name as a wrapper key holding it, e.g. {"kind": "exists", "exists": "Door"}).
_NAME_KINDS = ("exists", "dims", "faces_max", "origin_base", "material_on", "light", "animated",
               *acceptance.ADVISORY_KINDS)
_NAME_ALIASES = ("object", "target")
_CONTACT_A_ALIASES = ("object", "subject")  # -> a
_CONTACT_RESTS_ALIASES = ("on", "support")  # -> b, relation rests_on (what a rests_on is named after)

# A worker's role, mapped to the one it plainly meant (ROLES is the real, short list; anything else
# is still refused with that list, validate_wave's "{label}.role: one of ...").
_ROLE_SYNONYMS = {
    "model": "modeling", "modeler": "modeling", "modelling": "modeling", "props": "modeling", "geometry": "modeling",
    "light": "lighting", "lights": "lighting",
    "cam": "camera", "cameras": "camera", "framing": "camera",
    "anim": "animation", "animate": "animation", "motion": "animation",
    "material": "materials", "shading": "materials",
    "shot": "lighting", "shots": "lighting", "look": "lighting",  # With both aspects (SHOT_ASPECTS).
}
_SHOTS = ("shot", "shots", "look")
SHOT_ASPECTS = ["lighting", "camera"]  # A shot's worker owns its camera and the light it needs (audit item 8).


def normalise_role(role) -> str:
    """A role's known alias or default, after any continuation inheritance."""
    if role in ROLES:
        return role
    return _ROLE_SYNONYMS.get(role.strip().lower(), "modeling") if isinstance(role, str) else "modeling"


def _continued_thread(worker: dict, live: dict) -> dict | None:
    wanted = worker.get("continues")
    threads = live.get("threads")
    if not isinstance(wanted, str) or not wanted or wanted in (live.get("busy") or {}):
        return None
    return threads.get(wanted) if isinstance(threads, dict) else None


def effective_role(worker: dict, live: dict) -> str:
    """The role plan repair and retry identity use, including a live continuation's inheritance."""
    thread = _continued_thread(worker, live)
    return normalise_role(worker.get("role") or (thread or {}).get("role"))


def _unwrap_check(check: dict) -> bool:
    """A model sometimes nests a check's real fields one level down, under a key named for its own
    kind ({"kind": "dims", "dims": {"name": ..., "size": ...}}) or the generic "check"
    ({"kind": "contact", "check": {"a": ..., "b": ..., "relation": ...}}): both unwrap here, filling
    in whichever of the check's own fields are still missing, then the wrapper key is dropped. A
    wrapper holding a plain value instead of an object ({"kind": "exists", "exists": "Door"}) stands
    for the kind's one clear field (name for most kinds, size for dims, path for setting)."""
    kind = check.get("kind")
    changed = False
    for key in (kind, "check"):
        if not key or key not in check:
            continue
        nested = check[key]
        if isinstance(nested, dict):
            del check[key]
            changed = True
            for field, value in nested.items():
                if field not in check and field not in ("kind", "quote", "id") and value is not None:
                    check[field] = value  # A null in the nested copy means "unset", not "wrong type".
        elif key == kind and kind in _NAME_KINDS and "name" not in check and isinstance(nested, (str, int, float)):
            check["name"], changed = nested, True
            del check[key]
        elif key == kind and kind == "dims" and "size" not in check and isinstance(nested, list):
            check["size"], changed = nested, True
            del check[key]
        elif key == kind and kind == "setting" and "path" not in check and isinstance(nested, str):
            check["path"], changed = nested, True
            del check[key]
        # else: a scalar wrapper matching kind that isn't one of the above (contact's own
        # "contact"/"type" -> relation, e.g.) is left for the caller's other aliases to read.
    return changed


def _apply_contact_aliases(check: dict) -> bool:
    """contact written with object/subject for a, on/support for b (the relation "rests_on" then
    implied by the key): {"object": "Chest", "on": "Table"} -> a Chest, b Table, relation rests_on."""
    changed = False
    if "a" not in check:
        for alias in _CONTACT_A_ALIASES:
            if alias in check:
                check["a"] = check.pop(alias)
                changed = True
                break
    for alias in _CONTACT_RESTS_ALIASES:
        if alias in check:
            value = check.pop(alias)
            changed = True
            if "b" not in check:
                check["b"] = value
            check.setdefault("relation", "rests_on")
    return changed


def _apply_check_aliases(check: dict) -> bool:
    """Field names a model tried instead of the check's real one, mapped in place: fields nested a
    level down under a "check" or kind-named wrapper key unwrapped first; no_overlap's a/b -> names;
    contact's contact/type -> relation, object/subject -> a, on/support -> b (+ relation rests_on);
    light_inside's in/b -> container; count's name -> prefix; dims' dimensions -> size; animated's
    and setting's property/channel/data_path -> path; a name-kind's object/target -> name; setting
    with both equals and min/max -> equals alone; in_frame's region given 0-100 -> fractions. True
    if anything changed."""
    kind = check.get("kind")
    changed = _unwrap_check(check)
    for field in [f for f in check if f not in ("kind", "quote", "id") and check[f] is None]:
        del check[field]  # A null field means "unset", same as leaving it out; missing reads clearer than wrong.
        changed = True
    if kind == "no_overlap" and "names" not in check and isinstance(check.get("a"), str) and isinstance(check.get("b"), str):
        check["names"] = [check.pop("a"), check.pop("b")]
        changed = True
    if kind == "contact":
        changed = _apply_contact_aliases(check) or changed
    for field, aliases in _CHECK_ALIASES.get(kind, {}).items():
        if field in check:
            continue
        for alias in aliases:
            if alias in check:
                check[field] = check.pop(alias)
                changed = True
                break
    if kind in _NAME_KINDS and "name" not in check:
        for alias in _NAME_ALIASES:
            if alias in check:
                check["name"] = check.pop(alias)
                changed = True
                break
    if kind == "setting" and "equals" in check and ("min" in check or "max" in check):
        check.pop("min", None)
        check.pop("max", None)
        changed = True
    if kind == "in_frame":
        region = check.get("region")
        numeric = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        if (isinstance(region, list) and len(region) == 4 and all(numeric(v) for v in region)
                and max(region) > 1 and max(region) <= 100 and min(region) >= 0):
            check["region"] = [v / 100 for v in region]
            changed = True
    return changed


def _sanitize_prefix(prefix) -> str | None:
    """A prefix with its non-alphanumerics stripped and its first letter capitalised, or None when
    nothing letter-first survives (unrepairable)."""
    if not isinstance(prefix, str):
        return None
    cleaned = _NOT_ALNUM.sub("", prefix)
    cleaned = cleaned.lstrip("0123456789")
    if not cleaned:
        return None
    return cleaned[0].upper() + cleaned[1:]


def _numeric6(value) -> bool:
    return isinstance(value, list) and len(value) == 6 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)


def _reshape_bounds(value):
    """A box (or list of boxes) written flat, [x0,y0,z0,x1,y1,z1], reshaped to the real shape
    [[x0,y0,z0],[x1,y1,z1]]; a list of such flat boxes reshaped the same way, one per prop. None
    when the value isn't that: bounds too mangled to reshape are dropped, not guessed at."""
    if _numeric6(value):
        return [[value[0], value[1], value[2]], [value[3], value[4], value[5]]]
    if isinstance(value, list) and 1 <= len(value) <= MAX_BOXES and value and all(_numeric6(v) for v in value):
        return [[[v[0], v[1], v[2]], [v[3], v[4], v[5]]] for v in value]
    return None


_CHECK_FIELD_ERROR = re.compile(r"^check\[\d+\]( \(\w+\))? ")


def _clip_text(text: str, limit: int) -> str:
    from . import workers
    return workers.clip(text, limit)


def _check_problem(check: dict, words: str) -> str:
    """Why this check (aliased already) still can't run, or "": the same rules validate_wave holds
    every check to, so a check kept here never fails there."""
    errors = acceptance.validate([check], [words], where="check")
    return "; ".join(_CHECK_FIELD_ERROR.sub("", e) for e in errors)


def _shot_aspects(worker: dict, workers: list) -> str:
    """A shot's worker gets the aspects no other brief in the wave holds; says which it got."""
    held = {a for w in workers if isinstance(w, dict) and w is not worker and isinstance(w.get("aspects"), list)
            for a in w["aspects"]}
    aspects = worker["aspects"] if isinstance(worker.get("aspects"), list) else []
    added = [a for a in SHOT_ASPECTS if a not in held and a not in aspects]
    worker["aspects"] = aspects + added
    return f", aspects {' and '.join(added)} added (a shot owns its camera and its light)" if added else ""


def _repair_continues(worker: dict, label: str, workers: list, threads: dict, kept: set, shared: dict,
                      objects: dict, live: dict) -> list[str]:
    """A brief sent back to an earlier worker (continues): an unknown id starts fresh; a known one
    fills a missing deliverable, role or prefix from that worker and owns what it built that is still
    in the scene, less what is kept, shared or another brief's. threads: live_facts' "threads"."""
    wanted = worker.get("continues")
    if wanted in (None, "") or wanted in (live.get("busy") or {}):  # A running worker's: validate_wave says so.
        return []
    thread = _continued_thread(worker, live)
    if thread is None:
        worker["continues"] = ""
        known = f" (earlier workers: {', '.join(sorted(threads))})" if threads else ""
        return [f"{label}.continues: no earlier worker {wanted!r} this session{known}; it starts fresh"]
    notes = []
    for key in ("deliverable", "role", "prefix"):
        if not worker.get(key) and thread.get(key):
            worker[key] = thread[key]
            notes.append(f"{label}.{key}: {thread[key]!r}, from {wanted}")
    if worker.get("references") is None:
        worker["references"] = list(thread.get("references") or [])  # [] drops them; a list replaces them.
    owns = worker["owns"] if isinstance(worker.get("owns"), list) else []
    others = {n for w in workers if isinstance(w, dict) and w is not worker and isinstance(w.get("owns"), list)
              for n in w["owns"]}
    common = set(_names(shared.get("palette"))) | set(_support_tops(shared, live))
    present = set(objects) | set(live.get("names") or ())
    added = [n for n in thread.get("created") or () if n in present and n not in kept and n not in others
             and n not in common and n not in owns]
    if added:
        worker["owns"] = owns + added
        notes.append(f"{label}.owns: added what {wanted} built in wave {thread.get('wave', '?')} "
                     f"({', '.join(added[:4])}{f', +{len(added) - 4} more' if len(added) > 4 else ''})")
    return notes


# A tool a brief tells a worker to use that workers are not offered (their tools are the same for every
# role, for the cache: workers.schemas), and the offered one that does its job. GPT-6.1 Sol's lighting
# brief asked for inspect_scene (20260929-235231 W0100-W0106): the worker said so in every step.
# inspect_scene itself is not offered: 394 tokens on every worker request, and a render child of a
# child; capture_viewport's final angle renders the scene's own engine through the camera, and its camera and
# view_layer pick the shot's (Arcane run 20260930-154037: the staged layer and saved cameras were the brief's).
WORKER_INSTEAD = {"inspect_scene": "capture_viewport angle final (camera, view_layer)",
                  "compare_with_reference": "look_at_reference"}


def _not_offered() -> tuple:
    """The orchestrator's tools a worker is not offered, by name (with an underscore: "checks" and
    "delegate" are also plain words)."""
    from . import agent, workers
    offered = workers.tool_names()
    return tuple(n for n in (s["function"]["name"] for s in agent._tool_schemas() + SCHEMAS)
                 if n not in offered and "_" in n)


def _brief_texts(wave: dict):
    """(field, holder, key) for the orchestrator's own words a worker reads: shared.notes, and each
    brief's requirements' do, defaults and style (never a quote: those are the user's); only text.
    A malformed one is validate_wave's to name."""
    texts = [("shared.notes", wave["shared"], "notes")]
    for index, worker in enumerate(wave["workers"]):
        if not isinstance(worker, dict):
            continue
        label = f"workers[{index}]" + (f" ({worker.get('id')})" if isinstance(worker.get("id"), str) else "")
        requirements = worker.get("requirements") if isinstance(worker.get("requirements"), list) else []
        texts += [(f"{label}.requirements[{n}]", r, "do") for n, r in enumerate(requirements) if isinstance(r, dict)]
        defaults = worker.get("defaults") if isinstance(worker.get("defaults"), list) else []
        texts += [(f"{label}.defaults[{n}]", defaults, n) for n in range(len(defaults))]
        texts.append((f"{label}.style", worker, "style"))
    for field, holder, key in texts:
        if isinstance(holder[key] if isinstance(holder, list) else holder.get(key), str):
            yield field, holder, key


def _named_tools(text) -> list:
    """The tools workers are not offered that text names, in the order it names them."""
    found = {n: m.start() for n in _not_offered() if isinstance(text, str) and (m := re.search(rf"\b{n}\b", text))}
    return sorted(found, key=found.get)


def _repair_tools(wave: dict) -> list[str]:
    """A tool the workers are not offered, named in a brief, becomes the offered one that does its
    job (WORKER_INSTEAD), said so; one with none is tool_warnings'."""
    notes = []
    for field, holder, key in _brief_texts(wave):
        for name in _named_tools(holder[key]):
            if name in WORKER_INSTEAD:
                holder[key] = re.sub(rf"\b{name}\b", WORKER_INSTEAD[name], holder[key])
                notes.append(f"{field}: {name} is not a worker tool; replaced with {WORKER_INSTEAD[name]}")
    return notes


def tool_warnings(wave: dict) -> list[str]:
    """A brief naming a tool workers are not offered and nothing replaces (render_job, write_file...):
    the worker can't do that part, so it stays the orchestrator's. Said, never refused."""
    return [f"{field}: {', '.join(names)} {'is not a worker tool' if len(names) == 1 else 'are not worker tools'} "
            f"(workers build; renders, exports and files stay yours, after the last wave)"
            for field, holder, key in _brief_texts(wave) if (names := _named_tools(holder[key]))]


# Aspect materials, said where it is refused and where it is repaired: global, not per object.
MATERIALS_ASPECT = ("aspect materials is global: every material and shader node group in the scene, so an edit "
                    "reaches every object wearing it, and one worker at a time holds it. Recolouring a worker's own "
                    "objects (their slots, face material indices, its own <prefix>_ copies of palette materials) "
                    "needs only owns")


def _scope_materials(workers: list, objects: dict, busy: dict) -> list[str]:
    """Several briefs claiming aspect materials, or one beside a running worker that holds it, is
    almost always a recolour of each worker's own objects written as global ownership (GPT-6.1 Sol,
    20260929-230207 O0014: five dry-run conflicts from recolour workers). The claim is dropped from
    each such brief that owns objects, said so; one that owns none stays, for validate_wave to name."""
    claims = [w for w in workers if isinstance(w, dict) and isinstance(w.get("aspects"), list)
              and "materials" in w["aspects"]]
    running = [wid for wid, brief in busy.items() if "materials" in (brief.get("aspects") or ())]
    if len(claims) < 2 and not (claims and running):
        return []
    held = f"running worker {running[0]} holds it" if running and len(claims) < 2 else f"{len(claims)} briefs claimed it"
    notes = []
    for index, worker in enumerate(workers):
        local = [n for n in _names(worker.get("owns")) if n in objects] if any(worker is w for w in claims) else []
        if local:
            worker["aspects"] = [a for a in worker["aspects"] if a != "materials"]
            label = f"workers[{index}]" + (f" ({worker.get('id')})" if isinstance(worker.get("id"), str) else "")
            notes.append(f"{label}.aspects: dropped materials ({held}); {MATERIALS_ASPECT}, so it still recolours "
                         f"{', '.join(local[:3])}{', …' if len(local) > 3 else ''}")
    return notes


def _advisory_problem(check: dict, worker: dict) -> str:
    """Why this check can't be one of the brief's advisory checks, or "", after filling in what the
    brief already says: the deliverable's name, and inside_bounds' planned bounds."""
    _apply_check_aliases(check)
    if check.get("kind") in _NAME_KINDS and "name" not in check and isinstance(worker.get("deliverable"), str):
        check["name"] = worker["deliverable"]
    if check.get("kind") == "inside_bounds" and "bounds" not in check and _boxes(worker.get("bounds")):
        check["bounds"] = [[list(low), list(high)] for low, high in _boxes(worker["bounds"])]
    errors = acceptance.validate([check], where="check", advisory=True)
    return "; ".join(_CHECK_FIELD_ERROR.sub("", e) for e in errors)


def _attached_name(name, attached: list):
    """The attached image a brief's reference means: its exact name, else the one it names without case
    or extension, else the only one it is part of; anything else as given, for validate_wave to name."""
    if not isinstance(name, str) or name in attached:
        return name
    wanted = name.strip().lower()
    for match in (lambda a: a.lower() == wanted, lambda a: a.lower().rsplit(".", 1)[0] == wanted.rsplit(".", 1)[0]):
        found = [a for a in attached if match(a)]
        if len(found) == 1:
            return found[0]
    found = [a for a in attached if wanted and wanted in a.lower()]
    return found[0] if len(found) == 1 else name


def repair_wave(wave: dict, words: str, live: dict) -> tuple[dict, list[str]]:
    """A normalised wave with small, mechanical mistakes fixed, and the notes to say so (each naming
    its field, "Repairs: ..." in the delegate result). Never invents scope or ownership: a repair only
    drops what has no quote or still fails validation once aliased, aliases a field name, maps or
    defaults a role, reshapes shared.keep given as a list or bounds given flat, sanitises a prefix,
    measures a rests_on support the brief forgot to declare, keeps a name over owning it, or drops a
    name that plainly isn't in the scene. wave: normalise_wave's. words: user_words. live: live_facts'."""
    wave = json.loads(json.dumps(wave))
    shared, workers = wave["shared"], wave["workers"]
    materials = set(live.get("materials", ()))
    objects = live.get("objects", {}) if isinstance(live.get("objects"), dict) else {}
    notes = []

    keep = shared["keep"]
    if keep.pop(_KEEP_LIST_SHAPE, False):
        notes.append(f"shared.keep: a list given instead of {{names, quote}}; read as names "
                     f"({', '.join(keep['names']) or 'none'})")
    if _names(keep.get("names")) and _quote_error(keep.get("quote"), words):
        notes.append("shared.keep.quote: no quote from the user; the names stay kept (nobody owns them) but get "
                     "no unchanged check")
        keep["quote"] = ""
    kept_names = set(_names(keep.get("names")))

    long_notes = shared.get("notes") or ""
    if isinstance(long_notes, str) and len(long_notes) > MAX_NOTES:
        trimmed = long_notes[:MAX_NOTES]
        trimmed = trimmed.rsplit(" ", 1)[0] if " " in trimmed else trimmed
        notes.append(f"shared.notes: {len(long_notes)} characters, over the {MAX_NOTES} cap; trimmed to one paragraph")
        shared["notes"] = trimmed

    palette = _names(shared["palette"])
    dropped = [n for n in palette if n not in materials]
    if dropped:
        notes.append(f"shared.palette: dropped {', '.join(dropped)} (not in the scene; the worker makes its own)")
        shared["palette"] = [n for n in palette if n in materials]

    if not isinstance(shared.get("supports"), list):
        shared["supports"] = []
    tops = _support_tops(shared, live)

    threads = live.get("threads") if isinstance(live.get("threads"), dict) else {}
    attached = list(live.get("references") or ())
    for index, worker in enumerate(workers):
        if not isinstance(worker, dict):
            continue
        label = f"workers[{index}]" + (f" ({worker.get('id')})" if isinstance(worker.get("id"), str) else "")
        mapped = effective_role(worker, live)
        notes += _repair_continues(worker, label, workers, threads, kept_names, shared, objects, live)

        role = worker.get("role")
        if role not in ROLES:
            if isinstance(role, str) and role.strip().lower() in _ROLE_SYNONYMS:
                aspects = _shot_aspects(worker, workers) if role.strip().lower() in _SHOTS else ""
                notes.append(f"{label}.role: {role!r} mapped to {mapped!r}{aspects}")
            else:
                notes.append(f"{label}.role: {role!r} is not a role or a known synonym; defaulted to 'modeling'")
        worker["role"] = mapped

        prefix = worker.get("prefix")
        if isinstance(prefix, str) and prefix and not _PREFIX.match(prefix):
            sanitised = _sanitize_prefix(prefix)
            if sanitised:
                notes.append(f"{label}.prefix: {prefix!r} sanitised to {sanitised!r}")
                worker["prefix"] = sanitised

        aspects = worker.get("aspects")
        if isinstance(aspects, list):
            invalid = [a for a in aspects if a not in ASPECTS]
            if invalid:
                notes.append(f"{label}.aspects: dropped {', '.join(map(str, invalid))} (only "
                             f"{', '.join(ASPECTS)})")
                worker["aspects"] = [a for a in aspects if a in ASPECTS]

        owns = worker.get("owns")
        if isinstance(owns, list):
            conflict = [n for n in owns if isinstance(n, str) and n in kept_names]
            if conflict:
                notes.append(f"{label}.owns: {', '.join(conflict)} also listed in shared.keep; keeping wins, "
                             f"removed from owns (stays kept, nobody owns it)")
                worker["owns"] = [n for n in owns if n not in conflict]

        style = worker.get("style")
        if isinstance(style, list) and all(isinstance(part, str) for part in style):
            notes.append(f"{label}.style: a list given; joined into one text")
            style = worker["style"] = "; ".join(part.strip() for part in style if part.strip())
        elif isinstance(style, dict):
            notes.append(f"{label}.style: an object given; read as its fields")
            style = worker["style"] = "; ".join(f"{k}: {v}" for k, v in style.items())
        if isinstance(style, str) and len(style) > MAX_STYLE:
            notes.append(f"{label}.style: {len(style)} characters, over the {MAX_STYLE} cap; trimmed")
            worker["style"] = _clip_text(style, MAX_STYLE)

        references = worker.get("references")
        if references is None:
            worker["references"] = []
        elif isinstance(references, (list, str)):
            given = [references] if isinstance(references, str) else references
            given = [r.get("name", r) if isinstance(r, dict) else r for r in given]  # [{name}] read as names.
            if all(isinstance(r, str) for r in given):
                worker["references"] = list(dict.fromkeys(_attached_name(r, attached) for r in given))

        rests_on = worker.get("rests_on")
        # Skip only what validate_wave itself treats as the implicit ground (that name, no real
        # object called that); a real object named "Floor" still needs its top measured and added.
        if (isinstance(rests_on, str) and rests_on and rests_on not in tops
                and not (rests_on.lower() == FLOOR and rests_on not in objects)):
            box = (objects.get(rests_on) or {}).get("box")
            if box is not None:
                top_z = float(box[1][2])
                shared["supports"].append({"name": rests_on, "top_z": top_z})
                tops[rests_on] = top_z
                notes.append(f"shared.supports: added {rests_on!r} (top_z {top_z:.3f}, measured; rests_on named "
                             f"it but shared.supports never declared it)")

        bounds = worker.get("bounds")
        if bounds is not None and _boxes(bounds) is None:
            reshaped = _reshape_bounds(bounds)
            if reshaped is not None and _boxes(reshaped) is not None:
                notes.append(f"{label}.bounds: reshaped from a flat list of numbers")
                worker["bounds"] = reshaped
            else:
                notes.append(f"{label}.bounds: malformed; dropped (no layout check for it)")
                worker["bounds"] = None

        requirements = worker.get("requirements")
        if isinstance(requirements, list):
            kept_reqs = []
            for n, requirement in enumerate(requirements):
                if not isinstance(requirement, dict) or not isinstance(requirement.get("do"), str):
                    kept_reqs.append(requirement)  # A structurally bad requirement: validate_wave names it.
                    continue
                problem = _quote_error(requirement.get("quote"), words)
                if problem:
                    if isinstance(worker.get("defaults"), list):
                        worker["defaults"].append(f"{requirement['do'].strip()[:100]} (not required: {problem})")
                    notes.append(f"{label}.requirements[{n}]: dropped - {problem}; moved to defaults")
                    continue
                kept_reqs.append(requirement)
            worker["requirements"] = kept_reqs

        advisory = worker.get("advisory")
        if not isinstance(advisory, list):
            notes.append(f"{label}.advisory: not a list; dropped")
            advisory = worker["advisory"] = []
        checks = worker.get("checks")
        if isinstance(checks, list):
            kept_checks = []
            for n, check in enumerate(checks):
                if not isinstance(check, dict):
                    kept_checks.append(check)
                    continue
                if _apply_check_aliases(check):
                    notes.append(f"{label}.checks[{n}]: fields aliased to {acceptance.title(check)}")
                problem = _check_problem(check, words)
                if problem and not _advisory_problem(check, worker):
                    # Measurable, only not the user's words (or an advisory kind): it still measures,
                    # it just never fails anything.
                    notes.append(f"{label}.checks[{n}]: moved to advisory ({acceptance.title(check)}) - {problem}")
                    check.pop("id", None)
                    advisory.append(check)
                    continue
                if problem:
                    if isinstance(worker.get("defaults"), list):
                        worker["defaults"].append(f"{acceptance.title(check)} (no check: {problem})")
                    notes.append(f"{label}.checks[{n}]: dropped ({acceptance.title(check)}) - {problem}; "
                                 f"moved to defaults")
                    continue
                kept_checks.append(check)
            worker["checks"] = kept_checks
        kept_advice = []
        for n, check in enumerate(advisory):
            problem = _advisory_problem(check, worker) if isinstance(check, dict) else "not a check object"
            if not problem and len(kept_advice) >= acceptance.MAX_ADVISORY:
                problem = f"at most {acceptance.MAX_ADVISORY}"
            if problem:
                what = acceptance.title(check) if isinstance(check, dict) else "?"
                notes.append(f"{label}.advisory[{n}]: dropped ({what}) - {problem}")
                continue
            kept_advice.append(check)
        worker["advisory"] = kept_advice

    notes += _scope_materials(workers, objects, live.get("busy") or {})
    notes += _repair_tools(wave)
    return normalise_wave(wave), notes


def validate_wave(wave: dict, words: str, live: dict) -> list[str]:
    """Why this wave must not start, one line per problem, each naming its field; [] when it may.
    wave: normalise_wave's. words: user_words. live: live_facts' shape ({"objects": {name: {"type",
    "box", "data"?}}, "materials": [...], "names": [...], "world"?})."""
    from .worker_ipc import valid_worker_id
    errors = []
    shared, workers = wave["shared"], wave["workers"]
    objects, materials, names = live.get("objects", {}), set(live.get("materials", ())), live.get("names", ())
    if not workers:
        return ["workers: at least one worker"]
    keep = shared["keep"]
    kept = _names(keep.get("names"))
    for name in kept:
        if name not in objects and name not in names:
            errors.append(f"shared.keep: {name!r} is not in the scene")
    # shared.keep.quote: repair_wave clears an unfounded quote (the names stay kept regardless), so
    # nothing to refuse here.
    for name in _names(shared["palette"]):
        if name not in materials:
            errors.append(f"shared.palette: material {name!r} is not in the scene; create it yourself first")
    tops = _support_tops(shared, live)
    for index, support in enumerate(shared["supports"] if isinstance(shared["supports"], list) else []):
        name = support.get("name") if isinstance(support, dict) else None
        if name not in tops:
            errors.append(f"shared.supports[{index}]: needs name and top_z (a number, measured)")
        elif name not in objects:
            errors.append(f"shared.supports[{index}]: {name!r} is not in the scene; build supports first")
        elif objects[name].get("box"):
            measured = objects[name]["box"][1][2]
            if abs(measured - tops[name]) > TOLERANCE:
                errors.append(f"shared.supports[{index}]: top_z {tops[name]:.3f} but {name}'s top is at {measured:.3f} "
                              f"(measured); use the measured height")
    if len(shared["notes"]) > MAX_NOTES:
        errors.append(f"shared.notes: one short paragraph, at most {MAX_NOTES} characters")
    # The palette and the supports are everyone's: their names (a support's own data too) block no prefix.
    supported = [s.get("name") for s in shared["supports"] if isinstance(s, dict)] if isinstance(shared["supports"], list) else []
    common = set(_names(shared["palette"])) | {n for n in supported if isinstance(n, str)}
    common |= {objects[n]["data"] for n in common if (objects.get(n) or {}).get("data")}
    seen_ids, seen_prefixes, owners, aspect_owner, continued = set(), {}, {}, {}, {}
    for index, worker in enumerate(workers):
        if not isinstance(worker, dict):
            errors.append(f"workers[{index}]: must be an object")
            continue
        wid = worker.get("id")
        label = f"workers[{index}]" + (f" ({wid})" if isinstance(wid, str) else "")
        if not (isinstance(wid, str) and _ID.fullmatch(wid)):
            errors.append(f"{label}.id: letters, digits, - and _, up to 32")
        elif not valid_worker_id(wid):
            errors.append(f"{label}.id: reserved Windows filename; choose another id")
        elif wid.casefold() in seen_ids:
            errors.append(f"{label}.id: used twice")
        if isinstance(wid, str):
            seen_ids.add(wid.casefold())
        deliverable = worker.get("deliverable")
        if not (isinstance(deliverable, str) and deliverable.strip()):
            errors.append(f"{label}.deliverable: the name of what it builds")
        elif deliverable in objects and deliverable not in _names(worker.get("owns")):
            # The merge keeps the root's name, and the checks measure what is called that.
            errors.append(f"{label}.deliverable: {deliverable!r} is already in the scene; to rework it, list it in "
                          f"owns; for a new one, give it a new name")
        if worker.get("role") not in ROLES:
            errors.append(f"{label}.role: one of {', '.join(ROLES)}")
        prefix = worker.get("prefix")
        if not (isinstance(prefix, str) and _PREFIX.match(prefix)):
            errors.append(f"{label}.prefix: a letter, then letters and digits")
        else:
            lower = prefix.lower()
            if lower in seen_prefixes:
                errors.append(f"{label}.prefix: {prefix!r} is also {seen_prefixes[lower][0]}'s")
            else:
                # A name may carry a prefix with no underscore (merge.prefixed), so two prefixes where
                # one starts the other would own each other's names; refuse before the wave starts.
                clash = next((v for k, v in seen_prefixes.items() if lower.startswith(k) or k.startswith(lower)), None)
                if clash is not None:
                    errors.append(f"{label}.prefix: {prefix!r} would collide with {clash[0]}'s {clash[1]!r} "
                                  f"(one starts with the other); use prefixes that share no leading run")
            seen_prefixes[lower] = (wid, prefix)
            mine = _owned_names(worker, live)  # What it owns may carry its prefix already.
            taken = [n for n in names if n.startswith(prefix + "_") and n not in mine and n not in common]
            if taken:
                errors.append(f"{label}.prefix: {prefix!r} is taken by {', '.join(taken[:3])}; to rework those, "
                              f"list them in owns and use a new prefix")
        for name in _names(worker.get("owns")) if isinstance(worker.get("owns"), list) else [None]:
            if name is None:
                errors.append(f"{label}.owns: a list of names")
            elif name in kept:
                errors.append(f"{label}.owns: {name!r} is kept (shared.keep); nobody may own it")
            elif name not in objects and name not in names:
                errors.append(f"{label}.owns: {name!r} is not in the scene; a worker creates what is new")
            elif name in owners:
                errors.append(f"{label}.owns: {name!r} is already owned by {owners[name]}")
            else:
                owners[name] = wid
        for aspect in worker.get("aspects") if isinstance(worker.get("aspects"), list) else [None]:
            if aspect not in ASPECTS:
                errors.append(f"{label}.aspects: only {', '.join(ASPECTS)}")
            elif aspect in aspect_owner:
                why = f" ({MATERIALS_ASPECT}; drop it here)" if aspect == "materials" else ""
                errors.append(f"{label}.aspects: {aspect} is already {aspect_owner[aspect]}'s{why}")
            else:
                aspect_owner[aspect] = wid
        requirements = worker.get("requirements")
        if not isinstance(requirements, list) or not requirements:
            errors.append(f"{label}.requirements: at least one, quoting what the user asked for; a worker "
                          f"with nothing the user asked for is work they did not ask for")
        else:
            for number, requirement in enumerate(requirements):
                if not isinstance(requirement, dict) or not isinstance(requirement.get("do"), str):
                    errors.append(f"{label}.requirements[{number}]: {{quote, do}}")
                    continue
                problem = _quote_error(requirement.get("quote"), words)
                if problem:
                    errors.append(f"{label}.requirements[{number}]: {problem}")
        if not isinstance(worker.get("defaults"), list) or any(not isinstance(d, str) for d in worker["defaults"]):
            errors.append(f"{label}.defaults: plain strings; a default is your choice, so it is never checked")
        if not isinstance(worker.get("style"), str) or len(worker["style"]) > MAX_STYLE:
            errors.append(f"{label}.style: one text, at most {MAX_STYLE} characters")
        errors += _reference_errors(worker.get("references"), label, list(live.get("references") or ()))
        continues = worker.get("continues")
        if not isinstance(continues, str):
            errors.append(f"{label}.continues: an earlier worker's id")
        elif continues and continues in continued:
            errors.append(f"{label}.continues: {continues!r} is also {continued[continues]}'s; one brief per worker")
        elif continues:
            continued[continues] = wid
        checks = worker.get("checks")
        if not isinstance(checks, list):
            errors.append(f"{label}.checks: a list")
            checks = []
        if len(checks) > MAX_CHECKS:
            errors.append(f"{label}.checks: at most {MAX_CHECKS}, got {len(checks)}")
        errors += acceptance.validate(checks, [words], where=f"{label}.checks")  # The same rules the checks run by.
        if worker.get("bounds") is not None and _boxes(worker["bounds"]) is None:
            errors.append(f"{label}.bounds: [[x0,y0,z0],[x1,y1,z1]], or a list of up to {MAX_BOXES} such boxes (one "
                          f"per prop), each low <= high")
        rests_on = worker.get("rests_on")
        if rests_on:
            if not isinstance(rests_on, str):
                errors.append(f"{label}.rests_on: a name")
            elif rests_on in tops or (rests_on.lower() == FLOOR and rests_on not in objects):
                if worker.get("bounds") is None:
                    errors.append(f"{label}.bounds: needed with rests_on, so its base can be checked")
            elif any(isinstance(w, dict) and rests_on == w.get("deliverable") for w in workers):
                errors.append(f"{label}.rests_on: {rests_on!r} is built in this same wave; build what others rest "
                              f"on in an earlier wave (or yourself) so its top is measured")
            else:
                errors.append(f"{label}.rests_on: {rests_on!r} is not in shared.supports; add it with its measured top_z")
        errors += _running_clashes(worker, label, live.get("busy") or {})
    return errors + keep_conflicts(wave)


def _reference_errors(references, label: str, attached: list) -> list[str]:
    """A brief's references: names of images the user attached (live_facts' "references"), at most
    MAX_REFERENCES. None (not given) is none."""
    if references is None:
        return []
    if not isinstance(references, list) or any(not isinstance(r, str) for r in references):
        return [f"{label}.references: a list of the names of images the user attached"]
    errors = []
    unknown = [r for r in references if r not in attached]
    if unknown:
        errors.append(f"{label}.references: {', '.join(map(repr, unknown))} not attached; the user attached "
                      + (', '.join(attached) if attached else "no images"))
    if len(references) > MAX_REFERENCES:
        errors.append(f"{label}.references: at most {MAX_REFERENCES}, got {len(references)}")
    return errors


def _running_clashes(worker: dict, label: str, busy: dict) -> list[str]:
    """A brief beside the workers whose results are not in yet (waves.busy): it may not continue one
    (wait for its result first), nor take one's id, prefix or deliverable, nor own what one owns."""
    continues, wid = worker.get("continues"), worker.get("id")
    if continues in busy:
        return [f"{label}.continues: {continues!r} is still running; delegate wait true for its result, then send this"]
    problems = [f"{label}.id: {wid!r} is a running worker's; use another id"] if wid in busy else []
    for other, brief in busy.items():
        if isinstance(worker.get("prefix"), str) and worker["prefix"].lower() == str(brief["prefix"]).lower():
            problems.append(f"{label}.prefix: {worker['prefix']!r} is running worker {other}'s")
        if worker.get("deliverable") == brief["deliverable"]:
            problems.append(f"{label}.deliverable: running worker {other} builds {brief['deliverable']!r}")
        taken = [n for n in _names(worker.get("owns")) if n in brief["owns"]]
        if taken:
            problems.append(f"{label}.owns: {', '.join(taken)} {'is' if len(taken) == 1 else 'are'} running worker "
                            f"{other}'s")
    return problems


# A brief that changes a kept object (M4 audit: a reveal worker told to parent the kept product meshes
# spent two rollbacks, finished partial, and the orchestrator did the parenting itself). Words are
# matched on the orchestrator's own text (do, defaults), names case-sensitively as the scene spells
# them: a change verb with the kept name as its object ("parent Bottle and Box under Rig"), or the
# kept name as the subject of one ("Bottle rotates"); a preposition in between means another object
# is the one changed ("parent the Lamp to Box").
_CHANGE_VERBS = frozenset(
    "parent parents parented parenting reparent reparented unparent move moves moved moving rotate rotates rotated "
    "rotating spin spins spinning scale scales scaled scaling resize resized recolor recolour recolored recoloured "
    "repaint repainted retexture retextured animate animates animated animating keyframe keyframed rename renamed "
    "delete deleted remove removed replace replaced hide hidden reshape reshaped remodel remodelled remodeled "
    "rebuild rebuilt bevel bevelled beveled subdivide edit edited modify modified alter altered tilt tilted lift "
    "lifted raise raised lower lowered shrink enlarge join joined".split())
_PREPOSITIONS = frozenset(
    "to on onto in into under over above below behind beside besides next near around at by from with within "
    "toward towards against along across between inside outside through past beneath like than of".split())
_FILLER = frozenset("the a an its their both all each every and or , kept existing product products mesh meshes "
                    "object objects is are be gets get should must will so it them".split())
_NEGATIONS = frozenset("not never no don't dont doesn't without unchanged stays stay keep keeps leave".split())
_TOKEN = re.compile(r"[A-Za-z0-9_.'*-]+|,")


def _kept_by(token: str, kept: set) -> list:
    """The kept names a word names: itself, or those a pattern such as Camera_* matches."""
    if token in kept:
        return [token]
    return sorted(k for k in kept if fnmatch.fnmatchcase(k, token)) if "*" in token else []


def _changes_kept(text: str, kept: set) -> str:
    """The kept name (or pattern, with what it matches) a text asks to change, or ""."""
    for clause in re.split(r"[.;:!?\n()]+(?:\s|$)|[;:!?\n()]", text):
        tokens = [t.rstrip(".").removesuffix("'s") for t in _TOKEN.findall(clause)]
        for i, token in enumerate(tokens):
            names = _kept_by(token, kept)
            if not names:
                continue
            if names != [token]:
                token = f"{token} ({', '.join(names[:3])}{', …' if len(names) > 3 else ''})"
            for step in (-1, 1):  # Back to a verb it is the object of, then on to one it is the subject of.
                j, words = i + step, 0
                while 0 <= j < len(tokens) and words <= 2:
                    word = tokens[j].lower()
                    if word in _NEGATIONS or (step < 0 and word in _PREPOSITIONS):
                        break
                    beyond = tokens[j + step] if 0 <= j + step < len(tokens) else ""
                    if word == "," and not (_kept_by(beyond, kept) or beyond.lower() in ("and", "or")):
                        break  # A comma ends the phrase unless a list of kept names goes on past it.
                    if word in _CHANGE_VERBS:
                        before = tokens[j - 1].lower() if j > 0 else ""
                        if before not in _NEGATIONS:
                            return token
                        break
                    if step > 0 and word in _PREPOSITIONS:
                        break
                    if word not in _FILLER and not _kept_by(tokens[j], kept):
                        words += 1
                    j += step
    return ""


def keep_conflicts(wave: dict) -> list[str]:
    """Briefs that ask a worker to change what shared.keep keeps: its calls would be rolled back."""
    keep = wave["shared"]["keep"]
    kept = set(_names(keep.get("names")))
    if not kept:
        return []
    said = f" (user: \"{keep['quote'][:60]}\")" if keep.get("quote") else ""
    errors = []
    for index, worker in enumerate(wave["workers"]):
        if not isinstance(worker, dict):
            continue
        label = f"workers[{index}]" + (f" ({worker.get('id')})" if isinstance(worker.get("id"), str) else "")
        texts = [(f"requirements[{n}]", r.get("do")) for n, r in enumerate(worker.get("requirements") or [])
                 if isinstance(r, dict)]
        texts += [(f"defaults[{n}]", d) for n, d in enumerate(worker.get("defaults") or [])]
        found = [(where, str(text), _changes_kept(str(text), kept)) for where, text in texts if isinstance(text, str)]
        found += [(f"checks[{n}]", acceptance.title(c), c["name"]) for n, c in enumerate(worker.get("checks") or [])
                  if isinstance(c, dict) and c.get("kind") == "animated" and c.get("name") in kept]
        for where, text, name in found:
            if name:
                errors.append(f"{label}.{where}: {text[:80]!r} changes {name}, which shared.keep keeps{said}; nobody "
                              f"may change a kept object, so the worker's calls would be rolled back. If the user "
                              f"asked for this change, take {name} out of keep and list it in this worker's owns; "
                              f"else leave it out of the brief")
    return errors


# Requirements that clearly need a type of datablock: (type, what, the aspect that grants it, pattern).
_NEEDS = (
    ("LIGHT", "a light", "lighting", re.compile(
        r"\b(?:point|spot|area|sun|key|fill|rim|bulb|ceiling|accent)[ -]?lights?\b|\bspotlights?\b|\blight sources?\b"
        r"|\bemit(?:s|ting)? light\b|\b(?:add|create|make|place|put)\b[^.;]{0,30}?\blights?\b"
        r"(?![ -]?(?:grey|gray|wood|weight|blue|green|brown|colou?r|tone|oak|beige|pink|yellow))", re.I)),
    ("CAMERA", "a camera", "camera", re.compile(r"\b(?:add|create|make|place|put|set up|new)\b[^.;]{0,30}?\bcameras?\b", re.I)),
    ("WORLD", "a world", "lighting", re.compile(r"\bhdri\b|\bworld (?:background|colou?r|shader|lighting)\b", re.I)),
)


def role_warnings(wave: dict) -> list[str]:
    """Requirements that clearly need something the worker's role and aspects can't create (the
    merge would quarantine it): said by the dry run and when a wave starts, never refused."""
    from . import ownership
    warnings = []
    for worker in (w for w in wave["workers"] if isinstance(w, dict)):
        aspects = [a for a in worker.get("aspects") or () if a in ASPECTS]
        types = ownership.creatable(worker.get("role"), aspects)
        if types is None:
            continue
        texts = [f"{r.get('do', '')} {r.get('quote', '')}" for r in worker.get("requirements") or ()
                 if isinstance(r, dict)]
        for kind, what, aspect, pattern in _NEEDS:
            found = next((m for m in map(pattern.search, texts) if m), None)
            if found and kind not in types[0] | types[1]:
                warnings.append(f"{worker.get('id')}: {found.group(0)!r} needs {what}, which a {worker['role']} worker "
                                f"{'with aspects ' + ', '.join(aspects) + ' ' if aspects else ''}can't create (the "
                                f"merge would quarantine it); give it aspect {aspect}, or brief that part to the "
                                f"worker that has it")
    return warnings


def _overlap(a, b) -> list[float]:
    return [min(a[1][i], b[1][i]) - max(a[0][i], b[0][i]) for i in range(3)]


def _in_frame_share(projected: list, region) -> tuple[float, list[str]]:
    """Share of a box's corners inside the frame (or region) and in front of the camera, and which
    edges the others are past."""
    x0, y0, x1, y1 = region or (0.0, 0.0, 1.0, 1.0)
    inside, edges = 0, set()
    for x, y, depth in projected:
        if depth <= 0:
            edges.add("behind the camera")
            continue
        past = [edge for edge, out in (("left", x < x0), ("right", x > x1), ("bottom", y < y0), ("top", y > y1)) if out]
        edges.update(past)
        inside += not past
    return inside / max(1, len(projected)), sorted(edges)


def _corners(box) -> list:
    (x0, y0, z0), (x1, y1, z1) = box
    return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def _labels(worker_id: str, boxes: list) -> list[str]:
    """What a problem calls each of a worker's boxes: the worker, or "props box 2" when it has several."""
    return [worker_id] if len(boxes) == 1 else [f"{worker_id} box {n}" for n in range(1, len(boxes) + 1)]


def _set_bounds_z0(worker: dict, index: int, z0: float) -> None:
    """Writes a snapped z0 back into a worker's own bounds (a single box, or box `index` of a
    scattered list), in place, so the repair reaches the wave the workers actually build from."""
    raw = worker.get("bounds")
    target = raw if _box(raw) is not None else raw[index]
    target[0] = [target[0][0], target[0][1], z0]


def layout(wave: dict, live: dict, project=None) -> tuple[list[str], list[str], list[str], list[str]]:
    """The dry run: pure math on the planned bounds, each of a worker's boxes on its own; mutates
    wave in place for the one repair it can make (a bounds.z0 close to its support, snapped to it).
    Returns (problems, repairs, warnings, notes): problems refuse the wave; repairs already happened;
    warnings never refuse (a slight overlap between neighbours, or a box this wave is exempt from);
    notes are every number checked that held. project(camera, points) -> [(x, y, depth)] in frame
    fractions, for in_frame checks against a camera that exists now; None skips them."""
    problems, repairs, warnings, notes = [], [], [], []
    shared, workers = wave["shared"], [w for w in wave["workers"] if isinstance(w, dict)]
    objects, tops = live.get("objects", {}), _support_tops(shared, live)
    boxes = {w["id"]: _boxes(w["bounds"]) for w in workers if w.get("bounds") is not None and _boxes(w["bounds"])}
    for worker in workers:
        planned = boxes.get(worker["id"]) or []
        notes += [f"{label}: planned {_box_text(box)}" for label, box in zip(_labels(worker["id"], planned), planned)]
        support = worker.get("rests_on")
        if not (planned and isinstance(support, str) and support):
            continue
        top = tops.get(support, 0.0 if support.lower() == FLOOR else None)
        if top is None:
            continue
        held = []
        for n, (label, box) in enumerate(zip(_labels(worker["id"], planned), planned)):
            off = box[0][2] - top
            if abs(off) > Z0_SNAP:
                problems.append(f"{label}: bounds z0 {box[0][2]:.3f} but it rests on {support}, top_z {top:.3f} "
                                f"({off * 1000:+.0f} mm); too far to snap, set z0 to {top:.3f}")
            elif abs(off) > TOLERANCE:
                repairs.append(f"{label}: bounds z0 {box[0][2]:.3f} snapped to {top:.3f} (rests on {support}, "
                               f"was {off * 1000:+.0f} mm off)")
                _set_bounds_z0(worker, n, top)
                planned[n] = ((box[0][0], box[0][1], top), box[1])
                held.append(f"{top:.3f}")
            else:
                held.append(f"{box[0][2]:.3f}")
        if held:
            notes.append(f"{worker['id']}: base{'s' if len(held) > 1 else ''} at {', '.join(held)} on {support} "
                         f"(top_z {top:.3f})")
    # Physical deliverables only: a lighting or camera worker's box stands for a non-physical thing
    # (a room-sized throw, a shot's frustum) and may reasonably share space with modeling's.
    ids = [w["id"] for w in workers if w["id"] in boxes and w.get("role") == "modeling"]
    # Workers still running from an earlier delegate build at the same time: as neighbours in this wave.
    running = []
    for wid, brief in (live.get("busy") or {}).items():
        planned = _boxes(brief.get("bounds")) if brief.get("role") == "modeling" and brief.get("bounds") else None
        if planned:
            boxes[f"{wid} (running)"] = planned
            running.append(f"{wid} (running)")
    for i, a in enumerate(ids):
        for b in ids[i + 1:] + running:
            nearest, clashes = None, 0  # How far apart the closest boxes are, when none overlap.
            for label_a, box_a in zip(_labels(a, boxes[a]), boxes[a]):
                for label_b, box_b in zip(_labels(b, boxes[b]), boxes[b]):
                    depth = _overlap(box_a, box_b)
                    if all(d > TOLERANCE for d in depth):
                        if min(depth) < OVERLAP_WARN:
                            warnings.append(f"{label_a} and {label_b}: planned bounds overlap by {depth[0]:.2f} x "
                                            f"{depth[1]:.2f} x {depth[2]:.2f} m (under {OVERLAP_WARN * 100:.0f} cm "
                                            f"between neighbours; not refused)")
                        else:
                            clashes += 1
                            problems.append(f"{label_a} and {label_b}: planned bounds overlap by {depth[0]:.2f} x "
                                            f"{depth[1]:.2f} x {depth[2]:.2f} m; move one apart or shrink it")
                    else:
                        gap = max(-d for d in depth)
                        nearest = gap if nearest is None else min(nearest, gap)
            if nearest is not None and not clashes:
                notes.append(f"{a} and {b}: " + (f"{nearest:.3f} m apart at the closest" if nearest >= 0 else
                                                 f"overlap {-nearest * 1000:.0f} mm at most (up to {TOLERANCE * 1000:.0f} allowed)"))
    by_name = {w.get("deliverable"): w["id"] for w in workers}
    for worker in workers:
        for check in worker.get("checks") or []:
            if not (isinstance(check, dict) and check.get("kind") == "in_frame"):
                continue
            camera = check.get("camera")
            if project is None or objects.get(camera, {}).get("type") != "CAMERA":
                notes.append(f"{worker['id']} {check.get('id')}: camera {camera!r} is not in the scene yet; framing is "
                             f"checked after the wave")
                continue
            need = float(check.get("min_inside", 1.0))
            for name in _names(check.get("names")):
                live_box = (objects.get(name) or {}).get("box")
                planned = boxes.get(by_name.get(name)) or ([live_box] if live_box else [])
                if not planned:
                    notes.append(f"{worker['id']} {check['id']}: no planned bounds for {name}; framing checked after the wave")
                    continue
                at = (check["frame"],) if isinstance(check.get("frame"), int) else ()  # An animated camera's frame.
                for n, box in enumerate(planned, 1):
                    share, edges = _in_frame_share(project(camera, _corners(box), *at), check.get("region"))
                    where = "the region" if check.get("region") else "the frame"
                    which = f"{name}'s box" if len(planned) == 1 else f"{name}'s box {n}"
                    line = f"{worker['id']} {check['id']}: {which} is {share:.0%} inside {where} of {camera}"
                    if share + 1e-9 < need:
                        problems.append(f"{line} (needs {need:.0%}); corners past {', '.join(edges)}. Move the camera "
                                        f"(frame_camera report_only measures it) or the planned bounds")
                    else:
                        notes.append(line)
    return problems, repairs, warnings, notes


# ------------------------------------------------------------------ Blender (main thread)

# Every kind a brief's owns may name: texts too (a painter's recipe, eval 20260930-154037 O0014-O0016:
# "ArcaneRecipe" was refused as not in the scene, and it was unclear whether text was unsupported).
_ID_COLLECTIONS = ("objects", "meshes", "materials", "collections", "node_groups", "images", "lights", "cameras",
                   "actions", "worlds", "curves", "textures", "texts")


def _world_box(obj):
    """Evaluated world bounding box of an object and its children, or None for no geometry."""
    import bpy
    from mathutils import Vector
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for part in [obj, *obj.children_recursive]:
        if part.type in ("MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME"):
            evaluated = part.evaluated_get(depsgraph)
            points += [evaluated.matrix_world @ Vector(corner) for corner in evaluated.bound_box]
    if not points:
        return None
    return ([min(p[i] for p in points) for i in range(3)], [max(p[i] for p in points) for i in range(3)])


def live_facts(wave: dict) -> dict:
    """Main thread. What validate_wave and layout need from the scene: every object's type, the
    boxes of the objects the wave names, materials, and every datablock name."""
    import bpy
    wanted = set(_names(wave["shared"]["keep"].get("names"))) | set(_support_tops(wave["shared"], {}))
    for worker in wave["workers"]:
        if isinstance(worker, dict):
            wanted |= set(_names(worker.get("owns"))) | {worker.get("rests_on")}
            for check in worker.get("checks") or []:
                if isinstance(check, dict) and check.get("kind") == "in_frame":
                    wanted |= set(_names(check.get("names")))
    objects = {}
    for obj in bpy.data.objects:
        objects[obj.name] = {"type": obj.type, "box": _world_box(obj) if obj.name in wanted else None}
        data = obj.data
        if data is not None and data.library is None and data.users - int(data.use_fake_user) == 1:
            objects[obj.name]["data"] = data.name  # Owned with its object: its name may carry the prefix.
    names = [idb.name for attr in _ID_COLLECTIONS for idb in getattr(bpy.data, attr, ())]
    world = bpy.context.scene.world
    return {"objects": objects, "materials": [m.name for m in bpy.data.materials], "names": names,
            "world": world.name if world is not None and world.library is None else None}


def threads_of(session: dict) -> dict:
    """What repair_wave needs of the workers that can be sent a brief again (waves.remember)."""
    return {wid: {**{key: thread.get(key) for key in ("deliverable", "role", "prefix", "created", "wave")},
                  "references": [r["name"] for r in thread.get("references") or () if r.get("pinned")]}
            for wid, thread in (session.get("worker_threads") or {}).items()}


def project(camera: str, points: list, frame: int | None = None) -> list:
    """Main thread. World points to (x, y, depth) in the camera's frame: x and y 0-1 inside it, at
    frame (the scene goes back to its own frame after), else at the current one."""
    import bpy
    from bpy_extras.object_utils import world_to_camera_view
    from mathutils import Vector
    scene, cam = bpy.context.scene, bpy.data.objects[camera]
    back = (scene.frame_current, scene.frame_subframe)
    if frame is not None:
        scene.frame_set(frame)
    try:
        cam = cam.evaluated_get(bpy.context.evaluated_depsgraph_get())  # Where its constraints put it.
        return [tuple(world_to_camera_view(scene, cam, Vector(p))) for p in points]
    finally:
        if frame is not None:
            scene.frame_set(back[0], subframe=back[1])


def _repairs_line(repairs: list) -> str:
    return f"Repairs: {'; '.join(repairs)}" if repairs else ""


def budget_line(wave: dict, roles, job=None) -> str:
    """The shared dollars still available this turn and each new worker's own time limit."""
    roles = roles or config.Roles
    cap = job.budget.max_usd if job is not None else roles.job_max_usd
    left = max(0, cap - (job.budget.usd if job is not None else 0))
    seconds = job.budget.seconds if job is not None else roles.worker_max_seconds
    return ("Budget: " + (f"${left:.3f} of ${cap:.2f} left for this turn's workers; " if cap else "")
            + f"{seconds:g} s per worker run.")


def schedule_line(wave: dict, roles, busy: dict) -> str:
    """How these briefs would run: at most max_workers at once across the turn's waves, the rest
    queued in brief order behind the workers already queued or running (busy: waves.busy's). The
    queue is Loopcut's, so a job of any size is one call (GPT-6.1 Sol, 20260929-230207, sent seven
    props as six and one, twice, and did the scheduling itself)."""
    slots = roles.max_workers if roles else config.Roles.max_workers
    ahead = sum(1 for brief in busy.values() if brief.get("state") != "finished")
    ids = [str(w.get("id")) for w in wave.get("workers") or () if isinstance(w, dict)]
    now, later = ids[:max(0, slots - ahead)], ids[max(0, slots - ahead):]
    if not later:
        return f"Schedule: {'all ' + str(len(ids)) if len(ids) > 1 else ids[0] if ids else 'none'} start now."
    waiting = f" behind {ahead} already running or queued" if ahead else ""
    return (f"Schedule: {', '.join(now) + ' start now; ' if now else ''}{', '.join(later)} queue in that "
            f"order{waiting}, each starting as a worker finishes ({slots} run at once).")


def dry_run_report(problems: list, repairs: list, warnings: list, notes: list, layout_checked: bool = True,
                   budget: str = "") -> str:
    """What delegate dry_run says: whether a real call would start, the small mistakes already
    repaired (never refuse it), each problem (every one refuses it) and warning (none does) marked
    so, the step budget, then every number the layout check measured."""
    count = f"{len(problems)} problem{'s' if len(problems) != 1 else ''}"
    lines = [f"Dry run, nothing started: a real delegate would {'be refused (' + count + ')' if problems else 'start'}."]
    if repairs:
        lines.append(_repairs_line(repairs))
    lines += [f"- refused: {p}" for p in problems] + [f"- not refused (warning): {w}" for w in warnings]
    if budget:
        lines.append(budget)
    if not layout_checked:
        lines.append("The layout is checked once these are fixed.")
    elif notes:
        lines += ["Numbers checked:", *(f"  {n}" for n in notes)]
    return "\n".join(lines)


def _snapshot():
    from . import scene_diff
    return scene_diff.snapshot()


def _last_assistant(messages: list) -> int:
    return next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "assistant"), 0)


WAIT_SCHEMA = {"type": "function", "function": {
    "name": "delegate",
    "title": "Collecting helper results",
    "description": "Collect existing workers' next results. Starting new workers is disabled for this session.",
    "parameters": {"type": "object", "properties": {"wait": {"type": "boolean", "enum": [True]}},
                   "required": ["wait"], "additionalProperties": False}}}


def waits(name: str, arguments: dict) -> bool:
    """delegate wait with no briefs: the running workers' next results. It starts nothing, so it is not
    asked about (the delegate that started them was)."""
    wave = arguments.get("wave")
    return (name == "delegate" and str(arguments.get("wait")).lower() == "true"
            and not (isinstance(wave, dict) and wave.get("workers")))


def delegate(wave: dict | None = None, dry_run: bool = False, wait: bool = False):
    """Main thread. Validate, dry-run the layout, save the workers' copy, then hand the wave to the
    agent thread (waves.run_wave), which returns once one of the turn's workers has finished and
    merged. wait with no briefs: the next results of the workers still running (waves.collect)."""
    from . import tools, waves
    session = state.session()
    turn = session.get("turn")
    roles = getattr(turn, "roles", None)
    job = getattr(turn, "job", None)
    if waits("delegate", {"wave": wave, "wait": wait}) and not dry_run:
        if job is None or job.stopped or not waves.pending(job):
            return waves.wait_result(session, job)
        return tools.ToolResult("Waiting for the workers.", then=functools.partial(waves.collect, job))
    if roles is None or not roles.orchestrate:
        return tools.ToolResult("Workers are off in this conversation; do the work yourself.", ok=False)
    if session.get("workers_unavailable"):
        return tools.ToolResult(waves.unavailable_note(session), ok=False)
    if not isinstance(wave, dict):
        return tools.ToolResult("delegate refused: wave must be an object {shared, workers}; or wait true, with no "
                                "wave, for the running workers' next results.", ok=False, plan_refused=True)
    words = user_words(session)
    plan = normalise_wave(wave)
    snapshot_errors = [error for n, w in enumerate(plan["workers"]) if isinstance(w, dict) for key in ("checks", "advisory")
                       for error in with_baselines(w.get(key), session, f"workers[{n}].{key}")]
    live = live_facts(plan)
    live["threads"] = threads_of(session)
    live["busy"] = waves.busy(job)
    live["references"] = [r["name"] for r in session.get("references") or ()]  # What the user attached.
    plan, repairs = repair_wave(plan, words, live)  # Small mistakes fixed before validation ever sees them.
    errors = snapshot_errors + validate_wave(plan, words, live)
    if errors and dry_run:
        return tools.ToolResult(dry_run_report(errors, repairs, [], [], layout_checked=False),
                                ok=False, plan_refused=True)
    if errors:
        lines = ["delegate refused; nothing started. Fix these and call again:", *(f"- {e}" for e in errors)]
        if repairs:
            lines.append(_repairs_line(repairs))
        return tools.ToolResult("\n".join(lines), ok=False, plan_refused=True)
    problems, layout_repairs, layout_warnings, notes = layout(plan, live, project)
    repairs = repairs + layout_repairs
    warnings = role_warnings(plan) + tool_warnings(plan) + layout_warnings
    budget = f"{budget_line(plan, roles, getattr(turn, 'job', None))}\n{schedule_line(plan, roles, live['busy'])}"
    if dry_run:
        return tools.ToolResult(dry_run_report(problems, repairs, warnings, notes, budget=budget),
                                ok=not problems, plan_refused=bool(problems))
    if problems:
        lines = ["delegate refused; nothing started. The planned layout does not add up:",
                 *(f"- {p}" for p in problems), *(f"- warning: {w}" for w in warnings), *(f"  {n}" for n in notes)]
        if repairs:
            lines.append(_repairs_line(repairs))
        return tools.ToolResult("\n".join(lines), ok=False, plan_refused=True)
    warnings = [f"- warning: {w}" for w in warnings]
    before = _snapshot()
    try:
        ctx = waves.prepare(session, turn, plan, message_index=_last_assistant(session["messages"]))
    except Exception as ex:  # No copy, no wave: the orchestrator does it itself or says so.
        return tools.ToolResult(f"Could not start the workers: {ex}. Nothing started; do it yourself or tell the user.",
                                ok=False)
    ctx.scene_before = before  # run_wave's result carries it, so the turn's changes card includes the merge.
    lines = [f"Wave {ctx.number} started: {', '.join(w['deliverable'] for w in plan['workers'])}.", budget]
    if repairs:
        lines.append(_repairs_line(repairs))
    lines += warnings
    lead = "\n".join(lines)  # Said again at the top of what run_wave returns, which replaces this result.
    return tools.ToolResult(lead, then=functools.partial(waves.run_wave, ctx, lead=lead), scene_before=before)


def accept_quarantine(wave: str, names: list | None = None):
    """Main thread. What a wave's workers made outside their role, into the scene (merge.py)."""
    from . import tools
    try:
        from . import waves
        text = waves.accept_quarantine(state.session(), str(wave), names if isinstance(names, list) else None)
    except ImportError:
        return tools.ToolResult("Quarantine is not available in this build.", ok=False)
    except Exception as ex:
        return tools.ToolResult(f"Could not accept the quarantine of {wave}: {ex}", ok=False)
    return tools.ToolResult(str(text))


_QUOTED = re.compile(r'"([^"]+)"|“([^”]+)”')


def _reason_error(reason, words: str) -> str:
    """Why a retire reason will not do, or "": the user's words in double quotes (checked against
    their messages), or numbers measured on the scene."""
    if not isinstance(reason, str) or len(reason.strip()) < 8:
        return "say why: the user's words in double quotes, or what you measured"
    quotes = [a or b for a, b in _QUOTED.findall(reason)]
    for quote in quotes:
        if not quote_found(quote, words):
            return f"{quote[:60]!r} is not in the user's messages; quote them word for word"
    if quotes or re.search(r"\d", reason):
        return ""
    return ("quote the user (in double quotes) where they changed their mind, or give the numbers you measured "
            "that show the check is wrong")


def with_baselines(checks, session: dict, where: str) -> list[str]:
    """A preserved check's snapshot records, from the checks tool's snapshot, into the check itself
    (its baseline), so it measures the same in a worker's copy, after a merge and in a later turn.
    Returns what is wrong: an unknown snapshot, a name it did not record."""
    snapshots, errors = session.get("snapshots") or {}, []
    for n, check in enumerate(checks if isinstance(checks, list) else ()):
        if not isinstance(check, dict) or check.get("kind") != "preserved" or "baseline" in check:
            continue
        taken = snapshots.get(check.get("snapshot"))
        if taken is None:
            errors.append(f"{where}[{n}] (preserved) snapshot: {check.get('snapshot')!r} is not a snapshot; they are: "
                          f"{', '.join(snapshots) or 'none'} (checks snapshot takes one)")
            continue
        names = check["names"] if isinstance(check.get("names"), list) else []
        missing = [name for name in names if name not in taken["objects"]]
        if missing:
            errors.append(f"{where}[{n}] (preserved) names: {', '.join(map(str, missing))} not in {check['snapshot']}")
            continue
        check["baseline"] = {name: taken["objects"][name] for name in names}
    return errors


def _snapshot_now(names, session: dict) -> str:
    """checks snapshot: the named objects recorded (acceptance.record) under the next id, s1, s2, ..."""
    if not (isinstance(names, list) and 1 <= len(names) <= acceptance.MAX_SNAPSHOT
            and all(isinstance(n, str) and n for n in names) and len(set(names)) == len(names)):
        raise ValueError(f"snapshot: 1 to {acceptance.MAX_SNAPSHOT} different object names")
    snapshots = session.setdefault("snapshots", {})
    sid = f"s{len(snapshots) + 1}"
    snapshots[sid] = {"turn": session.get("turn_number"), "objects": acceptance.snapshot(names)}
    return (f"Snapshot {sid}: {', '.join(names)}. Compare with {{\"kind\": \"preserved\", \"names\": [...], "
            f"\"snapshot\": \"{sid}\", \"exclude\": [...]}} in a brief or add; exclude what may change, of "
            f"{', '.join(acceptance.ASPECTS)} (a look pass: materials).")


def checks(add: list | None = None, retire: list | None = None, snapshot: list | None = None):
    """Main thread. The finish gate's checks, the orchestrator's side: add checks its own work (a render
    setting, an export, books on a table, a camera: level 1 was never verified), run now on the live
    scene and again by the gate; retire takes a check out of the gate with a reason the Verified block
    shows (the user's words when they changed their mind, measured numbers when the check is wrong),
    an earlier turn's too; snapshot records named objects for preserved checks, first; none of them
    runs every check now (_rerun). The same quote rule as briefs: checks test what the user asked for."""
    from . import tools, waves
    session = state.session()
    turn = session.get("turn")
    if getattr(getattr(turn, "roles", None), "orchestrate", False) is not True:
        return tools.ToolResult("Checks go with workers, which are off in this conversation.", ok=False)
    add, retire = ([] if add is None else add), ([] if retire is None else retire)
    if not isinstance(add, list) or not isinstance(retire, list):
        return tools.ToolResult("checks: add is a list of checks, retire a list of {id, reason}; with neither, "
                                "every check runs now.", ok=False)
    lines = []
    if snapshot is not None:
        try:
            lines.append(_snapshot_now(snapshot, session))
        except Exception as ex:  # A bad list, or a name not in the file (acceptance._object says which).
            return tools.ToolResult(f"checks refused; nothing changed: {ex}", ok=False)
        if not (add or retire):
            return tools.ToolResult(lines[0])
    if not (add or retire):
        return tools.ToolResult(_rerun(waves.job_for(session, turn)))
    words = user_words(session)
    add = [dict(check) if isinstance(check, dict) else check for check in add]  # with_baselines fills in copies.
    errors = with_baselines(add, session, "add")
    errors += acceptance.validate(add, [words], where="add")
    if len(add) > MAX_CHECKS:
        errors.append(f"add: at most {MAX_CHECKS} at a time")
    job = waves.job_for(session, turn)
    standing = dict(job.final_checks())
    for n, item in enumerate(retire):
        label = item.get("id") if isinstance(item, dict) else None
        if label not in standing:
            errors.append(f"retire[{n}].id: {label!r} is not a check that runs; they are: {', '.join(standing) or 'none'}")
            continue
        problem = _reason_error(item.get("reason"), words)
        if problem:
            errors.append(f"retire[{n}].reason: {problem}")
    if errors:
        return tools.ToolResult("checks refused; nothing changed" + (" but the snapshot" if lines else "") + ":\n"
                                + "\n".join(lines + [f"- {e}" for e in errors]), ok=False)
    for item in retire:
        job.retired[item["id"]] = (standing[item["id"]], " ".join(item["reason"].split()))
        lines.append(f"Retired {item['id']} ({check_title(standing[item['id']])}): it no longer runs; the Verified "
                     f"block says why.")
    if add:
        taken = [int(label[1:]) for label in [*job.earlier, *job.own] if re.fullmatch(r"o\d+", label)]
        number = max(taken, default=0)
        new = []
        for check in add:
            number += 1
            new.append({**check, "id": f"o{number}"})
            job.own[new[-1]["id"]] = new[-1]
        results = acceptance.run(new, job.baseline)
        passed = sum(1 for r in results if r["status"] == "pass")
        lines.append(f"Added {', '.join(c['id'] for c in new)}: {passed} of {len(new)} pass on the live scene now; "
                     f"they run again before your reply ends.")
        lines += [f"- {acceptance.line(r)}"[:240] for r in results]
    job.write()
    return tools.ToolResult("\n".join(lines))


def _rerun(job) -> str:
    """checks with no arguments: every check the finish gate would run, on the live scene now."""
    labelled = job.final_checks()
    if not labelled:
        return "No checks yet: add some (add), or give workers checks."
    results = acceptance.run([check for _, check in labelled], job.baseline)
    rows = [(label, check, result) for (label, check), result in zip(labelled, results)]
    passed = [label for label, _, result in rows if result["status"] == "pass"]
    lines = [f"Ran every check on the live scene now: {len(passed)} of {len(rows)} pass."]
    lines += [f"- id \"{label}\": {result_line('', check, result).strip()}" for label, check, result in rows
              if result["status"] != "pass"]
    lines += [f"Pass: {', '.join(passed)}"] if passed else []
    retired = [(label, check, reason) for label, (check, reason) in job.retired.items()]
    return "\n".join(lines + coverage_lines(coverage(_requirements(job), rows, retired)))


DISPATCH = {"delegate": delegate, "checks": checks, "accept_quarantine": accept_quarantine, **glb.DISPATCH}


# ------------------------------------------------------------------ the orchestrator's own edits

_MESH_FIELDS = ("verts", "faces", "shape", "data", "materials")  # scene_diff: an object's mesh and its slots.


def worker_built(session: dict) -> dict:
    """{object name: worker id} for what this session's workers built (their threads, waves.remember)."""
    built = {}
    for worker, thread in (session.get("worker_threads") or {}).items():
        for name in thread.get("created") or ():
            built[name] = worker
    return built


def worker_edits(session: dict, before, after) -> dict:
    """{object name: worker id}: the objects a worker built whose mesh data or material assignments
    changed between two scene_diff snapshots. Whole-object moves (arrangement) are not edits."""
    built = worker_built(session)
    old, new = (before or {}).get("objects") or {}, (after or {}).get("objects") or {}
    return {name: built[name] for name, entry in new.items() if name in built and name in old
            and any(old[name].get(key) != entry.get(key) for key in _MESH_FIELDS)}


def edit_note(session: dict, name: str, result) -> str:
    """The orchestrator's own run_python changed a worker-built object's mesh or materials: one line on
    its result pointing back to that worker (owner decision 2: GLM workers do the work), and a record
    for the evals (session["worker_edits"]). Informational only: nothing is refused or undone."""
    if name != "run_python":
        return ""
    edits = worker_edits(session, getattr(result, "scene_before", None), getattr(result, "scene_after", None))
    if not edits:
        return ""
    session.setdefault("worker_edits", []).append({"turn": session.get("turn_number"), "objects": edits})
    by_worker = {}
    for obj, worker in edits.items():
        by_worker.setdefault(worker, []).append(obj)
    said = "; ".join(f"{', '.join(objects[:4])}{' and more' if len(objects) > 4 else ''} "
                     f"{'was' if len(objects) == 1 else 'were'} built by worker {worker}"
                     for worker, objects in by_worker.items())
    return f"\n\n{said}: send fixes back with delegate continues."


def only_reads(name: str, arguments: dict) -> bool:
    """A dry run starts nothing: no checkpoint, no approval, and it counts as a read."""
    return name == "delegate" and str(arguments.get("dry_run")).lower() == "true"


def _roles():
    turn = state.session().get("turn")
    return getattr(turn, "roles", None)


def describe(name: str, arguments: dict):
    """The step's label in the chat, like job_tools.describe; for delegate, what approving it allows."""
    if name == "accept_quarantine":
        return f"Accept what wave {arguments.get('wave', '?')} kept aside", ""
    if name in glb.DISPATCH:
        return glb.describe(name, arguments)
    if name == "checks":
        parts = [f"add {len(arguments['add'])}" if isinstance(arguments.get("add"), list) and arguments["add"] else "",
                 f"retire {len(arguments['retire'])}" if isinstance(arguments.get("retire"), list) and arguments["retire"] else "",
                 "snapshot" if arguments.get("snapshot") else ""]
        return f"Checks: {', '.join(p for p in parts if p) or 'run all now'}", json.dumps(arguments, indent=2)
    if name != "delegate":
        return None
    if waits(name, arguments):
        return "Wait for the helpers' next results", ""
    wave = arguments.get("wave") if isinstance(arguments.get("wave"), dict) else {}
    workers = wave.get("workers")
    if not isinstance(workers, list) or not workers or not all(isinstance(w, dict) for w in workers):
        return "Helper plan is empty or invalid", json.dumps(arguments, indent=2)
    what = ", ".join(str(w.get("deliverable") or w.get("id") or "?") for w in workers)
    code = json.dumps(wave, indent=2)
    if only_reads(name, arguments):
        return f"Check a helper plan: {what}", code
    roles = _roles()
    usd = roles.job_max_usd if roles else config.Roles.job_max_usd
    seconds = roles.worker_max_seconds if roles else config.Roles.worker_max_seconds
    count = len(workers)
    worker = getattr(roles, "worker", None)
    base_url = worker.base_url if worker else ""
    if config.on_service(base_url):
        usage = "Uses your chat allowance, not credits."
    elif config.chatgpt_plan(base_url):
        usage = "Uses your ChatGPT plan allowance."
    else:
        usage = (f"API spending limit: ${usd:.2f} shared by all helpers in this turn."
                 if usd else "Uses your API key; no API spending limit is set.")
    limits = (f"Internal token-cost ceiling: ${usd:.2f} shared by all helpers in this turn (not a price per approval)."
              if usd else "No token-cost ceiling is set.")
    code = f"{limits}\nTime limit: {seconds / 60:g} minutes per helper run.\n\n{code}"
    return (f"Start {count} helper{'s' if count != 1 else ''} for {what}. "
            f"Runs in the background (up to {seconds / 60:g} minutes per helper). {usage} "
            "Approving lets helpers run scripts without asking again until they finish or reach a limit. "
            "Helpers can read and write your files (no sandbox yet).", code)


# ------------------------------------------------------------------ the finish gate

check_title = acceptance.title  # A check in a few words: "contact Armchair rests_on Rug".


def result_line(label: str, check: dict, result: dict) -> str:
    detail = str(result.get("detail") or result.get("status") or "")
    return f"{label} {check_title(check)}: {detail}"[:240]


# Words that say where a thing is or what holds it, with the kinds that measure them, the first the
# one to suggest: a requirement with one whose passing checks measure none of it is only partly
# covered. "a hanging sign" passed as `exists HangingSign` while it stood on the ground (20260929-230207).
_PLACEMENT = tuple((re.compile(rf"\b({words})\b(?! \d)"), kinds) for words, kinds in (
    (r"hang|hangs|hanging|hung|suspended|dangling", ("suspended",)),
    (r"attached|mounted|hooked", ("attached_to", "suspended", "contact")),
    (r"on|onto|atop|upon|rests|resting|sits|sitting", ("contact", "above", "suspended", "attached_to")),
    (r"above|over", ("above", "suspended")),
    (r"below|under|beneath|underneath", ("below", "above", "contact")),
    (r"inside|within", ("contact", "light_inside")),
    (r"overlap|overlapping|touching|against|leaning", ("no_overlap", "contact", "attached_to"))))


def _requirements(job) -> list[str]:
    """The user's words this turn's briefs quote as requirements, in brief order."""
    return [str(r.get("quote") or "") for wave in getattr(job, "waves", None) or ()
            for brief in (wave.plan.get("workers") or ()) if isinstance(brief, dict)
            for r in brief.get("requirements") or () if isinstance(r, dict)]


def _same(quote: str, other) -> bool:
    """Two quotes of one requirement: either holds the other (the ellipsis rule of quote_found)."""
    return bool(normalise(quote)) and bool(normalise(other)) and (quote_found(other, quote) or quote_found(quote, str(other)))


def coverage(requirements: list[str], results: list, retired: list = ()) -> list[dict]:
    """Each distinct requirement of the user's (a brief's requirement, or a check's quote no brief
    quotes) with the checks that quote it or part of it: covered (they pass and measure its placement
    words), partly (a placement word nothing measures, or a check that could not be measured),
    failed, unverified (no check measured it) or retired. Information for the Verified block, never
    a gate."""
    quotes = []
    for quote in requirements:
        if normalise(quote) and all(normalise(quote) != normalise(q) for q in quotes):
            quotes.append(quote)
    for _, check, _ in [*results, *retired]:
        quote = str(check.get("quote") or "")
        if normalise(quote) and not any(_same(q, quote) for q in quotes):
            quotes.append(quote)
    out = []
    for quote in quotes:
        key = normalise(quote)

        def same(other) -> bool:
            return _same(quote, other)
        linked = [(label, check, result) for label, check, result in results if same(check.get("quote"))]
        labels = ", ".join(label for label, _, _ in linked[:4]) + (f" and {len(linked) - 4} more" if len(linked) > 4 else "")
        passing = {check.get("kind") for _, check, result in linked if result.get("status") == "pass"}
        unmeasured = [(m.group(1), kinds[0]) for pattern, kinds in _PLACEMENT for m in [pattern.search(key)]
                      if m and not passing & set(kinds)]
        if any(result.get("status") == "fail" for _, _, result in linked):
            status, evidence = "failed", f"{labels} fail"
        elif not passing:
            gone = any(same(check.get("quote")) for _, check, _ in retired)
            status = "retired" if gone and not linked else "unverified"
            evidence = f"{labels} could not be measured" if linked else "no check"
        elif unmeasured:
            status, evidence = "partly", (f"{labels} pass; nothing measures \"{unmeasured[0][0]}\" "
                                          f"(a {unmeasured[0][1]} check would)")
        elif len(passing) < len({check.get("kind") for _, check, _ in linked}) or any(
                result.get("status") != "pass" for _, _, result in linked):
            status, evidence = "partly", f"{labels}: some could not be measured"
        else:
            status, evidence = "covered", f"{labels} pass"
        out.append({"quote": quote, "status": status, "checks": [label for label, _, _ in linked], "evidence": evidence})
    return out


def coverage_lines(entries: list[dict]) -> list[str]:
    """How many of the user's requirements the checks cover, then each only partly covered or not at all."""
    counted = [e for e in entries if e["status"] != "retired"]
    if not counted:
        return []
    covered = sum(e["status"] == "covered" for e in counted)
    rest = [f"{n} {word}" for word, n in (("partly covered", sum(e["status"] == "partly" for e in counted)),
                                          ("unverified", sum(e["status"] == "unverified" for e in counted)),
                                          ("failed", sum(e["status"] == "failed" for e in counted))) if n]
    lines = [f"Requirements (the user's words): {covered} of {len(counted)} covered" + "".join(f", {r}" for r in rest) + "."]
    return lines + [f"- {'partly covered' if e['status'] == 'partly' else e['status']}: \"{e['quote'][:80]}\": "
                    f"{e['evidence'][:160]}" for e in counted if e["status"] in ("partly", "unverified")]


def verified_block(results: list[tuple[str, dict, dict]], retired: list[tuple[str, dict, str]] = (),
                   requirements: list[str] = ()) -> str:
    """results: (label, check, acceptance result); retired: (label, check, reason); requirements: the
    briefs' quotes. The block Loopcut appends to the final reply: the checks, then how much of what
    the user asked for they cover (coverage), so "32 of 32" never stands for all of it."""
    passed = sum(1 for _, _, r in results if r.get("status") == "pass")
    lines = [f"Verified by Loopcut on this scene: {passed} of {len(results)} checks."]
    lines += coverage_lines(coverage(list(requirements), results, retired))
    for _, check, result in results:
        if result.get("status") != "pass":
            state_word = "not measured" if result.get("status") == "unavailable" else "not met"
            lines.append(f"- {state_word}: \"{str(check.get('quote', ''))[:80]}\" ({check_title(check)}): "
                         f"{str(result.get('detail') or '')[:160]}")
    lines += [f"- retired: {reason[:200]} ({check_title(check)})" for _, check, reason in retired]
    return "\n".join(lines)


def _append_to_reply(session: dict, completion, block: str) -> None:
    """The block goes on the reply the user sees and on the message the model sees next turn."""
    items, messages = session["items"], session["messages"]
    reply = items[-1] if items and items[-1].get("kind") == "assistant" and completion and completion.text.strip() else None
    if reply is None:
        reply = state.item_assistant()
        reply["streaming"] = False
        items.append(reply)
    reply["text"] = f"{reply['text'].rstrip()}\n\n{block}" if reply["text"].strip() else block
    # Only the runtime sets this field. Text from the model (including an identical line)
    # stays ordinary prose; old conversations without provenance do too.
    reply["verification"] = block
    last = messages[-1] if messages else {}
    if last.get("role") == "assistant" and not last.get("tool_calls") and completion is not None:
        last["content"] = f"{(last.get('content') or '').rstrip()}\n\n{block}".lstrip()
    else:
        messages.append({"role": "assistant", "content": block})


def _late_workers(session: dict, turn, job):
    """The workers still running (or not yet reported) at a final reply: waited for and merged
    (waves.drain). Their report comes back once, as LATE with their contact sheet, so no work is merged
    unseen; after a guard stops the turn, which cannot bounce, a notice says so instead. None when there were none."""
    from . import waves
    late = waves.drain(job, turn.cancel.is_set)
    if late is None or turn.cancel.is_set():
        return None
    if late.scene_after is not None:
        turn.scene_before = turn.scene_before or late.scene_before
        turn.scene_after = late.scene_after
    if turn.outcome in ("step_limit", "no_progress"):
        reason = "at the step limit" if turn.outcome == "step_limit" else "when the no-progress guard stopped the turn"
        session["items"].append(state.item_notice(
            f"Workers still running {reason} were waited for; see each worker's outcome in its wave card. "
            "The agent did not review them."))
        return None
    session["items"].append(state.item_notice("Workers were still running at the reply: Loopcut collected their "
                                              "outcomes, and the agent reviews them."))
    job.log["late_bounces"] = job.log.get("late_bounces", 0) + 1  # Not a check bounce: counted apart.
    job.write()
    late.text = LATE.format(report=late.text)
    return late


def finish_gate(session: dict, turn, completion, run_checks):
    """LoopSpec.on_final for a turn that delegated: first any workers still running are waited for,
    merged and bounced back once for review (_late_workers); then every wave's checks (the last version
    of each) plus what the user said to keep, run on the live scene. Failures bounce back to the model
    up to MAX_BOUNCES times; then, or once all pass, the Verified block is appended. run_checks(checks)
    -> acceptance results in the same order. Returns the bounce (text, or the late workers' result with
    its contact sheet), or None."""
    job = getattr(turn, "job", None)
    if job is None or turn.cancel.is_set():
        return None
    late = _late_workers(session, turn, job)
    if late is not None:
        return late
    if turn.cancel.is_set():
        return None
    labelled = job.final_checks()
    retired = [(label, check, reason) for label, (check, reason) in job.retired.items()]
    if not labelled and not retired:
        return None
    try:
        results = run_checks([check for _, check in labelled]) if labelled else []
    except Exception as ex:  # Unverifiable is said, never hidden, and never bounced.
        _append_to_reply(session, completion, f"Not verified by Loopcut: the checks could not run ({ex}).")
        job.finish(bounces=job.bounces, results=[])
        return None
    rows = [(label, check, result) for (label, check), result in zip(labelled, results)]
    failed = [row for row in rows if row[2].get("status") != "pass"]
    if failed and job.bounces < MAX_BOUNCES and turn.outcome not in ("step_limit", "no_progress"):
        job.bounces += 1
        session["items"].append(state.item_notice(
            f"Checked the result on the scene: {len(failed)} of {len(rows)} checks fail, so the agent goes on."))
        return BOUNCE.format(lines="\n".join(f"- id \"{label}\": {result_line('', check, result).strip()}"
                                             for label, check, result in failed))
    requirements = _requirements(job)
    _append_to_reply(session, completion, verified_block(rows, retired, requirements))
    if isinstance(getattr(job, "log", None), dict):  # For the evals: what the block said of each requirement.
        job.log["coverage"] = [{k: e[k] for k in ("quote", "status", "checks")}
                               for e in coverage(requirements, rows, retired)]
    job.finish(bounces=job.bounces, results=rows)
    return None
