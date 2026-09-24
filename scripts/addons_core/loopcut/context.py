"""Keeping what the model sees within a budget, without losing why it did what it did.

Every request carries the whole conversation, and the provider caches the part of a request that
is the same as last time: appending is cheap, editing anything already sent re-bills everything
after the edit. So the history is append-only while the model works, and shrinks at a few
moments, in batches:

1. Step records. A step is one assistant message with tool calls, its results and the captures
   that follow. Folded, a step becomes its record: what the model said at that step, word for
   word (what it saw and why it acted: the cheapest and most useful thing in the history, which
   the model needs to avoid undoing its own fixes), then per call its label, whether it worked,
   what the code printed and the full "Scene changes". Code bodies and long outputs go: the scene
   is the source of truth for exact values, and the model can read it again. Nothing is edited:
   a step is marked FOLDED and its record is built when a request is sent.
2. When the user sends a message, every step of the earlier turns is folded. One cache break
   per turn, when the request grows anyway.
3. Mid-turn, only when a request would pass FOLD_AT of the budget, the oldest steps of the turn
   are folded, all at once, down to FOLD_TO; the newest LIVE_STEPS stay whole.
4. Compaction, the last resort. If records alone exceed the budget, the model summarizes
   everything before a cut into one message. The summary rides on the first message it does not
   cover, so a restore that cuts the conversation cuts the summary with it.
5. Images: kept_messages() keeps the captures the model has not acted on yet, the newest capture
   of all (the model is never without a picture of the scene), and the images the user attached,
   which are references and stay until unpinned. A dropped capture is remembered by what it showed.

The budget is counted as the provider counts it: the system prompt and tool schemas are part of
every request (agent passes their size as `fixed`), and calibrate() corrects only the tokenizer's
chars-per-token from the usage the provider reports.

Nothing here inserts or removes stored messages: checkpoints cut the conversation by index. Our
keys on messages start with PRIVATE_PREFIX and are never sent.
"""

import json
from typing import Callable

from . import llm

PRIVATE_PREFIX = "loopcut_"  # Keys we keep on stored messages. conversations.wire_messages drops them.
FOLDED = "loopcut_folded"     # On an assistant message with tool calls: the step is sent as its record.
SUMMARY = "loopcut_summary"   # On a message: a summary of everything before it.
CAPTURE = "loopcut_capture"   # On a user message that carries a viewport capture; not a turn.
CAPTURE_TEXT = "Viewport capture from capture_viewport:"  # Marks captures from before CAPTURE.
ATTACHED = "loopcut_attached"  # On a user message whose images the user attached.
STEER = "loopcut_steer"        # On a user message sent while the agent was working; agent._fold_notes.
REFERENCE_CARD = "loopcut_reference_card"  # On an attached message once its images were described.
KEEP_IMAGES = 3      # Captures sent per request: the newest step's, until the model has acted on them.
KEEP_ATTACHED = 3    # Pinned references sent per request, newest first.
RECORD_HEAD = ("Earlier steps. Their code and full results are no longer shown; what you said at each step "
               "and what it changed are. The scene is the source of truth: read it again for exact values.")

LIVE_STEPS = 3       # The newest steps of a turn are never folded mid-turn.
FOLD_AT = 0.75       # Mid-turn folding starts when a request would exceed this share of the budget...
FOLD_TO = 0.5        # ...and folds the oldest steps of the turn until the request is under this share.
COMPACT_TO = 0.5     # After a compaction the kept tail is at most this share of the budget.
RECORD_PRINTED = 400  # Characters of what a step's code printed that its record keeps.
RECORD_READ = 300     # Characters of any other tool's result that its record keeps.
RECORD_CHANGES = 15   # Scene-change lines a record keeps.
RECORD_LOOK = 300     # Characters of what a dropped capture showed.
# Measured on the Loopcut gateway (Meta upstream) against 56 logged requests, within 7%:
CHARS_PER_TOKEN = 3.4   # JSON and code run denser than prose; the ratio is corrected from usage.
IMAGE_TOKENS = 800      # One capture at tools.CAPTURE_WIDTH (640 px); it was 1700 at 960 px, scaling with pixels.
FIXED_TOKENS = 7200     # System prompt and tool schemas when the caller does not say (measured 2026-09-23).
SUMMARY_TOKENS = 700    # What a summary costs, at most, once made.
MIN_RATIO, MAX_RATIO = 0.5, 2.0

