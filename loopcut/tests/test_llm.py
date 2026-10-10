"""llm.py on its own: extra headers reach the server without overriding the api key, and a 429's
Retry-After is honoured and capped, not obeyed verbatim. No Blender needed.

    python3 -m unittest discover -s loopcut/tests
"""

import json
import os
import struct
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import llm  # noqa: E402


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

    def test_correlation_headers_builds_both_headers_for_a_role(self):
        self.assertEqual(llm.correlation_headers("abc123", "router"),
                         {"x-loopcut-conversation": "abc123", "x-loopcut-role": "router"})

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
