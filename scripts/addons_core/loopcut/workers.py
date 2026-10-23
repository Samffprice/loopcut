"""Workers: the same loop as the agent's (agent.run_loop), with a cheap model, one deliverable, and
tools that run in a background Blender holding a copy of the scene (worker_host.py).

The system prompt is served by Loopcut (contract 0.3). It and the tool list are the same for every role, so every worker in a wave shares
one cached prefix; what differs is the first message (the brief, the role notes, the copy's scene
context), the looks the child sets up for the role, and what the merge lets through.
"""

import importlib
import json
import re

from . import acceptance, agent, assets, camera, contact, ownership, paint_tools, texture_tools, tools, uv_tools


_CAPTURE = {"type": "function", "function": {
    "name": "capture_viewport",
    "title": "Checking the model’s appearance",
    "description": ("A render of your copy of the scene set up for your role (modeling clay, materials studio, "
                    "lighting with the scene's engine capped, camera through the camera with guides, animation a "
                    "sheet of frames)."),
    "parameters": {"type": "object", "properties": {
        "angle": {"type": "string", "enum": ["three_quarter", "front", "side", "back", "top", "camera", "sheet",
                                             "final", "crops"],
                  "description": "Default is your role's own view (lighting and camera: through the camera; "
                                 "modeling: close views of your own work; others: three_quarter). camera: through "
                                 "the scene camera. sheet: four views. final: the scene's own engine and settings, "
                                 "as the user will see it (modeling, materials: framed on your work). crops: each "
                                 "focus object cut from the camera's full-size render, with its size in px"},
        "camera": {"type": "string", "description": "Look through it; no angle: final"},
        "view_layer": {"type": "string", "description": "Rendered with its exclusions and world override"},
        "angles": {"type": "array", "items": {"type": "string"},
                   "description": "Several angles in one labelled sheet"},
        "shading": {"type": "array", "items": {"type": "string", "enum": ["clay", "flat", "studio", "albedo", "scene"]},
                    "description": "Default your role's. flat: material colours unlit; albedo: base colour or "
                                   "texture unlit, to judge paint; scene: the scene's lights. Two: each angle in both"},
        "isolate": {"type": "boolean", "description": "Hide all but focus"},
        "focus": {"type": "array", "items": {"type": "string"}, "description": "Objects to frame; default yours"},
        "frames": {"type": "array", "items": {"type": "integer"}, "description": "Animation: up to 6 frames"},
        "labels": {"type": "boolean", "description": "Draw object names, to tell objects apart; default off"}}}}}

SCHEMAS_OWN = [
    {"type": "function", "function": {
        "name": "check_brief",
        "title": "Checking the requested changes",
        "description": ("Run your brief's checks on your copy now: pass or fail for each, with the measured "
                        "numbers and, where known, the fix. Your status is decided by these checks."),
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "report_back",
        "title": "Reporting the results",
        "description": "End your work with a report to the orchestrator. Call it once, last, after check_brief.",
        "parameters": {"type": "object", "required": ["summary"], "properties": {
            "summary": {"type": "string", "description": "1-3 sentences: what you built and refined, what is left"},
            "numbers": {"type": "object", "description": "Key measured numbers, e.g. {\"Lamp\": {\"height\": 0.52}}"},
            "change_requests": {"type": "array", "items": {"type": "object", "properties": {
                "target": {"type": "string"}, "change": {"type": "string"}, "reason": {"type": "string"}}}},
            "question": {"type": "string", "description": "Only when you are blocked"}}}}},
]
# Tools from the agent's own set, in the order they are offered; capture_viewport has its own text.
_SHARED_TOOLS = ("run_python", "get_scene_info", "get_object_info", "inspect_api", "look_at_reference")
MAX_SUMMARY = 300
MAX_REQUESTS = 3
STEER_INSTRUCTION = ("Reconsider requirements that conflict with this correction. Change only what clearly applies "
                     "to your deliverable. Report unclear or unrelated requests to the orchestrator. "
                     "Your ownership and merge restrictions still apply.")


def schemas() -> list[dict]:
    """The worker's tools: the same for every role and every worker, for the cache."""
    own = {s["function"]["name"]: s for s in tools.SCHEMAS}
    return ([own[name] for name in _SHARED_TOOLS] + [_CAPTURE] + contact.SCHEMAS + camera.SCHEMAS + assets.SCHEMAS
            + [s for s in uv_tools.SCHEMAS if s["function"]["name"] not in uv_tools.SINGLE_ONLY]
            + texture_tools.SCHEMAS + paint_tools.SCHEMAS + SCHEMAS_OWN)


