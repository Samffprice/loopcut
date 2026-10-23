"""Streaming client for an OpenAI-compatible /chat/completions endpoint.

Stdlib only, so the extension ships without wheels. Blocking; call from a worker thread.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from . import allowance


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str = "", details: dict | None = None,
                 account: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code            # The API's error code, e.g. week_limit.
        self.details = details or {}  # The whole error object: Loopcut's gateway adds plan, resets_at, upgrade.
        self.account = account  # Usage headers also arrive on refusals.


class Cancelled(Exception):
    pass


# Worth another try: rate limits and the provider being briefly unwell. Anything else (bad key,
# bad request, no credit) will fail the same way again, so it is shown at once.
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
RETRY_DELAYS = (1.0, 3.0, 8.0)
MAX_RETRY_AFTER = 30.0
CONNECTION_LOST = "connection_lost"  # LLMError.code when the connection kept dropping: the panel offers Try again.
EMPTY_REPLY = "empty_reply"  # LLMError.code: the model answered with nothing to show or run, twice (agent._request).
CONTENT_FILTER = "content_filter"  # LLMError.code: the provider's own filter stopped the reply (agent._request).
# finish_reason values some providers send when their content filter stops a reply: z.ai's GLM API ends the
# stream with "sensitive" (and no usage), OpenAI-compatible servers with "content_filter". Measured 2026-10-20 on
# glm-5.3-flash, a request that names Taiwan's flag or Tiananmen Square stops this way, with no text.
FILTERED_FINISHES = frozenset({"sensitive", "content_filter"})


def dropped_connection(ex) -> bool:
    """A connection that broke on the way (a TLS alert such as bad_record_mac, a broken pipe, a reset, a reply
    cut off), which another try may get through. Not an HTTP answer, and not a certificate this computer
    refuses, which fails the same way every time."""
    import http.client
    import ssl
    if isinstance(ex, (urllib.error.HTTPError, ssl.SSLCertVerificationError)):
        return False
    if isinstance(ex, urllib.error.URLError):
        return not isinstance(ex.reason, ssl.SSLCertVerificationError)
    return isinstance(ex, (TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException))


def short_cause(ex) -> str:
    """"SSLError: SSLV3_ALERT_BAD_RECORD_MAC", "BrokenPipeError: Broken pipe": enough for a bug report, short."""
    if isinstance(ex, str):
        return ex[:100]
    if isinstance(ex, urllib.error.URLError) and isinstance(ex.reason, BaseException):
        ex = ex.reason
    reason = getattr(ex, "reason", None)
    detail = reason if isinstance(reason, str) and reason else getattr(ex, "strerror", None) or str(ex)
    return f"{type(ex).__name__}: {str(detail)[:80]}" if detail else type(ex).__name__


def connection_lost(service: str, cause=None, while_replying: bool = False) -> LLMError:
    """What the panel shows when the connection to `service` kept dropping after the retries: plain words,
    Try again on the card (CONNECTION_LOST), and the technical cause last, in brackets."""
    when = "while it was replying" if while_replying else "before it could answer"
    tail = f" ({short_cause(cause)})" if cause is not None else ""
    return LLMError(f"The connection to {service} dropped {when}. Nothing was lost: press Try again.{tail}",
                    code=CONNECTION_LOST)


@dataclass
class ToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None  # {"input": tokens, "output": tokens} when the API reports it.
    account: dict | None = None  # Plan and window usage from Loopcut's gateway headers; see account_from_headers.
    timed_out: bool = False  # call_max_seconds cut the stream short; finish_reason is set to "length" too.
    prompt_version: str | None = None
    # Characters of hidden reasoning the stream carried (delta.reasoning_content on z.ai and DeepSeek-style
    # servers, delta.reasoning on OpenRouter). Never shown or sent back; it tells a reasoning-only reply, which
    # has nothing to show, from an empty one in the log.
    reasoning_chars: int = 0

    def as_message(self) -> dict:
        message = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            message["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": c.arguments}}
                for c in self.tool_calls
            ]
        return message


def _error_payload(body: bytes) -> dict:
    """The error object of an API reply, or {} when there is none."""
    try:
        payload = json.loads(body)
    except ValueError:
        return {}
    error = payload.get("error") if isinstance(payload, dict) else None
    return error if isinstance(error, dict) else {}


def _error_detail(body: bytes) -> str:
    error = _error_payload(body)
    if error.get("message"):
        return str(error["message"])
    try:
        return json.dumps(json.loads(body))[:500]
    except ValueError:
        return body.decode("utf-8", "replace")[:500]


def account_from_headers(headers) -> dict | None:
    """What Loopcut's gateway says about the account on every reply: the plan, and how much of
    the session and week windows is used (0..1). None from any other provider."""
    plan = headers.get("x-loopcut-plan")
    if not plan:
        return None

    def window(name: str) -> dict:
        result = {"used": allowance.fraction(headers.get(f"x-loopcut-{name}-used")),
                  "resets_at": headers.get(f"x-loopcut-{name}-resets-at") or None}
        reserved = allowance.fraction(headers.get(f"x-loopcut-{name}-used-with-reservations"))
        if reserved is not None:
            result["used_with_reservations"] = reserved
        return result

    return {"plan": str(plan), "session": window("session"), "week": window("week")}


def _wait(seconds: float, is_cancelled: Callable[[], bool]) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if is_cancelled():
            raise Cancelled()
        time.sleep(0.1)


def _open(build: Callable[[], urllib.request.Request], base_url: str, timeout: float,
          is_cancelled: Callable[[], bool], on_drop: Callable[[], bool] = lambda: False):
    """Open the stream, retrying failures that are likely to pass. Nothing has been streamed to
    the user yet at this point, so a retry is invisible apart from the wait. A read or connect
    timeout at this stage (getting the status line and headers) is a socket.timeout/TimeoutError
    that urlopen itself wraps as URLError, so it is already covered by the clause below. The
    request is built again for each try: after a dropped connection or a 413, on_drop
    (image_budget.Sized.tighten) has made its images smaller."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        last = attempt == len(RETRY_DELAYS)
        request = build()
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as ex:
            if request.get_header("X-loopcut-prompt"):
                from . import cloud
                cloud.note_prompt_version(ex.headers.get("x-loopcut-prompt-version"))
            with ex:
                body = ex.read()
                retry_after = ex.headers.get("Retry-After", "")
            # 413: too large for the server (Loopcut's gateway, on Vercel, takes 4.5 MB): retried with smaller images.
            if last or (ex.code not in RETRY_STATUSES and not (ex.code == 413 and on_drop())):
                error = _error_payload(body)
                raise LLMError(f"HTTP {ex.code}: {_error_detail(body)}", ex.code,
                               str(error.get("code") or error.get("type") or ""), error,
                               account_from_headers(ex.headers)) from ex
            delay = RETRY_DELAYS[attempt]
            if retry_after.replace(".", "", 1).isdigit():
                delay = min(max(delay, float(retry_after)), MAX_RETRY_AFTER)
            print(f"Loopcut: retrying the request after HTTP {ex.code} ({attempt + 1}/{len(RETRY_DELAYS)}, "
                  f"waiting {delay:g}s)")
        except urllib.error.URLError as ex:
            if last:
                raise LLMError(f"Could not reach {base_url}: {ex.reason}", code=CONNECTION_LOST) from ex
            on_drop()  # A broken pipe or a reset as the body went up: large uploads are cut that way.
            delay = RETRY_DELAYS[attempt]
            print(f"Loopcut: retrying the request after {ex.reason!r} ({attempt + 1}/{len(RETRY_DELAYS)}, "
                  f"waiting {delay:g}s)")
        _wait(delay, is_cancelled)
    raise AssertionError("unreachable")


