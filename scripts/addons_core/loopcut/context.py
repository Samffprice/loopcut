"""Keeping what the model sees within a budget.

Every request carries the whole conversation, so a long session pays for everything again on
every step: a hundred steps over a 50k-token history is five million tokens. Three measures:

1. Elision, once the request grows past ELIDE_AT of the budget. Then, oldest first, a step's
   code is cut to a line (the model only needs it while reacting to its result; a failed step
   is still whole when the retry is written) and its result is cut to its first line plus a
   note to call the tool again (a result describes the scene as it was, and the scene can be
   read again), until the request is down to ELIDE_TO of the budget. The newest PROTECTED_CODE
   calls and PROTECTED_RESULTS results are never cut. Cutting in batches means the provider's
   prompt cache is broken rarely rather than on every step; an elision once made is never
   undone. Under the threshold nothing is cut: the budget is the user's to spend, and a model
   that can still see its last ten steps repeats fewer of them.
2. Compaction. If the budget is still exceeded, the model summarizes everything before the current
   turn (or a later point, if the current turn alone is too big) into one message. The summary
   rides on the first message it does not cover, so a restore that cuts the conversation cuts
   the summary with it, and one that keeps it keeps it valid: everything before that message is
   common to every branch.
3. Folding, at request time only. Finished steps (code and result both elided) and captures
   that are no longer sent are folded into one assistant message per run of them: one line per
   step, about 15 tokens instead of the 100 a stubbed step still costs with its envelopes.
4. Images: kept_images() keeps the captures the model has not acted on yet, the newest capture
   of all (so the model always has a picture of the scene as it last saw it, until a newer one
   replaces it), and the images the user attached, which are references: they stay in every
   request until the user unpins them, at a reduced size, so the cache pays for them. A capture
   on the provider we measured costs IMAGE_TOKENS, ten times a typical tool result. A capture
   message may carry a second, smaller image: a strip of the earlier looks (STRIP), so the
   history of a piece of work costs one small image instead of one large image per look.

Nothing here inserts or removes stored messages: checkpoints cut the conversation by index.
Elision edits a message in place and the summary is a key on a message. Our keys on messages
start with PRIVATE_PREFIX and are never sent.
"""

import json
from typing import Callable

from . import llm

PRIVATE_PREFIX = "loopcut_"  # Keys we keep on stored messages. conversations.wire_messages drops them.
ELIDED = "loopcut_elided"     # On a tool message: how many characters its content had.
SUMMARY = "loopcut_summary"   # On a message: a summary of everything before it.
CAPTURE = "loopcut_capture"   # On a user message that carries a viewport capture; not a turn.
CAPTURE_TEXT = "Viewport capture from capture_viewport:"  # Marks captures from before CAPTURE.
ATTACHED = "loopcut_attached"  # On a user message whose images the user attached.
REFERENCE_CARD = "loopcut_reference_card"  # On an attached message once its images were described.
STRIP = "loopcut_strip"  # On a capture message whose last image is a strip of earlier looks.
KEEP_IMAGES = 3      # Captures sent per request: the newest step's, until the model has acted on them.
KEEP_ATTACHED = 3    # Pinned references sent per request, newest first.
LEDGER_HEAD = "Earlier steps this turn (results elided; call a tool again if you need the details):"

PROTECTED_RESULTS = 4   # The newest tool results are never cut.
PROTECTED_CODE = 2      # The newest run_python calls always keep their code.
ELIDE_AT = 0.6          # Elision starts when a request would exceed this share of the budget...
ELIDE_TO = 0.4          # ...and cuts, oldest first, until the request is under this share.
STUB_CHARS = 160        # What an elided result keeps: enough to recognize it.
COMPACT_TO = 0.5        # After a compaction the kept tail is at most this share of the budget.
# Measured on the Loopcut gateway (Meta upstream) against 56 logged requests, within 7%:
CHARS_PER_TOKEN = 3.4   # JSON and code run denser than prose; the ratio is corrected from usage.
IMAGE_TOKENS = 800      # One capture at tools.CAPTURE_WIDTH (640 px); it was 1700 at 960 px, scaling with pixels.
STRIP_TOKENS = 350      # A strip of earlier looks at tools.STRIP_HEIGHT: fewer pixels than one capture.
FIXED_TOKENS = 1950     # System prompt and tool schemas.
SUMMARY_TOKENS = 700    # What a summary costs, at most, once made.
MIN_RATIO, MAX_RATIO = 0.5, 3.0