def tool_names() -> set[str]:
    return {s["function"]["name"] for s in schemas()}


def _changed(result) -> bool:
    """A worker's scene is in its child: the child says whether a call changed it."""
    return bool(getattr(result, "changed", False))


def SPEC(role: str, save, before_request=None, on_usage=None, after_step=None) -> agent.LoopSpec:  # noqa: N802
    """The worker's LoopSpec: never asks (the delegate approval covered the wave), never checkpoints
    the live scene, no changes card. The hooks come from waves.py."""
    return agent.LoopSpec(role="worker", system=lambda session: "", prompt_id="worker", schemas=schemas, save=save, asks=False,
                          checkpoints=False, changes_card=False, scene_changed=_changed,
                          before_request=before_request, on_usage=on_usage, after_step=after_step,
                          steps_left_note=agent.WORKER_STEPS_LEFT_NOTE)


# ------------------------------------------------------------------ the first message

_RELATION_WORDS = {"rests_on": "rests on", "touches": "touches", "clear_of": "is clear of", "inside": "is inside"}


def _size(values) -> str:
    return " x ".join("any" if v is None else f"{v:g}" for v in values)


def check_sentence(check: dict) -> str:
    """A check as the worker reads it."""
    kind, c = check.get("kind"), check
    names = ", ".join(map(str, c.get("names") or []))
    if kind == "exists":
        text = f"{c['name']} exists" + (f" as a {c['type']}" if c.get("type") else "")
    elif kind == "count":
        what = (f"objects {acceptance.prefix_rule(c['prefix'])}" if c.get("prefix")
                else f"objects in collection {c.get('collection')}")
        bounds = [f"at least {c['min']}" if c.get("min") is not None else "", f"at most {c['max']}" if c.get("max") is not None else ""]
        text = f"{' and '.join(b for b in bounds if b) or 'some'} {what}" + (f" ({c['type']})" if c.get("type") else "")
    elif kind == "dims":
        text = f"{c['name']} measures {_size(c['size'])} m" + (f" (within {c['tol']})" if c.get("tol") else "")
    elif kind == "contact":
        text = f"{c['a']} {_RELATION_WORDS.get(c['relation'], c['relation'])} {c['b']}" + (
            f" (gap {c['gap']} m)" if c.get("gap") is not None else "")
    elif kind in ("suspended", "attached_to", "above", "below"):
        a, b = acceptance._label(c["a"], c.get("a_part")), acceptance._label(c["b"], c.get("b_part"))
        text = {"suspended": f"{a} hangs from {b}: attached in its top half, its lowest point at least "
                             f"{float(c.get('clear', acceptance.CLEAR)):g} m above " + (
                                 f"{c['ground']}'s top" if c.get("ground") else "the ground (z 0)"),
                "attached_to": f"{a} is attached to {b} (within {float(c.get('tol', acceptance.ATTACH_TOL)):g} m)",
                "above": f"{a} is above {b}, over it", "below": f"{a} is below {b}, under it"}[kind]
        if kind in ("above", "below") and c.get("gap"):
            text += f", at least {c['gap']:g} m apart"
        if c.get("a_part") or c.get("b_part"):
            text += " ([...]: the faces of that object with that material, vertex group or attribute)"
    elif kind == "no_overlap":
        text = f"{names} do not overlap"
    elif kind == "in_frame":
        text = f"{names} {float(c.get('min_inside', 1)):.0%} inside camera {c['camera']}'s frame" + (
            f", within region {c['region']}" if c.get("region") else "") + (
            f", at frame {c['frame']}" if c.get("frame") is not None else "")
    elif kind == "faces_max":
        text = f"{c['name']} has at most {c['max']} triangles"
    elif kind == "origin_base":
        text = f"{c['name']}'s origin is at " + (f"x {c['origin_xy'][0]:g}, y {c['origin_xy'][1]:g}, at its lowest point"
                                                   if c.get("origin_xy") else "the centre of its base (its lowest 1 cm)")
    elif kind == "material_on":
        text = f"{c['name']} uses material {c['material']}"
    elif kind == "light_inside":
        text = f"light {c['light']} is inside {c['container']}"
    elif kind == "light":
        span = lambda pair, unit: f"{'' if pair[0] is None else f'{pair[0]:g}'}..{'' if pair[1] is None else f'{pair[1]:g}'}{unit}"  # noqa: E731
        wanted = [f"a {c['type'].lower()}" if c.get("type") else "",
                  f"energy {span(c['energy'], ' W')}" if c.get("energy") else "",
                  f"color {_size(c['color'])} (within {c.get('tol', 0.05):g})" if c.get("color") else "",
                  f"spot_size {span(c['spot_size'], ' deg')}" if c.get("spot_size") else ""]
        text = f"light {c['name']}: " + (", ".join(w for w in wanted if w) or "is a light")
    elif kind == "setting":
        wanted = "; ".join(f"{k} {c[k]}" for k in ("equals", "min", "max") if k in c)
        text = f"setting {c['path']}: {wanted}"
    elif kind == "animated":
        text = f"{c['name']} {c['path']} changes between frames {c['frames'][0]} and {c['frames'][1]}"
    elif kind == "unchanged":
        text = f"{names} stay unchanged"
    elif kind == "preserved":
        kept = [a for a in acceptance.ASPECTS if a not in (c.get("exclude") or ())]
        text = f"{names} keep their {', '.join(kept)} as recorded in snapshot {c['snapshot']}"
    elif kind == "file":
        text = f"file {c['path']} exists and is not empty"
    elif kind == "no_modifiers":
        text = f"{c['name']} has no unapplied modifiers"
    elif kind == "flat_shaded":
        text = f"{c['name']} is flat shaded (at most {float(c.get('max_smooth', 0.05)):.0%} of faces smooth)"
    elif kind == "inside_bounds":
        text = f"{c['name']} stays inside its planned bounds (within {float(c.get('tol', 0.02)) * 100:g} cm)"
    elif kind == "triangles":
        band = [f"at least {c['min']:,}" if c.get("min") is not None else "", f"at most {c['max']:,}" if c.get("max") is not None else ""]
        text = f"{c['name']} has {' and '.join(b for b in band if b)} triangles"
    else:
        text = json.dumps(c)
    if c.get("id", "").startswith("a") and not c.get("quote"):
        return f"{c['id']}: {text}"
    return f"{c.get('id', '?')}: {text} (user: \"{c.get('quote', '')}\")"


