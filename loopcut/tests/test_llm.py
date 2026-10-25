"""llm.py on its own: extra headers reach the server without overriding the api key, and a 429's
Retry-After is honoured and capped, not obeyed verbatim. No Blender needed.

    python3 -m unittest discover -s loopcut/tests
"""

import io
import json
import os
import ssl
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_imbuf  # noqa: E402
from loopcut import image_budget, llm  # noqa: E402


def sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def text_reply(text: str) -> bytes:
    return sse({"choices": [{"delta": {"content": text}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}]})


class Hang:
    """A reply that writes `prefix` (maybe none), then stalls: the client's socket read times out
    (a bare TimeoutError, not an HTTPError/URLError -- urlopen has already returned by then) after
    `delay`, which must clear the test's timeout= for stream_chat. With reset=True the connection is
    hard-closed (RST) instead, for ConnectionResetError."""

    def __init__(self, prefix: bytes = b"", delay: float = 0.25, reset: bool = False):
        self.prefix, self.delay, self.reset = prefix, delay, reset


class Trickle:
    """Streams `pieces` as separate content deltas, `interval` seconds apart, then a normal
    finish_reason "stop" and [DONE]: each piece keeps the client's read timeout from firing on its
    own, the way a provider's periodic reasoning deltas (or cc_bridge's keepalive comments) do, so
    only a wall-clock limit (call_max_seconds) can cut it short."""

    def __init__(self, pieces: list[str], interval: float = 0.03):
        self.pieces, self.interval = pieces, interval


def windows_drop() -> OSError:
    """What Windows raised from the socket in 0.2.14's logs, waiting for a hosted reply's status line: a bare
    OSError (no winerror), which urlopen does not wrap as URLError."""
    return FileNotFoundError(2, "No such file or directory")


class Breaks(io.BytesIO):
    """A response urlopen returned whose body reads `data`, then raises `error`: the socket broke mid-body."""

    def __init__(self, data: bytes, error: OSError):
        super().__init__(data)
        self.headers, self.error = {}, error

    def __iter__(self):
        yield from iter(self.readline, b"")
        raise self.error


class Script:
    def __init__(self):
        self.replies: list = []
        self.requests: list[dict] = []  # {"body": ..., "headers": <email.message.Message>}


