"""Keeping what the model sees within a budget.

Every request carries the whole conversation, so a long session pays for everything again on
every step: a hundred steps over a 50k-token history is five million tokens. Three measures:

1. Elision, on every request. A step's code is kept for the newest PROTECTED_CODE run_python
   calls and then cut to a line, since the model only needs it while reacting to its result
   (a failed step is still whole when the retry is written). Tool results are kept whole for
   the newest PROTECTED_RESULTS and then cut down to their first line plus a note to call the
   tool again: a result describes the scene as it was, and the scene can be read again. Both
   edits land a few messages from the end of the request, so the provider's prompt cache keeps
   everything before them; an elision once made is never undone, so nothing deep in the prefix
   changes. In the conversation that motivated this, code and results were 43% of the input.
2. Compaction. If the budget is still exceeded, the model summarizes everything before the current
   turn (or a later point, if the current turn alone is too big) into one message. The summary
   rides on the first message it does not cover, so a restore that cuts the conversation cuts
   the summary with it, and one that keeps it keeps it valid: everything before that message is
   common to every branch.
3. Folding, at request time only. Finished steps (code and result both elided) and captures
   that are no longer sent are folded into one assistant message per run of them: one line per
   step, about 15 tokens instead of the 100 a stubbed step still costs with its envelopes.
4. Images: kept_images() keeps only the newest KEEP_IMAGES captures of the current turn and the
   newest KEEP_ATTACHED images the user attached. A capture on the provider we measured costs
   IMAGE_TOKENS, ten times a typical tool result.

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
KEEP_IMAGES = 3      # Captures sent per request: the newest step's, until the model has acted on them.
KEEP_ATTACHED = 3    # Attached images, counted apart, for the turn they were attached in.
LEDGER_HEAD = "Earlier steps this turn (results elided; call a tool again if you need the details):"

PROTECTED_RESULTS = 4   # The newest tool results are sent whole.
PROTECTED_CODE = 2      # The newest run_python calls keep their code.
STUB_CHARS = 160        # What an elided result keeps: enough to recognize it.
COMPACT_TO = 0.5        # After a compaction the kept tail is at most this share of the budget.
# Measured on the Loopcut gateway (Meta upstream) against 56 logged requests, within 7%:
CHARS_PER_TOKEN = 3.4   # JSON and code run denser than prose; the ratio is corrected from usage.
IMAGE_TOKENS = 800      # One capture at tools.CAPTURE_WIDTH (640 px); it was 1700 at 960 px, scaling with pixels.
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


def kept_images(messages: list) -> set[str]:
    """The image references still sent: the captures the model has not yet acted on (the
    trailing run of capture messages, at most KEEP_IMAGES) and the images the user attached to
    the current turn (newest KEEP_ATTACHED). A capture is consumed by the step after it, which
    also changes what it shows; the model can look again. A reference image is for the work it
    was attached to, and every request of that turn carries it; the user can attach it again.
    An image once dropped stays dropped, so the request prefix before it holds."""
    turn_start = next((i for i in range(len(messages) - 1, -1, -1) if is_turn_start(messages[i])), 0)
    batch: list[str] = []  # A step's captures come right after its results, consecutively.
    for message in messages[turn_start + 1:]:
        batch = batch + _image_refs(message) if is_capture(message) else []
    attached = [ref for m in messages[turn_start:] if m.get(ATTACHED) for ref in _image_refs(m)]
    return set(batch[-KEEP_IMAGES:]) | set(attached[-KEEP_ATTACHED:])


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
    chars = images = 0
    for message in messages:
        c, i = _size(message)
        chars, images = chars + c, images + i
    return FIXED_TOKENS + round(chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS


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


def view(messages: list, upto: int | None = None) -> list[dict]:
    """What is sent for messages[:upto]: the latest summary, then the messages after it with
    finished steps folded. Stored messages are returned as themselves, never copied or edited."""
    start, summary = window(messages)
    tail = fold(messages[start:upto], kept_images(messages))
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


def fold(messages: list, kept: set[str]) -> list[dict]:
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
        elif is_capture(message) and not any(ref in kept for ref in _image_refs(message)):
            ledger.append("- looked at the viewport")
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


def elide(messages: list) -> int:
    """Cut the results and code of steps the model has moved past, oldest first, in the part of
    the conversation a summary does not cover. Returns how many edits were made."""
    start, _ = window(messages)
    edits = 0
    results = [m for m in messages[start:] if m.get("role") == "tool" and ELIDED not in m
               and isinstance(m.get("content"), str)]
    for message in results[:-PROTECTED_RESULTS] if len(results) > PROTECTED_RESULTS else []:
        message[ELIDED] = len(message["content"])
        message["content"] = _stub(message["content"])
        edits += 1
    calls = [call for m in messages[start:] if m.get("role") == "assistant"
             for call in m.get("tool_calls") or [] if (call.get("function") or {}).get("name") == "run_python"]
    for call in calls[:-PROTECTED_CODE] if len(calls) > PROTECTED_CODE else []:
        edits += _elide_code(call)
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
    messages = session["messages"]
    budget, ratio = cfg.context_budget, session.get("token_ratio") or 1.0
    compacted = 0
    elide(messages)
    if estimate_tokens(view(messages)) * ratio > budget:
        cut = compaction_cut(messages, budget, ratio)
        if cut is not None:
            try:
                summary = summarize(cfg, view(messages, cut), is_cancelled)
            except llm.LLMError as ex:
                print(f"Loopcut: could not summarize the conversation, sending it as is: {ex}")
                summary = ""
            if summary:
                start, _ = window(messages)
                messages[cut][SUMMARY] = summary
                compacted = cut - start
    return view(messages), compacted
