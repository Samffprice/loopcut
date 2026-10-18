"""The Anthropic provider without the network: anthropic_messages.py against a fake Messages API (message
and tool mapping, cache_control placement, thinking settings and replay, streaming into llm.Completion,
usage and its price, every error path, Stop mid-stream), and the same provider under llm.stream_chat, the
agent loop and the router. No Blender needed.

    python3 -m unittest discover -s loopcut/tests -p 'test_anthropic.py'
"""

import base64
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, anthropic_messages as am, config, llm, pricing, router, state  # noqa: E402

KEY = "test-anthropic-key"   # Not a secret: what the fake server checks for.
BASE = config.ANTHROPIC_BASE_URL
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\0" * 32).decode()
TOOLS = [{"type": "function", "function": {"name": "run_python", "description": "Run code in Blender.",
                                           "parameters": {"type": "object", "properties": {"code": {"type": "string"}}}}},
         {"type": "function", "function": {"name": "capture_viewport", "description": "Look.",
                                           "parameters": {"type": "object", "properties": {}}}}]


# --- A scripted Messages API ------------------------------------------------------------------------

def event(kind: str, **data) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()


def text(*pieces):
    return ("text", pieces)


def thinking(*pieces, signature="sig-1"):
    return ("thinking", pieces, signature)


def tool(call_id, name, *pieces):
    return ("tool_use", call_id, name, pieces)


USAGE = {"input_tokens": 50, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 3000, "output_tokens": 1}


def reply(*blocks, stop="end_turn", usage=None, stop_details=None, output=120, end=True) -> bytes:
    """A whole streamed reply, as the API sends it: message_start, a ping, each block, message_delta with
    the stop reason and cumulative output tokens, message_stop. end=False stops after the blocks."""
    out = [event("message_start", message={"id": "msg_1", "type": "message", "role": "assistant", "content": [],
                                           "model": "claude-opus-5-5", "stop_reason": None,
                                           "usage": usage or USAGE}), event("ping")]
    for index, block in enumerate(blocks):
        if block[0] == "text":
            out.append(event("content_block_start", index=index, content_block={"type": "text", "text": ""}))
            out += [event("content_block_delta", index=index, delta={"type": "text_delta", "text": p}) for p in block[1]]
        elif block[0] == "thinking":
            out.append(event("content_block_start", index=index,
                             content_block={"type": "thinking", "thinking": "", "signature": ""}))
            out += [event("content_block_delta", index=index, delta={"type": "thinking_delta", "thinking": p})
                    for p in block[1]]
            out.append(event("content_block_delta", index=index, delta={"type": "signature_delta", "signature": block[2]}))
        elif block[0] == "redacted":
            out.append(event("content_block_start", index=index,
                             content_block={"type": "redacted_thinking", "data": block[1]}))
        else:
            out.append(event("content_block_start", index=index,
                             content_block={"type": "tool_use", "id": block[1], "name": block[2], "input": {}}))
            out += [event("content_block_delta", index=index, delta={"type": "input_json_delta", "partial_json": p})
                    for p in block[3]]
        out.append(event("content_block_stop", index=index))
    if end:
        delta = {"stop_reason": stop, "stop_sequence": None}
        if stop_details:
            delta["stop_details"] = stop_details
        out.append(event("message_delta", delta=delta, usage={"output_tokens": output}))
        out.append(event("message_stop"))
    return b"".join(out)


def error_body(kind: str, message: str = "nope", **extra) -> bytes:
    return json.dumps({"type": "error", "error": {"type": kind, "message": message, **extra},
                       "request_id": "req_body"}).encode()


class Fake(BaseHTTPRequestHandler):
    """api.anthropic.com/v1: a queue of (status, payload[, headers][, stall seconds]); every request recorded."""
    script: list = []
    requests: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        Fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": None})
        body = json.dumps({"data": [{"id": "claude-opus-5-5", "type": "model"}, {"id": "claude-haiku-5-5", "type": "model"}],
                           "has_more": False}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        Fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                              "body": json.loads(self.rfile.read(length))})
        entry = Fake.script.pop(0) if Fake.script else (200, reply(text("done")))
        status, payload, headers, stall = (list(entry) + [{}, 0.0])[:4] if len(entry) < 4 else entry
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
        self.send_header("request-id", f"req_{len(Fake.requests)}")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if status != 200:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        if stall:
            self.wfile.flush()
            time.sleep(stall)


