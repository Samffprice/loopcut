"""The "Anthropic" provider's model calls: Loopcut's chat-completions messages and tools sent to Anthropic's
Messages API (POST https://api.anthropic.com/v1/messages) with the user's own key, streamed back into the
same llm.Completion / llm.ToolCall that llm.stream_chat returns for every other provider. The agent loop,
tools and UI do not know the difference; llm.stream_chat hands a request here when the base URL is
config.ANTHROPIC_BASE_URL (anthropic://api.anthropic.com/v1). Stdlib only, like llm.py.

The wire format (platform.claude.com/docs, checked 2026-10-10: api/messages, build-with-claude/streaming,
prompt-caching, thinking, preserved-thinking, vision, api/errors):
- Headers x-api-key, anthropic-version 2023-06-01, anthropic-beta for the betas below. max_tokens is required.
- The leading system messages are the top-level `system`; tools are {name, description, input_schema}.
- An assistant's tool calls are tool_use blocks; their results go back as tool_result blocks, first in the
  next user message, every call answered. Consecutive messages of one role are merged here (the API would
  merge them anyway), so a viewport capture after the results lands in the same user message.
- Images are base64 image blocks (png, jpeg, gif, webp; 10 MB of base64 each on the direct API).
- Current models reject a conversation that ends with an assistant message (no prefill).

Prompt caching: three cache_control breakpoints of the four allowed. One on the system prompt (the tools
render before it, so it caches both; on the last tool when there is no system prompt); one on the last
block of the request, which the next request reads back; and one on the end of the previous request's
last message, so a step that appends more than the API's 20-block lookback still finds the previous
entry. 5-minute TTL: an agent's steps are seconds to minutes apart, and each read renews it.

Thinking: adaptive thinking with output_config.effort from Loopcut's reasoning effort, on the models that
have it (Opus/Sonnet 4.6 and later, Haiku 5 and later, Fable and Mythos); none on older ones.
LOOPCUT_ANTHROPIC_THINKING=off turns it off where the model allows (Sonnet 5.5: between_tools; Haiku 5.5
and Opus 5: disabled up to high effort; Opus 5.5 and Fable cannot turn it off). Thinking blocks come back
with signatures and must be sent back unchanged: every reply's content blocks are kept in a process-wide
replay cache keyed by the reply's text and tool-call ids (as chatgpt_responses.py does for reasoning) and
replayed in place of the plain assistant message. A message the cache does not know (a resumed
conversation, a folded step) is sent without thinking.
- Loopcut rewrites history (context.py folds old steps), which invalidates later thinking blocks
  ("preserved thinking": a 400 on accounts created since 2026-08-31). Requests ask the API to drop such
  blocks instead (thinking.block_binding.prefix_mismatch_behavior "drop_block", beta
  thinking-binding-controls-2026-08-01); should a signature 400 come anyway, the request is sent once more
  with no thinking blocks at all.
- On Opus 5.5, Sonnet 5.5 and Fable the notes a model writes between tool calls come back as thinking
  blocks, empty by default; display "updates" (beta thinking-display-updates-2026-08-18) returns them as
  short text, which is shown and kept as the reply's text, as other providers' text is.
A beta or display setting the API refuses (HTTP 400 naming it) is dropped for the rest of the session and
the request sent again, once per feature.

Usage: input_tokens counts only the uncached rest, so Completion.usage["input"] adds the cache reads
("cached") and writes ("cache_write"), which pricing.py prices separately.
"""

import base64
import copy
import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Callable

from . import image_budget, llm

API_VERSION = "2023-06-01"
BINDING_BETA = "thinking-binding-controls-2026-08-01"
UPDATES_BETA = "thinking-display-updates-2026-08-18"
KEYS_URL = "https://platform.claude.com/settings/keys"
# The current models (platform.claude.com/docs/en/about-claude/models/overview, 2026-10-10), the default first.
MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1")
DEFAULT_MAX_TOKENS = 32_000          # As config.DEFAULT_MAX_OUTPUT_TOKENS; the API has no default.
MAX_OUTPUT_TOKENS = 128_000          # Every current model's ceiling (models overview).
MAX_IMAGE_BASE64 = 10_000_000        # 10 MB of base64 per image on the direct API (vision docs).
# Silent stretches while a model thinks: pings normally keep the stream alive, but a longer read
# timeout than llm.py's default costs nothing; Stop and call_max_seconds close the socket (_Watchdog).
DEFAULT_READ_TIMEOUT = 300.0
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CAPTURE_PLACEHOLDER = "[an image too large to send to Anthropic was left out]"
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_disabled: set[str] = set()   # Optional features ("binding", "updates") the API refused this session.
_wire_names: dict[str, str] = {}   # Sanitized tool name -> Loopcut's name, for a name the API would refuse.
# In-stream errors worth the same bounded retries as a broken stream.
_RETRY_IN_STREAM = {"overloaded_error", "api_error", "timeout_error"}