SUMMARY_PROMPT = """You are condensing the earlier part of a conversation between a user and Loopcut, an AI \
agent that works inside the user's Blender session by running Python. The conversation will continue with your \
summary standing in for those messages, so write it for the agent. Cover, in this order and only where there is \
something to say:
1. What the user asked for, and their preferences: style, scale and units, naming, anything they said not to do.
2. What is now in the scene as a result: the exact names of objects, materials, collections, node groups, \
cameras and lights that were created or changed, and the settings that matter (values, node setups, modifiers).
3. What was tried and did not work, and what was learned about this Blender's API (from inspect_api or errors).
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
    """A message the user sent, as opposed to a capture the agent asked for."""
    return message.get("role") == "user" and not is_capture(message)


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
    against and stays until the user says otherwise. An image once dropped stays dropped, so
    the request prefix before it holds. Messages, not references: two identical captures share
    a file, and the older one must still drop."""
    turn_start = next((i for i in range(len(messages) - 1, -1, -1) if is_turn_start(messages[i])), 0)
    batch: list[dict] = []  # A step's captures come right after its results, consecutively.
    for message in messages[turn_start + 1:]:
        batch = batch + [message] if is_capture(message) else []
    latest = [m for m in reversed(messages) if is_capture(m)][:1]
    attached = [m for m in messages if m.get(ATTACHED) and any(ref not in unpinned for ref in _image_refs(m))]
    return {id(m) for m in batch[-KEEP_IMAGES:] + latest} | {id(m) for m in attached[-KEEP_ATTACHED:]}


def kept_images(messages: list, unpinned: frozenset = frozenset()) -> set[str]:
    """The image references still sent; see kept_messages."""
    kept = kept_messages(messages, unpinned)
    return {ref for m in messages if id(m) in kept for ref in _image_refs(m) if ref not in unpinned}


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


