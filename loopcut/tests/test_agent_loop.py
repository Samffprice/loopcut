"""End-to-end test of llm.py + agent.py against a scripted local SSE server. No Blender needed.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# agent.py only needs bpy for the session store; give it a stand-in.
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, checkpoints, llm, state  # noqa: E402
from loopcut.ui import layout  # noqa: E402


def sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def text_reply(*pieces: str) -> bytes:
    return sse(*({"choices": [{"delta": {"content": p}}]} for p in pieces),
               {"choices": [{"delta": {}, "finish_reason": "stop"}]})


def tool_reply(name: str, arguments: dict) -> bytes:
    raw = json.dumps(arguments)
    half = len(raw) // 2  # Arguments arrive split across chunks, as real APIs send them.
    return sse(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                "function": {"name": name, "arguments": raw[:half]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": raw[half:]}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    )


class Script:
    def __init__(self):
        self.replies: list = []
        self.requests: list[dict] = []


SCRIPT = Script()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body["_auth"] = self.headers.get("Authorization")
        SCRIPT.requests.append(body)
        reply = SCRIPT.replies.pop(0)
        if isinstance(reply, tuple):
            status, payload = reply
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


class AgentLoopTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        import tempfile
        cls.data_dir = tempfile.TemporaryDirectory()
        os.environ.update({
            "LOOPCUT_DATA_DIR": cls.data_dir.name,  # The agent saves conversations as it goes.
            "LOOPCUT_API_KEY": "test-key",
            "LOOPCUT_BASE_URL": f"http://127.0.0.1:{cls.server.server_port}",
            "LOOPCUT_MODEL": "fake-model",
            "LOOPCUT_AUTO_RUN": "false",
        })
        agent._redraw = lambda: None
        agent._tool_schemas = lambda: []
        agent._needs_approval = lambda name: name == "run_python"
        agent._changes_scene = lambda name: name == "run_python"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.data_dir.cleanup()

    def setUp(self):
        SCRIPT.replies.clear()
        SCRIPT.requests.clear()
        self.session = state.reset()
        self.ran: list[tuple[str, str]] = []
        self.checkpoint_calls: list[int] = []  # How many tools had run when each was requested.
        self.checkpoint_error: Exception | None = None

    def ensure_checkpoint(self, session):
        self.checkpoint_calls.append(len(self.ran))
        if self.checkpoint_error:
            raise self.checkpoint_error

    def run_tool(self, name, arguments):
        self.ran.append((name, arguments))
        return types.SimpleNamespace(text="cube added", ok=True, image_path=None)

    def start(self, text: str) -> agent.Turn:
        session = self.session
        session["items"].append(state.item_user(text))
        session["messages"].append({"role": "user", "content": text})
        turn = agent.Turn()
        session["busy"], session["turn"] = True, turn
        turn.thread = threading.Thread(target=agent._run, daemon=True,
                                       args=(session, turn, lambda *a: self.run_tool(*a), self.ensure_checkpoint))
        turn.thread.start()
        return turn

    def wait_for(self, predicate, what: str):
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for {what}; items={self.session['items']}")

    def test_streams_text(self):
        SCRIPT.replies.append(text_reply("Hel", "lo."))
        self.start("hi").thread.join(5)
        self.assertEqual([i["kind"] for i in self.session["items"]], ["user", "assistant"])
        self.assertEqual(self.session["items"][1]["text"], "Hello.")
        self.assertFalse(self.session["busy"])
        self.assertEqual(SCRIPT.requests[0]["_auth"], "Bearer test-key")
        self.assertEqual(SCRIPT.requests[0]["messages"][0]["role"], "system")

    def test_tool_call_waits_for_approval_then_runs_and_continues(self):
        SCRIPT.replies += [tool_reply("run_python", {"code": "add_cube()", "summary": "Add a cube"}),
                           text_reply("Done.")]
        turn = self.start("add a cube")
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        self.assertEqual(self.ran, [], "must not run before the user approves")
        card = self.session["items"][-1]
        self.assertEqual((card["summary"], card["code"]), ("Add a cube", "add_cube()"))

        self.assertTrue(agent.decide(True))
        turn.thread.join(5)
        self.assertEqual(json.loads(self.ran[0][1])["code"], "add_cube()")
        self.assertEqual(card["status"], "done")
        self.assertEqual(self.session["items"][-1]["text"], "Done.")
        followup = SCRIPT.requests[1]["messages"]
        self.assertEqual(followup[-1], {"role": "tool", "tool_call_id": "call_1", "content": "cube added"})
        self.assertEqual(followup[-2]["tool_calls"][0]["function"]["name"], "run_python")

    def test_rejection_is_reported_to_the_model_and_nothing_runs(self):
        SCRIPT.replies += [tool_reply("run_python", {"code": "rm()", "summary": "Delete all"}),
                           text_reply("OK, what instead?")]
        turn = self.start("clean up")
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        agent.decide(False)
        turn.thread.join(5)
        self.assertEqual(self.ran, [])
        self.assertIn("rejected", SCRIPT.requests[1]["messages"][-1]["content"])

    def test_stop_while_awaiting_approval(self):
        SCRIPT.replies.append(tool_reply("run_python", {"code": "x", "summary": "x"}))
        turn = self.start("go")
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        turn.cancel.set()
        turn.thread.join(5)
        self.assertEqual(self.session["items"][-1], state.item_error("Stopped."))
        self.assertFalse(self.session["busy"])
        self.assertEqual(self.ran, [])
        # The cut-short call must be answered, or the API rejects the next request.
        self.assertEqual(self.session["messages"][-1]["tool_call_id"], "call_1")
        SCRIPT.replies.append(text_reply("Still here."))
        self.start("and now?").thread.join(5)
        roles = [m["role"] for m in SCRIPT.requests[-1]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])

    def test_conversation_is_on_disk_after_a_turn(self):
        from loopcut import conversations
        SCRIPT.replies.append(text_reply("Saved."))
        self.start("remember me").thread.join(5)
        loaded = conversations.load(self.session["id"])
        self.assertEqual([i["text"] for i in loaded["items"]], ["remember me", "Saved."])
        self.assertEqual(loaded["title"], "remember me")

    def test_checkpoint_is_requested_before_scene_changes_and_not_for_read_only_tools(self):
        SCRIPT.replies += [tool_reply("get_scene_info", {}), tool_reply("run_python", {"code": "a", "summary": "a"}),
                           text_reply("Done.")]
        turn = self.start("go")
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        self.assertEqual(self.checkpoint_calls, [], "no snapshot for a read-only tool or before approval")
        agent.decide(True)
        turn.thread.join(5)
        self.assertEqual(self.checkpoint_calls, [1], "requested once, after get_scene_info, before run_python")
        self.assertEqual([name for name, _ in self.ran], ["get_scene_info", "run_python"])

    def test_code_does_not_run_when_the_checkpoint_cannot_be_saved(self):
        self.checkpoint_error = checkpoints.CheckpointError("Disk full.")
        SCRIPT.replies += [tool_reply("run_python", {"code": "danger()", "summary": "x"}), text_reply("Sorry.")]
        turn = self.start("go")
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        agent.decide(True)
        turn.thread.join(5)
        self.assertEqual(self.ran, [])
        card = next(i for i in self.session["items"] if i["kind"] == "tool")
        self.assertEqual((card["status"], card["output"]), ("failed", "Not run. Disk full."))
        self.assertIn("NOT run", SCRIPT.requests[1]["messages"][-1]["content"])

    def test_two_captures_in_one_step_send_two_different_images(self):
        # capture_viewport always writes the same file; the second capture must not replace the first.
        shot = Path(self.data_dir.name) / "viewport.png"
        pictures = [b"\x89PNG front", b"\x89PNG side"]

        def run_tool(name, arguments):
            shot.write_bytes(pictures[len(self.ran)])
            self.ran.append((name, arguments))
            return types.SimpleNamespace(text="Image attached.", ok=True, image_path=shot)

        self.run_tool = run_tool
        calls = [{"index": i, "id": f"call_{i}", "type": "function",
                  "function": {"name": "capture_viewport", "arguments": "{}"}} for i in range(2)]
        SCRIPT.replies += [sse({"choices": [{"delta": {"tool_calls": calls}}]},
                               {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
                           text_reply("Looks right.")]
        self.start("check both sides").thread.join(5)
        sent = [part["image_url"]["url"] for m in SCRIPT.requests[1]["messages"] if isinstance(m["content"], list)
                for part in m["content"] if part["type"] == "image_url"]
        import base64
        self.assertEqual([base64.b64decode(url.split(",", 1)[1]) for url in sent], pictures)

    def test_turn_ends_with_a_card_of_what_changed_linked_to_the_checkpoint(self):
        from test_scene_diff import cube, scene
        before, after = scene(), scene({"Cube": cube(), "Sphere": cube(data="Sphere")})

        def run_tool(name, arguments):
            self.ran.append((name, arguments))
            return types.SimpleNamespace(text="ok", ok=True, image_path=None, scene_before=before, scene_after=after)

        self.run_tool = run_tool
        os.environ["LOOPCUT_AUTO_RUN"] = "true"
        self.addCleanup(os.environ.update, {"LOOPCUT_AUTO_RUN": "false"})
        SCRIPT.replies += [tool_reply("run_python", {"code": "add()", "summary": "Add"}), text_reply("Added.")]
        turn = self.start("add a sphere")
        self.session["items"][0]["checkpoint"] = "c" * 32
        turn.thread.join(5)
        card = self.session["items"][-1]
        self.assertEqual((card["kind"], card["text"], card["checkpoint"]), ("changes", "1 added", "c" * 32))
        self.assertEqual(card["lines"], ["+ Sphere (mesh) at [-1, -1, -1]..[1, 1, 1]"])

    def test_chat_only_turn_has_no_changes_card(self):
        SCRIPT.replies.append(text_reply("Three objects."))
        self.start("what is here?").thread.join(5)
        self.assertNotIn("changes", [i["kind"] for i in self.session["items"]])

    def test_always_allow_stops_asking_for_the_rest_of_the_conversation(self):
        SCRIPT.replies += [tool_reply("run_python", {"code": "a()", "summary": "a"}),
                           tool_reply("run_python", {"code": "b()", "summary": "b"}), text_reply("Done.")]
        turn = self.start("two steps")
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        self.assertTrue(agent.decide(True, always=True))
        turn.thread.join(5)
        self.assertEqual([json.loads(a)["code"] for _, a in self.ran], ["a()", "b()"], "second step ran unasked")
        self.assertFalse(state.new_session()["auto_run"], "a new conversation asks again")

    def test_token_usage_adds_up_over_the_conversation(self):
        usage = {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 8}}
        SCRIPT.replies += [sse({"choices": [{"delta": {"content": "Hi."}}]}, usage)] * 2
        self.start("hi").thread.join(5)
        self.start("again").thread.join(5)
        self.assertEqual(self.session["usage"], {"input": 240, "output": 16})
        self.assertEqual(SCRIPT.requests[0]["stream_options"], {"include_usage": True})

    def test_overloaded_api_is_retried_and_the_user_never_sees_it(self):
        llm.RETRY_DELAYS, saved = (0.01, 0.01, 0.01), llm.RETRY_DELAYS
        self.addCleanup(setattr, llm, "RETRY_DELAYS", saved)
        SCRIPT.replies += [(503, {"error": {"message": "Overloaded."}}), (429, {"error": {"message": "Slow down."}}),
                           text_reply("Here now.")]
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1]["text"], "Here now.")
        self.assertEqual(len(SCRIPT.requests), 3)

    def test_retries_give_up_and_show_the_last_error(self):
        llm.RETRY_DELAYS, saved = (0.01, 0.01, 0.01), llm.RETRY_DELAYS
        self.addCleanup(setattr, llm, "RETRY_DELAYS", saved)
        SCRIPT.replies += [(503, {"error": {"message": "Overloaded."}})] * 4
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1], state.item_error("HTTP 503: Overloaded."))

    def test_http_error_is_shown_not_swallowed(self):
        SCRIPT.replies.append((402, {"error": {"message": "Billing verification failed."}}))
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1],
                         state.item_error("HTTP 402: Billing verification failed."))
        self.assertFalse(self.session["busy"])


class LayoutTest(unittest.TestCase):
    @staticmethod
    def measure(font, size, text):
        return len(text) * size * 0.5

    def test_wrap_offsets_reconstruct_the_text(self):
        text = "alpha beta  gamma\n\nsupercalifragilisticexpialidocious end"
        lines = layout.wrap(text, "ui", 10, 60, self.measure)
        for start, line in lines:
            self.assertEqual(text[start:start + len(line)], line)
            self.assertLessEqual(self.measure("ui", 10, line.rstrip()), 60)
        self.assertEqual("".join(line for _, line in lines), text.replace("\n", ""))

    def test_split_markdown_handles_unclosed_fence_while_streaming(self):
        self.assertEqual(layout.split_markdown("Intro\n```python\nx = 1"), [("p", "Intro"), ("code", "x = 1")])

    def test_latest_item_is_in_view_and_pending_buttons_are_clickable(self):
        session = state.new_session()
        session["items"] = [state.item_user("hello " * 40) for _ in range(12)]
        card = state.item_tool("run_python", "Do it", "print(1)")
        card["status"] = "awaiting"
        session["items"].append(card)
        display = layout.build(session, 400, 600, 1.0, self.measure, "m")
        self.assertGreater(display["max_scroll"], 0)
        input_top = next(p for p in display["prims"] if p["id"] == "input.zone")["y"]
        run = next(h for h in display["hits"] if h["id"] == "item12.tool.run")
        self.assertLess(run["y"] + run["h"], input_top)
        self.assertEqual(layout.hit_test(display, run["x"] + 2, run["y"] + 2), ("approve", 12))


if __name__ == "__main__":
    unittest.main()
