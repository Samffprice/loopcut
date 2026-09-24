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

from loopcut import agent, checkpoints, context, conversations, llm, state  # noqa: E402
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


def multi_tool_reply(*calls: tuple) -> bytes:
    """Several tool calls in one reply, as parallel tool calls arrive."""
    return sse(*({"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"call_{i + 1}", "type": "function",
                                                        "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
                 for i, (name, args) in enumerate(calls)),
               {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})


class Script:
    def __init__(self):
        self.replies: list = []
        self.requests: list[dict] = []
        self.headers: dict = {}  # Sent with every reply, like the gateway's x-loopcut-* usage headers.


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
            for name, value in SCRIPT.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        for name, value in SCRIPT.headers.items():
            self.send_header(name, value)
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
            "LOOPCUT_AUTO_LOOK": "false",  # Tests that want the automatic look turn it on.
        })
        agent._scene_snapshot = lambda: None
        agent._addons_line = lambda: ""  # Reads bpy; the prompt test sets its own line.
        agent._project_roots = lambda: ()  # Reads bpy on the main thread; the file tests set roots themselves.
        agent._alike_on_main = lambda a, b: False  # Needs Blender to read pixels; tests pass their own.
        agent._update_account = lambda info: state.ui.__setitem__("account", info)  # account.py needs bpy.
        agent._redraw = lambda: None
        agent._tool_schemas = lambda: []
        agent._changes_scene = lambda name: name == "run_python"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.data_dir.cleanup()

    def setUp(self):
        SCRIPT.replies.clear()
        SCRIPT.requests.clear()
        SCRIPT.headers.clear()
        state.ui["account"] = None
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

    def test_installed_addons_are_named_at_the_end_of_the_system_prompt(self):
        agent._addons_line = lambda: "Installed add-ons: Probe [probe] bpy.ops.probe.say_* (2)"
        agent._scene_context = lambda text, since=None: "<scene_context>\nfile: unsaved\n</scene_context>"
        SCRIPT.replies.append(text_reply("Hi."))
        self.assertTrue(agent.send("hi"))  # send() reads the line; the loop puts it in the prompt.
        self.session["turn"].thread.join(5)
        system = SCRIPT.requests[0]["messages"][0]["content"]
        self.assertTrue(system.startswith(agent.SYSTEM_PROMPT), "the fixed prompt comes first")
        self.assertTrue(system.endswith("\n\nInstalled add-ons: Probe [probe] bpy.ops.probe.say_* (2)"), system[-120:])
        # The line is read on the main thread at the start of each turn, not stored with the conversation.
        self.assertNotIn("addons_line", conversations._PERSISTED)

    def awaiting(self):
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "the approval card")

    def test_a_note_sent_mid_turn_is_folded_in_before_the_next_request(self):
        SCRIPT.replies += [tool_reply("run_python", {"code": "add_cube()", "summary": "Add a cube"}),
                           text_reply("Done, in red.")]
        turn = self.start("add a cube")
        self.awaiting()
        self.assertTrue(agent.send("make it red"), "a message while busy is queued, not refused")
        queued = self.session["items"][-1]
        self.assertEqual((queued["kind"], queued["text"], queued.get("queued")), ("user", "make it red", True))
        self.assertEqual(self.session["input"], "", "send() does not touch the input; the panel clears it")
        agent.decide(True)
        turn.thread.join(5)
        self.assertNotIn("queued", queued, "folded in: the tag is gone")
        sent = SCRIPT.requests[1]["messages"]
        self.assertEqual((sent[-1]["role"], sent[-1]["content"]), ("user", "<steer>make it red</steer>"))
        self.assertEqual(sent[-2]["role"], "tool", "the note comes after the step's results")
        self.assertIn("<steer>", sent[0]["content"], "the prompt says what a steer message is")
        self.assertEqual(len([m for m in self.session["messages"] if m.get(context.STEER)]), 1)
        self.assertEqual(self.session["items"][-1]["text"], "Done, in red.")

    def test_a_note_too_late_to_fold_in_starts_the_next_turn(self):
        resent = []
        agent._resend_on_main = resent.append
        fold = agent._fold_notes
        agent._fold_notes = lambda session, turn: None  # As if the note came during the final request.
        try:
            SCRIPT.replies += [tool_reply("run_python", {"code": "add_cube()", "summary": "Add a cube"}),
                               text_reply("Done.")]
            turn = self.start("add a cube")
            self.awaiting()
            self.assertTrue(agent.send("and a sphere"))
            item = self.session["items"][-1]
            agent.decide(True)
            turn.thread.join(5)
        finally:
            agent._fold_notes = fold
        self.assertEqual(resent, ["and a sphere"])
        self.assertNotIn(item, self.session["items"], "the queued item is replaced by the turn it starts")
        self.assertFalse(self.session["busy"])

    def test_a_note_left_over_after_a_stop_goes_back_to_the_input(self):
        restored = []
        agent._restore_input_on_main = restored.append
        SCRIPT.replies.append(tool_reply("run_python", {"code": "add_cube()", "summary": "Add a cube"}))
        turn = self.start("add a cube")
        self.awaiting()
        self.assertTrue(agent.send("wait"))
        agent.stop()
        turn.thread.join(5)
        self.assertEqual(restored, ["wait"])
        self.assertFalse(any(i.get("queued") for i in self.session["items"]))

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
        # The stopped turn's step is sent as its record: one assistant message, no dangling call.
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertIn('run_python "x": not run: Cancelled before this ran.', SCRIPT.requests[-1]["messages"][2]["content"])

    def test_stop_during_a_batch_does_not_execute_remaining_calls(self):
        def run_tool(name, arguments):
            self.ran.append((name, arguments))
            agent.stop()
            return types.SimpleNamespace(text="first call finished", ok=True, image_path=None)

        self.run_tool = run_tool
        calls = [{"index": i, "id": f"call_{i}", "type": "function",
                  "function": {"name": "get_scene_info", "arguments": "{}"}} for i in range(2)]
        SCRIPT.replies.append(sse({"choices": [{"delta": {"tool_calls": calls}}]},
                                  {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
        turn = self.start("inspect twice")
        turn.thread.join(5)
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(turn.outcome, "cancelled")
        results = [m for m in self.session["messages"] if m["role"] == "tool"]
        self.assertEqual([r["tool_call_id"] for r in results], ["call_0", "call_1"])
        self.assertIn("Cancelled", results[-1]["content"])

    def test_stop_during_checkpoint_does_not_run_the_mutation(self):
        self.ensure_checkpoint = lambda session: agent.stop()
        SCRIPT.replies.append(tool_reply("run_python", {"code": "add()", "summary": "Add"}))
        turn = self.start("add")
        self.awaiting()
        agent.decide(True)
        turn.thread.join(5)
        self.assertEqual(self.ran, [])
        self.assertEqual(self.session["turn_outcome"], "cancelled")

    def test_step_limit_is_not_a_completed_turn(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"LOOPCUT_MAX_STEPS": "1"}):
            SCRIPT.replies.append(tool_reply("get_scene_info", {}))
            turn = self.start("inspect")
            turn.thread.join(5)
        self.assertEqual(turn.outcome, "step_limit")
        self.assertEqual(self.session["turn_outcome"], "step_limit")

    def test_cancelled_main_thread_job_never_executes_after_worker_stops(self):
        from concurrent.futures import Future
        from unittest.mock import patch
        from loopcut import mainthread, tools
        queued = []
        future = Future()
        cancelled = threading.Event()

        def enqueue(fn):
            queued.append(fn)
            cancelled.set()
            return future

        with patch.object(mainthread, "run_on_main", enqueue), patch.object(tools, "execute", create=True) as execute:
            with self.assertRaises(llm.Cancelled):
                agent._run_tool_on_main("run_python", "{}", cancelled.is_set)
            self.assertTrue(future.cancelled())
            self.assertFalse(future.set_running_or_notify_cancel())
            with self.assertRaises(llm.Cancelled):
                queued[0]()  # Also protected if the pump got to it at the same instant.
            execute.assert_not_called()

    def test_a_tool_timeout_is_not_retried_as_a_poll_timeout(self):
        from concurrent.futures import Future
        from unittest.mock import patch
        from loopcut import mainthread
        future = Future()
        future.set_exception(TimeoutError("tool timed out"))
        with patch.object(mainthread, "run_on_main", return_value=future):
            with self.assertRaisesRegex(TimeoutError, "tool timed out"):
                agent._run_tool_on_main("get_scene_info", "{}")

    def test_stop_during_auto_preview_preserves_the_completed_mutation(self):
        from test_scene_diff import cube, scene
        from unittest.mock import patch
        before, after = scene(), scene({"Cube": cube(), "Sphere": cube(data="Sphere")})
        def run_tool(name, arguments):
            if name == "capture_viewport":
                agent.stop()
                raise llm.Cancelled()
            return types.SimpleNamespace(text="sphere added", ok=True, image_path=None,
                                         scene_before=before, scene_after=after)
        self.run_tool = run_tool
        with patch.dict(os.environ, {"LOOPCUT_AUTO_LOOK": "true", "LOOPCUT_AUTO_RUN": "true"}):
            SCRIPT.replies.append(tool_reply("run_python", {"code": "add()", "summary": "Add"}))
            turn = self.start("add sphere")
            turn.thread.join(5)
        result = next(m for m in self.session["messages"] if m["role"] == "tool")
        self.assertIn("sphere added", result["content"])
        self.assertEqual(turn.scene_after, after)
        self.assertEqual(turn.outcome, "cancelled")
        self.assertEqual(next(i for i in self.session["items"] if i["kind"] == "tool")["status"], "done")

    def test_a_huge_step_result_is_folded_not_summarized_at_the_next_turn(self):
        os.environ["LOOPCUT_CONTEXT_BUDGET"] = "16000"
        self.addCleanup(os.environ.pop, "LOOPCUT_CONTEXT_BUDGET")
        self.run_tool = lambda name, arguments: types.SimpleNamespace(
            text="x" * 60_000 + "\n\nScene changes: none. (If you expected a change, the code did not do what you think.)",
            ok=True, image_path=None)
        SCRIPT.replies += [tool_reply("run_python", {"code": "big()", "summary": "Big"}), text_reply("Done.")]
        turn = self.start("first")
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        agent.decide(True)
        turn.thread.join(5)
        SCRIPT.replies.append(text_reply("Sure."))
        self.start("second").thread.join(5)
        self.assertEqual(len(SCRIPT.requests), 3, "no summarizing call: the record is small")
        sent = json.dumps(SCRIPT.requests[-1]["messages"][1:])  # Past the system prompt.
        self.assertIn("run_python \\\"Big\\\": ok, printed:", sent)
        self.assertLess(len(sent), 1500)

    def test_a_long_history_is_summarized_before_the_next_request(self):
        from loopcut import context
        os.environ["LOOPCUT_CONTEXT_BUDGET"] = "16000"
        self.addCleanup(os.environ.pop, "LOOPCUT_CONTEXT_BUDGET")
        SCRIPT.replies.append(text_reply("Read it."))
        self.start("first: " + "x" * 60_000).thread.join(5)  # A pasted document: a message is never folded.
        self.assertEqual(len(SCRIPT.requests), 1)

        SCRIPT.replies += [text_reply("Turn 1 was a long paste."), text_reply("Sure.")]
        self.start("second").thread.join(5)
        self.assertEqual(len(SCRIPT.requests), 3)
        summary_request = SCRIPT.requests[1]
        self.assertEqual(summary_request["messages"][0]["content"], context.SUMMARY_PROMPT)
        self.assertIn("USER: first: xxxx", summary_request["messages"][1]["content"])
        self.assertNotIn("tools", summary_request)
        sent = SCRIPT.requests[2]["messages"]
        self.assertEqual([m["role"] for m in sent], ["system", "user", "user"])
        self.assertIn("Turn 1 was a long paste.", sent[1]["content"])
        self.assertTrue(sent[2]["content"].endswith("second"))
        self.assertFalse(any(k.startswith("loopcut_") for m in sent for k in m))
        self.assertEqual(len(self.session["messages"]), 4, "the stored conversation is whole")
        self.assertEqual(self.session["messages"][2][context.SUMMARY], "Turn 1 was a long paste.")
        self.assertEqual([i["kind"] for i in self.session["items"]][-3:], ["user", "notice", "assistant"])
        self.assertIn("Summarized 2 earlier messages", self.session["items"][-2]["text"])

    def test_looks_are_capped_when_nothing_changed_and_free_after_a_change(self):
        from loopcut import conversations
        self.session["auto_run"] = True
        shot = Path(self.data_dir.name) / "viewport.png"
        shot.write_bytes(b"\x89PNG look")
        empty = {"too_many": False, "objects": {}, "object_count": 0, "materials": {}, "collections": {},
                 "scene": {}, "mode": "OBJECT"}
        changed = types.SimpleNamespace(text="OK\n\nScene changes: +Cube", ok=True, image_path=None,
                                        scene_before=empty, scene_after={**empty, "object_count": 1})
        self.run_tool = lambda name, arguments: (
            types.SimpleNamespace(text="Image attached.", ok=True, image_path=shot) if name == "capture_viewport" else changed)
        look = lambda: tool_reply("capture_viewport", {})
        SCRIPT.replies += [look(), look(), look(), look(), look(),                 # A first look, then 4 more at the same scene.
                           tool_reply("run_python", {"code": "move()", "summary": "Move"}),
                           look(), look(), text_reply("Done.")]
        self.start("check it").thread.join(5)
        cards = [i for i in self.session["items"] if i["kind"] == "tool"]
        self.assertEqual([c["status"] for c in cards], ["done"] * 4 + ["failed", "done", "done", "done"])
        refusals = [m for m in self.session["messages"] if m["role"] == "tool" and "has not changed" in m["content"]]
        self.assertEqual(len(refusals), 1)
        sent = SCRIPT.requests[-1]["messages"]
        images = [p for m in sent if isinstance(m.get("content"), list) for p in m["content"] if p["type"] == "image_url"]
        self.assertEqual(len(images), 1, "only the newest look is still an image")
        self.assertTrue(any("- looked" in (m.get("content") or "") for m in sent), "older looks folded")

    def test_after_a_change_the_agent_looks_by_itself_at_the_angle_last_asked_for(self):
        from loopcut import conversations, context
        os.environ["LOOPCUT_AUTO_LOOK"] = "true"
        self.addCleanup(os.environ.__setitem__, "LOOPCUT_AUTO_LOOK", "false")
        self.session["auto_run"] = True
        shot = Path(self.data_dir.name) / "viewport.png"
        empty = {"too_many": False, "objects": {}, "object_count": 0, "materials": {}, "collections": {},
                 "scene": {}, "mode": "OBJECT"}

        def run_tool(name, arguments):
            self.ran.append((name, arguments))
            if name == "capture_viewport":
                shot.write_bytes(b"\x89PNG look %d" % len(self.ran))
                return types.SimpleNamespace(text="Image attached: the view through Camera. Nearest to the viewpoint first: Cube 1.0 m.",
                                             ok=True, image_path=shot)
            return types.SimpleNamespace(text="OK\n\nScene changes: +Cube", ok=True, image_path=None,
                                         scene_before=empty, scene_after={**empty, "object_count": len(self.ran)})

        self.run_tool = run_tool
        step = lambda: tool_reply("run_python", {"code": "add()", "summary": "Add"})
        SCRIPT.replies += [tool_reply("capture_viewport", {"angle": "camera"}), step(), step(), text_reply("Done.")]
        session = self.session
        turn = agent.Turn()
        session["items"].append(state.item_user("build it"))
        session["messages"].append({"role": "user", "content": "build it"})
        session["busy"], session["turn"] = True, turn
        agent._run(session, turn, run_tool, self.ensure_checkpoint)
        self.assertEqual([(n, json.loads(a)) for n, a in self.ran],
                         [("capture_viewport", {"angle": "camera"}), ("run_python", {"code": "add()", "summary": "Add"}),
                          ("capture_viewport", {"angle": "camera"}), ("run_python", {"code": "add()", "summary": "Add"}),
                          ("capture_viewport", {"angle": "camera"})], "a look after each change, the model's angle")
        results = [m["content"] for m in session["messages"] if m["role"] == "tool"]
        self.assertTrue(results[1].startswith("OK\n\nScene changes: +Cube\n\nImage attached: the view through Camera"),
                        "the look is part of the step's result, as if the step had asked for it")
        captures = [m for m in session["messages"] if context.is_capture(m)]
        self.assertEqual(len(captures), 3)
        self.assertTrue(captures[1]["content"][0]["text"].startswith(context.CAPTURE_LABEL + "the view through Camera"))
        self.assertEqual(turn.captures, 1, "automatic looks are not counted against the model")
        sent = SCRIPT.requests[-1]["messages"]
        images = [p for m in sent if isinstance(m.get("content"), list) for p in m["content"] if p["type"] == "image_url"]
        self.assertEqual(len(images), 1, "the newest capture; earlier looks are remembered by what they showed")
        self.assertTrue(any("- looked: the view through Camera" in (m.get("content") or "") for m in sent))

    def _looking_tools(self):
        shot = Path(self.data_dir.name) / "viewport.png"
        empty = {"too_many": False, "objects": {}, "object_count": 0, "materials": {}, "collections": {},
                 "scene": {}, "mode": "OBJECT"}

        def run_tool(name, arguments):
            self.ran.append(name)
            if name in ("capture_viewport", "inspect_scene"):
                shot.write_bytes(b"\x89PNG look %d" % len(self.ran))
                return types.SimpleNamespace(text="Image attached: the view through Camera.", ok=True, image_path=shot)
            if name == "run_python":
                return types.SimpleNamespace(text="OK\n\nScene changes: +Cube", ok=True, image_path=None,
                                             scene_before=empty, scene_after={**empty, "object_count": len(self.ran)})
            return types.SimpleNamespace(text="read", ok=True, image_path=None)
        return run_tool

    def _run_batch(self, *calls):
        from unittest.mock import patch
        self.session["auto_run"] = True
        SCRIPT.replies += [multi_tool_reply(*calls), text_reply("Done.")]
        turn = agent.Turn()
        self.session["items"].append(state.item_user("go"))
        self.session["messages"].append({"role": "user", "content": "go"})
        self.session["busy"], self.session["turn"] = True, turn
        with patch.dict(os.environ, {"LOOPCUT_AUTO_LOOK": "true"}):
            agent._run(self.session, turn, self._looking_tools(), self.ensure_checkpoint, lambda a, b: False)

    def test_a_look_the_model_placed_after_its_change_is_not_doubled(self):
        """A reply of run_python then capture_viewport once got an automatic look between the two:
        two images of the same scene in every lighting step."""
        self._run_batch(("run_python", {"code": "add()", "summary": "Add"}), ("capture_viewport", {"angle": "camera"}))
        self.assertEqual(self.ran, ["run_python", "capture_viewport"])

    def test_the_automatic_look_comes_after_the_whole_reply(self):
        self._run_batch(("run_python", {"code": "add()", "summary": "Add"}), ("get_scene_info", {}))
        self.assertEqual(self.ran, ["run_python", "get_scene_info", "capture_viewport"])
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertIn("Image attached", results[0], "the look belongs to the step that changed the scene")
        self.assertEqual(results[1], "read")

    def test_a_change_after_the_models_look_still_gets_a_look(self):
        self._run_batch(("capture_viewport", {}), ("run_python", {"code": "add()", "summary": "Add"}))
        self.assertEqual(self.ran, ["capture_viewport", "run_python", "capture_viewport"])

    def test_an_automatic_look_that_shows_nothing_new_is_a_note_not_an_image(self):
        from loopcut import context
        os.environ["LOOPCUT_AUTO_LOOK"] = "true"
        self.addCleanup(os.environ.__setitem__, "LOOPCUT_AUTO_LOOK", "false")
        self.session["auto_run"] = True
        shot = Path(self.data_dir.name) / "viewport.png"
        empty = {"too_many": False, "objects": {}, "object_count": 0, "materials": {}, "collections": {},
                 "scene": {}, "mode": "OBJECT"}
        compared: list[tuple] = []

        def run_tool(name, arguments):
            self.ran.append((name, arguments))
            if name == "capture_viewport":
                shot.write_bytes(b"\x89PNG look %d" % len(self.ran))
                return types.SimpleNamespace(text="Image attached: three_quarter view of Cube.", ok=True, image_path=shot)
            return types.SimpleNamespace(text="OK\n\nScene changes: ~Cube", ok=True, image_path=None,
                                         scene_before=empty, scene_after={**empty, "object_count": len(self.ran)})

        def alike(previous, current):
            compared.append((previous.name, current.name))
            return len(compared) == 1  # The second step's look repeats the first; the third differs.

        step = lambda: tool_reply("run_python", {"code": "tweak()", "summary": "Tweak"})
        SCRIPT.replies += [step(), step(), step(), text_reply("Done.")]
        session = self.session
        turn = agent.Turn()
        session["items"].append(state.item_user("tweak it"))
        session["messages"].append({"role": "user", "content": "tweak it"})
        session["busy"], session["turn"] = True, turn
        agent._run(session, turn, run_tool, self.ensure_checkpoint, alike)
        self.assertEqual([n for n, _ in self.ran], ["run_python", "capture_viewport"] * 3, "the capture is still taken: it is free")
        captures = [m for m in session["messages"] if context.is_capture(m)]
        self.assertEqual(len(captures), 2, "the repeated look is not stored as an image")
        results = [m["content"] for m in session["messages"] if m["role"] == "tool"]
        self.assertIn("Image attached", results[0])
        self.assertIn("no visible difference from your last capture", results[1])
        self.assertNotIn("Image attached", results[1])
        self.assertIn("Image attached", results[2])
        self.assertEqual(len(compared), 2, "the first look has nothing to compare with")
        self.assertTrue(all(p.endswith(".png") for p, _ in compared))

    def test_many_looks_are_nudged_not_refused(self):
        self.session["auto_run"] = True
        shot = Path(self.data_dir.name) / "viewport.png"
        shot.write_bytes(b"\x89PNG look")
        self.run_tool = lambda name, arguments: types.SimpleNamespace(text="Image attached: front view of Cube.", ok=True, image_path=shot)
        angles = ["front", "side"] * 5  # Never the same look twice in a row: not idle, just many.
        SCRIPT.replies += [tool_reply("capture_viewport", {"angle": a}) for a in angles] + [text_reply("Done.")]
        self.start("check it").thread.join(5)
        cards = [i for i in self.session["items"] if i["kind"] == "tool"]
        self.assertEqual([c["status"] for c in cards], ["done"] * 10, "no hard ceiling per turn")
        self.assertNotIn("Look 8 this turn", cards[7]["output"])
        self.assertIn("(Look 9 this turn.", cards[8]["output"])
        self.assertIn("(Look 10 this turn.", cards[9]["output"])

    def test_attached_images_get_a_reference_card_before_the_first_request(self):
        from loopcut import conversations, context
        picture = Path(self.data_dir.name) / "chair.png"
        picture.write_bytes(b"\x89PNG chair photo")
        session = self.session
        session["attachments"] = [{"ref": conversations.store_image(session, picture), "full": conversations.store_image(session, picture), "name": "chair.png"}]
        SCRIPT.replies += [text_reply("A wooden chair: seat 0.45 m high, four round legs, #8B5A2B, matte."),
                           text_reply("I will build it.")]
        agent._scene_context = lambda text, since=None: "<scene_context>\nfile: unsaved\n</scene_context>"
        self.assertTrue(agent.send("copy this chair"))
        session["turn"].thread.join(5)
        card_request = SCRIPT.requests[0]["messages"]
        self.assertEqual(card_request[0]["content"], agent.REFERENCE_PROMPT)
        self.assertEqual(card_request[1]["content"][1]["type"], "image_url")
        message = session["messages"][0]
        self.assertTrue(message[context.REFERENCE_CARD])
        self.assertIn('<reference_card images="chair.png">\nA wooden chair', message["content"][0]["text"])
        self.assertEqual(session["references"][0]["name"], "chair.png")
        self.assertTrue(session["references"][0]["pinned"])
        self.assertEqual([i["kind"] for i in session["items"]], ["user", "notice", "assistant"])
        self.assertIn("reference_card", SCRIPT.requests[1]["messages"][1]["content"][0]["text"], "the card is in the real request")
        SCRIPT.replies.append(text_reply("Still looking at it."))
        self.start("now the legs").thread.join(5)
        sent = SCRIPT.requests[2]["messages"]
        self.assertEqual(sent[1]["content"][1]["type"], "image_url", "the reference is still sent next turn")
        session["references"][0]["pinned"] = False
        SCRIPT.replies.append(text_reply("Gone."))
        self.start("and now").thread.join(5)
        self.assertEqual(SCRIPT.requests[3]["messages"][1]["content"][1]["type"], "text", "unpinned: dropped")

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

    def test_a_render_asks_every_time_even_after_always_allow(self):
        SCRIPT.replies += [tool_reply("run_python", {"code": "a()", "summary": "a"}),
                           tool_reply("see_render", {"render": True}),
                           tool_reply("run_python", {"code": "bpy.ops.render.render(write_still=True)", "summary": "render"}),
                           text_reply("Done.")]
        turn = self.start("render it")
        awaiting = lambda: [i for i in self.session["items"] if i.get("status") == "awaiting"]
        self.wait_for(awaiting, "first approval card")
        self.assertFalse(awaiting()[0].get("heavy"))
        self.assertTrue(agent.decide(True, always=True))
        self.wait_for(lambda: awaiting() and awaiting()[0]["name"] == "see_render", "the render's card")
        self.assertTrue(awaiting()[0]["heavy"], "a render is marked heavy so the card offers Always allow renders")
        self.assertEqual(awaiting()[0]["summary"], "Render the scene (preview)")
        agent.decide(True)
        self.wait_for(lambda: awaiting() and awaiting()[0]["name"] == "run_python", "the rendering code's card")
        self.assertTrue(awaiting()[0]["heavy"])
        agent.decide(False)
        turn.thread.join(5)
        self.assertEqual([n for n, _ in self.ran], ["run_python", "see_render"], "the rejected render never ran")

    def test_always_allow_renders_stops_asking_for_renders_only(self):
        SCRIPT.replies += [tool_reply("see_render", {"render": True}),
                           tool_reply("see_render", {"render": True}),
                           tool_reply("run_python", {"code": "a()", "summary": "a"}),
                           tool_reply("run_python", {"code": "bpy.ops.render.render(write_still=True)", "summary": "render"}),
                           text_reply("Done.")]
        turn = self.start("render it twice")
        awaiting = lambda: [i for i in self.session["items"] if i.get("status") == "awaiting"]
        self.wait_for(awaiting, "the render's card")
        self.assertTrue(awaiting()[0]["heavy"])
        self.assertTrue(agent.decide(True, always=True))
        self.wait_for(lambda: awaiting() and awaiting()[0]["summary"] == "a", "the ordinary step's card")
        self.assertFalse(self.session["auto_run"], "allowing renders does not allow other steps")
        self.assertEqual([n for n, _ in self.ran], ["see_render", "see_render"], "second render ran unasked")
        agent.decide(True)
        self.wait_for(lambda: awaiting() and awaiting()[0]["summary"] == "render", "the rendering code's card")
        self.assertFalse(awaiting()[0]["heavy"], "code that renders still asks as code, so its Always is the plain one")
        agent.decide(False)
        turn.thread.join(5)
        self.assertEqual([n for n, _ in self.ran], ["see_render", "see_render", "run_python"])
        self.assertFalse(state.new_session()["auto_heavy"], "a new conversation asks again")

    def test_a_render_asks_even_with_auto_run_on(self):
        os.environ["LOOPCUT_AUTO_RUN"] = "true"
        self.addCleanup(os.environ.update, {"LOOPCUT_AUTO_RUN": "false"})
        SCRIPT.replies += [tool_reply("see_render", {"render": True}), text_reply("Done.")]
        turn = self.start("render")
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "approval card")
        agent.decide(True)
        turn.thread.join(5)
        self.assertEqual([n for n, _ in self.ran], ["see_render"])

    def test_file_reads_inside_the_project_run_unasked_and_writes_ask(self):
        project = Path(self.data_dir.name) / "project"
        project.mkdir(exist_ok=True)
        SCRIPT.replies += [tool_reply("read_file", {"path": str(project / "notes.txt")}),
                           tool_reply("see_render", {}),
                           tool_reply("list_files", {"path": str(Path(self.data_dir.name) / "elsewhere")}),
                           tool_reply("write_file", {"path": str(project / "out.txt"), "content": "hi"}),
                           text_reply("Done.")]
        turn = self.start("look around")
        turn.roots = (project.resolve(),)
        awaiting = lambda: [i for i in self.session["items"] if i.get("status") == "awaiting"]
        self.wait_for(lambda: awaiting() and awaiting()[0]["name"] == "list_files", "the outside read's card")
        self.assertEqual([n for n, _ in self.ran], ["read_file", "see_render"], "in-project read and last render ran unasked")
        self.assertEqual(awaiting()[0]["summary"], f"List {Path(self.data_dir.name) / 'elsewhere'}")
        agent.decide(True)
        self.wait_for(lambda: awaiting() and awaiting()[0]["name"] == "write_file", "the write's card")
        self.assertEqual(awaiting()[0]["code"], "hi", "the card shows what would be written")
        agent.decide(True)
        turn.thread.join(5)
        self.assertEqual([n for n, _ in self.ran], ["read_file", "see_render", "list_files", "write_file"])

    def test_token_usage_adds_up_over_the_conversation(self):
        usage = {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 8}}
        SCRIPT.replies += [sse({"choices": [{"delta": {"content": "Hi."}}]}, usage)] * 2
        self.start("hi").thread.join(5)
        self.start("again").thread.join(5)
        self.assertEqual(self.session["usage"], {"input": 240, "cached": 0, "output": 16, "context": 120})
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

    def test_out_of_allowance_is_a_card_with_the_gateway_s_offer_not_an_error(self):
        SCRIPT.replies += [(402, {"error": {
            "message": "You have used this week's allowance on the Free plan. It resets in 3 days.",
            "type": "week_limit", "code": "week_limit", "plan": "free", "resets_at": "2026-09-23T00:00:00Z",
            "upgrade": {"url": "https://loopcut.org/pricing?from=app&reason=week_limit&plan=pro", "plan": "pro",
                        "label": "Upgrade to Pro", "note": "Pro has 13× the allowance, $20/month."}}})]
        turn = self.start("make a chair")
        turn.thread.join(5)
        card = self.session["items"][-1]
        self.assertEqual(card["kind"], "limit", card)
        self.assertEqual((card["reason"], card["plan"], card["status"]), ("week_limit", "free", ""))
        self.assertEqual(card["upgrade"]["label"], "Upgrade to Pro")
        self.assertTrue(card["text"].startswith("You have used this week's allowance"))
        self.assertFalse(any(i["kind"] == "error" for i in self.session["items"]), "no red error card as well")
        self.assertFalse(self.session["busy"])
        # The card's buttons are on the newest card only, and Upgrade comes first.
        display = layout.build(self.session, 400, 600, 1.0, LayoutTest.measure, "m")
        buttons = [h for h in display["hits"] if h["id"].startswith("item") and ".limit." in h["id"]]
        self.assertEqual([h["action"][0] for h in buttons], ["limit_upgrade", "resume"])
        # A second 402 from a provider that says nothing structured still gets a card, without a button.
        SCRIPT.replies += [(402, {"error": {"message": "Insufficient credit.", "type": "billing"}})]
        turn = self.start("try again")
        turn.thread.join(5)
        card = self.session["items"][-1]
        self.assertEqual((card["kind"], card["reason"], card["upgrade"]), ("limit", "billing", None))

    def test_resume_makes_the_request_again_without_adding_a_message(self):
        SCRIPT.replies += [(402, {"error": {"message": "Used up.", "code": "week_limit", "plan": "free"}}),
                           text_reply("Here is your chair.")]
        turn = self.start("make a chair")
        turn.thread.join(5)
        before = len(self.session["messages"])
        self.assertTrue(agent.resume())
        self.session["turn"].thread.join(5)
        self.assertEqual(len(SCRIPT.requests), 2)
        self.assertEqual(SCRIPT.requests[1]["messages"][-1]["content"], "make a chair", "the same message, sent again")
        self.assertEqual(len(self.session["messages"]), before + 1, "only the reply was added")
        self.assertEqual(self.session["items"][-1]["text"], "Here is your chair.")
        self.assertFalse(agent.resume(), "nothing to resume once the turn has finished")

    def test_usage_headers_update_the_account_and_warn_once_when_the_week_is_nearly_used(self):
        SCRIPT.headers.update({"x-loopcut-plan": "free", "x-loopcut-session-used": "0.310",
                               "x-loopcut-week-used": "0.842", "x-loopcut-week-resets-at": "2026-09-23T00:00:00Z"})
        SCRIPT.replies += [text_reply("One."), text_reply("Two.")]
        turn = self.start("hello")
        turn.thread.join(5)
        self.assertEqual(state.ui["account"]["plan"], "free")
        self.assertEqual(state.ui["account"]["week"], {"used": 0.842, "resets_at": "2026-09-23T00:00:00Z"})
        notices = [i for i in self.session["items"] if i["kind"] == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertIn("About 16% of this week's free allowance is left", notices[0]["text"])
        self.assertEqual(notices[0]["action_label"], "See plans")
        self.assertIn("/pricing?from=app&reason=low_allowance", notices[0]["url"])
        turn = self.start("again")
        turn.thread.join(5)
        self.assertEqual(len([i for i in self.session["items"] if i["kind"] == "notice"]), 1, "warned once")
        self.assertEqual(layout.usage_note(state.ui["account"]), ("84% of week used", 0.842))
        self.assertEqual(layout.usage_note({"week": {"used": 0.2}, "session": {"used": 0.5}}), ("", 0.5))

    def test_http_error_is_shown_not_swallowed(self):
        SCRIPT.replies.append((403, {"error": {"message": "This key was revoked."}}))
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1],
                         state.item_error("HTTP 403: This key was revoked."))
        self.assertFalse(self.session["busy"])


