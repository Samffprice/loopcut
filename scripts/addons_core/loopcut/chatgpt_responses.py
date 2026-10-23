"""The "ChatGPT plan" provider's model calls: Loopcut's chat-completions messages and tools sent to
OpenAI's Responses API on the signed-in user's ChatGPT plan (chatgpt_auth.py), streamed back into
the same llm.Completion / llm.ToolCall that llm.stream_chat returns for every other provider. The
agent loop, tools and UI do not know the difference; llm.stream_chat hands a request here when the
base URL is chatgpt_auth.CHATGPT_PLAN_BASE_URL.

What the SIWC preview requires (developers.openai.com/siwc/token-sharing-open-source/preview-limitations):
- POST https://api.openai.com/v1/responses with the account's bearer token, store:false, stream:true,
  the whole history in `input` every time (no previous_response_id over HTTP).
- No system-role items: the system prompt is `instructions`; a later system message is a developer one.
- None of temperature, top_p, max_output_tokens, metadata, prompt, user, truncation, ... (so Loopcut's
  max_tokens is not sent; the agent's own retry on a capped reply simply never triggers here).
- Function tools grouped in a namespace: Loopcut's tools are sent as one namespace, NAMESPACE, with
  strict:false (the chat-completions path is non-strict too); calls come back as name + namespace and
  are mapped back to Loopcut's bare tool names.
- Success only on response.completed; usage limits can arrive mid-stream as response.failed.

Quality details for stateless (store:false) tool loops:
- Reasoning carries across steps: every output item of a reply (encrypted reasoning, assistant
  messages with their phase, function calls) is kept in a process-wide replay cache keyed by the
  reply's text and call ids, and replayed verbatim in place of the plain assistant message when that
  message comes back in a later request's history. A message the cache does not know (a resumed
  conversation, a folded step) is sent as plain items, without reasoning.
- Images in tool results: a viewport capture that the loop sends as a user message right after
  tool results ("Viewport capture: ...") is put into the last function_call_output instead, as the
  Responses API allows images in tool outputs.
- Images use high detail by default, as Codex does: references and final renders must retain
  the fine marks the model is comparing. An explicit detail setting is kept.
- prompt_cache_key from the conversation and role, so the long shared prefix stays cached.
Optional fields the route turns out to refuse (HTTP 400 naming them) are dropped for the rest of the
session and the request is sent again, once per field.

LOOPCUT_CHATGPT_LOG_DIR: a folder to keep a transcript in (evals): every request body (later
requests as an extension of the previous one when they extend it, images saved once as files,
encrypted reasoning shortened to its length) and every reply's events, output items, usage, status
and response headers. Never the Authorization header or any token.
"""

import base64
import hashlib
import http.client
import json
import os
import socket
import threading
import time
import urllib.error
import uuid
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from . import chatgpt_auth, chatgpt_ready, cloud, context, image_budget, llm

NAMESPACE = "loopcut"
NAMESPACE_DESCRIPTION = ("Loopcut's tools. They act on the user's open Blender scene and, when one is set up, "
                         "the workspace folder.")
