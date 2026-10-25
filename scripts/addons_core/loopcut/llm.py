"""Streaming client for an OpenAI-compatible /chat/completions endpoint.

Stdlib only, so the extension ships without wheels. Blocking; call from a worker thread.
"""

import http.client
import json
import math
import socket
import ssl
import threading
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
STREAM_FAILED = "stream_failed"  # LLMError.code: the reply kept ending in an error after the retries: Try again.
CONTENT_FILTER = "content_filter"  # LLMError.code: the provider's own filter stopped the reply (agent._request).
# finish_reason values some providers send when their content filter stops a reply: z.ai's GLM API ends the
# stream with "sensitive" (and no usage), OpenAI-compatible servers with "content_filter". Measured 2026-10-20 on
# glm-5.3-flash, a request that names Taiwan's flag or Tiananmen Square stops this way, with no text.
FILTERED_FINISHES = frozenset({"sensitive", "content_filter"})
# Loopcut's gateway takes a gzipped chat body (Content-Encoding: gzip, at most 12 MiB decoded) since 2026-10-25:
# hosted requests of tens to hundreds of KB died on the way, with no trace on the server, while small ones from the
# same computers arrived (4 of the 5 users had a Russian interface, which fits ISPs freezing a connection to a
# foreign host after about 16 KB; not proven). JSON text compresses several times over. A 400 or 415 to a gzipped
# body is sent once more uncompressed; when that one is not refused the same way, the rest of this Blender session
# sends uncompressed (_gzip_refused). Never to another provider or a LOOPCUT_SERVICE_URLS stand-in.
GZIP_REFUSED = (400, 415)
_gzip_refused = False


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


def network_cause(ex) -> str | None:
    """For an OSError a network call raised that dropped_connection does not know, such as Windows' bare
    FileNotFoundError(2) waiting for a reply (0.2.14 logs): "network error, errno 2" (or the type's name,
    without an errno), since "No such file or directory" on a card that says the connection dropped reads as a
    missing file. None for anything else, whose own text stays. The console line keeps repr(ex)."""
    if not isinstance(ex, OSError) or isinstance(ex, urllib.error.URLError) or dropped_connection(ex):
        return None
    return f"network error, errno {ex.errno}" if ex.errno else f"network error, {type(ex).__name__}"


def short_cause(ex) -> str:
    """"SSLError: SSLV3_ALERT_BAD_RECORD_MAC", "BrokenPipeError: Broken pipe": enough for a bug report, short."""
    if isinstance(ex, str):
        return ex[:100]
    if isinstance(ex, urllib.error.URLError) and isinstance(ex.reason, BaseException):
        ex = ex.reason
    reason = getattr(ex, "reason", None)
    detail = reason if isinstance(reason, str) and reason else getattr(ex, "strerror", None) or str(ex)
    return f"{type(ex).__name__}: {str(detail)[:80]}" if detail else type(ex).__name__


# Every try at Loopcut's gateway died before a status line came back, though the connection opened. On 2026-10-25
# such requests (tens to hundreds of KB) had left no trace on the server while small ones from the same computers
# arrived; "press Try again" only repeated it. The card says what is in the way instead (still with Try again, for
# after the user has changed it); the cause follows in brackets as on connection_lost's card.
NETWORK_DROPPED = ("Your network dropped the request before it reached Loopcut. A VPN, proxy, antivirus web filter "
                   "or your internet provider can do this, most often to large requests.")


def network_dropped(cause=None) -> LLMError:
    """NETWORK_DROPPED's card (CONNECTION_LOST, so it still offers Try again), the technical cause last."""
    tail = f" ({short_cause(cause)})" if cause is not None else ""
    return LLMError(NETWORK_DROPPED + tail, code=CONNECTION_LOST)