def estimate_tokens(messages: list) -> int:
    """Rough and cheap; calibrate() corrects it from what the API reports."""
    chars = images = strips = 0
    for message in messages:
        c, i = _size(message)
        chars, images = chars + c, images + i
        strips += 1 if i and message.get(STRIP) else 0
    return FIXED_TOKENS + round(chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS - strips * (IMAGE_TOKENS - STRIP_TOKENS)


def calibrate(session: dict, estimated: int, reported: int) -> None:
    """Remember how far the estimate was from the tokens the API counted for the same request."""
    if estimated > 0 and reported > 0:
        session["token_ratio"] = min(MAX_RATIO, max(MIN_RATIO, reported / estimated))


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
    finished steps folded. Stored messages are returned as themselves, never copied or edited."""
    start, summary = window(messages)
    tail = fold(messages[start:upto], kept_messages(messages, unpinned))
    return ([_summary_message(summary)] if summary else []) + tail


def _ledger_line(call: dict, result: dict) -> str:
    function = call.get("function") or {}
    name = function.get("name", "?")
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except ValueError:
        arguments = {}
    if name == "run_python":
        label = str(arguments.get("summary") or name) if isinstance(arguments, dict) else name
    else:
        label = f"{name}({json.dumps(arguments, separators=(',', ':'))[:60]})"
    first = str(result.get("content", "")).split("\n", 1)[0][:120]
    return f"- {label}: {first}"


CAPTURE_LABEL = "Viewport capture: "  # How a capture message's text starts; the rest says what it shows.


def _look_line(message: dict) -> str:
    """What a dropped capture is remembered by: what it showed, when the message says."""
    content = message.get("content")
    text = next((p.get("text", "") for p in content if p.get("type") == "text"), "") if isinstance(content, list) else ""
    first = text.split("\n", 1)[0]
    if first.startswith(CAPTURE_LABEL):
        return f"- looked: {first[len(CAPTURE_LABEL):][:120]}"
    return "- looked at the viewport"


def fold(messages: list, kept: set[int]) -> list[dict]:
    """Runs of finished steps and dropped captures become one assistant message each. A step is
    finished when its results are all elided; the newest steps, user messages, replies to the
    user and kept captures stay as they are."""
    out, ledger = [], []

    def flush() -> None:
        if ledger:
            out.append({"role": "assistant", "content": LEDGER_HEAD + "\n" + "\n".join(ledger)})
            ledger.clear()

    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if calls:
            results = messages[index + 1:index + 1 + len(calls)]
            if all(r.get("role") == "tool" and ELIDED in r for r in results) and len(results) == len(calls):
                ledger.extend(_ledger_line(call, result) for call, result in zip(calls, results))
                index += 1 + len(calls)
                continue
        elif is_capture(message) and id(message) not in kept:
            ledger.append(_look_line(message))
            index += 1
            continue
        flush()
        out.append(message)
        index += 1
    flush()
    return out


# ------------------------------------------------------------------ elision

def _headline(text: str) -> str:
    """What a result is remembered by: its first line, or for a traceback the exception, plus
    the first scene change a run_python step made."""
    output, _, changes = text.partition("Scene changes")
    lines = [line for line in output.strip().splitlines() if line.strip()]
    first = (lines[-1] if lines and lines[0].startswith("Traceback") else lines[0]) if lines else ""
    change = next((line.strip() for line in changes.splitlines()[1:] if line.strip()), "")
    return (f"{first} | {change}" if first and change else first or change)[:STUB_CHARS]


def _stub(text: str) -> str:
    return f"{_headline(text)}\n[+{len(text)} chars elided; call the tool again if needed]"


def _elide_code(call: dict) -> bool:
    function = call.get("function") or {}
    if function.get("name") != "run_python":
        return False
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except ValueError:
        return False
    if not isinstance(arguments, dict) or not isinstance(arguments.get("code"), str) or arguments["code"].startswith("["):
        return False
    arguments["code"] = f"[{arguments['code'].count(chr(10)) + 1} lines elided]"
    function["arguments"] = json.dumps(arguments)
    return True


def elide(messages: list, budget: int, ratio: float = 1.0, unpinned: frozenset = frozenset()) -> int:
    """Once a request would exceed ELIDE_AT of the budget, cut the results and code of steps
    the model has moved past, oldest first, until it is under ELIDE_TO of it; only in the part
    of the conversation a summary does not cover, and never the newest steps. Returns how many
    edits were made."""
    def over(share: float) -> bool:
        return estimate_tokens(view(messages, unpinned=unpinned)) * ratio > budget * share

    if not over(ELIDE_AT):
        return 0
    start, _ = window(messages)
    results = [m for m in messages[start:] if m.get("role") == "tool" and isinstance(m.get("content"), str)]
    calls = [call for m in messages[start:] if m.get("role") == "assistant"
             for call in m.get("tool_calls") or [] if (call.get("function") or {}).get("name") == "run_python"]
    protected = {id(m) for m in results[-PROTECTED_RESULTS:]} | {id(c) for c in calls[-PROTECTED_CODE:]}
    edits = 0
    for message in messages[start:]:
        if message.get("role") == "tool" and id(message) not in protected and ELIDED not in message \
                and isinstance(message.get("content"), str):
            message[ELIDED] = len(message["content"])
            message["content"] = _stub(message["content"])
            edits += 1
        elif message.get("role") == "assistant":
            edits += sum(_elide_code(call) for call in message.get("tool_calls") or []
                         if id(call) not in protected and (call.get("function") or {}).get("name") == "run_python")
        else:
            continue
        if edits and not over(ELIDE_TO):
            break
    return edits


# ------------------------------------------------------------------ compaction

def compaction_cut(messages: list, budget: int, ratio: float = 1.0) -> int | None:
    """Where to cut so that what follows fits COMPACT_TO of the budget: the earliest turn start
    that does, else the earliest message a request may start with (a user or assistant message;
    a tool result cannot come first). None when nothing can be summarized or nothing fits."""
    start, _ = window(messages)

    def fits(index: int) -> bool:
        return (estimate_tokens(messages[index:]) + SUMMARY_TOKENS) * ratio <= budget * COMPACT_TO

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


def prepare(session: dict, cfg, is_cancelled: Callable[[], bool], summarize=summarize) -> tuple[list[dict], int]:
    """The messages for the next request, kept within cfg.context_budget, and how many stored
    messages a new summary now stands for (0 when none was made). Edits session["messages"]
    in place; never changes their number or order."""
    messages, unpinned = session["messages"], unpinned_refs(session)
    budget, ratio = cfg.context_budget, session.get("token_ratio") or 1.0
    compacted = 0
    elide(messages, budget, ratio, unpinned)
    if estimate_tokens(view(messages, unpinned=unpinned)) * ratio > budget:
        cut = compaction_cut(messages, budget, ratio)
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
