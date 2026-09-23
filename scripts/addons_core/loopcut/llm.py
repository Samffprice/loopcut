"""Streaming client for an OpenAI-compatible /chat/completions endpoint.

Stdlib only, so the extension ships without wheels. Blocking; call from a worker thread.
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str = "", details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code            # The API's error code, e.g. week_limit.
        self.details = details or {}  # The whole error object: Loopcut's gateway adds plan, resets_at, upgrade.


class Cancelled(Exception):
    pass


# Worth another try: rate limits and the provider being briefly unwell. Anything else (bad key,
# bad request, no credit) will fail the same way again, so it is shown at once.
RETRY_STATUSES = {429, 500, 502, 503, 504, 529}
RETRY_DELAYS = (1.0, 3.0, 8.0)
MAX_RETRY_AFTER = 30.0


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
        try:
            used = float(headers.get(f"x-loopcut-{name}-used", ""))
        except ValueError:
            used = None
        return {"used": used, "resets_at": headers.get(f"x-loopcut-{name}-resets-at") or None}

    return {"plan": str(plan), "session": window("session"), "week": window("week")}


def _wait(seconds: float, is_cancelled: Callable[[], bool]) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if is_cancelled():
            raise Cancelled()
        time.sleep(0.1)


def _open(request, base_url: str, timeout: float, is_cancelled: Callable[[], bool]):
    """Open the stream, retrying failures that are likely to pass. Nothing has been streamed to
    the user yet at this point, so a retry is invisible apart from the wait."""
    for attempt in range(len(RETRY_DELAYS) + 1):
        last = attempt == len(RETRY_DELAYS)
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as ex:
            with ex:
                body = ex.read()
                retry_after = ex.headers.get("Retry-After", "")
            if last or ex.code not in RETRY_STATUSES:
                error = _error_payload(body)
                raise LLMError(f"HTTP {ex.code}: {_error_detail(body)}", ex.code,
                               str(error.get("code") or error.get("type") or ""), error) from ex
            delay = RETRY_DELAYS[attempt]
            if retry_after.replace(".", "", 1).isdigit():
                delay = min(max(delay, float(retry_after)), MAX_RETRY_AFTER)
        except urllib.error.URLError as ex:
            if last:
                raise LLMError(f"Could not reach {base_url}: {ex.reason}") from ex
            delay = RETRY_DELAYS[attempt]
        _wait(delay, is_cancelled)
    raise AssertionError("unreachable")


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
) -> Completion:
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "reasoning_effort": reasoning_effort,
    }
    if tools:  # An empty list is rejected by some providers.
        body["tools"] = tools
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    response = _open(request, base_url, timeout, is_cancelled)

    completion = Completion(account=account_from_headers(response.headers))
    calls: dict[int, ToolCall] = {}
    with response:
        for raw in response:
            if is_cancelled():
                raise Cancelled()
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
                    on_text(text)
                for part in delta.get("tool_calls") or []:
                    call = calls.setdefault(part.get("index", 0), ToolCall())
                    call.id = part.get("id") or call.id
                    function = part.get("function") or {}
                    call.name = function.get("name") or call.name
                    call.arguments += function.get("arguments") or ""
                if choice.get("finish_reason"):
                    completion.finish_reason = choice["finish_reason"]

    completion.tool_calls = [calls[i] for i in sorted(calls)]
    return completion