def _bounds(value) -> str:
    """Planned bounds as the worker reads them: one box, or each of several (one per prop)."""
    from .orchestrator import _box_text, _boxes
    boxes = _boxes(value) or []
    if len(boxes) == 1:
        return _box_text(boxes[0])
    return "; ".join(f"box {n}: {_box_text(box)}" for n, box in enumerate(boxes, 1))


MAX_USER_WORDS = 3000  # Characters of the user's own messages a worker reads, newest kept first.


def user_request(texts, included: list[int] | None = None) -> str:
    """The user's messages this conversation, oldest first, each on one line: whole while they fit in
    MAX_USER_WORDS, the newest first (a newest one too long alone is clipped between items), then how
    many earlier ones were left out. included records the original indices that enter the request,
    including a clipped newest message, so receipts follow items rather than matching their text."""
    texts = [(n, " ".join(str(t).split())) for n, t in enumerate(texts or ()) if str(t).strip()]
    kept, left = [], MAX_USER_WORDS
    for n, text in reversed(texts):
        if len(text) + 3 > left:
            if not kept:
                kept.append((n, clip(text, left - 3)))
            break
        kept.append((n, text))
        left -= len(text) + 3
    if included is not None:
        included.extend(n for n, _ in reversed(kept))
    lines = [f'- "{text}"' for _, text in reversed(kept)]
    if len(kept) < len(texts):
        lines.insert(0, f"({len(texts) - len(kept)} earlier message{'s' if len(texts) - len(kept) != 1 else ''} "
                        f"left out)")
    return "\n".join(lines)


SHOT_NOTE = ("This brief is a shot: you own its cameras and the light they need. Frame first (frame_camera), then "
             "light what the camera sees, and judge the light through the camera (capture_viewport angle camera), "
             "not from the side.")


def _is_shot(role: str, brief: dict) -> bool:
    return role == "lighting" and isinstance(brief, dict) and "camera" in (brief.get("aspects") or ())


def role_notes(role: str, brief: dict, notes: str = "") -> str:
    """Selected served placeholders/local text; shot plumbing stays open beside the craft notes."""
    text = _craft(role, brief, notes)
    if _is_shot(role, brief):
        return f"{text}\n{SHOT_NOTE}" if text else SHOT_NOTE
    return text


def _craft(role: str, brief: dict, notes: str = "") -> str:
    from . import cloud
    skills = importlib.import_module(".skills", __package__)
    return skills.skills_for(role, brief, notes, **cloud.worker_skill_options())


CONTINUE = ("[Loopcut] More work on your deliverable. What you built earlier was merged into the user's scene, "
            "and this copy starts from that scene as it is now: the orchestrator or other workers may have "
            "changed it since your report. The brief below replaces your earlier one (its requirements and checks; "
            "you own what it lists), and its style says what to change. Make the change, then your "
            "craft pass, then check_brief and report_back.")