class _ReadInterrupted(Exception):
    """A read timeout or connection reset while a stream's body was being read (after urlopen
    already returned the status line and headers): urlopen wraps neither as URLError, since they
    happen later, outside its own call. streamed: whether a content or tool-call delta had already
    reached on_text this attempt, so the caller may have applied something from it."""

    def __init__(self, streamed: bool):
        super().__init__(streamed)
        self.streamed = streamed


_READ_ERRORS = (TimeoutError, ConnectionResetError)  # socket.timeout is TimeoutError since Python 3.10.


def _read(response, on_text: Callable[[str], None], is_cancelled: Callable[[], bool],
         deadline: float | None = None) -> Completion:
    """One stream, start to finish, into a Completion. Raises _ReadInterrupted(streamed) for a read
    timeout or connection reset (see _READ_ERRORS); everything else (a malformed chunk, an error
    object in the stream, Cancelled) is unrelated to the connection and is not retryable here.

    deadline: a time.monotonic() past which the stream is cut short like a normal finish, not an
    error -- finish_reason is set to "length" (and timed_out=True) so a caller that already retries
    a capped reply (agent._request) treats this the same way, with whatever text and tool calls had
    already arrived. Checked once per line, so a keepalive comment (cc_bridge's, say) is as good an
    opportunity to notice as a content delta: stream_chat leaves deadline None for a base URL on
    127.0.0.1, so this never fires for the Claude Code bridge."""
    completion = Completion(account=account_from_headers(response.headers),
                            prompt_version=response.headers.get("x-loopcut-prompt-version"))
    calls: dict[int, ToolCall] = {}
    streamed = False
    try:
        with response:
            for raw in response:
                if is_cancelled():
                    raise Cancelled()
                if deadline is not None and time.monotonic() > deadline:
                    completion.finish_reason, completion.timed_out = "length", True
                    break
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError as ex:
                    raise LLMError(f"Malformed stream chunk: {data[:200]}") from ex
                if chunk.get("error"):
                    raise LLMError(_error_detail(data.encode("utf-8")))
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    completion.usage = {"input": int(usage.get("prompt_tokens") or 0),
                                        "cached": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                                        "output": int(usage.get("completion_tokens") or 0)}
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        completion.text += text
                        streamed = True
                        on_text(text)
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(reasoning, str) and reasoning:
                        completion.reasoning_chars += len(reasoning)  # Nothing shown, so not "streamed".
                    for part in delta.get("tool_calls") or []:
                        streamed = True
                        call = calls.setdefault(part.get("index", 0), ToolCall())
                        call.id = part.get("id") or call.id
                        function = part.get("function") or {}
                        call.name = function.get("name") or call.name
                        call.arguments += function.get("arguments") or ""
                    if choice.get("finish_reason"):
                        completion.finish_reason = choice["finish_reason"]
    except _READ_ERRORS as ex:
        raise _ReadInterrupted(streamed) from ex
    completion.tool_calls = [calls[i] for i in sorted(calls)]
    return completion


