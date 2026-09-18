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

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "extension"))

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
        os.environ.update({
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
                                       args=(session, turn, self.run_tool, self.ensure_checkpoint))
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