SUMMARY_PROMPT = """You are condensing the earlier part of a conversation between a user and Loopcut, an AI \
agent that works inside the user's Blender session by running Python. The conversation will continue with your \
summary standing in for those messages, so write it for the agent. Cover, in this order and only where there is \
something to say:
1. What the user asked for, and their preferences: style, scale and units, naming, anything they said not to do.
2. What is now in the scene as a result: the exact names of objects, materials, collections, node groups, \
cameras and lights that were created or changed, and the settings that matter (values, node setups, modifiers).
3. What was tried and did not work, why, and what was done instead, so it is not tried again; what was learned \
about this Blender's API (from inspect_api or errors).
4. What was still in progress or left undecided.
Be specific: names and numbers, not prose. No preamble. At most 500 words."""


# ------------------------------------------------------------------ shape of the history

def is_capture(message: dict) -> bool:
    if message.get("role") != "user":
        return False
    if message.get(CAPTURE):
        return True
    content = message.get("content")
    if isinstance(content, list):
        text = next((p.get("text", "") for p in content if p.get("type") == "text"), "")
        return text.startswith(CAPTURE_TEXT)
    return False


def is_turn_start(message: dict) -> bool:
    """A message the user sent to start a turn, as opposed to a capture the agent asked for or a
    note sent while the agent was working (which continues the turn it arrived in)."""
    return message.get("role") == "user" and not is_capture(message) and not message.get(STEER)


def turn_start(messages: list) -> int:
    return next((i for i in range(len(messages) - 1, -1, -1) if is_turn_start(messages[i])), 0)