def read_timeout(timeout: float) -> float:
    try:
        return max(float(timeout or 0), float(os.environ.get("LOOPCUT_ANTHROPIC_READ_TIMEOUT") or DEFAULT_READ_TIMEOUT))
    except ValueError:
        return max(float(timeout or 0), DEFAULT_READ_TIMEOUT)


def api_base(base_url: str) -> str:
    """The https endpoint behind an anthropic:// base URL. LOOPCUT_ANTHROPIC_API_URL replaces it for a
    test or fake server; only 127.0.0.1 is accepted, so a stray variable can never send the key elsewhere."""
    override = os.environ.get("LOOPCUT_ANTHROPIC_API_URL", "").strip().rstrip("/")
    if override:
        if not override.startswith("http://127.0.0.1:"):
            raise llm.LLMError("LOOPCUT_ANTHROPIC_API_URL may only point at http://127.0.0.1 (a local test server)")
        return override
    rest = (base_url or "").split("://", 1)[-1].strip().rstrip("/")
    return "https://" + (rest or "api.anthropic.com/v1")


def _host(base_url: str) -> str:
    return api_base(base_url).split("://", 1)[-1].split("/", 1)[0]


# --- Replay cache: thinking blocks across steps --------------------------------------------------

class _Replay:
    """A reply's content blocks, keyed by what the loop stores of it (text and tool-call ids)."""

    def __init__(self, limit: int = 4096):
        self._items: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.limit = limit

    @staticmethod
    def key(text: str, call_ids: list[str]) -> str:
        return hashlib.sha256(json.dumps([text or "", list(call_ids)]).encode("utf-8")).hexdigest()

    def put(self, text: str, call_ids: list[str], blocks: list[dict]) -> None:
        key = self.key(text, call_ids)
        with self._lock:
            self._items[key] = blocks
            self._items.move_to_end(key)
            while len(self._items) > self.limit:
                self._items.popitem(last=False)

    def get(self, text: str, call_ids: list[str]) -> list[dict] | None:
        with self._lock:
            found = self._items.get(self.key(text, call_ids))
        return copy.deepcopy(found) if found is not None else None

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


replay = _Replay()


# --- Models and thinking ---------------------------------------------------------------------------

_GENERATION = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?=$|[-@\[])")


def generation(model: str) -> tuple[str, tuple[int, int]] | None:
    """("opus", (5, 5)) for claude-opus-5-5 (dated and suffixed ids too); None for an id of another shape."""
    found = _GENERATION.match((model or "").strip().lower())
    if not found:
        return None
    return found.group(1), (int(found.group(2)), int(found.group(3) or 0))


def adaptive(model: str) -> bool:
    """Models with adaptive thinking and output_config.effort: Opus and Sonnet 4.6 on, Haiku 5 on, Fable, Mythos."""
    found = generation(model)
    if found is None:
        return False
    family, version = found
    return family in ("fable", "mythos") or (family == "haiku" and version >= (5, 0)) or version >= (4, 6)


def _updates(model: str) -> bool:
    """Where the notes between tool calls come back as thinking blocks (display "updates")."""
    found = generation(model)
    if found is None:
        return False
    family, version = found
    return family in ("fable", "mythos") or (family in ("opus", "sonnet") and version >= (5, 5))


def _effort(model: str, reasoning_effort: str) -> str:
    effort = (reasoning_effort or "").strip().lower()
    effort = {"minimal": "low", "none": "low"}.get(effort, effort)
    if effort not in EFFORTS:
        return ""
    if effort == "xhigh" and generation(model)[1] < (4, 7):  # xhigh arrived with Opus 4.7.
        return "high"
    return effort