def _never_connected(ex) -> bool:
    """A failure before any connection opened (the name lookup, a refused connection): offline or blocked
    outright, not a request dropped on its way."""
    reason = ex.reason if isinstance(ex, urllib.error.URLError) else ex
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


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
    usage: dict | None = None  # {"input": tokens, "output": tokens[, "cost": USD]} when the API reports it.
    account: dict | None = None  # Plan and window usage from Loopcut's gateway headers; see account_from_headers.
    timed_out: bool = False  # call_max_seconds cut the stream short; finish_reason is set to "length" too.
    prompt_version: str | None = None
    # Characters of hidden reasoning the stream carried (delta.reasoning_content on z.ai and DeepSeek-style
    # servers, delta.reasoning on OpenRouter). Never shown or sent back; it tells a reasoning-only reply, which
    # has nothing to show, from an empty one in the log.
    reasoning_chars: int = 0
    # Calls the reply wrote out as text instead of making them; set by agent._drop_copied_records.
    unmade: list[str] = field(default_factory=list)

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

    info = {"plan": str(plan), "session": window("session"), "week": window("week")}
    # The credits arm (chat_credits.py): chat comes out of the balance, which each reply reports as it was before
    # the request's charge. Only added when sent, so other accounts' info is exactly what it was.
    if headers.get("x-loopcut-billing") in ("credits", "allowance"):
        info["billing"] = headers["x-loopcut-billing"]
    balance = headers.get("x-loopcut-credits-balance")
    if isinstance(balance, str) and balance.strip().isdecimal() and balance.strip().isascii():
        info["credits_balance"] = int(balance.strip())
    return info


def _wait(seconds: float, is_cancelled: Callable[[], bool]) -> None:
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if is_cancelled():
            raise Cancelled()
        time.sleep(0.1)


def _open(build: Callable[[], urllib.request.Request], base_url: str, timeout: float,
          is_cancelled: Callable[[], bool], on_drop: Callable[[], bool] = lambda: False,
          retry_delays: tuple[float, ...] | None = None, unanswered: Callable[..., LLMError] | None = None):
    """Open the stream, retrying failures that are likely to pass. Nothing has been streamed to
    the user yet at this point, so a retry is invisible apart from the wait. urlopen wraps an
    OSError from connecting and sending as URLError, but not one from waiting for the status line
    (getresponse): a read timeout, a reset, a TLS alert, or the bare FileNotFoundError(2) Windows
    raised there after ~19s (0.2.14 logs). That is retried the same way, unless it is a refused
    certificate, which fails the same way every time. retry_delays (RETRY_DELAYS when None) are the
    waits before each further try. The request is built again for each try:
    after a dropped connection or a 413, on_drop (image_budget.Sized.tighten) has made its images
    smaller, and after a gzipped body was refused (GZIP_REFUSED) it goes uncompressed (_uncompressed).
    unanswered(cause), when given, is the error when no try got a status line, though a connection opened
    (network_dropped for Loopcut's gateway)."""
    delays = RETRY_DELAYS if retry_delays is None else retry_delays
    answered = False
    for attempt in range(len(delays) + 1):
        last = attempt == len(delays)
        request = build()
        try:
            return opened(lambda: urllib.request.urlopen(request, timeout=timeout), is_cancelled)
        except urllib.error.HTTPError as ex:
            answered = True
            if request.get_header("X-loopcut-prompt"):
                from . import cloud
                cloud.note_prompt_version(ex.headers.get("x-loopcut-prompt-version"))
            with ex:
                body = ex.read()
                retry_after = ex.headers.get("Retry-After", "")
            if ex.code in GZIP_REFUSED and request.get_header("Content-encoding") == "gzip" and not _gzip_refused:
                return _uncompressed(ex.code, lambda: _open(build, base_url, timeout, is_cancelled, on_drop, delays))
            # 413: too large for the server (Loopcut's gateway, on Vercel, takes 4.5 MB): retried with smaller images.
            if last or (ex.code not in RETRY_STATUSES and not (ex.code == 413 and on_drop())):
                error = _error_payload(body)
                raise LLMError(f"HTTP {ex.code}: {_error_detail(body)}", ex.code,
                               str(error.get("code") or error.get("type") or ""), error,
                               account_from_headers(ex.headers)) from ex
            delay = delays[attempt]
            if retry_after.replace(".", "", 1).isdigit():
                delay = min(max(delay, float(retry_after)), MAX_RETRY_AFTER)
            print(f"Loopcut: retrying the request after HTTP {ex.code} ({attempt + 1}/{len(delays)}, "
                  f"waiting {delay:g}s)")
        except urllib.error.URLError as ex:
            if last and unanswered and not answered and dropped_connection(ex) and not _never_connected(ex):
                raise unanswered(ex) from ex
            if last:
                raise LLMError(f"Could not reach {base_url}: {ex.reason}", code=CONNECTION_LOST) from ex
            on_drop()  # A broken pipe or a reset as the body went up: large uploads are cut that way.
            delay = delays[attempt]
            print(f"Loopcut: retrying the request after {ex.reason!r} ({attempt + 1}/{len(delays)}, "
                  f"waiting {delay:g}s)")
        except OSError as ex:  # From getresponse, which urlopen does not wrap (see above).
            if isinstance(ex, ssl.SSLCertVerificationError):
                raise
            if last and unanswered and not answered:
                raise unanswered(network_cause(ex) or ex) from ex
            if last:
                raise connection_lost("the model", network_cause(ex) or ex) from ex
            on_drop()
            delay = delays[attempt]
            print(f"Loopcut: retrying the request after {ex!r} ({attempt + 1}/{len(delays)}, waiting {delay:g}s)")
        _wait(delay, is_cancelled)
    raise AssertionError("unreachable")