class Server:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    model = "claude-opus-5-5"

    def setUp(self):
        self.server = Server()
        Fake.script, Fake.requests = [], []
        env = mock.patch.dict(os.environ, {"LOOPCUT_ANTHROPIC_API_URL": self.server.url})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("LOOPCUT_ANTHROPIC_THINKING", None)
        self.addCleanup(self.server.close)
        am._disabled.clear()
        am.replay.clear()
        self.waits = []
        patcher = mock.patch.object(llm, "_wait", lambda seconds, cancelled: self.waits.append(seconds))
        patcher.start()
        self.addCleanup(patcher.stop)

    def stream(self, messages=None, model=None, **kwargs):
        seen = []
        kwargs.setdefault("is_cancelled", lambda: False)
        completion = am.stream(base_url=BASE, api_key=KEY, model=model or self.model,
                               messages=messages or conversation(), tools=TOOLS, reasoning_effort="medium",
                               on_text=seen.append, headers=llm.correlation_headers("c0ffee", "single"), **kwargs)
        return completion, "".join(seen)

    def body(self, n=-1) -> dict:
        return Fake.requests[n]["body"]


def conversation(*more) -> list[dict]:
    return [{"role": "system", "content": "You are Loopcut."},
            {"role": "user", "content": [{"type": "text", "text": "Add a cube."},
                                         {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}]},
            *more]


def marks(body: dict) -> list[str]:
    """Where the request's cache_control breakpoints are, as paths."""
    found = [f"system.{n}" for n, b in enumerate(body.get("system") or []) if "cache_control" in b]
    found += [f"tools.{n}" for n, t in enumerate(body.get("tools") or []) if "cache_control" in t]
    for m, message in enumerate(body["messages"]):
        found += [f"messages.{m}.{n}" for n, b in enumerate(message["content"]) if "cache_control" in b]
    return found


# --- Mapping, no network ---------------------------------------------------------------------------