SCRIPT = Script()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # self.headers stays an email.message.Message (case-insensitive .get), not a plain dict:
        # Request.add_header capitalizes only the header's first letter, so "x-loopcut-role"
        # arrives as "X-loopcut-role", not the title-cased form a naive dict compare would expect.
        SCRIPT.requests.append({"body": body, "headers": self.headers})
        reply = SCRIPT.replies.pop(0)
        if isinstance(reply, tuple):
            status, payload, extra_headers = reply
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
            return
        if isinstance(reply, Hang):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if reply.prefix:
                self.wfile.write(reply.prefix)
                self.wfile.flush()
            if reply.reset:
                import socket
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.connection.close()
                return
            time.sleep(reply.delay)
            return
        if isinstance(reply, Trickle):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                for piece in reply.pieces:
                    self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(reply.interval)
                self.wfile.write(b"data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}).encode() + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass  # The client hit its own call_max_seconds and closed the stream; nothing more to send.
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


class ErrorHeadersTest(unittest.TestCase):
    def test_reservation_headers_are_totals_and_survive_a_successful_stream(self):
        from email.message import Message
        response = io.BytesIO(text_reply("Done."))
        response.headers = Message()
        for name, value in {"X-Loopcut-Plan": "free", "X-Loopcut-Session-Used": "0.62",
                            "X-Loopcut-Session-Used-With-Reservations": "0.82",
                            "X-Loopcut-Week-Used": "0.31",
                            "X-Loopcut-Week-Used-With-Reservations": "0.43"}.items():
            response.headers[name] = value
        reply = llm._read(response, lambda _: None, lambda: False)
        self.assertEqual(reply.account["session"], {"used": 0.62, "used_with_reservations": 0.82, "resets_at": None})
        self.assertEqual(reply.account["week"], {"used": 0.31, "used_with_reservations": 0.43, "resets_at": None})

    def test_missing_or_invalid_reservation_headers_preserve_settled_usage(self):
        for invalid in (None, "", "bad", "nan", "inf", "-inf", "-0.1", True, [], {}, 10 ** 400):
            with self.subTest(invalid=str(invalid)):
                headers = {"x-loopcut-plan": "free", "x-loopcut-session-used": "0.82",
                           "x-loopcut-week-used": None, "x-loopcut-session-used-with-reservations": invalid}
                info = llm.account_from_headers(headers)
                self.assertEqual(info["session"], {"used": 0.82, "resets_at": None})
                self.assertIsNone(info["week"]["used"])
        self.assertIsNone(llm.account_from_headers({"x-loopcut-session-used-with-reservations": "0.9"}))
        for value, expected in (("0", 0.0), (" 0.91 ", 0.91), ("1.2", 1.0)):
            info = llm.account_from_headers({"x-loopcut-plan": "free", "x-loopcut-session-used-with-reservations": value})
            self.assertEqual(info["session"]["used_with_reservations"], expected)

    def test_refusal_keeps_plan_and_window_headers(self):
        from email.message import Message
        from urllib.error import HTTPError
        from urllib.request import Request
        headers = Message()
        headers["x-loopcut-plan"] = "free"
        headers["x-loopcut-session-used"] = "0.82"
        headers["x-loopcut-session-used-with-reservations"] = "0.98"
        headers["x-loopcut-session-resets-at"] = "2099-10-21T18:00:00Z"
        body = io.BytesIO(json.dumps({"error": {"code": "session_limit", "message": "Used up."}}).encode())
        refusal = HTTPError("https://loopcut.org/v1/chat/completions", 402, "Used up", headers, body)
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=refusal) as request:
            with self.assertRaises(llm.LLMError) as caught:
                llm._open(lambda: Request(refusal.url), "https://loopcut.org/v1", 1, lambda: False)
        error = caught.exception
        self.assertEqual((error.status, error.code), (402, "session_limit"))
        self.assertEqual(error.account["plan"], "free")
        self.assertEqual(error.account["session"], {"used": 0.82, "used_with_reservations": 0.98,
                                                    "resets_at": "2099-10-21T18:00:00Z"})
        request.assert_called_once()