def correlation_headers(conversation_id: str, role: str) -> dict:
    """x-loopcut-conversation / x-loopcut-role for Loopcut's own gateway: which of a turn's
    simultaneous streams (orchestrator, worker, router, summary, single) a request is, for its
    usage row. These headers go only to the gateway (see stream_chat)."""
    return {"x-loopcut-conversation": conversation_id, "x-loopcut-role": role}


def _is_loopback(base_url: str) -> bool:
    """Whether base_url is Loopcut's own Claude Code bridge (harness/cc_bridge.py, always
    127.0.0.1): its replies can legitimately take several minutes, so call_max_seconds never
    applies to it."""
    try:
        return urlparse(base_url).hostname == "127.0.0.1"
    except ValueError:
        return False


def wire_tools(schemas: list[dict]) -> list[dict]:
    """Activity titles are local UI metadata, not part of the model's prompt or token budget."""
    return [{**schema, "function": {key: value for key, value in schema["function"].items() if key != "title"}}
            if isinstance(schema.get("function"), dict) else schema for schema in schemas]


def stream_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict],
    reasoning_effort: str,
    on_text: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    timeout: float = 120.0,
    max_tokens: int | None = None,
    call_max_seconds: float | None = None,
    headers: dict | None = None,
    prompt_id: str | None = None,
    on_retry: Callable[[], None] | None = None,
) -> Completion:
    """`headers` adds to (never replaces) Authorization/Content-Type/Accept -- for Loopcut's own
    gateway, the caller passes correlation_headers()'s x-loopcut-conversation / x-loopcut-role so a
    usage row can be told apart from another conversation's on the same account (see the web repo's
    gateway/correlation.ts). All x-loopcut-* headers go only to the gateway. prompt_id selects
    its single, orchestrator, worker or router prompt; absent means pass-through.

    max_tokens caps the reply the way every OpenAI-compatible provider expects, in the request body
    (not a header); the caller decides the number (config.Config.max_output_tokens) so a runaway
    reasoning call cannot run unbounded.

    call_max_seconds bounds the whole call's wall clock, start to finish (not the 120s+ per-read
    timeout= above, which stays as it is): a stream still running past it is cut short and reported
    as if it had hit max_tokens (finish_reason "length", Completion.timed_out=True) rather than as an
    error, so a caller that already retries a capped reply treats both the same way. Ignored --
    always uninterrupted -- when base_url is 127.0.0.1 (_is_loopback): the Claude Code bridge.

    A read timeout or connection reset (a z.ai/provider hiccup mid-stream, not an HTTP error) is
    retried like a 429/5xx: same capped backoff, a fresh request each time, as long as nothing of
    this attempt's reply reached on_text yet. Once something has (the agent loop may already have
    shown it), a blind retry would double it up, so that is retried exactly once, from scratch, and
    only when on_retry is given: it is the caller saying it can undo what on_text already did (drop
    the partial reply card's text) before the retried request's own deltas start arriving. Without
    on_retry, or after that one retry also breaks, the error surfaces as it did before this existed.

    Every provider sends the messages through image_budget.Sized: their inline images stay under a
    byte budget, and a try after the connection dropped as the request went up sends them smaller.

    The ChatGPT plan provider (base URL chatgpt_auth.CHATGPT_PLAN_BASE_URL) is not chat completions:
    chatgpt_responses.stream speaks the Responses API with the signed-in account's token instead,
    and returns the same Completion. It reads correlation headers locally and never sends them
    over the wire. api_key and max_tokens do not apply there.

    The Anthropic provider (base URL config.ANTHROPIC_BASE_URL, anthropic://...) is not chat completions either:
    anthropic_messages.stream speaks Anthropic's Messages API with the user's key and prompt caching, and returns
    the same Completion. It never sends the x-loopcut-* headers."""
    if prompt_id is not None and prompt_id not in ("single", "orchestrator", "worker", "router"):
        raise ValueError(f"Unknown Loopcut prompt_id: {prompt_id!r}")
    tools = wire_tools(tools)
    if base_url.startswith("chatgpt-plan"):
        from . import chatgpt_responses
        return chatgpt_responses.stream(
            model=model, messages=messages, tools=tools, reasoning_effort=reasoning_effort, on_text=on_text,
            is_cancelled=is_cancelled, call_max_seconds=call_max_seconds, headers=headers, on_retry=on_retry)
    if base_url.startswith("anthropic://"):
        from . import anthropic_messages
        return anthropic_messages.stream(
            base_url=base_url, api_key=api_key, model=model, messages=messages, tools=tools,
            reasoning_effort=reasoning_effort, on_text=on_text, is_cancelled=is_cancelled, timeout=timeout,
            max_tokens=max_tokens, call_max_seconds=call_max_seconds, headers=headers, on_retry=on_retry)
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "reasoning_effort": reasoning_effort,
    }
    if tools:  # An empty list is rejected by some providers.
        body["tools"] = tools
    if max_tokens:
        body["max_tokens"] = max_tokens
    from . import config, image_budget
    service = config.on_service(base_url)
    request_headers = {name: value for name, value in (headers or {}).items()
                       if name.lower() != "x-loopcut-prompt"
                       and (service or not name.lower().startswith("x-loopcut-"))}
    if service and prompt_id is not None:
        from . import cloud
        request_headers["x-loopcut-prompt"] = prompt_id
    if service:  # Loopcut's gateway only: add-on, Blender, OS and processor, as on every other Loopcut request.
        try:
            from . import cloud_http
            request_headers["x-loopcut-client"] = cloud_http.client_value()
        except Exception:
            pass
    request_headers.update({
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    })
    sized = image_budget.Sized(messages)

    def build() -> urllib.request.Request:
        return urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps({**body, "messages": sized.messages()}).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
    deadline = (time.monotonic() + call_max_seconds
                if call_max_seconds and not _is_loopback(base_url) else None)
    # Two separate, small budgets: before any content this attempt, the same capped backoff as a
    # connect failure (bounded by RETRY_DELAYS); once content has streamed, exactly one further
    # retry, regardless of how much of the first budget that used.
    before_content = 0
    retried_partial = False
    while True:
        response = _open(build, base_url, timeout, is_cancelled, sized.tighten)
        if service and prompt_id is not None:
            cloud.note_prompt_version(response.headers.get("x-loopcut-prompt-version"))
        try:
            return _read(response, on_text, is_cancelled, deadline)
        except _ReadInterrupted as ex:
            cause = ex.__cause__
            if ex.streamed:
                if retried_partial or on_retry is None:
                    raise LLMError(f"Connection lost while streaming the reply: {cause!r}", code=CONNECTION_LOST) from cause
                retried_partial = True
                print(f"Loopcut: retrying once from scratch after the stream broke mid-reply ({cause!r})")
                on_retry()
                continue
            if before_content == len(RETRY_DELAYS):
                raise LLMError(f"Connection lost before any reply arrived: {cause!r}", code=CONNECTION_LOST) from cause
            delay = RETRY_DELAYS[before_content]
            print(f"Loopcut: retrying the request after {cause!r} ({before_content + 1}/{len(RETRY_DELAYS)}, "
                  f"waiting {delay:g}s)")
            before_content += 1
            _wait(delay, is_cancelled)