class MappingTest(unittest.TestCase):
    def setUp(self):
        am._disabled.clear()
        am.replay.clear()

    def test_the_default_model_and_base_url_agree_across_modules(self):
        self.assertEqual(config.ANTHROPIC_MODEL, am.MODELS[0])
        self.assertEqual(am.MODELS[0], "claude-opus-5-5")
        self.assertTrue(config.anthropic(BASE))
        self.assertFalse(config.anthropic("https://api.anthropic.com/v1"), "Custom's OpenAI-compatible endpoint stays chat completions")
        config._check_base_url(BASE, "LOOPCUT_BASE_URL")   # Accepted: always sent as https.
        with mock.patch.dict(os.environ, {"LOOPCUT_ANTHROPIC_API_URL": ""}):
            self.assertEqual(am.api_base(BASE), "https://api.anthropic.com/v1")
        with mock.patch.dict(os.environ, {"LOOPCUT_ANTHROPIC_API_URL": "https://evil.example/v1"}):
            with self.assertRaises(llm.LLMError):
                am.api_base(BASE)

    def test_tools_and_the_system_prompt(self):
        system, wired = am.to_messages(conversation())
        self.assertEqual(system, "You are Loopcut.")
        self.assertEqual(am.to_tools(TOOLS)[0], {"name": "run_python", "description": "Run code in Blender.",
                                                 "input_schema": TOOLS[0]["function"]["parameters"]})
        self.assertEqual(am.to_tools(TOOLS)[1]["input_schema"], {"type": "object", "properties": {}})
        odd = am.wire_name("loopcut_cloud.image generate")
        self.assertRegex(odd, r"^[A-Za-z0-9_-]{1,64}$")
        self.assertEqual(am.loopcut_name(odd), "loopcut_cloud.image generate")
        self.assertEqual(wired[0]["role"], "user")

    def test_images_are_base64_blocks_of_their_real_type_within_the_size_limit(self):
        _, wired = am.to_messages([{"role": "user", "content": [
            {"type": "text", "text": "Look:"},
            {"type": "text", "text": "   "},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{JPEG}"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * (am.MAX_IMAGE_BASE64 + 4)}},
            {"type": "image_url", "image_url": {"url": "https://example.com/ref.png"}}]}])
        blocks = wired[0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "Look:"}, "the blank text block is dropped")
        self.assertEqual(blocks[1]["source"], {"type": "base64", "media_type": "image/png", "data": PNG})
        self.assertEqual(blocks[2]["source"]["media_type"], "image/jpeg", "labelled PNG, sent as what it is")
        self.assertEqual(blocks[3]["type"], "text")
        self.assertIn("too large", blocks[3]["text"])
        self.assertEqual(blocks[4]["source"], {"type": "url", "url": "https://example.com/ref.png"})

    def test_calls_and_results_pair_up_with_results_first_and_captures_after(self):
        history = conversation(
            {"role": "assistant", "content": "Adding it.", "tool_calls": [
                {"id": "toolu_1", "type": "function", "function": {"name": "run_python", "arguments": '{"code": "x"}'}},
                {"id": "toolu_2", "type": "function", "function": {"name": "capture_viewport", "arguments": "{"}}]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "cube added"},
            {"role": "tool", "tool_call_id": "toolu_2", "content": "Error: no viewport", "is_error": True},
            {"role": "user", "content": [{"type": "text", "text": "Viewport capture: the cube"},
                                         {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}]})
        _, wired = am.to_messages(history)
        self.assertEqual([m["role"] for m in wired], ["user", "assistant", "user"])
        self.assertEqual(wired[1]["content"], [
            {"type": "text", "text": "Adding it."},
            {"type": "tool_use", "id": "toolu_1", "name": "run_python", "input": {"code": "x"}},
            {"type": "tool_use", "id": "toolu_2", "name": "capture_viewport", "input": {"unparsed_arguments": "{"}}])
        results = wired[2]["content"]
        self.assertEqual([b["type"] for b in results], ["tool_result", "tool_result", "text", "image"])
        self.assertEqual(results[0], {"type": "tool_result", "tool_use_id": "toolu_1", "content": "cube added"})
        self.assertTrue(results[1]["is_error"])

    def test_unanswered_calls_stray_results_and_a_trailing_assistant_are_made_valid(self):
        history = conversation(
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "toolu_1", "type": "function", "function": {"name": "run_python", "arguments": "{}"}}]},
            {"role": "user", "content": "Stop, do something else."},
            {"role": "tool", "tool_call_id": "toolu_gone", "content": "late"},
            {"role": "assistant", "content": "Earlier records:\n- did things"})
        _, wired = am.to_messages(history)
        self.assertEqual(wired[2]["content"][0], {"type": "tool_result", "tool_use_id": "toolu_1",
                                                  "content": "No result was recorded for this call."})
        self.assertEqual(wired[2]["content"][2], {"type": "text", "text": "Result of an earlier call: late"})
        self.assertEqual(wired[-1], {"role": "user", "content": [{"type": "text", "text": "Continue."}]},
                         "no prefill: the request ends with the user")

    def test_a_folded_record_merges_with_the_next_step_which_loses_its_thinking(self):
        am.replay.put("Next step.", ["toolu_2"], [
            {"type": "thinking", "thinking": "", "signature": "sig-old"},
            {"type": "text", "text": "Next step."},
            {"type": "tool_use", "id": "toolu_2", "name": "run_python", "input": {}}])
        _, wired = am.to_messages(conversation(
            {"role": "assistant", "content": "Earlier steps, as records:\n- ran code"},
            {"role": "assistant", "content": "Next step.", "tool_calls": [
                {"id": "toolu_2", "type": "function", "function": {"name": "run_python", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "toolu_2", "content": "ok"}))
        self.assertEqual([b["type"] for b in wired[1]["content"]], ["text", "text", "tool_use"])

    def test_breakpoints_system_last_block_and_the_previous_requests_end(self):
        history = conversation(
            {"role": "assistant", "content": "Adding it.", "tool_calls": [
                {"id": "toolu_1", "type": "function", "function": {"name": "run_python", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "cube added"})
        body, _ = am.request_body(model="claude-opus-5-5", messages=history, tools=TOOLS, reasoning_effort="medium")
        self.assertEqual(marks(body), ["system.0", "messages.0.1", "messages.2.0"])
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        first, _ = am.request_body(model="claude-opus-5-5", messages=conversation(), tools=TOOLS, reasoning_effort="low")
        self.assertEqual(marks(first), ["system.0", "messages.0.1"])
        bare, _ = am.request_body(model="claude-opus-5-5", messages=conversation()[1:], tools=TOOLS, reasoning_effort="low")
        self.assertEqual(marks(bare), ["tools.1", "messages.0.1"], "no system prompt: the tools carry it")
        self.assertNotIn("cache_control", json.dumps(TOOLS) + json.dumps(history), "the caller's messages untouched")

    def test_a_long_conversation_never_goes_past_four_breakpoints(self):
        history = conversation()
        for n in range(30):
            history += [{"role": "assistant", "content": f"Step {n}.", "tool_calls": [
                {"id": f"toolu_{n}", "type": "function", "function": {"name": "run_python", "arguments": "{}"}}]},
                        {"role": "tool", "tool_call_id": f"toolu_{n}", "content": "ok"}]
        body, _ = am.request_body(model="claude-sonnet-5-5", messages=history, tools=TOOLS, reasoning_effort="high")
        self.assertLessEqual(len(marks(body)), 4)
        self.assertEqual(len(marks(body)), 3)

    def test_thinking_follows_the_model_and_effort(self):
        body, betas = am.request_body(model="claude-opus-5-5", messages=conversation(), tools=[], reasoning_effort="high")
        self.assertEqual(body["thinking"], {"type": "adaptive", "display": "updates",
                                            "block_binding": {"prefix_mismatch_behavior": "drop_block"}})
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual(betas, [am.UPDATES_BETA, am.BINDING_BETA])
        self.assertEqual(body["max_tokens"], am.DEFAULT_MAX_TOKENS)
        self.assertNotIn("temperature", body)
        haiku, betas = am.request_body(model="claude-haiku-5-5", messages=conversation(), tools=[], reasoning_effort="low")
        self.assertEqual(haiku["thinking"], {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}})
        self.assertEqual(betas, [am.BINDING_BETA])
        legacy, betas = am.request_body(model="claude-haiku-4-5-20251001", messages=conversation(), tools=[],
                                        reasoning_effort="medium", max_tokens=200_000)
        self.assertNotIn("thinking", legacy)
        self.assertNotIn("output_config", legacy)
        self.assertEqual((betas, legacy["max_tokens"]), ([], 200_000))
        capped, _ = am.request_body(model="claude-opus-5-5", messages=conversation(), tools=[], reasoning_effort="max",
                                    max_tokens=200_000)
        self.assertEqual((capped["max_tokens"], capped["output_config"]["effort"]), (am.MAX_OUTPUT_TOKENS, "max"))
        self.assertEqual(am._effort("claude-opus-4-6", "xhigh"), "high", "xhigh arrived with Opus 4.7")
        self.assertEqual(am.generation("claude-opus-4-20250514"), ("opus", (4, 0)))
        self.assertFalse(am.adaptive("gpt-6.1-sol"))

    def test_thinking_off_where_the_model_allows_it(self):
        with mock.patch.dict(os.environ, {"LOOPCUT_ANTHROPIC_THINKING": "off"}):
            sonnet, betas = am.request_body(model="claude-sonnet-5-5", messages=conversation(), tools=[], reasoning_effort="medium")
            self.assertEqual((sonnet["thinking"], betas), ({"type": "between_tools"}, []))
            haiku, _ = am.request_body(model="claude-haiku-5-5", messages=conversation(), tools=[], reasoning_effort="low")
            self.assertEqual(haiku["thinking"], {"type": "disabled"})
            haiku_max, _ = am.request_body(model="claude-haiku-5-5", messages=conversation(), tools=[], reasoning_effort="max")
            self.assertEqual(haiku_max["thinking"]["type"], "adaptive", "disabled only up to high effort")
            opus, _ = am.request_body(model="claude-opus-5-5", messages=conversation(), tools=[], reasoning_effort="low")
            self.assertEqual(opus["thinking"]["type"], "adaptive", "Opus 5.5 cannot turn thinking off")

    def test_usage_counts_cache_reads_and_writes_and_prices_them(self):
        usage = am._usage({**USAGE, "output_tokens": 120})
        self.assertEqual(usage, {"input": 4050, "cached": 3000, "cache_write": 1000, "output": 120})
        self.assertAlmostEqual(pricing.usd("claude-opus-5-5", usage), (50 * 4 + 3000 * 0.2 + 1000 * 5 + 120 * 20) / 1e6)
        self.assertAlmostEqual(pricing.usd("claude-sonnet-5-5", usage), (50 * 2 + 3000 * 0.1 + 1000 * 2.5 + 120 * 10) / 1e6)
        self.assertAlmostEqual(pricing.usd("claude-fable-5-1", usage), (50 * 10 + 3000 * 0.25 + 1000 * 12.5 + 120 * 50) / 1e6)
        long = {"input": 150_000, "cached": 100_000, "cache_write": 0, "output": 1000}
        self.assertAlmostEqual(pricing.usd("claude-haiku-5-5", long), (50_000 * 0.5 + 100_000 * 0.05 + 1000 * 2.5) / 1e6)
        self.assertAlmostEqual(pricing.usd("claude-haiku-5-5", usage), (50 * 0.1 + 3000 * 0.01 + 1000 * 0.125 + 120 * 0.5) / 1e6)
        self.assertAlmostEqual(pricing.usd("glm-5.3-flash", {"input": 1000, "cached": 0, "output": 0}), 1000 * 0.15 / 1e6,
                               msg="three-figure prices are unchanged")


# --- Streaming against the fake API ----------------------------------------------------------------

class StreamTest(Base):
    def test_text_usage_headers_and_body(self):
        Fake.script = [(200, reply(text("Hel", "lo.")))]
        completion, streamed = self.stream()
        self.assertEqual((streamed, completion.text, completion.finish_reason), ("Hello.", "Hello.", "stop"))
        self.assertEqual(completion.usage, {"input": 4050, "cached": 3000, "cache_write": 1000, "output": 120})
        request = Fake.requests[-1]
        self.assertEqual(request["path"], "/v1/messages")
        self.assertEqual(request["headers"]["x-api-key"], KEY)
        self.assertEqual(request["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(request["headers"]["anthropic-beta"], f"{am.UPDATES_BETA},{am.BINDING_BETA}")
        self.assertNotIn("authorization", request["headers"])
        self.assertFalse([h for h in request["headers"] if h.startswith("x-loopcut")], "gateway headers stay home")
        self.assertTrue(self.body()["stream"])
        self.assertEqual(self.body()["model"], "claude-opus-5-5")

    def test_one_tool_call_from_partial_json(self):
        Fake.script = [(200, reply(text("Adding a cube."), tool("toolu_1", "run_python", '{"co', 'de": "bpy.ops', '.mesh"}'),
                                   stop="tool_use"))]
        completion, streamed = self.stream()
        self.assertEqual(streamed, "Adding a cube.")
        self.assertEqual([(c.id, c.name, c.arguments) for c in completion.tool_calls],
                         [("toolu_1", "run_python", '{"code": "bpy.ops.mesh"}')])
        self.assertEqual(completion.finish_reason, "tool_calls")

    def test_several_tool_calls_in_one_turn(self):
        Fake.script = [(200, reply(text("Two things."), tool("toolu_1", "run_python", '{"code":', ' "a"}'),
                                   tool("toolu_2", "capture_viewport"), tool("toolu_3", "run_python", '{"code": "b"}'),
                                   stop="tool_use"))]
        completion, _ = self.stream()
        self.assertEqual([(c.id, c.arguments) for c in completion.tool_calls],
                         [("toolu_1", '{"code": "a"}'), ("toolu_2", "{}"), ("toolu_3", '{"code": "b"}')])

    def test_thinking_is_sent_back_unchanged_across_a_tool_turn(self):
        Fake.script = [(200, reply(thinking(), thinking("I see an empty scene; ", "adding a cube.", signature="sig-B"),
                                   tool("toolu_1", "run_python", '{"code": "x"}'), stop="tool_use")),
                       (200, reply(text("Done.")))]
        first, streamed = self.stream()
        self.assertEqual(streamed, "I see an empty scene; adding a cube.", "the notes between tool calls are shown")
        self.assertEqual(first.text, "I see an empty scene; adding a cube.")
        history = conversation(first.as_message(), {"role": "tool", "tool_call_id": "toolu_1", "content": "cube added"})
        second, _ = self.stream(history)
        self.assertEqual(second.text, "Done.")
        assistant = self.body()["messages"][1]
        self.assertEqual(assistant["content"], [
            {"type": "thinking", "thinking": "", "signature": "sig-1"},
            {"type": "thinking", "thinking": "I see an empty scene; adding a cube.", "signature": "sig-B"},
            {"type": "tool_use", "id": "toolu_1", "name": "run_python", "input": {"code": "x"}}])
        self.assertEqual(self.body()["messages"][2]["content"][0]["tool_use_id"], "toolu_1")
        self.assertEqual(marks(self.body()), ["system.0", "messages.0.1", "messages.2.0"])

    def test_redacted_thinking_is_replayed_too(self):
        Fake.script = [(200, reply(("redacted", "opaque=="), tool("toolu_1", "capture_viewport"), stop="tool_use")),
                       (200, reply(text("ok")))]
        first, _ = self.stream()
        self.stream(conversation(first.as_message(), {"role": "tool", "tool_call_id": "toolu_1", "content": "a picture"}))
        self.assertEqual(self.body()["messages"][1]["content"][0], {"type": "redacted_thinking", "data": "opaque=="})

    def test_a_cut_off_reply_is_length_and_a_refusal_runs_nothing(self):
        Fake.script = [(200, reply(text("Half"), stop="max_tokens"))]
        completion, _ = self.stream()
        self.assertEqual(completion.finish_reason, "length")
        Fake.script = [(200, reply(text("Let me"), tool("toolu_1", "run_python", '{"code": "x"}'), stop="refusal",
                                   stop_details={"type": "refusal", "category": "cyber", "explanation": "..."}))]
        completion, streamed = self.stream()
        self.assertEqual(completion.tool_calls, [])
        self.assertIn("declined", completion.text)
        self.assertIn("(cyber)", streamed)

    def test_through_stream_chat_and_list_models(self):
        Fake.script = [(200, reply(text("hi")))]
        completion = llm.stream_chat(base_url=BASE, api_key=KEY, model="claude-sonnet-5-5", messages=conversation(),
                                     tools=TOOLS, reasoning_effort="low", on_text=lambda _: None,
                                     is_cancelled=lambda: False, max_tokens=4000, prompt_id="single",
                                     headers=llm.correlation_headers("c1", "single"))
        self.assertEqual(completion.text, "hi")
        self.assertEqual((self.body()["max_tokens"], self.body()["output_config"]), (4000, {"effort": "low"}))
        self.assertNotIn("x-loopcut-prompt", Fake.requests[-1]["headers"])
        self.assertEqual(am.list_models(BASE, KEY), ["claude-opus-5-5", "claude-haiku-5-5"])
        self.assertEqual(Fake.requests[-1]["path"], "/v1/models?limit=1000")
        self.assertEqual(Fake.requests[-1]["headers"]["x-api-key"], KEY)


class ErrorTest(Base):
    def test_a_bad_key_is_shown_at_once_and_clearly(self):
        Fake.script = [(401, error_body("authentication_error", "invalid x-api-key"))]
        with self.assertRaises(llm.LLMError) as caught:
            self.stream()
        self.assertEqual((caught.exception.status, caught.exception.code), (401, "authentication_error"))
        self.assertIn("did not accept the API key", str(caught.exception))
        self.assertIn(am.KEYS_URL, str(caught.exception))
        self.assertIn("(request req_1)", str(caught.exception))
        self.assertEqual(len(Fake.requests), 1)
        self.assertNotIn(KEY, str(caught.exception))

    def test_permission_and_unknown_model(self):
        Fake.script = [(403, error_body("permission_error", "not allowed"))]
        with self.assertRaisesRegex(llm.LLMError, "refused this key's request"):
            self.stream()
        Fake.script = [(404, error_body("not_found_error", "model: claude-nope"))]
        with self.assertRaisesRegex(llm.LLMError, "no model 'claude-nope'"):
            self.stream(model="claude-nope")
        self.assertEqual(len(Fake.requests), 2)

    def test_a_broken_pipe_is_retried_with_smaller_images(self):
        import urllib.error
        import fake_imbuf
        photo = base64.b64encode(fake_imbuf.make(1568, 882, kind="JPEG", quality=85)).decode()
        looks = [{"role": "user", "content": [{"type": "text", "text": f"Viewport capture: look {n}."},
                                              {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{photo}"}}]}
                 for n in range(6)]
        Fake.script = [(200, reply(text("ok")))]
        sizes, real = [], am._post

        def post(url, body, *args):
            sizes.append(len(json.dumps(body)))
            if len(sizes) == 1:
                raise urllib.error.URLError(BrokenPipeError(32, "Broken pipe"))
            return real(url, body, *args)
        with mock.patch.dict(sys.modules, {"imbuf": fake_imbuf}), mock.patch.object(am, "_post", post):
            completion, _ = self.stream(conversation(*looks))
        self.assertEqual((completion.text, len(sizes)), ("ok", 2))
        from loopcut import image_budget
        self.assertLess(sizes[1], min(sizes[0], image_budget.REQUEST_BYTES // 2 + 50_000), "the retry: half the budget")
        sent = json.dumps(self.body())
        self.assertEqual(sent.count('"type": "image"') + sent.count("omitted to keep the request small"), 7,
                         "every look sent, smaller, or named in a note")

    def test_rate_limit_waits_as_retry_after_says_capped(self):
        Fake.script = [(429, error_body("rate_limit_error"), {"retry-after": "2"}),
                       (429, error_body("rate_limit_error"), {"retry-after": "900"}),
                       (200, reply(text("ok")))]
        completion, _ = self.stream()
        self.assertEqual(completion.text, "ok")
        self.assertEqual(self.waits, [2.0, llm.MAX_RETRY_AFTER])

    def test_a_spend_limit_is_not_retried(self):
        Fake.script = [(429, error_body("rate_limit_error", "spend limit", details={"error_code": "enforced_spend_limit_reached"}))]
        with self.assertRaisesRegex(llm.LLMError, "spend limit is reached"):
            self.stream()
        self.assertEqual((len(Fake.requests), self.waits), (1, []))

    def test_overloaded_and_server_errors_are_retried_then_shown(self):
        Fake.script = [(529, error_body("overloaded_error", "Overloaded")), (200, reply(text("ok")))]
        self.assertEqual(self.stream()[0].text, "ok")
        self.assertEqual(self.waits, [1.0])
        Fake.script = [(500, error_body("api_error"))] * 4
        with self.assertRaisesRegex(llm.LLMError, "HTTP 500"):
            self.stream()
        self.assertEqual(len(Fake.requests), 2 + 4)

    def test_an_unreachable_api_is_retried_then_named(self):
        closed = Server()
        closed.close()
        with mock.patch.dict(os.environ, {"LOOPCUT_ANTHROPIC_API_URL": closed.url}):
            with self.assertRaisesRegex(llm.LLMError, "Could not reach 127.0.0.1"):
                self.stream()
        self.assertEqual(len(self.waits), len(llm.RETRY_DELAYS))

    def test_overloaded_in_the_stream_before_any_text_is_retried(self):
        Fake.script = [(200, reply(end=False) + event("error", error={"type": "overloaded_error", "message": "Overloaded"})),
                       (200, reply(text("ok")))]
        completion, streamed = self.stream()
        self.assertEqual((completion.text, streamed), ("ok", "ok"))

    def test_a_stream_that_breaks_after_text_is_retried_once_only_with_on_retry(self):
        broken = reply(text("Half a rep"), end=False) + event("error", error={"type": "overloaded_error", "message": "x"})
        Fake.script = [(200, broken)]
        with self.assertRaisesRegex(llm.LLMError, "Connection lost while streaming"):
            self.stream()
        undone = []
        Fake.script = [(200, broken), (200, reply(text("Whole reply.")))]
        completion, _ = self.stream(on_retry=lambda: undone.append(True))
        self.assertEqual((completion.text, undone), ("Whole reply.", [True]))
        Fake.script = [(200, reply(text("cut"), end=False))]   # Ends without message_stop.
        with self.assertRaisesRegex(llm.LLMError, "Connection lost"):
            self.stream()

    def test_an_invalid_request_in_the_stream_is_shown(self):
        Fake.script = [(200, reply(end=False) + event("error", error={"type": "invalid_request_error", "message": "bad"}))]
        with self.assertRaisesRegex(llm.LLMError, "bad"):
            self.stream()
        self.assertEqual(len(Fake.requests), 1)

    def test_a_refused_beta_is_dropped_for_the_session(self):
        Fake.script = [(400, error_body("invalid_request_error", "thinking.block_binding: Extra inputs are not permitted")),
                       (200, reply(text("ok"))), (200, reply(text("again")))]
        self.assertEqual(self.stream()[0].text, "ok")
        self.assertIn("block_binding", Fake.requests[0]["body"]["thinking"])
        self.assertNotIn("block_binding", self.body()["thinking"])
        self.assertEqual(Fake.requests[-1]["headers"]["anthropic-beta"], am.UPDATES_BETA)
        self.stream()
        self.assertNotIn("block_binding", self.body()["thinking"], "remembered")
        self.assertEqual(len(Fake.requests), 3)

    def test_a_signature_refusal_is_sent_again_without_thinking(self):
        Fake.script = [(200, reply(thinking(signature="sig-A"), tool("toolu_1", "run_python", "{}"), stop="tool_use"))]
        first, _ = self.stream()
        history = conversation(first.as_message(), {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"})
        Fake.script = [(400, error_body("invalid_request_error", "messages.1.content.0: Invalid `signature` in `thinking` "
                                        "block. The block is bound to a different conversation. Remove the block, or set "
                                        "`thinking.block_binding.prefix_mismatch_behavior` to \"drop_block\". That setting "
                                        f"requires the `{am.BINDING_BETA}` value in the `anthropic-beta` header.")),
                       (200, reply(text("ok")))]
        self.assertEqual(self.stream(history)[0].text, "ok")
        self.assertEqual(Fake.requests[1]["body"]["messages"][1]["content"][0]["type"], "thinking")
        self.assertEqual([b["type"] for b in self.body()["messages"][1]["content"]], ["tool_use"])
        self.assertNotIn("binding", am._disabled, "the binding itself was not refused")


class StopTest(Base):
    def test_stop_mid_stream_returns_at_once(self):
        Fake.script = [(200, reply(text("Working"), end=False), {}, 5.0)]
        cancelled = threading.Event()
        started = time.monotonic()
        with self.assertRaises(llm.Cancelled):
            am.stream(base_url=BASE, api_key=KEY, model="claude-opus-5-5", messages=conversation(), tools=TOOLS,
                      reasoning_effort="low", on_text=lambda _: cancelled.set(), is_cancelled=cancelled.is_set)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_the_wall_clock_limit_ends_the_reply_like_the_output_cap(self):
        Fake.script = [(200, reply(text("Working"), tool("toolu_1", "run_python", '{"code": "x"}'), end=False), {}, 5.0)]
        started = time.monotonic()
        completion, _ = self.stream(call_max_seconds=0.5)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual((completion.finish_reason, completion.timed_out, completion.text), ("length", True, "Working"))
        self.assertEqual([c.id for c in completion.tool_calls], ["toolu_1"], "a finished call is kept")


# --- The agent loop and the router on this provider ------------------------------------------------

class AgentLoopTest(Base):
    """One turn of the single loop, end to end: the model's tool call runs, its result goes back with
    the model's thinking, and the reply lands in the chat."""

    def setUp(self):
        super().setUp()
        self.data = tempfile.TemporaryDirectory()
        self.addCleanup(self.data.cleanup)
        empty = Path(self.data.name) / "empty.env"
        empty.write_text("")
        env = mock.patch.dict(os.environ, {
            "LOOPCUT_DATA_DIR": self.data.name, "LOOPCUT_ENV_FILE": str(empty),
            "LOOPCUT_API_KEY": KEY, "LOOPCUT_BASE_URL": BASE, "LOOPCUT_MODEL": "claude-opus-5-5",
            "LOOPCUT_AUTO_RUN": "true", "LOOPCUT_AUTO_LOOK": "false", "LOOPCUT_ROUTER": "off"})
        env.start()
        self.addCleanup(env.stop)
        from prompt_index_fixture import index_patch
        for patcher in (index_patch(), *(mock.patch.object(agent, name, value) for name, value in (
                ("_scene_snapshot", lambda: None), ("_addons_line", lambda: ""), ("_project_roots", lambda: ()),
                ("_alike_on_main", lambda a, b: False), ("_update_account", lambda info: None),
                ("_keeps_context", lambda cfg: False), ("_redraw", lambda: None), ("_tool_schemas", lambda: TOOLS),
                ("_changes_scene", lambda name, arguments="": name == "run_python")))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ran = []

    def test_a_turn_with_a_tool_call(self):
        Fake.script = [(200, reply(thinking("Empty scene; adding a cube.", signature="sig-T"),
                                   tool("toolu_1", "run_python", '{"code": "bpy.ops.mesh.primitive_cube_add()"}'),
                                   stop="tool_use")),
                       (200, reply(text("Added a cube.")))]
        session = state.reset()
        session["messages"].append({"role": "user", "content": "Add a cube."})
        session["items"].append(state.item_user("Add a cube."))
        turn = agent.Turn()
        session["busy"], session["turn"] = True, turn

        def run_tool(name, arguments):
            self.ran.append((name, arguments))
            return types.SimpleNamespace(text="cube added", ok=True, image_path=None)

        agent._run(session, turn, run_tool, lambda s: None)
        self.assertEqual(self.ran, [("run_python", '{"code": "bpy.ops.mesh.primitive_cube_add()"}')])
        self.assertEqual(len(Fake.requests), 2)
        second = self.body()
        self.assertEqual(second["messages"][1]["content"][0], {"type": "thinking", "thinking": "Empty scene; adding a cube.",
                                                              "signature": "sig-T"})
        result = second["messages"][2]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"]), ("tool_result", "toolu_1"))
        self.assertIn("cube added", result["content"])
        self.assertEqual(second["system"][0]["cache_control"], {"type": "ephemeral"})
        replies = [i["text"] for i in session["items"] if i["kind"] == "assistant"]
        self.assertIn("Empty scene; adding a cube.", replies[0])
        self.assertEqual(replies[-1], "Added a cube.")
        self.assertEqual(session["usage"]["cached"], 6000, "two requests' cache reads")
        self.assertFalse(session["busy"])


class RouterTest(Base):
    """BYOK Anthropic is the open single loop: workers and the router need the Loopcut gateway's prompts.
    Pointed at it as a stand-in for the gateway (the harness's LOOPCUT_SERVICE_URLS), the router's call
    goes through this provider like any role's."""

    def test_byok_anthropic_runs_the_single_loop(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env") as empty, mock.patch.dict(os.environ, {
                "LOOPCUT_ENV_FILE": empty.name, "LOOPCUT_API_KEY": KEY, "LOOPCUT_BASE_URL": BASE,
                "LOOPCUT_MODEL": "claude-opus-5-5", "LOOPCUT_WORKER_BASE_URL": BASE,
                "LOOPCUT_WORKER_MODEL": "claude-haiku-5-5"}):
            roles = config.load_roles()
        self.assertEqual((roles.orchestrate, roles.router), (False, None))
        self.assertEqual((roles.orchestrator.base_url, roles.worker.model), (BASE, "claude-haiku-5-5"))
        self.assertTrue(roles.orchestrator.keep_context, "keep mode: the cache and thinking reward an unrewritten turn")
        with tempfile.NamedTemporaryFile("w", suffix=".env") as empty, mock.patch.dict(os.environ, {
                "LOOPCUT_ENV_FILE": empty.name, "LOOPCUT_API_KEY": KEY, "LOOPCUT_BASE_URL": BASE,
                "LOOPCUT_MODEL": "claude-opus-5-5", "LOOPCUT_KEEP_CONTEXT": "false"}):
            self.assertFalse(config.load().keep_context)

    def test_a_router_call_through_this_provider(self):
        from prompt_index_fixture import index_patch
        decision = json.dumps({"level": 3, "why": "three pieces", "deliverables": [
            {"name": "Armchair", "quote": "an armchair"}, {"name": "SideTable", "quote": "a side table"}]})
        Fake.script = [(200, reply(text(decision), usage={"input_tokens": 400, "cache_creation_input_tokens": 0,
                                                          "cache_read_input_tokens": 0, "output_tokens": 1}))]
        cfg = config.Config(KEY, BASE, "claude-haiku-5-5", "low", True, False, 25, 40_000, max_output_tokens=4000)
        session = state.new_session()
        session["messages"].append({"role": "user", "content": "Make an armchair and a side table."})
        with index_patch(), mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": BASE}):
            found = router.route(session, session["messages"][-1], cfg, lambda: False)
        self.assertEqual((found.level, [d[0] for d in found.deliverables]), (3, ["Armchair", "SideTable"]))
        body = self.body()
        self.assertEqual((body["model"], body["max_tokens"], body["output_config"]), ("claude-haiku-5-5", 4000, {"effort": "low"}))
        self.assertNotIn("tools", body)
        self.assertEqual(session["usage_by_role"]["router"]["input"], 400)


if __name__ == "__main__":
    unittest.main()