def _uncompressed(status: int, again: Callable[[], object]):
    """The gateway answered `status` to a gzipped body: again() is the same request uncompressed, since build()
    reads _gzip_refused, which stays set for the session. When that one is refused the same way the body was at
    fault, not its encoding (a request too long for the model, say), so later requests are gzipped again."""
    global _gzip_refused
    _gzip_refused = True
    print(f"Loopcut: the service refused a gzipped request (HTTP {status}); sending it uncompressed")
    try:
        return again()
    except LLMError as ex:
        if ex.status in GZIP_REFUSED:
            _gzip_refused = False
        raise


def opened(open_fn: Callable[[], object], is_cancelled: Callable[[], bool]):
    """open_fn()'s response (urlopen's, here and in the ChatGPT and Anthropic adapters), unless the turn is cancelled
    first: then Cancelled at once. _Watchdog's job before there is a stream: urlopen gives no socket to shut until it
    returns, while it can block for timeout= in the name lookup, the TLS handshake, sending the body or waiting for
    the status line. A stall tester's stand-in that accepted and sent nothing kept Stop (pressed at 3.5 s) waiting
    120 s here, 300 s on Anthropic, 600 s on the ChatGPT plan. So the open runs on its own thread while this one
    checks is_cancelled every 0.2 s, as _Watchdog does; after a Stop it goes on alone until it ends, at most
    timeout=, and a response it still gets is closed."""
    if is_cancelled():
        raise Cancelled()
    done, lock, box = threading.Event(), threading.Lock(), {}

    def run():
        try:
            box["response"] = open_fn()
        except BaseException as ex:  # Raised again on the caller's thread, an HTTPError with its body.
            box["error"] = ex
        with lock:
            done.set()
            left = box.get("left", False)
        if left:  # Nobody waits for it: free the connection.
            try:
                (box.get("response") or box.get("error")).close()
            except Exception:
                pass

    threading.Thread(target=run, name="loopcut-llm-open", daemon=True).start()
    while not done.wait(0.2):
        if is_cancelled():
            with lock:
                if not done.is_set():
                    box["left"] = True
                    raise Cancelled()
    if "error" in box:
        raise box["error"]
    return box["response"]


class _Watchdog:
    """Shuts the stream's socket when the turn is cancelled, so a read blocked on a silent stream (each line may
    take timeout= seconds) returns at once and Stop ends the turn in under a second, as the ChatGPT and Anthropic
    adapters' own watchdogs do. Before the status line, opened() does the same for the open."""

    def __init__(self, response, is_cancelled: Callable[[], bool]):
        self.response, self.is_cancelled = response, is_cancelled
        self.fired = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loopcut-llm-watchdog", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(0.2):
            if self.is_cancelled():
                self.fired = True
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


class _ReadInterrupted(Exception):
    """A read timeout, connection reset or other socket error while a stream's body was being read
    (after urlopen already returned the status line and headers): urlopen wraps none as URLError,
    since they happen later, outside its own call. streamed: whether a content or tool-call delta
    had already reached on_text this attempt, so the caller may have applied something from it."""

    def __init__(self, streamed: bool):
        super().__init__(streamed)
        self.streamed = streamed