CONTINUE_UNMERGED = ("[Loopcut] More work on your deliverable. Nothing you built earlier reached the user's scene "
                     "(your earlier work {status}), and this copy starts from that scene: none of the objects your "
                     "earlier steps made exist here, whatever your conversation shows. The brief below replaces your "
                     "earlier one (its requirements and checks; you own what it lists). Build it again with "
                     "tool calls, then your craft pass, then check_brief and report_back.")


def continuation(brief: dict, shared: dict, others: list[dict], scene: str, requests=(), role: str = "",
                 merged: bool = True, status: str = "", request_indices: list[int] | None = None) -> str:
    """The message that sends a worker that built this before a new brief: CONTINUE, or when its
    earlier work never merged (it failed, crashed or was stopped) CONTINUE_UNMERGED, so it does not
    take the objects of its own earlier steps for the scene's; then the same fields as opening; the
    role notes again only when the role changed (role: its earlier one)."""
    lead = CONTINUE if merged else CONTINUE_UNMERGED.format(
        status=f"ended {status} and was not merged" if status else "was not merged")
    body = opening(brief, shared, others, scene, requests, notes=role != brief["role"], request_indices=request_indices)
    return f"{lead}\n{body}"


def opening(brief: dict, shared: dict, others: list[dict], scene: str, requests=(), notes: bool = True,
            request_indices: list[int] | None = None) -> str:
    """The worker's first message: what everyone shares (the user's own messages first), its brief,
    the craft notes for its role, and the scene context of its copy (made in the parent from the live
    scene at spawn). requests: the user's messages this conversation, oldest first."""
    keep = shared.get("keep") or {}
    lines = ["<shared>"]
    said = user_request(requests, request_indices)
    if said:
        lines += ["the user's words (their messages, oldest first; the brief quotes the parts that are yours):", said]
    lines.append(f"units: {shared.get('units')}")
    if keep.get("names"):
        lines.append(f"keep (the user's; never change): {', '.join(keep['names'])} (user: \"{keep.get('quote', '')}\")")
    if shared.get("palette"):
        lines.append(f"palette (use these materials by name; do not recreate them): {', '.join(shared['palette'])}")
    for support in shared.get("supports") or []:
        lines.append(f"support: {support['name']}, top_z {float(support['top_z']):g}")
    if shared.get("notes"):
        lines.append(f"notes: {shared['notes']}")
    for other in others:
        planned = f", planned {_bounds(other['bounds'])}" if other.get("bounds") else ""
        lines.append(f"also in this wave (not yours): {other['deliverable']} by {other['id']} ({other['role']}){planned}")
    lines += ["</shared>", "<brief>",
              f"deliverable: {brief['deliverable']} (the root object's name)",
              f"role: {brief['role']}",
              f"prefix: {brief['prefix']}: for materials and other internal/helper datablocks "
              f"({brief['prefix']}_Name); name parts for what they are",
              f"owns (you may change these): {', '.join(brief['owns']) or 'nothing existing; create only'}"]
    if brief.get("aspects"):
        made = {"lighting": "lights and the world", "camera": "cameras"}
        lines.append(f"aspects: {', '.join(brief['aspects'])} (you may create "
                     f"{' and '.join(made.get(a, a) for a in brief['aspects'])})")
    lines.append("requirements:")
    lines += [f"- {r['do']} (user: \"{r.get('quote', '')}\")" for r in brief["requirements"]]
    if brief.get("defaults"):
        lines.append("defaults (the orchestrator's choice, never checked): " + "; ".join(brief["defaults"]))
    if brief.get("style"):
        lines.append(f"style (art direction: the quality bar for your craft pass, not extra scope): {brief['style']}")
    if brief.get("references"):
        lines.append(f"references (the user's images, attached: the look your style asks for; look_at_reference for "
                     f"detail): {', '.join(brief['references'])}")
    if brief.get("bounds"):
        lines.append(f"planned bounds: {_bounds(brief['bounds'])}")
    if brief.get("rests_on"):
        lines.append(f"rests on: {brief['rests_on']}")
    if brief.get("checks"):
        lines.append("checks (check_brief runs them; your status comes from them):")
        lines += [f"- {check_sentence(c)}" for c in brief["checks"]]
    if brief.get("advisory"):
        lines.append("advisory (the brief's own targets: check_brief measures them and the orchestrator reads each "
                     "miss; they never fail your work):")
        lines += [f"- {check_sentence(c)}" for c in brief["advisory"]]
    lines.append("</brief>")
    if notes:
        # The user's words pick craft topics too: an orchestrator's brief can paraphrase the style away
        # ("designed paint", eval 20260930-151649) and its worker would miss the painterly recipe.
        craft = role_notes(brief['role'], brief, "\n".join(filter(None, (shared.get('notes'), said))))
        if craft:
            lines.append(f"<role_notes>\n{craft}\n</role_notes>")
    lines.append(scene)
    return "\n".join(lines)