CAPTURE_PREFIXES = (context.CAPTURE_LABEL, context.CAPTURE_TEXT, context.FILE_PREVIEW_LABEL)
DEFAULT_READ_TIMEOUT = 600.0  # xhigh reasoning can be silent for minutes; cancel is handled by a watchdog.
_OPTIONAL = ("prompt_cache_key", "reasoning_summary", "include", "tool_images", "replay_ids", "replay")
_disabled: set[str] = set()   # Optional features the route refused this session.
_NO_RETRY = {
    "subscription_sharing_usage_limit_exceeded", "subscription_sharing_user_not_eligible",
    "subscription_sharing_unsupported_capability", "subscription_sharing_route_not_supported",
    "subscription_sharing_invalid_user", "chatpass_v2_scope_not_authorized",
    "chatpass_v2_invalid_authorization_context",
}
# Failures inside a stream that are worth the same bounded retries as a broken stream (the docs: retry
# usage_unavailable and user_unavailable with backoff; server_error is the API's own transient one).
_RETRY_IN_STREAM = {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable", "server_error"}
_MESSAGES = {
    "subscription_sharing_usage_limit_exceeded":
        "Your ChatGPT plan's usage limit for Loopcut is reached (your plan's limit, or the weekly limit set for "
        f"Loopcut). New requests are paused. Manage usage: {chatgpt_auth.USAGE_URL}",
    "subscription_sharing_user_not_eligible":
        "ChatGPT says this account can't share its plan with Loopcut. OpenAI decides that by plan, workspace "
        "and country. Pick another provider.",
    "subscription_sharing_invalid_user":
        "ChatGPT could not validate this sign-in. Sign in with ChatGPT again.",
    "subscription_sharing_usage_unavailable":
        "ChatGPT could not check your plan's usage just now. Try again in a little while.",
    "subscription_sharing_user_unavailable":
        "ChatGPT could not read your account just now. Try again in a little while.",
}


def read_timeout() -> float:
    try:
        return max(30.0, float(os.environ.get("LOOPCUT_CHATGPT_READ_TIMEOUT") or DEFAULT_READ_TIMEOUT))
    except ValueError:
        return DEFAULT_READ_TIMEOUT


# --- Replay cache: reasoning and phases across steps ---------------------------------------------

class _Replay:
    """A reply's output items, keyed by what the loop stores of it (text and tool-call ids)."""

    def __init__(self, limit: int = 4096):
        self._items: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.limit = limit

    @staticmethod
    def key(text: str, call_ids: list[str]) -> str:
        return hashlib.sha256(json.dumps([text or "", list(call_ids)]).encode("utf-8")).hexdigest()

    def put(self, text: str, call_ids: list[str], items: list[dict]) -> None:
        with self._lock:
            self._items[self.key(text, call_ids)] = items
            self._items.move_to_end(self.key(text, call_ids))
            while len(self._items) > self.limit:
                self._items.popitem(last=False)

    def get(self, text: str, call_ids: list[str]) -> list[dict] | None:
        with self._lock:
            return self._items.get(self.key(text, call_ids))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


replay = _Replay()


# --- Chat messages to Responses input -------------------------------------------------------------

def bare_name(name: str, namespace: str | None = None) -> str:
    """A call's tool name as Loopcut knows it: without the namespace, however it is attached."""
    bare = cloud.tool_name(name)
    if bare is not None:
        return cloud.PREFIX + bare
    if namespace == cloud.NAMESPACE:
        return cloud.PREFIX + name
    for prefix in ((namespace,) if namespace else ()) + (NAMESPACE,):
        for separator in (".", "__", "/", ":"):
            if name.startswith(prefix + separator):
                return name[len(prefix) + len(separator):]
    return name


def _wire_name(name):
    bare = cloud.tool_name(name)
    return (cloud.NAMESPACE, bare) if bare is not None else (NAMESPACE, bare_name(name))


def to_tools(tools: list[dict]) -> list[dict]:
    groups = {NAMESPACE: [], cloud.NAMESPACE: []}
    for tool in tools or []:
        function = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        namespace, name = _wire_name(function["name"])
        entry = {"type": "function", "name": name, "strict": False}
        if function.get("description"):
            entry["description"] = function["description"]
        entry["parameters"] = function.get("parameters") or {"type": "object", "properties": {}}
        groups[namespace].append(entry)
    return [{"type": "namespace", "name": namespace,
             "description": NAMESPACE_DESCRIPTION if namespace == NAMESPACE else "Loopcut's available cloud and BYOK services.",
             "tools": functions} for namespace, functions in groups.items() if functions]


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("text", "output_text"))
    return ""


def _input_parts(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}] if content else []
    parts = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": part.get("text", "")})
        elif kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url:
                image = {"type": "input_image", "image_url": url,
                         "detail": (part.get("image_url") or {}).get("detail") or "high"}
                parts.append(image)
        elif kind == "input_image":
            parts.append({**part, "detail": part.get("detail") or "high"})
        elif kind == "input_text":
            parts.append(part)
    return parts


def _is_capture(message: dict) -> bool:
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list) or not content:
        return False
    first = content[0] if isinstance(content[0], dict) else {}
    return first.get("type") == "text" and str(first.get("text", "")).startswith(CAPTURE_PREFIXES)