def _read(response, on_text: Callable[[str], None], is_cancelled: Callable[[], bool],
         deadline: float | None = None, on_progress: Callable[[], None] | None = None) -> Completion:
    """One stream, start to finish, into a Completion. Raises _ReadInterrupted(streamed) for an
    OSError from the socket (a read timeout, a reset, a TLS alert, Windows' bare FileNotFoundError(2);
    not a refused certificate) or a body cut short (http.client's IncompleteRead); everything else (a
    malformed chunk, an error object in the stream, Cancelled) is unrelated to the connection and is
    not retryable here. A Stop shuts the socket (_Watchdog), and whatever that reads as is Cancelled.

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
    watchdog = _Watchdog(response, is_cancelled)
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
                    # With its code, as an HTTP error has it: Loopcut's gateway sends upstream_error this way too.
                    error = _error_payload(data.encode("utf-8"))
                    raise LLMError(_error_detail(data.encode("utf-8")),
                                   code=str(error.get("code") or error.get("type") or ""), details=error)
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    completion.usage = {"input": int(usage.get("prompt_tokens") or 0),
                                        "cached": int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
                                        "output": int(usage.get("completion_tokens") or 0)}
                    cost = usage.get("cost")  # USD, when the gateway says what the response cost (pricing.charged).
                    if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                        completion.usage["cost"] = float(cost)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if on_progress and (delta.get("content") or delta.get("tool_calls")
                                        or delta.get("reasoning_content") or delta.get("reasoning")):
                        on_progress()
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
    except (OSError, http.client.HTTPException) as ex:  # socket.timeout is TimeoutError since Python 3.10.
        if watchdog.fired or is_cancelled():
            raise Cancelled() from None
        if isinstance(ex, ssl.SSLCertVerificationError):
            raise
        raise _ReadInterrupted(streamed) from ex
    finally:
        watchdog.stop()
    if watchdog.fired:  # The socket it shut can also read as the stream's end.
        raise Cancelled()
    completion.tool_calls = [calls[i] for i in sorted(calls)]
    return completion


TRY_IT_HEADER = "x-loopcut-try-it"


def correlation_headers(conversation_id: str, role: str, try_it: str = "") -> dict:
    """x-loopcut-conversation / x-loopcut-role for Loopcut's own gateway: which of a turn's
    simultaneous streams (orchestrator, worker, router, summary, single) a request is, for its
    usage row. These headers go only to the gateway (see stream_chat).

    try_it: the setup Try it card whose unedited request started this turn (agent.try_it_card), sent as
    x-loopcut-try-it so the gateway can leave the request uncharged (credits arm, capped server-side). "" on
    every other turn, which sends no such header."""
    headers = {"x-loopcut-conversation": conversation_id, "x-loopcut-role": role}
    if try_it:
        headers[TRY_IT_HEADER] = try_it
    return headers


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


def _on_openrouter(base_url: str) -> bool:
    """Whether base_url is OpenRouter's API: the OpenRouter provider's https://openrouter.ai/api/v1, or a Custom
    one pointed there."""
    try:
        return urlparse(base_url).hostname == "openrouter.ai"
    except ValueError:
        return False


def _non_strict(tools: list[dict]) -> list[dict]:
    """OpenRouter serves OpenAI's GPT-6 models with a reasoning effort through OpenAI's Responses API, where a
    function tool without strict is strict: every property becomes required, so the model fills each optional one
    with a placeholder. On 2026-10-25 Luna sent search_assets polypizza's fields on every source and import_asset
    pick 0 beside source and id, apply_to [] and target_size 0. strict false gives only what the call needs; the
    gateway does the same for hosted users (web repo, gateway/upstream.ts) and chatgpt_responses.to_tools for the
    ChatGPT plan. A tool that already says strict keeps it."""
    return [{**tool, "function": {**tool["function"], "strict": False}}
            if isinstance(tool.get("function"), dict) and "strict" not in tool["function"] else tool for tool in tools]


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
    on_progress: Callable[[], None] | None = None,
    retry_delays: tuple[float, ...] | None = None,
) -> Completion:
    """`headers` adds to (never replaces) Authorization/Content-Type/Accept -- for Loopcut's own
    gateway, the caller passes correlation_headers()'s x-loopcut-conversation / x-loopcut-role so a
    usage row can be told apart from another conversation's on the same account (see the web repo's
    gateway/correlation.ts), and x-loopcut-try-it on a setup Try it card's turn. All x-loopcut-* headers go only to the gateway. prompt_id selects
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
    retry_delays (RETRY_DELAYS when None) bounds both: the waits before each further try, on the chat
    completions route (the router's are shorter: router.RETRY_DELAYS).

    on_progress, when given, is called for everything the model streams (text, a tool call's arguments, reasoning;
    never a keepalive): the turn's stall clock (agent.Turn.touch).

    Every provider sends the messages through image_budget.Sized: their inline images stay under a
    byte budget, and a try after the connection dropped as the request went up sends them smaller.
    Loopcut's own gateway (config.SERVICE_BASE_URL) gets the body gzipped, unless it refused that this
    session (GZIP_REFUSED). When no try at the gateway (or a stand-in) got a status line back, the error
    is network_dropped's, which says the network is in the way rather than "press Try again".

    The ChatGPT plan provider (base URL chatgpt_auth.CHATGPT_PLAN_BASE_URL) is not chat completions:
    chatgpt_responses.stream speaks the Responses API with the signed-in account's token instead,
    and returns the same Completion. It reads correlation headers locally and never sends them
    over the wire. api_key and max_tokens do not apply there.

    The Anthropic provider (base URL config.ANTHROPIC_BASE_URL, anthropic://...) is not chat completions either:
    anthropic_messages.stream speaks Anthropic's Messages API with the user's key and prompt caching, and returns
    the same Completion. It never sends the x-loopcut-* headers.

    An OpenAI model on OpenRouter (openai/*, _on_openrouter) gets each function tool with strict false
    (_non_strict); every other route sends the tools as wire_tools leaves them. On the wire only: the prompt's
    token estimate (context.fixed_tokens) counts wire_tools."""
    if prompt_id is not None and prompt_id not in ("single", "orchestrator", "worker", "router"):
        raise ValueError(f"Unknown Loopcut prompt_id: {prompt_id!r}")
    tools = wire_tools(tools)
    if base_url.startswith("chatgpt-plan"):
        from . import chatgpt_responses
        return chatgpt_responses.stream(
            model=model, messages=messages, tools=tools, reasoning_effort=reasoning_effort, on_text=on_text,
            is_cancelled=is_cancelled, call_max_seconds=call_max_seconds, headers=headers, on_retry=on_retry,
            on_progress=on_progress)
    if base_url.startswith("anthropic://"):
        from . import anthropic_messages
        return anthropic_messages.stream(
            base_url=base_url, api_key=api_key, model=model, messages=messages, tools=tools,
            reasoning_effort=reasoning_effort, on_text=on_text, is_cancelled=is_cancelled, timeout=timeout,
            max_tokens=max_tokens, call_max_seconds=call_max_seconds, headers=headers, on_retry=on_retry,
            on_progress=on_progress)
    if _on_openrouter(base_url) and model.startswith("openai/"):
        tools = _non_strict(tools)
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
    gateway = base_url.strip().rstrip("/") == config.SERVICE_BASE_URL  # Gzipped bodies: see GZIP_REFUSED.

    def build() -> urllib.request.Request:
        data, sent = json.dumps({**body, "messages": sized.messages()}).encode("utf-8"), request_headers
        if gateway and not _gzip_refused:
            import gzip
            data, sent = gzip.compress(data, 6, mtime=0), {**request_headers, "Content-Encoding": "gzip"}
        return urllib.request.Request(f"{base_url}/chat/completions", data=data, headers=sent, method="POST")
    deadline = (time.monotonic() + call_max_seconds
                if call_max_seconds and not _is_loopback(base_url) else None)
    # Two separate, small budgets: before any content this attempt, the same capped backoff as a
    # connect failure (bounded by retry_delays); once content has streamed, exactly one further
    # retry, regardless of how much of the first budget that used.
    before_content, delays = 0, RETRY_DELAYS if retry_delays is None else retry_delays
    retried_partial = False
    while True:
        first = before_content == 0 and not retried_partial  # After that a try has had its status line.
        response = _open(build, base_url, timeout, is_cancelled, sized.tighten, delays,
                         network_dropped if service and first else None)
        if service and prompt_id is not None:
            cloud.note_prompt_version(response.headers.get("x-loopcut-prompt-version"))
        try:
            return _read(response, on_text, is_cancelled, deadline, on_progress)
        except _ReadInterrupted as ex:
            cause = ex.__cause__
            shown = network_cause(cause) or repr(cause)
            if ex.streamed:
                if retried_partial or on_retry is None:
                    raise LLMError(f"Connection lost while streaming the reply: {shown}",
                                   code=CONNECTION_LOST) from cause
                retried_partial = True
                print(f"Loopcut: retrying once from scratch after the stream broke mid-reply ({cause!r})")
                on_retry()
                continue
            if before_content == len(delays):
                raise LLMError(f"Connection lost before any reply arrived: {shown}", code=CONNECTION_LOST) from cause
            delay = delays[before_content]
            print(f"Loopcut: retrying the request after {cause!r} ({before_content + 1}/{len(delays)}, "
                  f"waiting {delay:g}s)")
            before_content += 1
            _wait(delay, is_cancelled)