# ------------------------------------------------------------------ report_back

_OPEN, _CLOSE = "([{", ")]}"


def clip(text: str, limit: int) -> str:
    """text in at most limit characters, cut between items and never inside one (a number, a name, a
    bracketed tuple): at the last "; " or ", " outside brackets in the second half, else the last
    space outside brackets, else the last space; then "…"."""
    text = str(text)
    if len(text) <= limit:
        return text
    room = max(1, limit - 1)  # For the "…".
    items = words = spaces = 0
    depth = 0
    for i, ch in enumerate(text[:room + 1]):
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        elif ch == " " and i <= room:
            spaces = i
            if depth == 0:
                words = i
                if text[i - 1] in ";," and i - 1 >= room // 2:
                    items = i - 1
    cut = items or words or spaces or room
    return text[:cut].rstrip(" ,;:") + "…"


def clip_list(entries: list, limit: int, separator: str = ", ") -> str:
    """Whole entries joined while they fit in limit characters, then "+N more" for the rest."""
    entries = [str(e) for e in entries]
    for count in range(len(entries), -1, -1):
        rest = len(entries) - count
        text = separator.join(entries[:count] + ([f"+{rest} more"] if rest else []))
        if len(text) <= limit:
            return text
    return clip(entries[0], limit) if entries else ""


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def clip_items(value, limit: int) -> str:
    """value as compact JSON in at most limit characters, cut between items, never inside a value: a
    dict or list that does not fit keeps its first items whole (the next one clipped the same way
    when it is a dict itself: a list such as a size is one value) and ends with "…+N", the items
    left out."""
    text = _compact(value)
    if len(text) <= limit:
        return text
    if isinstance(value, dict):
        pairs, bounds = [(f"{_compact(str(k))}:", v) for k, v in value.items()], "{}"
    elif isinstance(value, (list, tuple)):
        pairs, bounds = [("", v) for v in value], "[]"
    else:
        return "…"
    reserve = len(f",…+{len(pairs)}")
    kept = []
    for n, (head, item) in enumerate(pairs):
        whole = head + _compact(item)
        if len(bounds) + len(",".join(kept + [whole])) + reserve <= limit:
            kept.append(whole)
            continue
        room = limit - len(bounds) - len(",".join(kept + [head])) - (1 if kept else 0) - reserve
        if isinstance(item, dict) and item and room >= 8:
            inner = clip_items(item, room)
            if len(inner) <= room and not inner[1:].startswith("…"):  # At least one whole item of it.
                kept.append(head + inner)
                n += 1
        rest = len(pairs) - n
        return bounds[0] + ",".join(kept + ([f"…+{rest}"] if rest else [])) + bounds[1]
    return bounds[0] + ",".join(kept) + bounds[1]


def _sentences(text: str, count: int = 3, limit: int = MAX_SUMMARY) -> str:
    """The first sentences, whole, while they fit (at most count), then " …" when something was left
    out; a first sentence too long alone is clipped between items."""
    text = " ".join(str(text).split())
    if len(text) <= limit and len(re.findall(r"[.!?](?:\s|$)", text)) <= count:
        return text
    parts = [p for p in re.split(r"(?<=[.!?])\s+", text) if p][:count]
    kept = []
    for part in parts:
        if len(" ".join(kept + [part])) > limit - 2:
            break
        kept.append(part)
    if not kept:
        return clip(parts[0], limit) if parts else ""
    return " ".join(kept) + " …"


def parse_report(arguments: dict) -> dict:
    """report_back's arguments, clipped: the only words of a worker the orchestrator reads."""
    requests = []
    for request in arguments.get("change_requests") or []:
        if isinstance(request, dict) and request.get("target") and request.get("change"):
            requests.append({key: " ".join(str(request.get(key, "")).split())[:120] for key in ("target", "change", "reason")})
    numbers = arguments.get("numbers") if isinstance(arguments.get("numbers"), dict) else {}
    question = " ".join(str(arguments.get("question") or "").split())[:200]
    return {"summary": _sentences(arguments.get("summary") or ""), "numbers": numbers,
            "change_requests": requests[:MAX_REQUESTS], "question": question or None}