def _image_refs(message: dict) -> list[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [p["image_url"]["url"] for p in content if p.get("type") == "image_url"]


def unpinned_refs(session: dict) -> frozenset:
    return frozenset(r["ref"] for r in session.get("references") or [] if not r.get("pinned", True))


def kept_messages(messages: list, unpinned: frozenset = frozenset()) -> set[int]:
    """ids of the messages whose images are still sent: the captures the model has not yet
    acted on (the trailing run of capture messages, at most KEEP_IMAGES), the newest capture
    of the conversation whatever came after it, and the images the user attached and has not
    unpinned (newest KEEP_ATTACHED), from any turn. Several looks in one step are all seen
    once; after that the newest stays as the model's picture of the scene until a newer look
    replaces it, so the model is never without one. A reference is what the work is measured
    against and stays until the user says otherwise. Messages, not references: two identical
    captures share a file, and the older one must still drop."""
    batch: list[dict] = []  # A step's captures come right after its results, consecutively.
    for message in messages[turn_start(messages) + 1:]:
        batch = batch + [message] if is_capture(message) else []
    latest = [m for m in reversed(messages) if is_capture(m)][:1]
    attached = [m for m in messages if m.get(ATTACHED) and any(ref not in unpinned for ref in _image_refs(m))]
    return {id(m) for m in batch[-KEEP_IMAGES:] + latest} | {id(m) for m in attached[-KEEP_ATTACHED:]}


def kept_images(messages: list, unpinned: frozenset = frozenset()) -> set[str]:
    """The image references still sent; see kept_messages."""
    kept = kept_messages(messages, unpinned)
    return {ref for m in messages if id(m) in kept for ref in _image_refs(m) if ref not in unpinned}


def steps(messages: list, start: int = 0, end: int | None = None) -> list[int]:
    """Indexes of the assistant messages that made tool calls, in messages[start:end]."""
    end = len(messages) if end is None else end
    return [i for i in range(start, end) if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")]


# ------------------------------------------------------------------ size

def _size(message: dict) -> tuple[int, int]:
    """(characters of text, number of images)."""
    chars, images = 0, 0
    content = message.get("content")
    if isinstance(content, str):
        chars += len(content)
    elif isinstance(content, list):
        for part in content:
            if part.get("type") == "image_url":
                images += 1
            else:
                chars += len(part.get("text", ""))
    for call in message.get("tool_calls") or []:
        chars += len(call["function"].get("name", "")) + len(call["function"].get("arguments", ""))
    return chars, images


def fixed_tokens(system_prompt: str, tools: list) -> int:
    """What every request costs before the conversation: the system prompt and the tool schemas."""
    return round((len(system_prompt) + len(json.dumps(tools))) / CHARS_PER_TOKEN)


def estimate_tokens(messages: list, fixed: int = FIXED_TOKENS) -> int:
    """Rough and cheap; calibrate() corrects the characters per token from what the API reports."""
    chars = images = 0
    for message in messages:
        c, i = _size(message)
        chars, images = chars + c, images + i
    return fixed + round(chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS


def calibrate(session: dict, estimated: int, reported: int) -> None:
    """Remember how far the estimate was from the tokens the API counted for the same request.
    The estimate includes the fixed part, so the ratio is the tokenizer's, not a stand-in for
    overhead the estimate left out (which once made a 10k history look like 15k and cut it)."""
    if estimated > 0 and reported > 0:
        session["token_ratio"] = min(MAX_RATIO, max(MIN_RATIO, reported / estimated))


# ------------------------------------------------------------------ records and the view

def window(messages: list) -> tuple[int, str | None]:
    """(index of the first message sent as itself, the summary standing for what is before it)."""
    for index in range(len(messages) - 1, -1, -1):
        summary = messages[index].get(SUMMARY)
        if isinstance(summary, str) and summary:
            return index, summary
    return 0, None


def _summary_message(summary: str) -> dict:
    return {"role": "user", "content": "<conversation_summary>\n" + summary.strip() + "\n</conversation_summary>\n"
            "The block above stands for earlier messages of this conversation. It continues below."}


def view(messages: list, upto: int | None = None, unpinned: frozenset = frozenset()) -> list[dict]:
    """What is sent for messages[:upto]: the latest summary, then the messages after it with
    folded steps as records. Stored messages are returned as themselves, never copied or edited."""
    start, summary = window(messages)
    tail = fold(messages[start:upto], kept_messages(messages, unpinned))
    return ([_summary_message(summary)] if summary else []) + tail


def _indent(text: str, limit: int) -> list[str]:
    text = text.strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + f" [+{len(text) - limit} chars]"
    return ["    " + line for line in text.splitlines() if line.strip()]


def _run_python_lines(result: str) -> tuple[str, list[str]]:
    """(outcome, detail lines) of a run_python result: output (prints, then a traceback if it
    failed), then "Scene changes" and its lines, then whatever was appended (an automatic look,
    a note). A result without "Scene changes" is a step that never ran: rejected, cancelled."""
    if "\n\nScene changes" not in result:
        return f"not run: {result.strip().splitlines()[0][:200] if result.strip() else 'no result'}", []
    output, _, rest = result.partition("\n\nScene changes")
    output = output.strip()
    changes = [line for line in rest.split("\n\n", 1)[0].splitlines()[1:] if line.strip()]
    printed, marker, failure = output.partition("Traceback (most recent call last)")
    if not marker:
        printed, marker, failure = output.partition("Stopped: the code ran for more than")
        failure = marker + failure if marker else ""
    if marker:
        last = [line.strip() for line in failure.splitlines() if line.strip()]
        outcome = f"failed: {(last[-1] if last else marker)[:200]}"
    else:
        outcome = "ok"
    printed = "" if printed.strip() == "OK (no output)" else printed.strip()
    details = _indent(printed, RECORD_PRINTED) if printed else []
    if printed and not marker:
        outcome = "ok, printed:"
    if not changes and rest.startswith(": none"):
        details.append("    no scene changes")
    if len(changes) > RECORD_CHANGES:
        # The rest by name only: a big build's objects must stay findable once its record is all
        # that is left (a run had to search for the backdrop its own first step made).
        rest = [line.strip() for line in changes[RECORD_CHANGES:]]
        names = [line[2:].split(" (", 1)[0].split(":", 1)[0] for line in rest if line[:2] in ("+ ", "- ", "~ ")]
        changes = changes[:RECORD_CHANGES] + [f"... and {len(rest)} more: " + ", ".join(names)]
    return outcome, details + ["    " + line.strip() for line in changes]


def _call_lines(call: dict, result: dict | None) -> list[str]:
    function = call.get("function") or {}
    name = function.get("name", "?")
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except ValueError:
        arguments = {}
    arguments = arguments if isinstance(arguments, dict) else {}
    text = str((result or {}).get("content") or "")
    if name == "run_python":
        outcome, details = _run_python_lines(text)
        return [f"- run_python \"{arguments.get('summary') or 'code'}\": {outcome}"] + details
    shown = {k: v for k, v in arguments.items() if k != "code"}
    head = f"- {name}({json.dumps(shown, separators=(',', ':'))[:100]})"
    first, _, more = text.strip().partition("\n")
    return [f"{head}: {_clause(first, RECORD_LOOK)}"] + (_indent(more, RECORD_READ) if more.strip() else [])


CAPTURE_LABEL = "Viewport capture: "  # How a capture message's text starts; the rest says what it shows.


def _look_line(message: dict) -> str:
    """What a dropped capture is remembered by: what it showed, when the message says."""
    content = message.get("content")
    text = next((p.get("text", "") for p in content if p.get("type") == "text"), "") if isinstance(content, list) else ""
    first = text.split("\n", 1)[0]
    if first.startswith(CAPTURE_LABEL):
        return f"- looked: {_clause(first[len(CAPTURE_LABEL):], RECORD_LOOK)}"
    return "- looked at the viewport"


def _clause(text: str, limit: int) -> str:
    """text, or its longest start within limit that ends at a clause: a line cut mid-word reads
    to the model as a record that lost something."""
    if len(text) <= limit:
        return text
    cut = max(text.rfind("; ", 0, limit), text.rfind(". ", 0, limit), text.rfind(", ", 0, limit))
    return (text[:cut] if cut > limit // 2 else text[:limit].rsplit(" ", 1)[0]) + " …"


def record(message: dict, results: list[dict]) -> list[str]:
    """A folded step: what the model said, then each call's outcome."""
    lines = [message["content"].strip()] if isinstance(message.get("content"), str) and message["content"].strip() else []
    by_id = {r.get("tool_call_id"): r for r in results}
    for call in message.get("tool_calls") or []:
        lines += _call_lines(call, by_id.get(call.get("id")))
    return lines


def fold(messages: list, kept: set[int]) -> list[dict]:
    """Runs of folded steps and dropped captures become one assistant message each, a record per
    step separated by a blank line; the other messages stay as they are."""
    out, entries = [], []

    def flush() -> None:
        if entries:
            # Only dropped looks between live steps: one line each, not the records' preamble again.
            looks_only = all(e.startswith("- looked") for e in entries)
            head = "Earlier look, image no longer attached:" if looks_only else RECORD_HEAD + "\n"
            out.append({"role": "assistant", "content": head + "\n" + "\n\n".join(entries)})
            entries.clear()

    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if calls and message.get(FOLDED):
            end = index + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            entries.append("\n".join(record(message, messages[index + 1:end])))
            index = end
            continue
        if is_capture(message) and id(message) not in kept:
            look = _look_line(message)
            if entries:
                entries[-1] += "\n" + look  # A look belongs to the step that took it.
            else:
                entries.append(look)
            index += 1
            continue
        flush()
        out.append(message)
        index += 1
    flush()
    return out


# ------------------------------------------------------------------ folding

def fold_steps(messages: list, indexes: list[int]) -> int:
    """Mark these steps folded; returns how many were not already."""
    count = 0
    for index in indexes:
        if not messages[index].get(FOLDED):
            messages[index][FOLDED] = True
            count += 1
    return count


def fold_for_budget(messages: list, budget: int, ratio: float = 1.0, unpinned: frozenset = frozenset(),
                    fixed: int = FIXED_TOKENS) -> int:
    """Earlier turns are always sent as records. In the current turn, once a request would exceed
    FOLD_AT of the budget, its oldest steps are folded, oldest first, until the request is under
    FOLD_TO, leaving the newest LIVE_STEPS whole. Returns how many steps were folded now."""
    start, _ = window(messages)
    current = turn_start(messages)
    folded = fold_steps(messages, steps(messages, start, current))

    def over(share: float) -> bool:
        return estimate_tokens(view(messages, unpinned=unpinned), fixed) * ratio > budget * share

    if not over(FOLD_AT):
        return folded
    for index in steps(messages, max(start, current))[:-LIVE_STEPS or None]:
        folded += fold_steps(messages, [index])
        if not over(FOLD_TO):
            break
    return folded


# ------------------------------------------------------------------ compaction

def compaction_cut(messages: list, budget: int, ratio: float = 1.0, unpinned: frozenset = frozenset(),
                   fixed: int = FIXED_TOKENS) -> int | None:
    """Where to cut so that what follows fits COMPACT_TO of the budget: the earliest turn start
    that does, else the earliest message a request may start with (a user or assistant message;
    a tool result cannot come first). None when nothing can be summarized or nothing fits."""
    start, _ = window(messages)
    kept = kept_messages(messages, unpinned)

    def fits(index: int) -> bool:
        return (estimate_tokens(fold(messages[index:], kept), fixed) + SUMMARY_TOKENS) * ratio <= budget * COMPACT_TO

    candidates = [i for i in range(start + 1, len(messages)) if messages[i].get("role") in ("user", "assistant")]
    for chosen in ([i for i in candidates if is_turn_start(messages[i])], candidates):
        for index in chosen:
            if fits(index):
                return index
    return None


def transcript(messages: list) -> str:
    """The messages as plain text for the summarizing call: valid whatever the provider, and
    needs no tool schemas."""
    lines = []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") if p.get("type") != "image_url" else "[image]" for p in content)
        label = {"user": "USER", "assistant": "ASSISTANT", "tool": "TOOL RESULT"}.get(role, role.upper())
        if content:
            lines.append(f"{label}: {content}")
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            lines.append(f"ASSISTANT CALLED {function.get('name')}: {function.get('arguments')}")
    return "\n\n".join(lines)


def summarize(cfg, messages: list, is_cancelled: Callable[[], bool]) -> str:
    completion = llm.stream_chat(
        base_url=cfg.base_url, api_key=cfg.api_key, model=cfg.model,
        messages=[{"role": "system", "content": SUMMARY_PROMPT},
                  {"role": "user", "content": transcript(messages) + "\n\nSummarize the conversation above as instructed."}],
        tools=[], reasoning_effort="low", on_text=lambda _: None, is_cancelled=is_cancelled)
    return completion.text.strip()


def prepare(session: dict, cfg, is_cancelled: Callable[[], bool], summarize=summarize,
            fixed: int = FIXED_TOKENS) -> tuple[list[dict], int]:
    """The messages for the next request, kept within cfg.context_budget, and how many stored
    messages a new summary now stands for (0 when none was made). Marks steps folded and may
    put a summary on a message; never changes the number, order or content of messages."""
    messages, unpinned = session["messages"], unpinned_refs(session)
    budget, ratio = cfg.context_budget, session.get("token_ratio") or 1.0
    compacted = 0
    fold_for_budget(messages, budget, ratio, unpinned, fixed)
    if estimate_tokens(view(messages, unpinned=unpinned), fixed) * ratio > budget:
        cut = compaction_cut(messages, budget, ratio, unpinned, fixed)
        if cut is not None:
            try:
                summary = summarize(cfg, view(messages, cut, unpinned), is_cancelled)
            except llm.LLMError as ex:
                print(f"Loopcut: could not summarize the conversation, sending it as is: {ex}")
                summary = ""
            if summary:
                start, _ = window(messages)
                messages[cut][SUMMARY] = summary
                compacted = cut - start
    return view(messages, unpinned=unpinned), compacted
