"""The router: one small call before the orchestrator's first request of a user turn, naming the
turn's level and its deliverables (04_port_plan §3.1, §4.1: the fallback for an orchestrator that
misjudges levels).

Why it exists: on 2026-09-27 GLM as orchestrator delegated nothing on six multi-deliverable tasks
(a furnished corner, a product shot with lighting and three cameras, a seven-prop pack, ...). Every
first reply announced one big script that would build everything, so every turn stayed level 1;
Claude, with the same prompt, split the same tasks into workers.

Rules come first. An attached image means level 2 or more. A reply to the question a turn asked
before doing anything goes back to that turn's work, with no call. Otherwise one low-effort call
with a static prompt (below) gets the user's message, a short scene summary and a line about the
previous turn, and answers in JSON. The decision reaches the orchestrator as a <routing> note at the
end of the user's message, so its system prompt and tools, and their cached prefix, stay fixed. A
call that fails, or a reply that is not the JSON asked for, means no note: the turn goes on as it
would without the router. The router runs in the parent Blender with its own key; children never
see it (worker_ipc.child_env).

Pure apart from route(), which talks to the model; no bpy.
"""

import json
import re
from dataclasses import dataclass, field

from . import context, llm, orchestrator

ROUTED = "loopcut_routed"   # On the user message a turn was routed from: its level, or -1 for no decision.
TIMEOUT = 30.0              # Seconds for the call; a slow router is no router.
MAX_DELIVERABLES = 8
MAX_NAME, MAX_QUOTE, MAX_WHY = 32, 80, 160
ROLES = (*orchestrator.ROLES, "shot")  # delegate's roles and shot (a camera with its lighting), for the skeleton.
MAX_SCENE, MAX_REQUEST, MAX_PREVIOUS = 1200, 2400, 400
SHARED_SETUP = "palette, supports, render and output settings"
STYLE = "look, parts, detail, finish"  # A brief's art direction; prompt served by Loopcut (contract 0.3).
# The scene_context lines that say nothing about the work: the build, engines, editors.
_NOISE = ("Blender ", "media type:")
_VERIFIED = re.compile(r"\n\s*\n(?:Not )?[Vv]erified by Loopcut.*\Z", re.S)


@dataclass
class Decision:
    level: int
    deliverables: list = field(default_factory=list)  # [(name, the user's words)]
    why: str = ""
    source: str = "model"   # model | rules (the image rule alone) | parked (the answer to a question)
    turn: int = 0

    def record(self) -> dict:
        return {"turn": self.turn, "level": self.level, "source": self.source, "why": self.why,
                "deliverables": [list(pair) for pair in self.deliverables]}

    @classmethod
    def from_record(cls, record: dict) -> "Decision":
        pairs = [tuple(pair) for pair in record.get("deliverables") or []]
        deliverables = [p if len(p) == 3 else (*p, "modeling") for p in pairs]  # Older records had no role.
        return cls(level=record["level"], deliverables=deliverables, why=record.get("why", ""),
                   source=record.get("source", "model"), turn=record.get("turn", 0))


# ------------------------------------------------------------------ the reply