class LLMTest(unittest.TestCase):
    def setUp(self):
        SCRIPT.replies.clear()
        SCRIPT.requests.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def call(self, **extra):
        return llm.stream_chat(
            base_url=self.base_url, api_key="secret", model="fast",
            messages=[{"role": "user", "content": "hi"}], tools=[],
            reasoning_effort="low", on_text=lambda _: None, is_cancelled=lambda: False,
            **extra,
        )

    def test_extra_headers_reach_the_server(self):
        SCRIPT.replies.append(text_reply("hi"))
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": self.base_url}):
            self.call(headers={"x-loopcut-conversation": "abc123", "x-loopcut-role": "worker"})
        headers = SCRIPT.requests[0]["headers"]
        self.assertEqual(headers.get("x-loopcut-conversation"), "abc123")
        self.assertEqual(headers.get("x-loopcut-role"), "worker")

    def test_the_gateway_hears_the_add_on_blender_and_platform(self):
        """x-loopcut-client on hosted requests, as on every other Loopcut request; never to another provider."""
        SCRIPT.replies += [text_reply("hi"), text_reply("hi")]
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": self.base_url}):
            self.call()
        self.call()
        hosted, other = (request["headers"] for request in SCRIPT.requests[-2:])
        self.assertRegex(hosted.get("x-loopcut-client", ""),
                         r"^loopcut-addon/\d+\.\d+\.\d+ blender/\S+ os/(mac|windows|linux) arch/(arm64|x86_64|other)$")
        self.assertNotIn("x-loopcut-client", {k.lower() for k in other})

    def test_extra_headers_never_override_the_api_key_or_content_type(self):
        SCRIPT.replies.append(text_reply("hi"))
        self.call(headers={"Authorization": "Bearer nope", "Content-Type": "text/plain", "Accept": "*/*"})
        headers = SCRIPT.requests[0]["headers"]
        self.assertEqual(headers.get("Authorization"), "Bearer secret")
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Accept"), "text/event-stream")

    def test_no_headers_argument_still_works(self):
        SCRIPT.replies.append(text_reply("ok"))
        self.assertEqual(self.call().text, "ok")

    def test_a_cost_in_the_final_usage_chunk_rides_on_the_usage(self):
        """The gateway's usage.cost (USD), which the job budget charges over the tokens (pricing.charged)."""
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 4}}
        for cost, expected in ((0.0123, 0.0123), (0, 0.0), (-1, None), ("0.01", None), (True, None),
                               (float("nan"), None), (None, None)):
            with self.subTest(cost=cost):
                SCRIPT.replies.append(sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
                                          {"choices": [], "usage": {**usage, **({} if cost is None else {"cost": cost})}}))
                completion = self.call()
                self.assertEqual(completion.usage.get("cost"), expected)
                self.assertEqual({k: completion.usage[k] for k in ("input", "cached", "output")},
                                 {"input": 10, "cached": 4, "output": 2})

    def test_an_error_event_mid_stream_keeps_its_code_as_an_http_error_does(self):
        """Loopcut's gateway ends a stream whose vendor failed with an error event; agent.py reads its code."""
        error = {"code": "upstream_error", "message": "The model provider returned an error."}
        SCRIPT.replies.append(sse({"choices": [{"delta": {"content": "Here is "}}]}, {"error": error}))
        with self.assertRaises(llm.LLMError) as caught:
            self.call()
        ex = caught.exception
        self.assertEqual((str(ex), ex.status, ex.code, ex.details),
                         ("The model provider returned an error.", None, "upstream_error", error))
        SCRIPT.replies.append(sse({"error": "overloaded"}))  # No error object: no code, as before.
        with self.assertRaises(llm.LLMError) as caught:
            self.call()
        self.assertEqual((str(caught.exception), caught.exception.code), ('{"error": "overloaded"}', ""))

    def test_correlation_headers_builds_both_headers_for_a_role(self):
        self.assertEqual(llm.correlation_headers("abc123", "router"),
                         {"x-loopcut-conversation": "abc123", "x-loopcut-role": "router"})
        self.assertEqual(llm.correlation_headers("abc123", "router", ""),
                         {"x-loopcut-conversation": "abc123", "x-loopcut-role": "router"}, "no card, no header")

    def test_correlation_headers_name_a_try_it_cards_turn(self):
        self.assertEqual(llm.correlation_headers("abc123", "worker", "motion"),
                         {"x-loopcut-conversation": "abc123", "x-loopcut-role": "worker", "x-loopcut-try-it": "motion"})

    def test_retry_after_is_respected_and_capped(self):
        llm.RETRY_DELAYS, saved_delays = (0.01, 0.01, 0.01), llm.RETRY_DELAYS
        self.addCleanup(setattr, llm, "RETRY_DELAYS", saved_delays)
        llm.MAX_RETRY_AFTER, saved_cap = 0.05, llm.MAX_RETRY_AFTER
        self.addCleanup(setattr, llm, "MAX_RETRY_AFTER", saved_cap)
        # A gateway concurrency_limit 429 with a Retry-After far larger than a real wait: honoured
        # (the delay is at least it) but capped at MAX_RETRY_AFTER, not slept verbatim.
        SCRIPT.replies.append((429, {"error": {"message": "busy", "code": "concurrency_limit"}}, {"Retry-After": "999"}))
        SCRIPT.replies.append(text_reply("ok"))
        started = time.monotonic()
        completion = self.call()
        elapsed = time.monotonic() - started
        self.assertEqual(completion.text, "ok")
        self.assertEqual(len(SCRIPT.requests), 2)
        self.assertLess(elapsed, 1.0, "a huge Retry-After was capped, not honoured verbatim")

    def test_retry_after_below_the_base_delay_does_not_shorten_the_wait(self):
        llm.RETRY_DELAYS, saved_delays = (0.2, 0.01, 0.01), llm.RETRY_DELAYS
        self.addCleanup(setattr, llm, "RETRY_DELAYS", saved_delays)
        SCRIPT.replies.append((429, {"error": {"message": "busy"}}, {"Retry-After": "0"}))
        SCRIPT.replies.append(text_reply("ok"))
        started = time.monotonic()
        self.call()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.2)

    # ------------------------------------------------------------------ read timeouts / resets

    def hung(self, **kw) -> llm.Completion:
        """A short timeout= so Hang's stall (0.25s) reliably reads as TimeoutError, same capped
        RETRY_DELAYS as the 429 tests."""
        llm.RETRY_DELAYS, saved_delays = (0.01, 0.01, 0.01), llm.RETRY_DELAYS
        self.addCleanup(setattr, llm, "RETRY_DELAYS", saved_delays)
        return llm.stream_chat(base_url=self.base_url, api_key="secret", model="fast",
                               messages=[{"role": "user", "content": "hi"}], tools=[], reasoning_effort="low",
                               is_cancelled=lambda: False, timeout=0.05, **kw)

    def test_a_read_timeout_before_any_content_is_retried_like_a_5xx(self):
        SCRIPT.replies.append(Hang())
        SCRIPT.replies.append(text_reply("ok"))
        completion = self.hung(on_text=lambda _: None)
        self.assertEqual(completion.text, "ok")
        self.assertEqual(len(SCRIPT.requests), 2, "no content arrived yet, so a plain retry needed no on_retry")

    def test_a_connection_reset_before_any_content_is_retried_too(self):
        SCRIPT.replies.append(Hang(reset=True))
        SCRIPT.replies.append(text_reply("ok"))
        completion = self.hung(on_text=lambda _: None)
        self.assertEqual(completion.text, "ok")
        self.assertEqual(len(SCRIPT.requests), 2)

    def test_read_timeouts_before_content_are_capped_like_connect_failures(self):
        SCRIPT.replies += [Hang(), Hang(), Hang(), Hang()]
        with self.assertRaises(llm.LLMError) as cm:
            self.hung(on_text=lambda _: None)
        self.assertIn("Connection lost before any reply arrived", str(cm.exception))
        self.assertEqual(len(SCRIPT.requests), 4, "the same 1-retry budget as a 429/5xx: 4 tries total")

    def test_a_read_timeout_after_content_streamed_is_not_retried_without_on_retry(self):
        SCRIPT.replies.append(Hang(prefix=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'))
        SCRIPT.replies.append(text_reply("ok"))  # Never reached: no on_retry means no retry.
        with self.assertRaises(llm.LLMError) as cm:
            self.hung(on_text=lambda _: None)
        self.assertIn("Connection lost while streaming the reply", str(cm.exception))
        self.assertEqual(len(SCRIPT.requests), 1, "nothing to undo was offered, so it surfaced at once")

    def test_a_read_timeout_after_content_streamed_retries_once_from_scratch(self):
        """The agent loop's own pattern: on_text appends to a buffer as the reply streams; on_retry
        clears it before the retried request's own deltas start arriving, so nothing doubles up."""
        SCRIPT.replies.append(Hang(prefix=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'))
        SCRIPT.replies.append(text_reply("fresh"))
        buffer = []
        completion = self.hung(on_text=buffer.append, on_retry=lambda: buffer.clear())
        self.assertEqual(completion.text, "fresh")
        self.assertEqual(buffer, ["fresh"], "the stale 'partial' delta was cleared by on_retry, not left in the buffer")
        self.assertEqual(len(SCRIPT.requests), 2)

    def test_a_read_timeout_after_content_streamed_retries_only_once(self):
        SCRIPT.replies.append(Hang(prefix=b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'))
        SCRIPT.replies.append(Hang(prefix=b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n'))
        SCRIPT.replies.append(text_reply("never reached"))
        buffer = []
        with self.assertRaises(llm.LLMError) as cm:
            self.hung(on_text=buffer.append, on_retry=lambda: buffer.clear())
        self.assertIn("Connection lost while streaming the reply", str(cm.exception))
        self.assertEqual(len(SCRIPT.requests), 2, "one retry used, then the second break surfaced as today")

    # ------------------------------------------------------------------ bare OSErrors (Windows)

    def flaky(self, *failures, **kw) -> llm.Completion:
        """stream_chat whose first urlopen calls go to `failures` in turn (an exception is raised from urlopen, a
        Breaks is returned as its response), then to the scripted server. self.opens counts every call."""
        real, queue, self.opens = urllib.request.urlopen, list(failures), 0

        def urlopen(request, timeout=None):
            self.opens += 1
            if not queue:
                return real(request, timeout=timeout)
            failure = queue.pop(0)
            if isinstance(failure, BaseException):
                raise failure
            return failure
        with mock.patch.object(llm.urllib.request, "urlopen", urlopen), \
                mock.patch.object(llm, "_wait", lambda s, c: None):
            return llm.stream_chat(base_url=self.base_url, api_key="secret", model="fast",
                                   messages=[{"role": "user", "content": "hi"}], tools=[], reasoning_effort="low",
                                   is_cancelled=lambda: False, **{"on_text": lambda _: None, **kw})

    def test_a_bare_oserror_from_urlopen_is_retried_like_a_dropped_connection(self):
        """0.2.14 logs: on Windows the status line never came and getresponse raised FileNotFoundError(2), which
        urlopen does not wrap. The turn ended as 'Internal error' with no Try again; now it is retried."""
        SCRIPT.replies.append(text_reply("ok"))
        self.assertEqual(self.flaky(windows_drop()).text, "ok")
        self.assertEqual((self.opens, len(SCRIPT.requests)), (2, 1))

    def test_a_bare_oserror_from_urlopen_every_try_ends_as_connection_lost(self):
        with self.assertRaises(llm.LLMError) as caught:
            self.flaky(*[windows_drop() for _ in range(len(llm.RETRY_DELAYS) + 1)])
        self.assertEqual((caught.exception.code, self.opens), (llm.CONNECTION_LOST, 4), "4 tries, as for a URLError")
        self.assertEqual(str(caught.exception), "The connection to the model dropped before it could answer. "
                                                "Nothing was lost: press Try again. (network error, errno 2)")

    def test_an_unknown_oserror_reads_as_a_network_error_and_a_known_one_keeps_its_words(self):
        """On a card that says the connection dropped, 'No such file or directory' read as a missing file (0.2.14)."""
        self.assertEqual(llm.network_cause(windows_drop()), "network error, errno 2")
        self.assertEqual(llm.network_cause(OSError()), "network error, OSError")
        for known in (TimeoutError("timed out"), ConnectionResetError(54, "Connection reset by peer"),
                      ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] bad record mac"),
                      urllib.error.URLError(windows_drop()), ValueError("not a network error")):
            with self.subTest(known=repr(known)):
                self.assertIsNone(llm.network_cause(known))

    def test_a_bare_oserror_reading_the_body_before_any_content_is_retried(self):
        SCRIPT.replies.append(text_reply("ok"))
        reasoning = b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n'  # Nothing shown yet.
        self.assertEqual(self.flaky(Breaks(reasoning, windows_drop())).text, "ok")
        self.assertEqual(self.opens, 2)
        with self.assertRaises(llm.LLMError) as caught:
            self.flaky(*[Breaks(b"", windows_drop()) for _ in range(len(llm.RETRY_DELAYS) + 1)])
        self.assertEqual((caught.exception.code, self.opens), (llm.CONNECTION_LOST, 4))
        self.assertEqual(str(caught.exception), "Connection lost before any reply arrived: network error, errno 2")

    def test_a_bare_oserror_reading_the_body_after_content_follows_the_mid_reply_rule(self):
        partial = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        with self.assertRaises(llm.LLMError) as caught:
            self.flaky(Breaks(partial, windows_drop()))
        self.assertEqual((caught.exception.code, self.opens), (llm.CONNECTION_LOST, 1), "no on_retry: surfaced at once")
        self.assertEqual(str(caught.exception), "Connection lost while streaming the reply: network error, errno 2")
        SCRIPT.replies.append(text_reply("fresh"))
        buffer = []
        completion = self.flaky(Breaks(partial, windows_drop()), on_text=buffer.append, on_retry=buffer.clear)
        self.assertEqual((completion.text, buffer, self.opens), ("fresh", ["fresh"], 2), "once, from scratch")

    def test_an_http_error_and_a_refused_certificate_are_not_dropped_connections(self):
        """Both are OSErrors too, but an HTTPError is an answer and a refused certificate fails the same way every
        time: as before, the first is the API's error at once, and the second is raised as it is."""
        body = io.BytesIO(json.dumps({"error": {"code": "invalid_api_key", "message": "Bad key."}}).encode())
        with self.assertRaises(llm.LLMError) as caught:
            self.flaky(urllib.error.HTTPError(self.base_url, 401, "Unauthorized", {}, body))
        self.assertEqual((caught.exception.status, caught.exception.code, self.opens), (401, "invalid_api_key", 1))
        refused = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        for failure in (refused, Breaks(b"", refused)):
            with self.subTest(failure=type(failure).__name__), self.assertRaises(ssl.SSLCertVerificationError):
                self.flaky(failure)
            self.assertEqual(self.opens, 1)

    # ------------------------------------------------------------------ max_tokens

    def test_max_tokens_is_sent_when_given(self):
        SCRIPT.replies.append(text_reply("ok"))
        self.call(max_tokens=12345)
        self.assertEqual(SCRIPT.requests[0]["body"]["max_tokens"], 12345)

    def test_max_tokens_is_omitted_by_default(self):
        SCRIPT.replies.append(text_reply("ok"))
        self.call()
        self.assertNotIn("max_tokens", SCRIPT.requests[0]["body"])

    def test_max_tokens_zero_is_also_omitted(self):
        """0 is not a real cap (config.py never emits it), but a falsy max_tokens should not be
        sent as one either."""
        SCRIPT.replies.append(text_reply("ok"))
        self.call(max_tokens=0)
        self.assertNotIn("max_tokens", SCRIPT.requests[0]["body"])

    # ------------------------------------------------------------------ image budget (image_budget.py)

    def looks(self, n: int = 6) -> list[dict]:
        """n stored renders as the agent sends them: ~390 KB of base64 each, 2.4 MB for six."""
        import base64
        photo = base64.b64encode(fake_imbuf.make(1568, 882, kind="JPEG", quality=85)).decode()
        return [{"role": "user", "content": [{"type": "text", "text": f"Viewport capture: look {i}."},
                                             {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo}"}}]}
                for i in range(n)]

    def fitted_call(self, messages: list, urlopen=None):
        patches = [mock.patch.dict(sys.modules, {"imbuf": fake_imbuf}), mock.patch.object(llm, "_wait", lambda s, c: None)]
        if urlopen:
            patches.append(mock.patch.object(llm.urllib.request, "urlopen", urlopen))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return llm.stream_chat(base_url=self.base_url, api_key="secret", model="fast", messages=messages, tools=[],
                               reasoning_effort="low", on_text=lambda _: None, is_cancelled=lambda: False)

    def test_a_broken_pipe_upload_is_retried_with_smaller_images(self):
        # The gateway, BYOK providers and the Claude Code bridge: the retry does not re-send the bytes that were cut.
        import urllib.error
        import urllib.request
        SCRIPT.replies.append(text_reply("ok"))
        sizes, real = [], urllib.request.urlopen

        def urlopen(request, timeout=None):
            sizes.append(len(request.data))
            if len(sizes) == 1:
                raise urllib.error.URLError(BrokenPipeError(32, "Broken pipe"))
            return real(request, timeout=timeout)
        self.assertEqual(self.fitted_call(self.looks(), urlopen).text, "ok")
        self.assertEqual(len(sizes), 2)
        self.assertLess(sizes[1], min(sizes[0], image_budget.REQUEST_BYTES // 2 + 50_000), "the retry: half the budget")
        self.assertEqual(len(SCRIPT.requests), 1)

    def test_a_413_is_retried_with_smaller_images_and_only_while_they_can_shrink(self):
        SCRIPT.replies += [(413, {"error": {"message": "Request Entity Too Large"}}, None), text_reply("ok")]
        self.assertEqual(self.fitted_call(self.looks()).text, "ok")
        first, second = (len(json.dumps(r["body"])) for r in SCRIPT.requests)
        self.assertLess(second, min(first, image_budget.REQUEST_BYTES // 2 + 50_000))
        SCRIPT.requests.clear()
        SCRIPT.replies.append((413, {"error": {"message": "Request Entity Too Large"}}, None))
        with self.assertRaises(llm.LLMError) as caught:
            self.fitted_call([{"role": "user", "content": "hi"}])
        self.assertEqual((caught.exception.status, len(SCRIPT.requests)), (413, 1), "no images: the same bytes again")

    # ------------------------------------------------------------------ call_max_seconds

    def test_is_loopback_matches_127_0_0_1_only(self):
        self.assertTrue(llm._is_loopback("http://127.0.0.1:8765/v1"))
        self.assertFalse(llm._is_loopback("https://api.example.com/v1"))
        self.assertFalse(llm._is_loopback("http://localhost:8765/v1"))
        self.assertFalse(llm._is_loopback("not-a-url::::"))

    def test_call_max_seconds_cuts_a_long_stream_short(self):
        """A provider that keeps sending deltas (so the 120s read timeout never fires on its own,
        just like the runaway GLM reasoning calls this guards against) is still cut off once the
        call's own wall clock runs out, and reported the same way a capped reply is."""
        SCRIPT.replies.append(Trickle(["a"] * 40, interval=0.03))  # ~1.2s if left to finish
        with mock.patch.object(llm, "_is_loopback", return_value=False):
            started = time.monotonic()
            completion = self.call(call_max_seconds=0.12)
            elapsed = time.monotonic() - started
        self.assertEqual(completion.finish_reason, "length")
        self.assertTrue(completion.timed_out)
        self.assertLess(elapsed, 0.8, "cut well before all 40 pieces could arrive")
        self.assertLess(len(completion.text), 40, "some pieces arrived, but not the whole reply")

    def test_call_max_seconds_none_never_cuts_the_stream(self):
        SCRIPT.replies.append(Trickle(["a", "b", "c"], interval=0.02))
        with mock.patch.object(llm, "_is_loopback", return_value=False):
            completion = self.call()  # call_max_seconds defaults to None.
        self.assertEqual((completion.text, completion.finish_reason, completion.timed_out), ("abc", "stop", False))

    def test_call_max_seconds_is_ignored_for_a_127_0_0_1_base_url(self):
        """self.base_url is 127.0.0.1 (the test server): the bridge's own address, so the wall-clock
        limit never applies even though it is given and would otherwise cut this stream short."""
        SCRIPT.replies.append(Trickle(["a", "b", "c", "d", "e"], interval=0.03))  # ~0.15s
        completion = self.call(call_max_seconds=0.05)
        self.assertEqual((completion.text, completion.finish_reason, completion.timed_out), ("abcde", "stop", False))