def thinking_settings(model: str, reasoning_effort: str, disabled: set | None = None) -> tuple[dict, list[str]]:
    """(the request's thinking, output_config fields, the betas they need). Nothing on a model without
    adaptive thinking."""
    disabled = _disabled if disabled is None else disabled
    if not adaptive(model):
        return {}, []
    effort = _effort(model, reasoning_effort)
    fields: dict = {"output_config": {"effort": effort}} if effort else {}
    family, version = generation(model)
    if os.environ.get("LOOPCUT_ANTHROPIC_THINKING", "").strip().lower() == "off":
        low_enough = effort in ("", "low", "medium", "high")
        if family == "sonnet" and version >= (5, 5):
            if low_enough:
                return {**fields, "thinking": {"type": "between_tools"}}, []
        elif family == "haiku" or (family == "opus" and version == (5, 0)):
            if low_enough:
                return {**fields, "thinking": {"type": "disabled"}}, []
        elif family == "opus" and version < (5, 0) or family == "sonnet":
            return {**fields, "thinking": {"type": "disabled"}}, []
        # Opus 5.5 and later, Fable and Mythos think whatever is asked: effort is the only control.
    thinking: dict = {"type": "adaptive"}
    betas = []
    if _updates(model) and "updates" not in disabled:
        thinking["display"] = "updates"
        betas.append(UPDATES_BETA)
    if "binding" not in disabled:
        thinking["block_binding"] = {"prefix_mismatch_behavior": "drop_block"}
        betas.append(BINDING_BETA)
    return {**fields, "thinking": thinking}, betas


# --- Chat messages to Messages API messages --------------------------------------------------------

def wire_name(name: str) -> str:
    """A tool name the API accepts (letters, digits, _ and -, at most 64); Loopcut's own names already are."""
    if _TOOL_NAME.match(name or ""):
        return name
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name or "tool")[:55] + "_" + hashlib.sha1((name or "").encode()).hexdigest()[:8]
    _wire_names[safe] = name
    return safe


def loopcut_name(name: str) -> str:
    return _wire_names.get(name, name)