def _one_line(value, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip(" \"'")
    return text[:limit]


def _deliverables(value) -> list:
    """(name, quote, role) triples from what the model said: dicts or bare names; names cut to
    letters, digits and underscores, duplicates dropped; role one of delegate's, else "modeling"."""
    found, seen = [], set()
    for entry in value if isinstance(value, list) else []:
        if isinstance(entry, dict):
            name, quote, role = entry.get("name"), entry.get("quote"), entry.get("role")
        else:
            name, quote, role = entry, "", None
        name = re.sub(r"[^A-Za-z0-9_]", "", str(name or ""))[:MAX_NAME]
        role = role if role in ROLES else "modeling"
        if name and name.lower() not in seen:
            seen.add(name.lower())
            found.append((name, _one_line(quote, MAX_QUOTE), role))
        if len(found) == MAX_DELIVERABLES:
            break
    return found


def parse(text: str) -> Decision | None:
    """The first JSON object in the reply that names a level 0-3, whatever surrounds it (code fences,
    a sentence). None for anything else: no note is better than a wrong one."""
    decoder, text = json.JSONDecoder(), text or ""
    for match in re.finditer(r"\{", text):
        try:
            reply, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if not isinstance(reply, dict) or "level" not in reply:
            continue
        level = reply["level"]
        if isinstance(level, str) and level.strip().isdigit():
            level = int(level)
        if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 3:
            return None
        return Decision(level=level, deliverables=_deliverables(reply.get("deliverables")) if level >= 2 else [],
                        why=_one_line(reply.get("why"), MAX_WHY))
    return None


# ------------------------------------------------------------------ what the router is told

def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return next((p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"), "")
    return content if isinstance(content, str) else ""


def scene_summary(message: dict) -> str:
    """The message's <scene_context>, less the lines about the build, engines and editors, capped."""
    found = re.search(r"<scene_context>(.*?)</scene_context>", _text(message), re.S)
    if not found:
        return "unknown"
    lines = [line for line in found.group(1).strip().splitlines() if line.strip() and not line.startswith(_NOISE)]
    summary = "\n".join(lines)
    return summary if len(summary) <= MAX_SCENE else summary[:MAX_SCENE].rstrip() + " ..."


def user_request(message: dict, limit: int = MAX_REQUEST) -> str:
    """What the user wrote (and the card of an image they attached), without the scene context or
    an earlier routing note."""
    text = _text(message)
    text = text.split("</scene_context>", 1)[-1]
    text = text.split("<routing>", 1)[0].strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ..."


def request_text(request: str, scene: str, previous: str = "none", image: bool = False) -> str:
    attached = "The user attached an image with this request.\n" if image else ""
    return f"Scene:\n{scene}\n\nPrevious turn: {previous}\n{attached}\nRequest: {request}"


def _turn_starts(messages: list) -> list[int]:
    return [i for i, m in enumerate(messages) if context.is_turn_start(m)]


def _last_reply(messages: list) -> str:
    """The last thing the agent said in these messages, less Loopcut's Verified block."""
    for message in reversed(messages):
        if message.get("role") == "assistant" and not message.get("tool_calls") and _text(message).strip():
            return _VERIFIED.sub("", _text(message)).strip()
    return ""


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def asked(reply: str) -> bool:
    """Whether a reply ends by asking the user something."""
    return _last_line(reply).rstrip("*_`)\"' ").endswith("?")


def _previous(session: dict) -> tuple[str, str, dict | None]:
    """(the previous turn's request, its last reply, its turn_levels entry), from this session."""
    messages = session["messages"]
    starts = _turn_starts(messages)
    if len(starts) < 2:
        return "", "", None
    number = (session.get("turn_number") or 1) - 1
    entry = next((e for e in session.get("turn_levels") or [] if e.get("turn") == number), None)
    return (user_request(messages[starts[-2]], 120), _last_reply(messages[starts[-2]:starts[-1]]), entry)


def previous_note(session: dict) -> str:
    """One line about the previous turn: what the user asked, the level it ran at, how the reply ended."""
    request, reply, entry = _previous(session)
    if not request and not reply:
        return "none"
    parts = [f"the user asked \"{_one_line(request, 120)}\""] if request else []
    if entry:
        parts.append(f"Loopcut worked at level {entry.get('final', 0)}")
    if reply:
        parts.append(f"its reply ended \"{_one_line(_last_line(reply), 160)}\"")
    return "; ".join(parts)[:MAX_PREVIOUS]


def parked(session: dict) -> Decision | None:
    """The previous turn's decision again when that turn changed nothing and ended with a question:
    this message answers it, and the work it was routed for is still to do."""
    record = session.get("routing")
    number = session.get("turn_number") or 1
    if not isinstance(record, dict) or record.get("turn") != number - 1 or record.get("level", 0) < 1:
        return None
    _, reply, entry = _previous(session)
    if entry is None or entry.get("final", 0) != 0 or not asked(reply):
        return None
    decision = Decision.from_record(record)
    decision.source, decision.why = "parked", "the answer to the question the previous turn asked before its work"
    return decision


# ------------------------------------------------------------------ what the orchestrator is told

def _listing(decision: Decision) -> str:
    return ", ".join(f"{name} (\"{quote}\")" if quote else name for name, quote, *_ in decision.deliverables)


def _skeleton_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower()) or "w"


def _skeleton_prefix(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", name)
    return (cleaned[:1].upper() + cleaned[1:]) if cleaned else "Item"


def _earlier(name: str, threads: dict | None) -> tuple[str, dict] | None:
    """The earlier worker (id, thread) that built a deliverable of this name, the newest."""
    found = [(wid, t) for wid, t in (threads or {}).items() if str(t.get("deliverable", "")).lower() == name.lower()]
    return max(found, key=lambda pair: pair[1].get("wave") or 0) if found else None


def skeleton(decision: Decision, threads: dict | None = None) -> str:
    """A compact worker-per-deliverable skeleton, from the router's own JSON: id, deliverable, role,
    prefix and the user's quote, for the orchestrator to fill in (requirements, bounds, checks) rather
    than plan from nothing; a deliverable an earlier worker built goes back to it (continues, with that
    worker's id and prefix). Per-turn text, not the fixed prompt; empty with no deliverables.
    threads: session["worker_threads"]."""
    parts = []
    for name, quote, *rest in decision.deliverables:
        role = rest[0] if rest and rest[0] in ROLES else "modeling"
        earlier = _earlier(name, threads)
        wid, prefix = (earlier[0], earlier[1].get("prefix") or _skeleton_prefix(name)) if earlier else (
            _skeleton_id(name), _skeleton_prefix(name))
        shot = role == "shot"  # A shot's worker: lighting that owns its cameras too (orchestrator.SHOT_ASPECTS).
        parts.append(f'{{"id": "{wid}", "deliverable": "{name}", "role": "{"lighting" if shot else role}", '
                     + (f'"aspects": {json.dumps(orchestrator.SHOT_ASPECTS)}, ' if shot else "")
                     + f'"prefix": "{prefix}", ' + (f'"continues": "{wid}", ' if earlier else "")
                     + f'"quote": "{quote}"}}')
    return "workers: [" + ", ".join(parts) + "]"


def note(decision: Decision, threads: dict | None = None) -> str:
    """The line added to the user's message for the orchestrator."""
    listing = _listing(decision)
    if decision.level == 0:
        body = "level 0 — a question: answer it; change nothing."
    elif decision.level == 1:
        body = "level 1 — a small edit: do it yourself."
    elif decision.level == 2:
        body = (f"level 2 — one deliverable{': ' + listing if listing else ''}. Build it yourself if it takes about "
                f"8 steps, else delegate it to one worker with a style ({STYLE}).")
    else:
        body = (f"level 3 — deliverables: {listing or 'several'}. Delegate these, one worker each, with a style "
                f"({STYLE}), planned bounds and a dry_run first; do shared setup ({SHARED_SETUP}) yourself.")
    if decision.level >= 2 and decision.deliverables:
        body += f" Skeleton: {skeleton(decision, threads)}"
    if decision.source == "parked":
        body = body.replace(" — ", " — the work your question was about: ", 1)
    return f"<routing>{body}</routing>"


NUDGE = ("[Loopcut] Routing planned workers for {what}; delegate them, or say in one line why you did it "
         "yourself.")


def nudge(turn, completion) -> str | None:
    """Once per turn, at a final reply of a level-3 turn that delegated nothing: the nudge. Not at the
    end of a guard or budget, where nothing can follow."""
    decision = getattr(turn, "routed", None)
    if (decision is None or decision.level < 3 or turn.delegations or getattr(turn, "nudged", False)
            or completion is None or turn.cancel.is_set() or turn.outcome in ("step_limit", "budget", "no_progress")):
        return None
    turn.nudged = True
    names = [name for name, *_ in decision.deliverables]
    return NUDGE.format(what=", ".join(names) if names else "this request's deliverables")


# ------------------------------------------------------------------ the turn

def _add_note(message: dict, text: str) -> None:
    content = message.get("content")
    if isinstance(content, list):
        part = next((p for p in content if isinstance(p, dict) and p.get("type") == "text"), None)
        if part is None:
            content.insert(0, {"type": "text", "text": text})
        else:
            part["text"] = f"{part['text'].rstrip()}\n\n{text}"
    else:
        message["content"] = f"{(content or '').rstrip()}\n\n{text}"


def _count(session: dict, usage: dict) -> None:
    """The router's tokens: its own role, and the conversation's sum the UI shows."""
    bucket = session.setdefault("usage_by_role", {}).setdefault(
        "router", {"input": 0, "cached": 0, "output": 0, "requests": 0})
    for key in ("input", "cached", "output"):
        value = int((usage or {}).get(key) or 0)
        bucket[key] += value
        session["usage"][key] = session["usage"].get(key, 0) + value
    bucket["requests"] += 1


def ask(cfg, message: dict, session: dict, is_cancelled, stream=None) -> tuple[Decision | None, dict]:
    """One call to the router model: (its decision or None, the usage it reported).
    Cancellation and hosted allowance refusals end the turn; other failures mean no decision."""
    from . import config, cloud
    if not config.on_service(cfg.base_url):
        return None, {}
    stream = stream or llm.stream_chat
    text = request_text(user_request(message), scene_summary(message), previous_note(session),
                        bool(message.get(context.ATTACHED)))
    try:
        cloud.hosted_index(required=True)
        completion = stream(base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
                            messages=[{"role": "user", "content": text}], prompt_id="router",
                            tools=[], reasoning_effort=cfg.reasoning_effort, on_text=lambda _: None,
                            is_cancelled=is_cancelled, timeout=TIMEOUT,
                            max_tokens=cfg.max_output_tokens, call_max_seconds=cfg.call_max_seconds,
                            headers=llm.correlation_headers(session["id"], "router"), on_retry=lambda: None)
    except llm.Cancelled:
        raise
    except Exception as ex:  # A router that fails is no router, never a failed turn.
        if isinstance(ex, llm.LLMError) and ex.status == 402:
            from . import state
            if ex.code in state.HOSTED_LIMITS:
                raise  # The account's refusal ends the turn; another model request cannot add allowance.
        print(f"Loopcut: no routing for this turn: {ex!r}")
        return None, {}
    return parse(completion.text), completion.usage or {}


def route(session: dict, message: dict, cfg, is_cancelled, stream=None) -> Decision | None:
    """Rules, then the model: this turn's decision, or None. Counts the call's usage."""
    decision = parked(session)
    if decision is None:
        decision, usage = ask(cfg, message, session, is_cancelled, stream)
        _count(session, usage)
        if message.get(context.ATTACHED):  # The image rule: planning at least.
            if decision is None:
                decision = Decision(level=2, why="an attached image", source="rules")
            elif decision.level < 2:
                decision.level, decision.source = 2, "rules"
    if decision is not None:
        decision.turn = session.get("turn_number") or 1
    return decision


# The roles of a look: the job restyles, paints, lights or frames what is there. Such a turn, with at
# most LOOK_MAX deliverables, goes to the single agent's workspace loop when the workspace is on
# (agent._route): Sol and Astra alone in a shell did the Arcane chest lookdev better and in a fraction
# of the time of the orchestrator and workers (workspace.py). Building several new things keeps the
# orchestrator, whose workers build them side by side.
LOOK_ROLES = ("materials", "lighting", "shot", "camera")
LOOK_MAX = 2


def to_workspace(decision: Decision) -> bool:
    """A look of one or two deliverables (LOOK_ROLES): the workspace loop's job."""
    roles = [d[2] if len(d) > 2 else "modeling" for d in decision.deliverables]
    return decision.level >= 2 and 0 < len(roles) <= LOOK_MAX and all(role in LOOK_ROLES for role in roles)


def start_turn(session: dict, turn, cfg, is_cancelled, stream=None, divert=None) -> None:
    """LoopSpec.on_turn_start for the orchestrator: route the message that started this turn, once.
    A resumed turn finds the message already routed and takes the decision it recorded. divert:
    decision -> True when the turn goes to the workspace loop instead (to_workspace): then the
    decision is recorded but no note is added, and turn.diverted is set."""
    messages = session["messages"]
    starts = _turn_starts(messages)
    if not starts:
        return
    message = messages[starts[-1]]
    if ROUTED in message:
        record = session.get("routing")
        if message[ROUTED] >= 0 and isinstance(record, dict) and record.get("turn") == session.get("turn_number"):
            turn.routed, turn.route_tried = Decision.from_record(record), True
        return
    if cfg is None:
        return
    turn.route_tried = True
    decision = route(session, message, cfg, is_cancelled, stream)
    turn.routed = decision
    message[ROUTED] = decision.level if decision else -1
    if decision is None:
        return
    session["routing"] = decision.record()
    if divert is not None and divert(decision):
        turn.diverted = True
        return
    _add_note(message, note(decision, session.get("worker_threads")))