def _assistant_items(message: dict, use_replay: bool) -> list[dict]:
    text = _text_of(message.get("content"))
    calls = [c for c in message.get("tool_calls") or [] if isinstance(c, dict)]
    call_ids = [str(c.get("id") or "") for c in calls]
    cached = replay.get(text, call_ids) if use_replay else None
    if cached is not None:
        if "replay_ids" in _disabled:
            return [{k: v for k, v in item.items() if k != "id"} for item in cached]
        return [dict(item) for item in cached]
    items = []
    if text:
        items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    for call in calls:
        function = call.get("function") or {}
        namespace, name = _wire_name(str(function.get("name") or ""))
        items.append({"type": "function_call", "call_id": str(call.get("id") or ""),
                      "name": name, "namespace": namespace,
                      "arguments": function.get("arguments") or "{}"})
    return items


def to_input(messages: list[dict], tool_images: bool = True, use_replay: bool = True) -> tuple[str, list[dict]]:
    """(instructions, input items). The system messages before the first other message are the
    instructions; a later one becomes a developer message."""
    instructions, items = [], []
    leading = True
    for message in messages:
        role = message.get("role")
        if role == "system":
            text = _text_of(message.get("content"))
            if leading:
                instructions.append(text)
            elif text:
                items.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]})
            continue
        leading = False
        if role == "assistant":
            items.extend(_assistant_items(message, use_replay))
        elif role == "tool":
            content = message.get("content")
            output = content if isinstance(content, str) else (_input_parts(content) or "")
            items.append({"type": "function_call_output", "call_id": str(message.get("tool_call_id") or ""),
                          "output": output})
        elif role == "user":
            parts = _input_parts(message.get("content"))
            if tool_images and _is_capture(message) and items and items[-1].get("type") == "function_call_output":
                output = items[-1]["output"]
                if isinstance(output, str):
                    output = [{"type": "input_text", "text": output}] if output else []
                items[-1] = {**items[-1], "output": output + parts}
            elif parts:
                items.append({"type": "message", "role": "user", "content": parts})
        elif role == "developer":
            parts = _input_parts(message.get("content"))
            if parts:
                items.append({"type": "message", "role": "developer", "content": parts})
    return "\n\n".join(t for t in instructions if t), items


def request_body(*, model: str, messages: list[dict], tools: list[dict], reasoning_effort: str,
                 cache_key: str = "", disabled: set | None = None) -> dict:
    disabled = _disabled if disabled is None else disabled
    instructions, items = to_input(messages, tool_images="tool_images" not in disabled,
                                   use_replay="replay" not in disabled)
    body: dict = {"model": model, "input": items, "store": False, "stream": True}
    if instructions:
        body["instructions"] = instructions
    namespaced = to_tools(tools)
    if namespaced:
        body["tools"] = namespaced
    reasoning = {}
    if reasoning_effort:
        reasoning["effort"] = reasoning_effort
    if "reasoning_summary" not in disabled:
        reasoning["summary"] = "auto"
    if reasoning:
        body["reasoning"] = reasoning
    if "include" not in disabled:
        body["include"] = ["reasoning.encrypted_content"]
    if cache_key and "prompt_cache_key" not in disabled:
        body["prompt_cache_key"] = cache_key
    return body


def _feature_for(error: dict) -> str | None:
    """Which optional feature a 400 names, if any: by error.param first, else by its message."""
    for where in (str(error.get("param") or "").lower(), str(error.get("message") or "").lower()):
        if "prompt_cache_key" in where:
            return "prompt_cache_key"
        if "summary" in where:
            return "reasoning_summary"
        if where.startswith("include") or "encrypted_content" in where:
            return "include"
        if ".output" in where or "function_call_output" in where:
            return "tool_images"
        if "not found" in where and ("item" in where or "store" in where):
            return "replay_ids"   # Replayed items' ids; without them next, then without replay at all.
    return None


# --- Transcript ------------------------------------------------------------------------------------

