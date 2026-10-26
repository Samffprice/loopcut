"""reasoning_details passback (keep_reasoning): merge, store, wire, fold, estimate. No Blender."""

import copy
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import config, context as cx, conversations as cv, llm  # noqa: E402

KIMI, OTHER = "moonshotai/kimi-k3.1-pro", "z-ai/glm-5"
DETAILS = [{"type": "reasoning.text", "text": "17*23 = 391", "format": "unknown", "index": 0}]


def call(call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": "run_python", "arguments": "{}"}}


def step(call_id: str, model: str = KIMI, details=DETAILS) -> list[dict]:
    """A stored assistant step the way run_loop stores it, with its tool result."""
    reply = llm.Completion(text="", tool_calls=[llm.ToolCall(call_id, "run_python", "{}")],
                           reasoning_details=copy.deepcopy(details)).as_message(model)
    return [reply, {"role": "tool", "tool_call_id": call_id, "content": "ok"}]


def chunk(*pieces) -> bytes:
    return b"data: " + json.dumps({"choices": [{"delta": {"reasoning_details": list(pieces)}}]}).encode() + b"\n\n"


class Handler(BaseHTTPRequestHandler):
    body = b""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class MergeTest(unittest.TestCase):
    def test_pieces_of_one_block_are_concatenated_and_the_signature_replaces(self):
        merged: list = []
        for piece in ({"type": "reasoning.text", "text": "a", "index": 0, "format": "unknown"},
                      {"type": "reasoning.text", "text": "b", "index": 0, "signature": "sig"},
                      {"type": "reasoning.encrypted", "data": "x", "index": 1, "id": "e1"},
                      {"type": "reasoning.text", "text": "c", "index": 0}):
            llm.merge_reasoning_details(merged, [piece])
        self.assertEqual(merged, [{"type": "reasoning.text", "text": "abc", "index": 0, "format": "unknown",
                                   "signature": "sig"},
                                  {"type": "reasoning.encrypted", "data": "x", "index": 1, "id": "e1"}])

    def test_odd_chunks_are_skipped(self):
        merged: list = []
        llm.merge_reasoning_details(merged, "text")
        llm.merge_reasoning_details(merged, [None, 3])
        self.assertEqual(merged, [])

    def test_the_stream_collects_them_and_the_reply_still_reads(self):
        Handler.body = (chunk({"type": "reasoning.text", "text": "think", "index": 0})
                        + chunk({"type": "reasoning.text", "text": "ing", "index": 0})
                        + b'data: {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}\n\n'
                        + b"data: [DONE]\n\n")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        completion = llm.stream_chat(
            base_url=f"http://127.0.0.1:{server.server_port}", api_key="k", model=KIMI,
            messages=[{"role": "user", "content": "hi"}], tools=[], reasoning_effort="low",
            on_text=lambda _: None, is_cancelled=lambda: False)
        self.assertEqual(completion.text, "hi")
        self.assertEqual(completion.reasoning_details, [{"type": "reasoning.text", "text": "thinking", "index": 0}])
        self.assertNotIn("reasoning_details", completion.as_message())
        self.assertEqual(completion.as_message(KIMI)["reasoning_details"], completion.reasoning_details)


class ContentTest(unittest.TestCase):
    def test_a_direct_providers_reasoning_content_is_kept_and_sent_back_under_its_name(self):
        Handler.body = (b'data: {"choices": [{"delta": {"reasoning_content": "th"}}]}\n\n'
                        b'data: {"choices": [{"delta": {"reasoning_content": "ink", "content": "hi"}, "finish_reason": "stop"}]}\n\n'
                        b"data: [DONE]\n\n")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        completion = llm.stream_chat(
            base_url=f"http://127.0.0.1:{server.server_port}", api_key="k", model="kimi-for-coding",
            messages=[{"role": "user", "content": "hi"}], tools=[], reasoning_effort="low",
            on_text=lambda _: None, is_cancelled=lambda: False)
        self.assertEqual(completion.reasoning_content, "think")
        self.assertNotIn("reasoning_content", completion.as_message())
        stored = completion.as_message("kimi-for-coding")
        self.assertEqual(stored["reasoning_content"], "think")
        self.assertNotIn("reasoning_details", stored)
        wired = cv.wire_messages("c", [stored], reasoning_model="kimi-for-coding")[0]
        self.assertEqual(wired["reasoning_content"], "think")
        self.assertNotIn("reasoning_content", cv.wire_messages("c", [stored], reasoning_model="")[0])
        self.assertGreater(cx.estimate_tokens([stored], 0), 0)


class SwitchTest(unittest.TestCase):
    def test_default_rule(self):
        d = config.keeps_reasoning_by_default
        for url, model in (("https://api.kimi.ai/coding/v1", "anything"), ("https://api.moonshot.ai/v1", "x"),
                           ("https://api.moonshot.cn/v1", "x"), ("https://x.test/v1", "kimi-for-coding"),
                           ("https://x.test/v1", "kimi-k3"), ("https://x.test/v1", "kimi-k3.1-pro"),
                           ("https://openrouter.ai/api/v1", "moonshotai/kimi-k3.1-pro")):
            self.assertTrue(d(url, model), (url, model))
        for url, model in (("https://openrouter.ai/api/v1", "z-ai/glm-5"), ("https://api.openai.com/v1", "gpt-6"),
                           ("https://x.test/v1", "kimi-k2.5"), ("anthropic://", "claude"), ("", ""),
                           ("https://api.kimi.ai.evil.test/v1", "x")):
            self.assertFalse(d(url, model), (url, model))

    def test_env_overrides_the_default_either_way(self):
        def load(**env):
            with mock.patch.dict(os.environ, {"LOOPCUT_API_KEY": "k", "LOOPCUT_BASE_URL": "https://api.kimi.ai/coding/v1",
                                              "LOOPCUT_MODEL": "kimi-for-coding", **env}):
                return config.load().keep_reasoning
        os.environ.pop("LOOPCUT_KEEP_REASONING", None)
        self.assertTrue(load())
        self.assertFalse(load(LOOPCUT_KEEP_REASONING="off"))
        with mock.patch.dict(os.environ, {"LOOPCUT_API_KEY": "k", "LOOPCUT_BASE_URL": "https://x.test/v1",
                                          "LOOPCUT_MODEL": "glm"}):
            os.environ.pop("LOOPCUT_KEEP_REASONING", None)
            self.assertFalse(config.load().keep_reasoning)
            os.environ["LOOPCUT_KEEP_REASONING"] = "on"
            self.assertTrue(config.load().keep_reasoning)


class ServiceFieldTest(unittest.TestCase):
    def test_the_models_entry_beats_the_model_id_default(self):
        from loopcut import agent
        service = "https://gateway.test/v1"  # account.py needs bpy, so agent._keeps_reasoning is the seam.
        listed_now = [None]
        cfg = config.Config(api_key="k", base_url=service, model=KIMI, reasoning_effort="low", auto_run=False,
                            auto_look=False, max_steps=0, context_budget=40000,
                            keep_reasoning=config.keeps_reasoning_by_default(service, KIMI))
        self.assertTrue(cfg.keep_reasoning)
        os.environ.pop("LOOPCUT_KEEP_REASONING", None)
        with mock.patch.object(agent, "_keeps_context", lambda c: False), \
                mock.patch.object(agent, "_keeps_reasoning", lambda c: listed_now[0]):
            for listed, expect in ((False, False), (True, True), (None, True)):
                listed_now[0] = listed
                self.assertEqual(agent._keep_mode(cfg).keep_reasoning, expect)
            listed_now[0] = False
            with mock.patch.dict(os.environ, {"LOOPCUT_KEEP_REASONING": "on"}):
                self.assertTrue(agent._keep_mode(cfg).keep_reasoning, "the env switch is the user's word")


class WireTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["LOOPCUT_DATA_DIR"] = self.tmp.name
        self.messages = [{"role": "user", "content": "build"}, *step("c1"), *step("c2")]

    def wire(self, messages, model=""):
        return cv.wire_messages("conv", messages, frozenset(), True, reasoning_model=model)

    def test_off_the_wire_is_exactly_todays(self):
        wired = self.wire(self.messages)
        self.assertTrue(all("reasoning_details" not in m for m in wired))
        self.assertTrue(all(not k.startswith("loopcut_") for m in wired for k in m))
        plain = [{k: v for k, v in m.items() if k not in ("reasoning_details", llm.REASONING_MODEL)}
                 for m in self.messages]
        self.assertEqual(wired, plain)

    def test_on_the_field_round_trips_through_disk_unchanged(self):
        stored = json.loads(json.dumps(self.messages))
        wired = self.wire(stored, KIMI)
        self.assertEqual([m["reasoning_details"] for m in wired if m["role"] == "assistant"], [DETAILS, DETAILS])
        self.assertTrue(all(not k.startswith("loopcut_") for m in wired for k in m))

    def test_a_model_switch_strips_it_and_a_switch_back_restores_it(self):
        self.assertTrue(all("reasoning_details" not in m for m in self.wire(self.messages, OTHER)))
        self.assertTrue(any("reasoning_details" in m for m in self.wire(self.messages, KIMI)))

    def test_two_prepares_are_byte_stable_and_the_store_is_untouched(self):
        before = json.dumps(self.messages)
        first = json.dumps(self.wire(self.messages, KIMI))
        self.messages += step("c3")  # The next step of the same turn.
        second = json.dumps(self.wire(self.messages, KIMI))
        self.assertTrue(second.startswith(first[:-1]), "earlier messages serialise the same on the next request")
        self.assertEqual(json.dumps(self.messages[:-2]), before)

    def test_a_new_turn_drops_the_reasoning_of_earlier_ones(self):
        self.messages += [{"role": "user", "content": "now make it red"}, *step("c3")]
        wired = self.wire(self.messages, KIMI)
        self.assertEqual(["reasoning_details" in m for m in wired if m["role"] == "assistant"], [False, False, True])


class ContextTest(unittest.TestCase):
    def test_folded_steps_do_not_carry_it(self):
        messages = [{"role": "user", "content": "build"}, *step("c1"), *step("c2")]
        messages[1][cx.FOLDED] = True
        shown = cx.view(messages, keep=True)
        self.assertNotIn("reasoning_details", json.dumps(shown[:-2]))
        self.assertIn("reasoning_details", shown[-2])

    def test_a_summary_stands_in_for_them(self):
        messages = [{"role": "user", "content": "old"}, *step("c1"),
                    {"role": "user", "content": "new", cx.SUMMARY: "did things"}, *step("c2")]
        self.assertEqual(json.dumps(cx.view(messages, keep=True)).count("reasoning_details"), 1)

    def test_estimate_counts_their_text(self):
        bare = [{"role": "assistant", "content": "x"}]
        big = [{**bare[0], "reasoning_details": [{"type": "reasoning.text", "text": "y" * 4000}]}]
        self.assertGreater(cx.estimate_tokens(big, 0) - cx.estimate_tokens(bare, 0), 900)


if __name__ == "__main__":
    unittest.main()