def to_tools(tools: list[dict]) -> list[dict]:
    out = []
    for tool in tools or []:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        entry = {"name": wire_name(function["name"])}
        if function.get("description"):
            entry["description"] = function["description"]
        entry["input_schema"] = function.get("parameters") or {"type": "object", "properties": {}}
        out.append(entry)
    return out


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _sniff(data: str) -> str:
    """The image type from its first bytes: a stored capture is labelled PNG whatever it holds."""
    try:
        head = base64.b64decode(data[:24] + "=" * (-len(data[:24]) % 4))
    except ValueError:
        return ""
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _image(url: str) -> dict:
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media = _sniff(data) or header[5:].split(";", 1)[0]
        if ";base64" not in header or media not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            return {"type": "text", "text": "[an image in a format Anthropic does not take was left out]"}
        if len(data) > MAX_IMAGE_BASE64:
            return {"type": "text", "text": _CAPTURE_PLACEHOLDER}
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _parts(content) -> list[dict]:
    """User or tool content as text and image blocks, without the empty text blocks the API refuses."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    blocks = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text") and str(part.get("text") or "").strip():
            blocks.append({"type": "text", "text": part["text"]})
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url:
                blocks.append(_image(url))
        elif kind == "image" and isinstance(part.get("source"), dict):
            blocks.append(dict(part))
    return blocks


def _arguments(text: str) -> dict:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {"unparsed_arguments": text}   # Cut short or invalid: the call's result already says so.
    return value if isinstance(value, dict) else {"value": value}


def _assistant_blocks(message: dict, use_replay: bool) -> list[dict]:
    text = _text_of(message.get("content"))
    calls = [c for c in message.get("tool_calls") or [] if isinstance(c, dict)]
    call_ids = [str(c.get("id") or "") for c in calls]
    cached = replay.get(text, call_ids) if use_replay and (text or call_ids) else None
    if cached is not None:
        return cached
    blocks = [{"type": "text", "text": text}] if text.strip() else []
    for call in calls:
        function = call.get("function") or {}
        blocks.append({"type": "tool_use", "id": str(call.get("id") or ""),
                       "name": wire_name(str(function.get("name") or "")),
                       "input": _arguments(function.get("arguments") or "")})
    return blocks


def _thinking(block: dict) -> bool:
    return block.get("type") in ("thinking", "redacted_thinking")


def _add(out: list[dict], role: str, blocks: list[dict]) -> None:
    """Append, merging into the previous message of the same role. Thinking blocks can only open an
    assistant message, and one merged behind other content comes from a rewritten history anyway
    (context.py's folded records), so it is dropped."""
    if not blocks:
        return
    if out and out[-1]["role"] == role:
        if role == "assistant":
            blocks = [b for b in blocks if not _thinking(b)]
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": list(blocks)})


def _pair_tool_results(out: list[dict]) -> list[dict]:
    """Every tool_use answered by a tool_result first in the next user message, and no result without
    its call: the API refuses either. A call with no result gets a note; a stray result becomes text."""
    fixed: list[dict] = []
    for n, message in enumerate(out):
        if message["role"] == "user":
            previous = fixed[-1] if fixed and fixed[-1]["role"] == "assistant" else None
            ids = [b["id"] for b in previous["content"] if b.get("type") == "tool_use"] if previous else []
            results, others = [], []
            for block in message["content"]:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in ids \
                        and block["tool_use_id"] not in {r["tool_use_id"] for r in results}:
                    results.append(block)
                elif block.get("type") == "tool_result":
                    others.append({"type": "text", "text": "Result of an earlier call: " + _text_of(block.get("content"))
                                   if isinstance(block.get("content"), (str, list)) else "Result of an earlier call."})
                else:
                    others.append(block)
            answered = {r["tool_use_id"] for r in results}
            results += [{"type": "tool_result", "tool_use_id": i, "content": "No result was recorded for this call."}
                        for i in ids if i not in answered]
            results.sort(key=lambda r: ids.index(r["tool_use_id"]))
            content = results + [b for b in others if b.get("type") != "text" or str(b.get("text") or "").strip()]
            if content:
                fixed.append({"role": "user", "content": content})
            continue
        if fixed and fixed[-1]["role"] == "assistant":   # Two assistant turns: the first's calls go unanswered.
            _close(fixed)
        fixed.append(message)
    if fixed and fixed[-1]["role"] == "assistant":
        _close(fixed)
    return fixed


def _close(fixed: list[dict]) -> None:
    ids = [b["id"] for b in fixed[-1]["content"] if b.get("type") == "tool_use"]
    if ids:
        fixed.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": i, "content": "Not run."} for i in ids]})


def to_messages(messages: list[dict], use_replay: bool = True, strip_thinking: bool = False) -> tuple[str, list[dict]]:
    """(system prompt, Messages API messages). The system messages before the first other message are
    the system prompt; a later one is passed on as user text."""
    system, out = [], []
    leading = True
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            text = _text_of(message.get("content"))
            if leading:
                system.append(text)
            elif text.strip():
                _add(out, "user", [{"type": "text", "text": f"<system>\n{text}\n</system>"}])
            continue
        leading = False
        if role == "assistant":
            blocks = _assistant_blocks(message, use_replay and not strip_thinking)
            if strip_thinking:
                blocks = [b for b in blocks if not _thinking(b)]
            _add(out, "assistant", blocks)
        elif role == "tool":
            content = message.get("content")
            result = {"type": "tool_result", "tool_use_id": str(message.get("tool_call_id") or "")}
            if isinstance(content, str):
                result["content"] = content if content.strip() else "(no output)"
            else:
                result["content"] = _parts(content) or "(no output)"
            if message.get("is_error"):
                result["is_error"] = True
            _add(out, "user", [result])
        elif role == "user":
            _add(out, "user", _parts(message.get("content")) or [{"type": "text", "text": "(empty message)"}])
    out = _pair_tool_results(out)
    if out and out[-1]["role"] == "assistant":   # No prefill on current models: the request ends with the user.
        out.append({"role": "user", "content": [{"type": "text", "text": "Continue."}]})
    return "\n\n".join(t for t in system if t), out


def _mark(block: dict) -> None:
    block["cache_control"] = {"type": "ephemeral"}


def request_body(*, model: str, messages: list[dict], tools: list[dict], reasoning_effort: str,
                 max_tokens: int | None = None, strip_thinking: bool = False,
                 disabled: set | None = None) -> tuple[dict, list[str]]:
    """(the request body, the anthropic-beta values it needs)."""
    system, wired = to_messages(messages, strip_thinking=strip_thinking)
    body: dict = {"model": model, "max_tokens": min(int(max_tokens or DEFAULT_MAX_TOKENS), MAX_OUTPUT_TOKENS)
                  if adaptive(model) else int(max_tokens or DEFAULT_MAX_TOKENS)}
    if system:
        body["system"] = [{"type": "text", "text": system}]
        _mark(body["system"][-1])
    definitions = to_tools(tools)
    if definitions:
        body["tools"] = definitions
        if not system:
            _mark(definitions[-1])
    # The rolling breakpoint at the end, and the previous request's (its last user message) before the
    # newest assistant turn. Only user blocks are marked: never a replayed thinking block.
    if wired:
        _mark(wired[-1]["content"][-1])
        last_assistant = max((n for n, m in enumerate(wired) if m["role"] == "assistant"), default=-1)
        if last_assistant > 0 and wired[last_assistant - 1]["role"] == "user":
            _mark(wired[last_assistant - 1]["content"][-1])
    body["messages"] = wired
    settings, betas = thinking_settings(model, reasoning_effort, disabled)
    body.update(settings)
    body["stream"] = True
    return body, betas


# --- Errors ----------------------------------------------------------------------------------------

def _request_id(headers) -> str:
    return (headers.get("request-id", "") if headers else "") or ""


def _error_from(status: int | None, body: bytes, headers, model: str = "") -> llm.LLMError:
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
    request_id = _request_id(headers) or str(payload.get("request_id") or "" if isinstance(payload, dict) else "")
    detail = str(error.get("message") or body.decode("utf-8", "replace")[:300] or "")
    return _error(status, str(error.get("type") or ""), detail, error, request_id, model)


def _spend_limit(error: dict) -> bool:
    return (error.get("details") or {}).get("error_code") == "enforced_spend_limit_reached" \
        if isinstance(error.get("details"), dict) else False


def _error(status: int | None, kind: str, detail: str, error: dict, request_id: str, model: str = "") -> llm.LLMError:
    http = f" (HTTP {status})" if status else ""   # None for an error event inside a stream.
    if status == 401 or kind == "authentication_error":
        friendly = (f"Anthropic did not accept the API key{http}. Check the key in Preferences > Add-ons > "
                    f"Loopcut, or create one at {KEYS_URL}.")
    elif status == 403 or kind == "permission_error":
        friendly = f"Anthropic refused this key's request{http}: {detail}"
    elif status == 404 or kind == "not_found_error":
        friendly = f"Anthropic has no model {model!r} for this key{http}: {detail} Pick another model."
    elif status == 402 or kind == "billing_error":
        friendly = f"Anthropic billing{http}: {detail}"
    elif status == 413 or kind == "request_too_large":
        friendly = f"The request is too large for Anthropic{http}: {detail} Lower the context budget."
    elif _spend_limit(error):
        friendly = f"Your Anthropic spend limit is reached{http}: {detail}"
    elif status == 429 or kind == "rate_limit_error":
        friendly = f"Anthropic's rate limit for this key is reached{http}: {detail}"
    elif status == 529 or kind == "overloaded_error":
        friendly = f"Anthropic is overloaded just now{http}: {detail} Try again in a little while."
    else:
        friendly = f"{'HTTP ' + str(status) + ': ' if status else ''}{detail or kind or 'Anthropic request failed'}"
    if request_id:
        friendly += f" (request {request_id})"
    return llm.LLMError(friendly, status, kind, {**error, "request_id": request_id, "message": friendly})


def _feature_for(error: llm.LLMError) -> str | None:
    """Which optional feature a 400 refuses, if any."""
    text = str(error.details.get("message") or error).lower()
    if "block_binding" in text or BINDING_BETA in text:
        return "binding"
    if "display" in text or UPDATES_BETA in text:
        return "updates"
    if "anthropic-beta" in text or "beta" in text and "header" in text:
        return "binding"   # An unnamed beta refused: the binding first, then (next time) the display.
    return None


def _signature_error(error: llm.LLMError) -> bool:
    text = str(error.details.get("message") or error).lower()
    return "signature" in text and ("thinking" in text or "bound to a different conversation" in text)


# --- Streaming --------------------------------------------------------------------------------------

class _Interrupted(Exception):
    def __init__(self, streamed: bool):
        super().__init__(streamed)
        self.streamed = streamed


class _Watchdog:
    """Closes the stream's socket when the turn is cancelled or the call's wall clock runs out, so a read
    blocked on a model thinking in silence returns at once."""

    def __init__(self, response, is_cancelled: Callable[[], bool], deadline: float | None):
        self.response, self.is_cancelled, self.deadline = response, is_cancelled, deadline
        self.fired = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loopcut-anthropic-watchdog", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(0.2):
            if self.is_cancelled():
                self.fired = "cancelled"
            elif self.deadline is not None and time.monotonic() > self.deadline:
                self.fired = "deadline"
            if self.fired:
                try:
                    sock = getattr(getattr(getattr(self.response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.shutdown(socket.SHUT_RDWR)
                    else:
                        self.response.close()
                except OSError:
                    pass
                return

    def stop(self):
        self._stop.set()


def _events(response):
    """(event type, payload) per server-sent event; the payload's own type names it."""
    data_lines = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                try:
                    payload = json.loads(data)
                except ValueError as ex:
                    raise llm.LLMError(f"Malformed stream event: {data[:200]}") from ex
                if isinstance(payload, dict):
                    yield str(payload.get("type") or ""), payload
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


def _usage(raw: dict) -> dict | None:
    if not raw:
        return None
    fresh = int(raw.get("input_tokens") or 0)
    written = int(raw.get("cache_creation_input_tokens") or 0)
    read = int(raw.get("cache_read_input_tokens") or 0)
    usage = {"input": fresh + written + read, "cached": read, "cache_write": written,
             "output": int(raw.get("output_tokens") or 0)}
    thinking = (raw.get("output_tokens_details") or {}).get("thinking_tokens") if isinstance(
        raw.get("output_tokens_details"), dict) else None
    if thinking is not None:
        usage["reasoning"] = int(thinking)
    return usage


_FINISH = {"tool_use": "tool_calls", "end_turn": "stop", "stop_sequence": "stop", "pause_turn": "stop",
           "max_tokens": "length", "model_context_window_exceeded": "length", "refusal": "stop"}


def _read(response, on_text, is_cancelled, deadline, meta: dict) -> llm.Completion:
    completion = llm.Completion()
    blocks: dict[int, dict] = {}
    order: list[int] = []
    partial: dict[int, list[str]] = {}
    done: set[int] = set()
    usage: dict = {}
    stop_reason, stop_details = None, None
    streamed, finished = False, False
    shown = {"last": None}   # The block whose text was shown last, to put a blank line before the next one.
    error: llm.LLMError | None = None

    def show(index: int, text: str) -> None:
        nonlocal streamed
        if not text:
            return
        if shown["last"] is not None and shown["last"] != index:
            on_text("\n\n")
        shown["last"] = index
        streamed = True
        on_text(text)

    watchdog = _Watchdog(response, is_cancelled, deadline)
    try:
        with response:
            for kind, event in _events(response):
                if is_cancelled():
                    raise llm.Cancelled()
                if kind == "message_start":
                    usage.update({k: v for k, v in ((event.get("message") or {}).get("usage") or {}).items()
                                  if v is not None})
                elif kind == "content_block_start":
                    index = int(event.get("index") or 0)
                    block = dict(event.get("content_block") or {})
                    blocks[index] = block
                    if index not in order:
                        order.append(index)
                    if block.get("type") == "tool_use":
                        streamed = True
                    show(index, block.get("text") or block.get("thinking") or "")
                elif kind == "content_block_delta":
                    index = int(event.get("index") or 0)
                    block = blocks.setdefault(index, {})
                    delta = event.get("delta") or {}
                    change = delta.get("type")
                    if change == "text_delta":
                        block["text"] = block.get("text", "") + str(delta.get("text") or "")
                        show(index, str(delta.get("text") or ""))
                    elif change == "input_json_delta":
                        partial.setdefault(index, []).append(str(delta.get("partial_json") or ""))
                        streamed = True
                    elif change == "thinking_delta":   # Only the notes between tool calls (display "updates").
                        block["thinking"] = block.get("thinking", "") + str(delta.get("thinking") or "")
                        show(index, str(delta.get("thinking") or ""))
                    elif change == "signature_delta":
                        block["signature"] = block.get("signature", "") + str(delta.get("signature") or "")
                elif kind == "content_block_stop":
                    done.add(int(event.get("index") or 0))
                elif kind == "message_delta":
                    delta = event.get("delta") or {}
                    stop_reason = delta.get("stop_reason") or stop_reason
                    stop_details = delta.get("stop_details") or stop_details
                    usage.update({k: v for k, v in (event.get("usage") or {}).items() if v is not None})
                elif kind == "message_stop":
                    finished = True
                    break
                elif kind == "error":
                    raw = event.get("error") or {}
                    error = _error(None, str(raw.get("type") or ""), str(raw.get("message") or "stream error"),
                                   raw if isinstance(raw, dict) else {}, meta.get("request_id", ""), meta.get("model", ""))
                    break
    except (TimeoutError, ConnectionError, OSError, http.client.HTTPException) as ex:
        if watchdog.fired == "cancelled" or is_cancelled():
            raise llm.Cancelled() from None
        if watchdog.fired != "deadline":
            raise _Interrupted(streamed) from ex
    finally:
        watchdog.stop()
    if watchdog.fired == "cancelled" or is_cancelled():
        raise llm.Cancelled()
    if error is not None:
        if error.code in _RETRY_IN_STREAM:
            raise _Interrupted(streamed) from error
        raise error
    timed_out = watchdog.fired == "deadline" and not finished
    if not finished and not timed_out:
        raise _Interrupted(streamed)

    texts, wire = [], []
    for index in order:
        block = blocks.get(index) or {}
        kind = block.get("type")
        if kind in ("text", "thinking"):
            text = block.get("text" if kind == "text" else "thinking") or ""
            if text:
                texts.append(text)
            if kind == "text" and text.strip():
                wire.append({"type": "text", "text": text})
            elif kind == "thinking" and block.get("signature"):
                wire.append({"type": "thinking", "thinking": block.get("thinking") or "", "signature": block["signature"]})
        elif kind == "redacted_thinking" and block.get("data"):
            wire.append({"type": "redacted_thinking", "data": block["data"]})
        elif kind == "tool_use" and index in done:
            arguments = "".join(partial.get(index, [])) or json.dumps(block.get("input") or {})
            completion.tool_calls.append(llm.ToolCall(id=str(block.get("id") or ""),
                                                      name=loopcut_name(str(block.get("name") or "")),
                                                      arguments=arguments))
            wire.append({"type": "tool_use", "id": str(block.get("id") or ""), "name": str(block.get("name") or ""),
                         "input": _arguments(arguments)})
    completion.text = "\n\n".join(texts)
    completion.usage = _usage(usage)
    if timed_out:
        completion.finish_reason, completion.timed_out = "length", True
        return completion
    completion.finish_reason = _FINISH.get(stop_reason or "", "stop")
    if stop_reason == "refusal":   # Declined by Anthropic's safety classifiers: nothing of it is acted on.
        category = (stop_details or {}).get("category") if isinstance(stop_details, dict) else None
        note = "Claude declined to continue this request" + (f" ({category})" if category else "") + "."
        on_text(("\n\n" if completion.text else "") + note)
        completion.text = (completion.text + "\n\n" if completion.text else "") + note
        completion.tool_calls = []
        return completion
    if stop_reason in ("tool_use", "end_turn", "stop_sequence") and wire \
            and all("unparsed_arguments" not in b.get("input", {}) for b in wire if b["type"] == "tool_use"):
        replay.put(completion.text, [c.id for c in completion.tool_calls], wire)
    return completion


def _post(url: str, body: dict, betas: list[str], api_key: str, timeout: float):
    headers = {"x-api-key": api_key, "anthropic-version": API_VERSION, "Content-Type": "application/json",
               "Accept": "text/event-stream"}
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    request = urllib.request.Request(url + "/messages", data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)


def _open(build: Callable[[], tuple[dict, list[str]]], url: str, api_key: str, timeout: float, host: str,
          model: str, state: dict, is_cancelled: Callable[[], bool], on_drop: Callable[[], bool] = lambda: False):
    """Open the stream: retries for 429 (honouring retry-after, capped), 529 and 5xx; one retry per
    optional feature a 400 refuses; one without thinking blocks after a signature 400; smaller images
    after a dropped connection (on_drop is image_budget.Sized.tighten)."""
    attempt, refused = 0, set()
    while True:
        body, betas = build()
        try:
            return _post(url, body, betas, api_key, timeout)
        except urllib.error.HTTPError as ex:
            with ex:
                raw = ex.read(200_000)
                headers = ex.headers
            error = _error_from(ex.code, raw, headers, model)
            if ex.code == 400:
                if _signature_error(error):   # Its text names the binding beta too: checked first.
                    if not state.get("strip"):
                        state["strip"] = True
                        print(f"Loopcut: Anthropic refused replayed thinking; sending the request without it ({error})")
                        continue
                else:
                    feature = _feature_for(error)
                    if feature and feature not in refused and feature not in _disabled:
                        refused.add(feature)
                        _disabled.add(feature)
                        print(f"Loopcut: Anthropic refused the request's {feature}; sending it without ({error})")
                        continue
            if _spend_limit(error.details) or ex.code not in llm.RETRY_STATUSES or attempt >= len(llm.RETRY_DELAYS):
                raise error from None
            delay = llm.RETRY_DELAYS[attempt]
            retry_after = (headers.get("retry-after", "") if headers else "") or ""
            if str(retry_after).replace(".", "", 1).isdigit():
                delay = min(max(delay, float(retry_after)), llm.MAX_RETRY_AFTER)
            print(f"Loopcut: retrying the Anthropic request after HTTP {ex.code} "
                  f"({attempt + 1}/{len(llm.RETRY_DELAYS)}, waiting {delay:g}s)")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as ex:
            if attempt >= len(llm.RETRY_DELAYS):
                raise llm.LLMError(f"Could not reach {host}: {getattr(ex, 'reason', ex)}", code=llm.CONNECTION_LOST) from None
            on_drop()
            delay = llm.RETRY_DELAYS[attempt]
            print(f"Loopcut: retrying the Anthropic request after {getattr(ex, 'reason', ex)!r} "
                  f"({attempt + 1}/{len(llm.RETRY_DELAYS)}, waiting {delay:g}s)")
        attempt += 1
        llm._wait(delay, is_cancelled)


def stream(*, base_url: str, api_key: str, model: str, messages: list[dict], tools: list[dict],
           reasoning_effort: str, on_text: Callable[[str], None], is_cancelled: Callable[[], bool],
           timeout: float = 120.0, max_tokens: int | None = None, call_max_seconds: float | None = None,
           headers: dict | None = None, on_retry: Callable[[], None] | None = None) -> llm.Completion:
    """llm.stream_chat for Anthropic: same arguments, same Completion, same retry rules for a stream that
    breaks (before any text: capped backoff; after some: once, from scratch, only with on_retry).
    `headers` (Loopcut's x-loopcut-* correlation headers) are for Loopcut's gateway and never sent here."""
    url, host = api_base(base_url), _host(base_url)
    deadline = time.monotonic() + call_max_seconds if call_max_seconds else None
    state = {"strip": False}
    before_content, retried_partial = 0, False
    sized = image_budget.Sized(messages)

    def build() -> tuple[dict, list[str]]:
        return request_body(model=model, messages=sized.messages(), tools=tools, reasoning_effort=reasoning_effort,
                            max_tokens=max_tokens, strip_thinking=state["strip"])

    while True:
        response = _open(build, url, api_key, read_timeout(timeout), host, model, state, is_cancelled, sized.tighten)
        meta = {"model": model, "request_id": _request_id(response.headers)}
        try:
            return _read(response, on_text, is_cancelled, deadline, meta)
        except _Interrupted as ex:
            cause = ex.__cause__ or "the stream ended before message_stop"
            if ex.streamed:
                if retried_partial or on_retry is None:
                    raise llm.LLMError(f"Connection lost while streaming the reply: {cause!r}",
                                       code=llm.CONNECTION_LOST) from ex.__cause__
                retried_partial = True
                print(f"Loopcut: retrying the Anthropic request once from scratch after the stream broke ({cause!r})")
                on_retry()
                continue
            if before_content == len(llm.RETRY_DELAYS):
                raise llm.LLMError(f"Connection lost before any reply arrived: {cause!r}",
                                   code=llm.CONNECTION_LOST) from ex.__cause__
            delay = llm.RETRY_DELAYS[before_content]
            print(f"Loopcut: retrying the Anthropic request after {cause!r} "
                  f"({before_content + 1}/{len(llm.RETRY_DELAYS)}, waiting {delay:g}s)")
            before_content += 1
            llm._wait(delay, is_cancelled)


def list_models(base_url: str, api_key: str, timeout: float = 10.0) -> list[str]:
    """The model ids this key can use, newest first (GET /v1/models). Raises urllib's errors."""
    request = urllib.request.Request(api_base(base_url) + "/models?limit=1000", headers={
        "x-api-key": api_key, "anthropic-version": API_VERSION, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read(4_000_000))
    rows = payload.get("data") if isinstance(payload, dict) else None
    return [str(r["id"]) for r in rows or [] if isinstance(r, dict) and r.get("id")]