class _Transcript:
    """LOOPCUT_CHATGPT_LOG_DIR's files. Safe from any thread; names carry the pid so worker processes
    writing to the same folder never collide."""

    _lock = threading.Lock()
    _count = 0
    _previous: dict = {}   # pid -> last request's input, to log later ones as extensions.

    def __init__(self):
        root = os.environ.get("LOOPCUT_CHATGPT_LOG_DIR", "")
        self.folder = Path(root) if root else None
        self.name = ""

    def _scrub(self, value, images: Path):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if key == "encrypted_content" and isinstance(item, str):
                    out[key] = f"<encrypted, {len(item)} chars>"
                elif key == "image_url" and isinstance(item, str) and item.startswith("data:"):
                    out[key] = self._image(item, images)
                else:
                    out[key] = self._scrub(item, images)
            return out
        if isinstance(value, list):
            return [self._scrub(item, images) for item in value]
        return value

    @staticmethod
    def _image(url: str, images: Path) -> str:
        header, _, data = url.partition(",")
        try:
            raw = base64.b64decode(data)
        except ValueError:
            return f"<image data URL, {len(url)} chars>"
        digest = hashlib.sha256(raw).hexdigest()
        suffix = ".jpg" if "jpeg" in header else ".png"
        target = images / (digest[:16] + suffix)
        if not target.exists():
            images.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        return f"<image {target.name}, {len(raw)} bytes>"

    def request(self, body: dict) -> None:
        if not self.folder:
            return
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with _Transcript._lock:
                _Transcript._count += 1
                self.name = f"{time.strftime('%H%M%S')}-{os.getpid()}-{_Transcript._count:04d}"
                previous = _Transcript._previous.get(os.getpid())
                _Transcript._previous[os.getpid()] = body
            images = self.folder / "images"
            record = {k: v for k, v in body.items() if k != "input"}
            items = body["input"]
            if previous and previous.get("instructions") == body.get("instructions") \
                    and previous.get("tools") == body.get("tools"):
                record = {k: v for k, v in record.items() if k not in ("instructions", "tools")}
                record["instructions_and_tools"] = "same as the previous request"
                old = previous["input"]
                same = 0
                while same < min(len(old), len(items)) and old[same] == items[same]:
                    same += 1
                record["input_extends_previous"] = same
                record["input_previous_length"] = len(old)
                items = items[same:]
            record["input"] = items
            (self.folder / f"{self.name}.request.json").write_text(
                json.dumps(self._scrub(record, images), indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError as ex:
            print(f"Loopcut: could not write the ChatGPT transcript: {ex}")

    def response(self, record: dict) -> None:
        if not self.folder or not self.name:
            return
        try:
            (self.folder / f"{self.name}.response.json").write_text(
                json.dumps(self._scrub(record, self.folder / "images"), indent=1, ensure_ascii=False), encoding="utf-8")
            line = {"name": self.name, **{k: record.get(k) for k in (
                "model", "effort", "status", "finish_reason", "usage", "seconds", "first_byte_seconds",
                "tool_calls", "text_chars", "error", "request_id")}}
            with _Transcript._lock, (self.folder / "requests.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line) + "\n")
        except OSError as ex:
            print(f"Loopcut: could not write the ChatGPT transcript: {ex}")


_SKIPPED_HEADERS = {"set-cookie", "cookie", "authorization"}


def _headers(response_headers) -> dict:
    return {k.lower(): v for k, v in (response_headers or {}).items() if k.lower() not in _SKIPPED_HEADERS}


# --- Streaming --------------------------------------------------------------------------------------

class _Interrupted(Exception):
    def __init__(self, streamed: bool):
        super().__init__(streamed)
        self.streamed = streamed


def _request_id(headers) -> str:
    """The id to quote in an error (the docs: preserve it). This route answers with x-oai-request-id."""
    if not headers:
        return ""
    return headers.get("x-oai-request-id", "") or headers.get("x-request-id", "")


def _error_from(status: int | None, body: bytes, headers) -> llm.LLMError:
    request_id = _request_id(headers)
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        payload = {}
    error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
    code = str(error.get("code") or error.get("type") or "")
    detail = str(error.get("message") or (payload.get("detail") if isinstance(payload, dict) else "") or
                 body.decode("utf-8", "replace")[:300])
    return _error(status, code, detail, error, request_id)


def _error(status: int | None, code: str, detail: str, error: dict, request_id: str) -> llm.LLMError:
    friendly = _MESSAGES.get(code)
    if chatgpt_ready.unsupported(llm.LLMError(detail, status, code)):
        code, friendly = "unsupported_model", chatgpt_ready.message("unsupported_model")
    elif code in ("invalid_grant", "unsupported_country_region_territory", "country_not_supported"):
        friendly = chatgpt_ready.message(code)
    if not friendly:
        if status == 401:
            friendly = f"ChatGPT did not accept the sign-in (HTTP 401{', ' + code if code else ''}): {detail}"
        elif status == 403:
            friendly = f"ChatGPT refused the request (HTTP 403{', ' + code if code else ''}): {detail}"
        else:
            friendly = f"{'HTTP ' + str(status) + ': ' if status else ''}{detail or code or 'ChatGPT request failed'}"
    if request_id:
        friendly += f" (request {request_id})"
    return llm.LLMError(friendly, status, code, {**error, "request_id": request_id, "message": friendly})


# Sticky routing within a turn: the route answers every request with this header, and OpenAI's own
# client (Codex) sends the turn's first value back on the turn's later requests, so they reach the
# machine holding the cached prefix. Measured without it (smoke 20261001-125645): the server replaced
# prompt_cache_key with a fresh id per request and only one of four follow-ups hit the cache.
TURN_STATE_HEADER = "x-codex-turn-state"
_turn_states: dict[str, tuple[int, str]] = {}   # cache key -> (turn, the turn's first state)
_turn_lock = threading.Lock()


def _turn(messages: list[dict]) -> int:
    """Which user turn a request belongs to: the user messages that are not viewport captures."""
    return sum(1 for m in messages if m.get("role") == "user" and not _is_capture(m))


def _turn_state(cache_key: str, turn: int) -> str:
    with _turn_lock:
        stored = _turn_states.get(cache_key)
        return stored[1] if stored and stored[0] == turn else ""


def _remember_turn_state(cache_key: str, turn: int, headers) -> None:
    value = (headers.get(TURN_STATE_HEADER, "") if headers else "") or ""
    if not cache_key or not value:
        return
    with _turn_lock:
        stored = _turn_states.get(cache_key)
        if not stored or stored[0] != turn:
            _turn_states[cache_key] = (turn, value)
            while len(_turn_states) > 256:
                _turn_states.pop(next(iter(_turn_states)))


def session_id(cache_key: str) -> str:
    """A stable UUID per conversation and role for the session_id header. The route takes its prompt
    cache key from that header (the reply's prompt_cache_key equals it), as for Codex; without it the
    key is a fresh UUID per request and step-to-step cache hits are luck (measured 2026-10-01: 1 hit in
    3 append-only follow-ups without it, 2 in 3 with it, warm-up included)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "loopcut:" + cache_key)) if cache_key else ""


def _post(body: dict, token: str, timeout: float, turn_state: str = "", session: str = ""):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "Accept": "text/event-stream", "User-Agent": chatgpt_auth.AGENT_NAME}
    if session:
        headers["session_id"] = session
    if turn_state:
        headers[TURN_STATE_HEADER] = turn_state
    request = urllib.request.Request(chatgpt_auth.api_base() + "/responses", data=json.dumps(body).encode("utf-8"),
                                     method="POST", headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)


def _open(build: Callable[[], dict], is_cancelled: Callable[[], bool], transcript: _Transcript,
          turn_state: Callable[[], str] = lambda: "", session: str = "", on_drop: Callable[[], bool] = lambda: False,
          probe: bool = False):
    """Open the stream: the token (refreshed when near expiry, and once more after a 401), retries
    for 429/5xx that are not usage limits, and one retry per optional feature a 400 refuses (the body
    is built again by build(), which leaves out every feature refused so far, and after a dropped
    connection sends smaller images: on_drop is image_budget.Sized.tighten)."""
    attempt, refreshed, refused = 0, False, set()
    while True:
        if is_cancelled():
            raise llm.Cancelled()
        body = build()
        try:
            token = chatgpt_auth.access_token()
        except chatgpt_auth.ChatGPTAuthError as ex:
            raise llm.LLMError(str(ex), ex.status or 401, ex.code or "chatgpt_not_signed_in") from None
        transcript.request(body)
        try:
            return _post(body, token, 15.0 if probe else read_timeout(), turn_state(), session)
        except urllib.error.HTTPError as ex:
            headers = ex.headers
            try:
                with ex:
                    raw = ex.read(200_000)
            except (OSError, http.client.HTTPException):
                raw = b""  # The answer's body broke off; its status still decides.
            error = _error_from(ex.code, raw, headers)
            transcript.response({"status": ex.code, "error": error.details, "headers": _headers(headers)})
            if probe:
                raise error from None
            if ex.code == 401 and not refreshed and error.code not in _NO_RETRY:
                refreshed = True
                try:
                    chatgpt_auth.refresh(force=True)
                except chatgpt_auth.ChatGPTAuthError as auth:
                    raise llm.LLMError(str(auth), 401, auth.code or "chatgpt_not_signed_in") from None
                continue
            if ex.code == 400:
                feature = _feature_for(error.details)
                if feature == "replay_ids" and "replay_ids" in _disabled:
                    feature = "replay"
                if feature and feature not in refused:
                    refused.add(feature)
                    _disabled.add(feature)
                    print(f"Loopcut: ChatGPT refused the request's {feature}; sending it without ({error})")
                    continue
            if error.code in _NO_RETRY or ex.code not in llm.RETRY_STATUSES or attempt >= len(llm.RETRY_DELAYS):
                raise error from None
            delay = llm.RETRY_DELAYS[attempt]
            retry_after = (headers or {}).get("Retry-After", "") if headers else ""
            if str(retry_after).replace(".", "", 1).isdigit():
                delay = min(max(delay, float(retry_after)), llm.MAX_RETRY_AFTER)
            print(f"Loopcut: retrying the ChatGPT request after HTTP {ex.code} ({attempt + 1}/{len(llm.RETRY_DELAYS)}, "
                  f"waiting {delay:g}s)")
        except (urllib.error.URLError, OSError, http.client.HTTPException) as ex:
            if probe:
                raise
            # A dropped connection: refused, reset, a broken pipe while the body went up, or a TLS alert such
            # as bad_record_mac read in place of the status line (ssl.SSLError, which urlopen does not wrap).
            # api.openai.com cut large uploads (2.5-5 MB) that way about one time in four on 2026-10-16.
            if not llm.dropped_connection(ex):
                if isinstance(ex, urllib.error.URLError):  # A refused certificate, say: the same every time.
                    raise llm.LLMError(f"Could not reach ChatGPT: {ex.reason}") from None
                raise
            if attempt >= len(llm.RETRY_DELAYS):
                raise llm.connection_lost("ChatGPT", ex) from None
            on_drop()
            delay = llm.RETRY_DELAYS[attempt]
            print(f"Loopcut: retrying the ChatGPT request after {getattr(ex, 'reason', ex)!r} "
                  f"({attempt + 1}/{len(llm.RETRY_DELAYS)}, waiting {delay:g}s)")
        attempt += 1
        llm._wait(delay, is_cancelled)


def _usage(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    return {"input": int(raw.get("input_tokens") or 0),
            "cached": int((raw.get("input_tokens_details") or {}).get("cached_tokens") or 0),
            "output": int(raw.get("output_tokens") or 0),
            "reasoning": int((raw.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)}


class _Watchdog:
    """Closes the stream's socket when the turn is cancelled or the call's wall clock runs out, so
    a read blocked for minutes on a silent, reasoning model returns at once."""

    def __init__(self, response, is_cancelled: Callable[[], bool], deadline: float | None):
        self.response, self.is_cancelled, self.deadline = response, is_cancelled, deadline
        self.fired = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loopcut-chatgpt-watchdog", daemon=True)
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
    """(event type, payload) per server-sent event."""
    data_lines = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data == "[DONE]":
                    return
                try:
                    payload = json.loads(data)
                except ValueError as ex:
                    raise llm.LLMError(f"Malformed stream event: {data[:200]}") from ex
                if isinstance(payload, dict):
                    yield str(payload.get("type") or ""), payload
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines))
            if isinstance(payload, dict):
                yield str(payload.get("type") or ""), payload
        except ValueError:
            pass


def _read(response, on_text, is_cancelled, deadline, transcript: _Transcript, meta: dict) -> llm.Completion:
    completion = llm.Completion()
    started = time.monotonic()
    items: dict[int, dict] = {}
    texts: dict[int, str] = {}
    order: list[int] = []
    streamed = False
    terminal = None
    final_response: dict = {}
    events: dict[str, int] = {}
    summaries: list[str] = []
    first_byte = None
    error: llm.LLMError | None = None
    watchdog = _Watchdog(response, is_cancelled, deadline)
    try:
        with response:
            for kind, event in _events(response):
                if first_byte is None:
                    first_byte = time.monotonic() - started
                events[kind] = events.get(kind, 0) + 1
                index = event.get("output_index")
                if is_cancelled():
                    raise llm.Cancelled()
                if kind == "response.output_item.added":
                    item = event.get("item") or {}
                    if isinstance(index, int) and index not in order:
                        order.append(index)
                    if item.get("type") == "message" and any(texts.get(i) for i in order if i != index):
                        on_text("\n\n")
                        streamed = True
                elif kind == "response.output_text.delta":
                    delta = str(event.get("delta") or "")
                    if delta:
                        texts[index] = texts.get(index, "") + delta
                        streamed = True
                        on_text(delta)
                elif kind == "response.function_call_arguments.delta":
                    streamed = True
                elif kind == "response.reasoning_summary_text.done":
                    summaries.append(str(event.get("text") or ""))
                elif kind == "response.output_item.done":
                    if isinstance(index, int):
                        items[index] = event.get("item") or {}
                        if index not in order:
                            order.append(index)
                elif kind in ("response.completed", "response.incomplete", "response.failed"):
                    terminal = kind
                    final_response = event.get("response") or {}
                    break
                elif kind == "error":
                    terminal = kind
                    error = _error(None, str(event.get("code") or ""), str(event.get("message") or ""),
                                   {k: event.get(k) for k in ("code", "message", "param")}, meta.get("request_id", ""))
                    break
    except (TimeoutError, ConnectionError, OSError, http.client.HTTPException) as ex:
        if watchdog.fired == "cancelled" or is_cancelled():
            raise llm.Cancelled() from None
        if watchdog.fired != "deadline":
            _log_response(transcript, meta, started, first_byte, events, items, order, final_response, completion,
                          summaries, error=f"stream broke: {ex!r}")
            raise _Interrupted(streamed) from ex
    finally:
        watchdog.stop()
    if watchdog.fired == "cancelled" or is_cancelled():
        raise llm.Cancelled()
    if watchdog.fired == "deadline" and terminal is None:
        completion.finish_reason, completion.timed_out = "length", True
    elif terminal is None:
        _log_response(transcript, meta, started, first_byte, events, items, order, final_response, completion,
                      summaries, error="stream ended without response.completed")
        raise _Interrupted(streamed)
    if terminal == "response.failed":
        raw = final_response.get("error") or {}
        error = _error(None, str(raw.get("code") or ""), str(raw.get("message") or "response.failed"),
                       raw if isinstance(raw, dict) else {}, meta.get("request_id", ""))
    if error is not None:
        _log_response(transcript, meta, started, first_byte, events, items, order, final_response, completion,
                      summaries, error=error.details)
        if error.code in _RETRY_IN_STREAM:
            raise _Interrupted(streamed) from error
        raise error
    if not items and isinstance(final_response.get("output"), list):
        for n, item in enumerate(final_response["output"]):
            items[n] = item
            if n not in order:
                order.append(n)
    output = [items[i] for i in order if i in items]
    message_texts = []
    for i in order:
        item = items.get(i) or {}
        if item.get("type") == "message":
            text = "".join(p.get("text", "") for p in item.get("content") or [] if isinstance(p, dict)
                           and p.get("type") == "output_text")
            message_texts.append(text or texts.get(i, ""))
        elif item.get("type") == "function_call":
            completion.tool_calls.append(llm.ToolCall(
                id=str(item.get("call_id") or item.get("id") or ""),
                name=bare_name(str(item.get("name") or ""), item.get("namespace")),
                arguments=str(item.get("arguments") or "")))
    if not message_texts and texts:  # No output_item.done for the text: fall back to the deltas.
        message_texts = [texts[i] for i in order if texts.get(i)]
    completion.text = "\n\n".join(t for t in message_texts if t)
    completion.usage = _usage(final_response.get("usage"))
    if terminal == "response.incomplete":
        reason = str((final_response.get("incomplete_details") or {}).get("reason") or "")
        completion.finish_reason = "length" if reason in ("", "max_output_tokens") else reason
    elif completion.finish_reason is None:
        completion.finish_reason = "tool_calls" if completion.tool_calls else "stop"
    if output and terminal in ("response.completed", "response.incomplete"):
        replay.put(completion.text, [c.id for c in completion.tool_calls], output)
    _log_response(transcript, meta, started, first_byte, events, items, order, final_response, completion, summaries)
    return completion


def _log_response(transcript, meta, started, first_byte, events, items, order, final_response, completion,
                  summaries, error=None) -> None:
    if not transcript.folder:
        return
    transcript.response({
        **meta, "seconds": round(time.monotonic() - started, 3),
        "first_byte_seconds": round(first_byte, 3) if first_byte is not None else None,
        "status": final_response.get("status"), "finish_reason": completion.finish_reason,
        "usage": completion.usage or final_response.get("usage"), "raw_usage": final_response.get("usage"),
        "tool_calls": [c.name for c in completion.tool_calls], "text_chars": len(completion.text),
        "reasoning_summaries": summaries, "events": events,
        "output": [items[i] for i in order if i in items], "error": error,
        "response": {k: v for k, v in final_response.items() if k not in ("output", "instructions", "tools", "input")},
    })


def _stream(*, model: str, messages: list[dict], tools: list[dict], reasoning_effort: str,
           on_text: Callable[[str], None], is_cancelled: Callable[[], bool],
           call_max_seconds: float | None = None, headers: dict | None = None,
           on_retry: Callable[[], None] | None = None) -> llm.Completion:
    """llm.stream_chat for the ChatGPT plan: same arguments that matter, same Completion, same retry
    rules for a stream that breaks (before any text: capped backoff; after some: once, from scratch,
    only with on_retry)."""
    conversation = (headers or {}).get("x-loopcut-conversation", "")
    role = (headers or {}).get("x-loopcut-role", "")
    cache_key = f"loopcut-{conversation}-{role}" if conversation else ""
    deadline = time.monotonic() + call_max_seconds if call_max_seconds else None
    before_content, retried_partial = 0, False
    sized = image_budget.Sized(messages)

    def build() -> dict:
        return request_body(model=model, messages=sized.messages(), tools=tools, reasoning_effort=reasoning_effort,
                            cache_key=cache_key)

    turn = _turn(messages)
    while True:
        transcript = _Transcript()
        response = _open(build, is_cancelled, transcript, lambda: _turn_state(cache_key, turn), session_id(cache_key),
                         sized.tighten)
        _remember_turn_state(cache_key, turn, response.headers)
        meta = {"model": model, "effort": reasoning_effort, "request_id": _request_id(response.headers),
                "headers": _headers(response.headers)}
        try:
            return _read(response, on_text, is_cancelled, deadline, transcript, meta)
        except _Interrupted as ex:
            cause = ex.__cause__ or EOFError("the stream ended before response.completed")
            if ex.streamed:
                if retried_partial or on_retry is None:
                    raise llm.connection_lost("ChatGPT", cause, while_replying=True) from cause
                retried_partial = True
                print(f"Loopcut: retrying the ChatGPT request once from scratch after the stream broke ({cause!r})")
                on_retry()
                continue
            if before_content == len(llm.RETRY_DELAYS):
                raise llm.connection_lost("ChatGPT", cause) from cause
            delay = llm.RETRY_DELAYS[before_content]
            print(f"Loopcut: retrying the ChatGPT request after {cause!r} ({before_content + 1}/{len(llm.RETRY_DELAYS)}, "
                  f"waiting {delay:g}s)")
            before_content += 1
            llm._wait(delay, is_cancelled)


def stream(*, model: str, **kwargs) -> llm.Completion:
    """Retry an explicitly unsupported model once, within the same ChatGPT account."""
    account = chatgpt_ready.account_key()
    actual = chatgpt_ready.effective_model(account, model)
    try:
        completion = _stream(model=actual, **kwargs)
    except llm.LLMError as ex:
        if not chatgpt_ready.unsupported(ex):
            raise
        chatgpt_ready.remember(account, actual, False)
        # A remembered fallback is already the one alternative for this request.
        alternative = chatgpt_ready.fallback(account, actual) if actual == model else None
        if not alternative:
            raise
        actual = alternative
        try:
            completion = _stream(model=actual, **kwargs)
        except llm.LLMError as fallback_error:
            if chatgpt_ready.unsupported(fallback_error):
                chatgpt_ready.remember(account, actual, False)
            raise
    if not completion.timed_out and completion.finish_reason != "length":
        chatgpt_ready.remember(account, actual, True, requested=model)
        if actual != model:
            chatgpt_ready.model_used(account, model, actual)
    return completion