class LayoutTest(unittest.TestCase):
    @staticmethod
    def measure(font, size, text):
        return len(text) * size * 0.5

    def test_thinking_line_shimmers_and_counts_dots_with_the_clock(self):
        session = state.new_session()
        session["items"] += [state.item_user("hi"), state.item_assistant()]
        session["busy"] = True

        def prims_at(now):
            display = layout.build(session, 400, 600, 1.0, self.measure, "m", None, {"now": now})
            return [p for p in display["prims"] if p.get("id", "").startswith("item1.thinking")]

        first, later = prims_at(0.0), prims_at(1.0)
        self.assertEqual("".join(p["text"] for p in first), "Thinking.")
        self.assertEqual("".join(p["text"] for p in later), "Thinking...")
        brightest = lambda prims: max(prims[:-1], key=lambda p: p["color"][0])["text"]
        self.assertNotEqual(brightest(first), brightest(later), "the highlight moves along the word")
        self.assertNotIn("…", "".join(p.get("text", "") for p in first))
        session["items"][-1]["streaming"] = False
        session["items"][-1]["text"] = "Done."
        display = layout.build(session, 400, 600, 1.0, self.measure, "m", None, {"now": 2.0})
        self.assertTrue(any(p.get("id", "").startswith("chat.working") for p in display["prims"]),
                        "between steps the same animation says Working")

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
