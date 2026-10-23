"""End-to-end test of llm.py + agent.py against a scripted local SSE server. No Blender needed.

    python3 -m unittest discover -s tests -v
"""

import dataclasses
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

# agent.py only needs bpy for the session store; give it a stand-in.
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, checkpoints, config, context, conversations, llm, state, tools, waves, workspace  # noqa: E402
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


class Hang:
    """A reply that writes `prefix` (maybe none), then stalls past the client's timeout= so its
    socket read raises a bare TimeoutError (not an HTTPError/URLError: urlopen has already returned
    by then). Same trick as test_llm.py's Hang, duplicated here rather than shared across files."""

    def __init__(self, prefix: bytes = b"", delay: float = 0.25):
        self.prefix, self.delay = prefix, delay


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
        # The Message object, not a plain dict: header names urllib sends are not what you typed
        # (Request.add_header capitalizes only the first letter), so lookups must stay case-insensitive.
        body["_headers"] = self.headers
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
        if isinstance(reply, Hang):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            for name, value in SCRIPT.headers.items():
                self.send_header(name, value)
            self.end_headers()
            if reply.prefix:
                self.wfile.write(reply.prefix)
                self.wfile.flush()
            time.sleep(reply.delay)
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
            "LOOPCUT_ORCHESTRATE": "false",  # Today's single loop; the orchestrator tests set their own spec.
            "LOOPCUT_ROUTER": "off",  # No router call in the scripts; test_router.py drives it.
        })
        # Put back after the class: test_workspace measures the real _tool_schemas.
        cls.replaced = {name: getattr(agent, name) for name in (
            "_scene_snapshot", "_addons_line", "_project_roots", "_alike_on_main", "_update_account",
            "_keeps_context", "_redraw", "_tool_schemas", "_changes_scene")}
        agent._scene_snapshot = lambda: None
        agent._addons_line = lambda: ""  # Reads bpy; the prompt test sets its own line.
        agent._project_roots = lambda: ()  # Reads bpy on the main thread; the file tests set roots themselves.
        agent._alike_on_main = lambda a, b: False  # Needs Blender to read pixels; tests pass their own.
        agent._update_account = lambda info: state.ui.__setitem__("account", info)  # account.py needs bpy.
        agent._keeps_context = lambda cfg: False  # account.py needs bpy.
        agent._redraw = lambda: None
        agent._tool_schemas = lambda: []
        agent._changes_scene = lambda name, arguments="": name == "run_python"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.data_dir.cleanup()
        for name, original in cls.replaced.items():
            setattr(agent, name, original)

    def setUp(self):
        from prompt_index_fixture import index_patch
        patcher = index_patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        SCRIPT.replies.clear()
        SCRIPT.requests.clear()
        SCRIPT.headers.clear()
        state.ui["account"] = None
        state.ui.pop("allowance_notice", None)
        state.ui.pop("warned_session_allowance", None)
        state.ui.pop("warned_week_allowance", None)
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

    def start(self, text: str, roots: tuple = (), spec=None) -> agent.Turn:
        session = self.session
        session["items"].append(state.item_user(text))
        session["messages"].append({"role": "user", "content": text})
        turn = agent.Turn()
        turn.spec = spec
        turn.roots = roots  # Before the thread starts, as send() does; setting it after races the first call.
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

    def test_sends_conversation_and_role_headers_for_the_gateway(self):
        """Loopcut's gateway correlates a usage row with the conversation and role that made it
        (x-loopcut-conversation / x-loopcut-role); the single loop's own request must send both."""
        SCRIPT.replies.append(text_reply("hi"))
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": os.environ["LOOPCUT_BASE_URL"]}):
            self.start("hi").thread.join(5)
        headers = SCRIPT.requests[0]["_headers"]
        self.assertEqual(headers.get("x-loopcut-conversation"), self.session["id"])
        self.assertEqual(headers.get("x-loopcut-role"), "single")

    def test_byok_request_has_no_loopcut_headers(self):
        SCRIPT.replies.append(text_reply("hi"))
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": ""}):
            self.start("hi").thread.join(5)
        self.assertEqual(len(SCRIPT.requests), 1)
        headers = SCRIPT.requests[0]["_headers"]
        self.assertFalse([name for name in headers if name.lower().startswith("x-loopcut-")])

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

    def test_a_setup_card_request_sent_unedited_marks_its_turn_and_an_edited_one_does_not(self):
        scene = mock.patch.object(agent, "_scene_context", lambda text, since=None: "<scene_context>\n</scene_context>")
        scene.start()
        self.addCleanup(scene.stop)
        for sent, marked in (("Turn the photo into a model.", "photo_3d"), ("Turn the photo into a blue model.", "")):
            SCRIPT.replies.append(text_reply("Done."))
            self.session["try_it"] = ("photo_3d", "Turn the photo into a model.")  # ui.host.start_request
            self.assertTrue(agent.send(sent))
            turn = self.session["turn"]
            turn.thread.join(5)
            self.assertEqual(turn.try_it, marked)  # Edited, it is the user's own request: an ordinary paid call.
            self.assertNotIn("try_it", self.session)  # Once: the next request is never free.

    def awaiting(self):
        self.wait_for(lambda: any(i.get("status") == "awaiting" for i in self.session["items"]), "the approval card")

    def paid_run_tool(self, name, arguments):
        """A paid cloud call as cloud.execute's finish makes it (C47): consent under the loop's card, then the
        result, or the refusal as the tool's text."""
        from loopcut import cloud, cloud_account, cloud_http
        try:
            cloud_account.confirm({"required": False, "text": "About 80 credits ($0.80)", "credits": 80,
                                   "threshold": 100})
        except cloud_http.CloudError as ex:
            return tools.ToolResult(cloud.error_text(ex), ok=False)
        self.ran.append((name, arguments))
        return tools.ToolResult("Made a 3D model.")

    def test_a_paid_call_over_ask_above_pauses_and_skip_lets_the_turn_go_on(self):
        self.run_tool = self.paid_run_tool
        SCRIPT.replies += [tool_reply("loopcut_cloud__image_to_3d", {"image": "lamp.png"}),
                           text_reply("Skipped the model; here is how to block it out instead.")]
        turn = self.start("make the lamp in 3D")
        self.awaiting()
        card = next(i for i in self.session["items"] if i.get("status") == "awaiting")
        self.assertEqual((card["ask"], card["ask_label"]), ("cost", "Approve"))
        agent.decide(False)
        turn.thread.join(5)
        self.assertEqual(turn.outcome, "completed", "a skipped paid call never ends the turn")
        self.assertEqual((card["status"], self.ran), ("rejected", []))
        result = next(m for m in SCRIPT.requests[1]["messages"] if m["role"] == "tool")
        self.assertTrue(result["content"].startswith("declined: The user declined this paid call (it costs 80"))
        self.assertEqual(self.session["items"][-1]["text"], "Skipped the model; here is how to block it out instead.")
        self.assertEqual(self.session["spend"]["credits"], 0)

    def test_a_paid_call_over_ask_above_pauses_and_approve_runs_it(self):
        self.run_tool = self.paid_run_tool
        SCRIPT.replies += [tool_reply("loopcut_cloud__image_to_3d", {"image": "lamp.png"}), text_reply("Done.")]
        turn = self.start("make the lamp in 3D")
        self.awaiting()
        self.assertEqual(self.ran, [], "nothing runs before the answer")
        agent.decide(True)
        turn.thread.join(5)
        self.assertEqual((turn.outcome, len(self.ran)), ("completed", 1))
        card = next(i for i in self.session["items"] if i.get("name") == "loopcut_cloud__image_to_3d")
        self.assertEqual(card["status"], "done")
        self.assertEqual(self.session["spend"], {"credits": 80, "auto": 0, "allowed": 0})

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
        with mock.patch.object(agent, "_resend_on_main", side_effect=lambda text, items: resent.append(text)), \
                mock.patch.object(agent, "_fold_notes"):  # As if the note came during the final request.
            SCRIPT.replies += [tool_reply("run_python", {"code": "add_cube()", "summary": "Add a cube"}),
                               text_reply("Done.")]
            turn = self.start("add a cube")
            self.awaiting()
            self.assertTrue(agent.send("and a sphere"))
            item = self.session["items"][-1]
            agent.decide(True)
            turn.thread.join(5)
        self.assertEqual(resent, ["and a sphere"])
        self.assertIn(item, self.session["items"], "the next turn reuses the note and its helper receipts")
        self.assertNotIn("queued", item)
        self.assertFalse(self.session["busy"])

    def test_a_note_left_over_after_a_stop_goes_back_to_the_input(self):
        restored = []
        with mock.patch.object(agent, "_restore_input_on_main", side_effect=restored.append):
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
            SCRIPT.replies += [tool_reply("get_scene_info", {}), text_reply("Read the scene; nothing else yet.")]
            turn = self.start("inspect")
            turn.thread.join(5)
        self.assertEqual(turn.outcome, "step_limit")
        self.assertEqual(self.session["turn_outcome"], "step_limit")

    def test_unlimited_turns_run_forty_tool_steps_in_every_user_loop(self):
        for spec in (agent.SINGLE_SPEC, agent.ORCH_SPEC, agent.WORKSPACE_SPEC):
            for limit in (None, "0"):
                with self.subTest(role=spec.role, limit=limit), \
                        mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": os.environ["LOOPCUT_BASE_URL"]}), \
                        mock.patch.object(agent, "_route"), mock.patch.object(agent, "_workspace_turn"):
                    os.environ.pop("LOOPCUT_MAX_STEPS", None)
                    if limit is not None:
                        os.environ["LOOPCUT_MAX_STEPS"] = limit
                    self.session = state.reset()
                    self.ran.clear()
                    SCRIPT.requests.clear()
                    SCRIPT.replies[:] = [tool_reply("get_scene_info", {"name_contains": f"part{i}"})
                                         for i in range(40)] + [text_reply("All forty finished.")]
                    turn = self.start("inspect", spec=spec)
                    turn.thread.join(10)
                    self.assertFalse(turn.thread.is_alive())
                    self.assertEqual(turn.outcome, "completed")
                    self.assertEqual(len(self.ran), 40)
                    self.assertEqual(len(SCRIPT.requests), 41)
                    self.assertFalse(any("steps left" in text for text in self.results()))
                    self.assertEqual(self.session["items"][-1]["text"], "All forty finished.")

    def failing_tools(self, *, change=False, ok=False):
        def run(name, arguments):
            self.ran.append((name, arguments))
            before = {"too_many": False, "objects": {}, "object_count": 0, "materials": {}, "collections": {},
                      "scene": {}, "mode": "OBJECT"}
            return types.SimpleNamespace(text="Tool failed" if not ok else "OK", ok=ok, image_path=None,
                                         scene_before=before, scene_after={**before, "object_count": len(self.ran)}
                                         if change else before.copy())
        self.run_tool = run
        self.session["auto_run"] = True

    def test_identical_failures_stop_and_wrap_up_in_every_user_loop(self):
        for spec in (agent.SINGLE_SPEC, agent.ORCH_SPEC, agent.WORKSPACE_SPEC):
            # Failed reads keep their refusal; refusals count toward the same guard as failures.
            for tool, args in (("get_scene_info", {}), ("run_python", {"code": "broken()", "summary": "Try"})):
                with self.subTest(role=spec.role, tool=tool), \
                        mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": os.environ["LOOPCUT_BASE_URL"]}), \
                        mock.patch.object(agent, "_route"), \
                        mock.patch.object(agent, "_workspace_turn"):
                    self.session = state.reset()
                    self.ran.clear()
                    SCRIPT.requests.clear()
                    self.failing_tools()
                    SCRIPT.replies[:] = [tool_reply(tool, args)] * 8 + [text_reply("Blocked by the tool failure.")]
                    turn = self.start("build", spec=spec)
                    turn.thread.join(10)
                    self.assertFalse(turn.thread.is_alive())
                    self.assertEqual(turn.outcome, "no_progress")
                    self.assertEqual(self.session["turn_outcome"], "no_progress")
                    self.assertEqual(len(self.ran), 2 if tool == "get_scene_info" else 8)
                    self.assertEqual(len(SCRIPT.requests), 9)
                    self.assertEqual(self.session["items"][-2], state.item_notice(agent.NO_PROGRESS_NOTICE))
                    self.assertEqual(self.session["items"][-1]["text"], "Blocked by the tool failure.")
                    self.assertEqual(SCRIPT.requests[-1]["messages"][-1]["content"], agent.NO_PROGRESS_PROMPT)
                    self.assertFalse(context.is_turn_start(self.session["messages"][-2]))
                    if tool == "get_scene_info":
                        self.assertIn("Same error as before", self.results()[1])
                        self.assertTrue(all(result.startswith("Not run: you made this same call at steps 1 and 2")
                                            for result in self.results()[2:]))
                        self.assertEqual([i["status"] for i in self.session["items"] if i["kind"] == "tool"],
                                         ["failed"] * 2 + ["rejected"] * 6)
                    else:
                        self.assertIn("This error has now come up 3 times", self.results()[2])

    def test_scene_changes_distinct_calls_and_successes_keep_the_turn_running(self):
        for case in ("scene_changes", "different_arguments", "successes", "success_between"):
            with self.subTest(case=case):
                self.session = state.reset()
                self.ran.clear()
                self.failing_tools(change=case == "scene_changes", ok=case == "successes")
                calls = [("run_python", {"code": "broken()", "summary": "Try"}) for _ in range(16)]
                if case == "different_arguments":
                    calls = [(name, {**args, "code": f"try_{i}()"}) for i, (name, args) in enumerate(calls)]
                if case == "success_between":
                    run = self.run_tool
                    def sometimes_ok(name, arguments):
                        result = run(name, arguments)
                        result.ok = len(self.ran) in (8, 16)
                        return result
                    self.run_tool = sometimes_ok
                SCRIPT.replies[:] = [tool_reply(*call) for call in calls] + [text_reply("Finished.")]
                turn = self.start("build")
                turn.thread.join(10)
                self.assertFalse(turn.thread.is_alive())
                self.assertEqual(turn.outcome, "completed")
                self.assertEqual(len(self.ran), 16)
                self.assertFalse(any(i.get("text") == agent.NO_PROGRESS_NOTICE for i in self.session["items"]))

    def test_no_progress_closes_pending_calls_and_ignores_tools_in_wrap_up(self):
        self.failing_tools()
        call = ("run_python", {"code": "broken()", "summary": "Try"})
        # Three failed calls plus three duplicates, then a failure and a duplicate reach eight.
        SCRIPT.replies[:] = [multi_tool_reply(call, call)] * 3
        SCRIPT.replies += [multi_tool_reply(call, call, ("get_scene_info", {})),
                           sse({"choices": [{"delta": {"content": "Blocked.", "tool_calls": [
                               {"index": 0, "id": "summary_call", "type": "function",
                                "function": {"name": "get_scene_info", "arguments": "{}"}}]}}]},
                               {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})]
        turn = self.start("build")
        turn.thread.join(10)
        self.assertEqual(turn.outcome, "no_progress")
        self.assertFalse(turn.thread.is_alive())
        self.assertEqual(len(self.ran), 4)
        self.assertEqual(len(SCRIPT.requests), 5)
        last_results = [m for m in SCRIPT.requests[-1]["messages"] if m["role"] == "tool"][-3:]
        self.assertEqual([m["tool_call_id"] for m in last_results], ["call_1", "call_2", "call_3"])
        self.assertIn("Not run: duplicate", last_results[-2]["content"])
        self.assertIn("Not run: stopped because no progress", last_results[-1]["content"])
        self.assertEqual(self.session["messages"][-1], {"role": "assistant", "content": "Blocked."})

    def test_refusals_after_successful_reads_stop_the_turn(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                self.session = state.reset()
                self.ran.clear()
                SCRIPT.requests.clear()
                read = ("get_scene_info", {})
                pending = ("get_object_info", {"names": ["Cube"]})
                # Two cached successes then eight refusals, or one success then eight duplicates.
                SCRIPT.replies[:] = ([] if duplicate else [tool_reply(*read)] * 9)
                SCRIPT.replies += [multi_tool_reply(*([read] * 9 if duplicate else [read]), pending),
                                   text_reply("Blocked by repeated reads.")]
                turn = self.start("inspect")
                turn.thread.join(10)
                self.assertFalse(turn.thread.is_alive())
                self.assertEqual(turn.outcome, "no_progress")
                self.assertEqual(len(self.ran), 1 if duplicate else 2)
                self.assertEqual(len(SCRIPT.requests), 2 if duplicate else 11)
                self.assertIn("Not run: stopped because no progress", self.results()[-1])
                cards = [i for i in self.session["items"] if i["kind"] == "tool"]
                self.assertEqual(sum(i["status"] == "rejected" for i in cards), 8)
                self.assertEqual(self.session["items"][-2], state.item_notice(agent.NO_PROGRESS_NOTICE))
                self.assertEqual(self.session["items"][-1]["text"], "Blocked by repeated reads.")

    def test_alternating_failures_stop_on_the_eighth_occurrence_of_one_key(self):
        for reads in (False, True):
            with self.subTest(reads=reads):
                self.session = state.reset()
                self.ran.clear()
                SCRIPT.requests.clear()
                self.failing_tools()
                a, b = (("get_scene_info", {}), ("get_object_info", {})) if reads else (
                    ("run_python", {"code": "a()", "summary": "A"}),
                    ("run_python", {"code": "b()", "summary": "B"}))
                SCRIPT.replies[:] = [tool_reply(*call) for call in [a, b] * 7 + [a]]
                SCRIPT.replies.append(text_reply("Blocked by alternating failures."))
                turn = self.start("build")
                turn.thread.join(10)
                self.assertFalse(turn.thread.is_alive())
                self.assertEqual(turn.outcome, "no_progress")
                self.assertEqual(len(self.ran), 4 if reads else 15)
                self.assertEqual(len(SCRIPT.requests), 16)

    def test_successful_reads_of_other_keys_do_not_reset_the_guard(self):
        self.failing_tools()
        run = self.run_tool
        def read_succeeds(name, arguments):
            result = run(name, arguments)
            result.ok = name == "get_scene_info"
            return result
        self.run_tool = read_succeeds
        failure = ("run_python", {"code": "broken()", "summary": "Try"})
        SCRIPT.replies[:] = [multi_tool_reply(failure, ("get_scene_info", {"name_contains": str(i)}))
                             for i in range(7)]
        SCRIPT.replies += [tool_reply(*failure), text_reply("Blocked.")]
        turn = self.start("build")
        turn.thread.join(10)
        self.assertFalse(turn.thread.is_alive())
        self.assertEqual(turn.outcome, "no_progress")
        self.assertEqual(len(self.ran), 15)
        self.assertEqual(len(SCRIPT.requests), 9)

    def test_success_of_one_key_does_not_reset_another_keys_failures(self):
        self.failing_tools()
        run = self.run_tool
        def succeeds_once(name, arguments):
            result = run(name, arguments)
            result.ok = len(self.ran) == 15
            return result
        self.run_tool = succeeds_once
        a = ("run_python", {"code": "a()", "summary": "A"})
        b = ("run_python", {"code": "b()", "summary": "B"})
        SCRIPT.replies[:] = [tool_reply(*call) for call in [a, b] * 7 + [a, a, b]]
        SCRIPT.replies.append(text_reply("B is still blocked."))
        turn = self.start("build")
        turn.thread.join(10)
        self.assertFalse(turn.thread.is_alive())
        self.assertEqual(turn.outcome, "no_progress")
        self.assertEqual(len(self.ran), 17)
        self.assertEqual(len(SCRIPT.requests), 18)

    def test_scene_changes_user_notes_and_approval_rejections_reset_all_counts(self):
        for reset in ("measured_change", "unmeasured_mutation", "user_note", "approval_rejection"):
            with self.subTest(reset=reset):
                self.session = state.reset()
                self.ran.clear()
                SCRIPT.requests.clear()
                self.failing_tools()
                run = self.run_tool
                def reset_between(name, arguments):
                    result = run(name, arguments)
                    if len(self.ran) == 14:
                        if reset == "user_note":
                            agent.send("Try again after my edits.")
                        elif reset == "approval_rejection":
                            self.session["auto_run"] = False
                    if json.loads(arguments)["code"] == "reset()":
                        if reset == "measured_change":
                            result.scene_after["object_count"] = 1
                        elif reset == "unmeasured_mutation":
                            del result.scene_before, result.scene_after
                    return result
                self.run_tool = reset_between
                def reject(turn):
                    self.session["auto_run"] = True
                    return False
                a = ("run_python", {"code": "a()", "summary": "A"})
                b = ("run_python", {"code": "b()", "summary": "B"})
                middle = [] if reset == "user_note" else [("run_python", {"code": "reset()", "summary": "Reset"})]
                SCRIPT.replies[:] = [tool_reply(*call) for call in [a, b] * 7 + middle + [a, b] * 7 + [b]]
                SCRIPT.replies.append(text_reply("Blocked after retrying."))
                with mock.patch.object(agent, "_wait_for_approval", side_effect=reject) as approval:
                    turn = self.start("build")
                    turn.thread.join(10)
                self.assertFalse(turn.thread.is_alive())
                self.assertEqual(turn.outcome, "no_progress")
                self.assertEqual(len(self.ran), 29 if reset in ("user_note", "approval_rejection") else 30)
                self.assertEqual(len(SCRIPT.requests), 30 + len(middle))
                self.assertEqual(approval.call_count, int(reset == "approval_rejection"))

    def test_capture_and_checkpoint_refusals_count_as_no_progress(self):
        for refusal in ("capture", "checkpoint"):
            with self.subTest(refusal=refusal):
                self.session = state.reset()
                self.session["auto_run"] = True
                self.ran.clear()
                SCRIPT.requests.clear()
                if refusal == "checkpoint":
                    self.checkpoint_error = checkpoints.CheckpointError("Disk full.")
                call = ("capture_viewport", {}) if refusal == "capture" else (
                    "run_python", {"code": "add()", "summary": "Add"})
                SCRIPT.replies[:] = [tool_reply(*call)] * 8 + [text_reply("Blocked.")]
                with mock.patch.object(agent, "_capture_refusal", return_value="Same look." if refusal == "capture" else ""):
                    turn = self.start("build")
                    turn.thread.join(10)
                self.assertFalse(turn.thread.is_alive())
                self.assertEqual(turn.outcome, "no_progress")
                self.assertEqual(self.ran, [])
                self.assertEqual(len(SCRIPT.requests), 9)

    def limited(self, steps: int, *summary) -> agent.Turn:
        """A turn of `steps` distinct steps against LOOPCUT_MAX_STEPS=steps, then the `summary` replies."""
        from unittest.mock import patch
        schemas = [{"type": "function", "function": {"name": "get_scene_info", "parameters": {}}}]
        with patch.dict(os.environ, {"LOOPCUT_MAX_STEPS": str(steps)}), \
                patch.object(agent, "_tool_schemas", lambda: schemas):
            SCRIPT.replies += [tool_reply("get_scene_info", {"name_contains": f"part{i}"}) for i in range(steps)]
            SCRIPT.replies += list(summary)
            turn = self.start("build it")
            turn.thread.join(5)
        return turn

    def results(self) -> list[str]:
        return [m["content"] for m in self.session["messages"] if m["role"] == "tool"]

    def test_three_steps_before_the_limit_the_model_is_told(self):
        turn = self.limited(5, text_reply("Summary."))
        self.assertEqual(turn.outcome, "step_limit")
        self.assertEqual(len(self.ran), 5)
        self.assertEqual(len(SCRIPT.requests), 6)
        self.assertEqual(self.session["items"][-1]["text"], "Summary.")
        results = self.results()
        self.assertEqual(results[:1] + results[2:], ["cube added"] * 4)
        self.assertEqual(results[1], "cube added\n\n(3 steps left in this turn. Finish what matters most, or wrap up: "
                                     "say what is done, what is left and what you did not verify.)")
        cards = [i for i in self.session["items"] if i["kind"] == "tool"]
        self.assertIn("3 steps left", cards[1]["output"])

    def test_at_the_limit_one_more_request_asks_where_the_work_stands(self):
        from loopcut import context
        prompt = agent.STEP_LIMIT_PROMPT.format(steps=2)
        for keep in ("false", "true"):  # Keep mode too: the summary request extends the last one whole.
            with self.subTest(keep_context=keep):
                self.setUp()
                from unittest.mock import patch
                with patch.dict(os.environ, {"LOOPCUT_KEEP_CONTEXT": keep}):
                    turn = self.limited(2, text_reply("Done: the table. Left: chairs. Not verified: scale."))
                self.assertEqual(len(SCRIPT.requests), 3)
                last, before = SCRIPT.requests[2], SCRIPT.requests[1]
                self.assertNotIn("tool_choice", last, "GLM and MiMo ignore it, Muse Spark refuses it")
                self.assertEqual(last["tools"], before["tools"], "the same tools: the cached prefix holds")
                self.assertEqual(last["messages"][:len(before["messages"])], before["messages"], "append-only")
                self.assertEqual(last["messages"][len(before["messages"]):][0]["role"], "assistant")
                self.assertEqual(last["messages"][-1], {"role": "user", "content": prompt})
                self.assertEqual(self.session["items"][-2], state.item_error("Stopped after 2 steps without finishing."))
                self.assertEqual(self.session["items"][-1]["kind"], "assistant")
                self.assertEqual(self.session["items"][-1]["text"], "Done: the table. Left: chairs. Not verified: scale.")
                asked, answer = self.session["messages"][-2:]
                self.assertEqual(asked["content"], prompt)
                self.assertFalse(context.is_turn_start(asked), "it continues the turn")
                self.assertEqual(answer, {"role": "assistant", "content": "Done: the table. Left: chairs. Not verified: scale."})
                self.assertEqual(turn.outcome, "step_limit")

    def test_a_tool_call_in_the_summary_is_dropped(self):
        summary = sse({"choices": [{"delta": {"content": "Done: the table."}}]},
                      {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_9", "type": "function",
                                                              "function": {"name": "get_scene_info", "arguments": "{}"}}]}}]},
                      {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        self.limited(1, summary)
        self.assertEqual(len(self.ran), 1)
        self.assertEqual(self.session["messages"][-1], {"role": "assistant", "content": "Done: the table."})

    def assert_bare_stop(self, turn: agent.Turn, requests: int):
        self.assertEqual(len(SCRIPT.requests), requests, "no retry")
        self.assertEqual(self.session["items"][-1], state.item_error("Stopped after 1 steps without finishing."))
        self.assertFalse(any(i["kind"] == "assistant" and not i["text"] for i in self.session["items"]))
        self.assertEqual(self.session["messages"][-1]["role"], "tool", "the unanswered request is not kept")
        self.assertEqual(turn.outcome, "step_limit")

    def test_when_the_summary_fails_the_bare_stop_stands(self):
        self.assert_bare_stop(self.limited(1, (400, {"error": {"message": "Bad request."}})), 2)

    def test_a_summary_with_only_tool_calls_is_a_bare_stop(self):
        self.assert_bare_stop(self.limited(1, tool_reply("get_scene_info", {})), 2)
        self.assertEqual(len(self.ran), 1)

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

    def test_main_thread_tasks_carry_the_tool_name_for_the_freeze_log(self):
        from concurrent.futures import Future
        from unittest.mock import patch
        from loopcut import mainthread
        queued = []
        future = Future()
        future.set_result("ok")

        def enqueue(fn):
            queued.append(fn)
            return future

        with patch.object(mainthread, "run_on_main", enqueue):
            agent._run_tool_on_main("capture_viewport", "{}")
            agent._ensure_checkpoint_on_main({})
        self.assertEqual([mainthread.task_name(fn) for fn in queued],
                         ["the capture_viewport tool", "the turn's checkpoint save"])

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

    def test_cost_guard_switches_are_panel_notices_right_after_their_step(self):
        from unittest.mock import patch
        line = "Subsurf on Chair_Seat, est. 12M faces: viewport display off; lower the viewport level"
        self.run_tool = lambda name, arguments: types.SimpleNamespace(
            text=f"OK\n\nCost guard: switched off show_viewport ...\n- {line}", ok=True, image_path=None,
            guard_notes=[{"text": line, "modifiers": [["Chair_Seat", "Subsurf"]]}])
        with patch.dict(os.environ, {"LOOPCUT_AUTO_LOOK": "false", "LOOPCUT_AUTO_RUN": "true"}):
            SCRIPT.replies += [tool_reply("run_python", {"code": "seat()", "summary": "Seat"}), text_reply("Done.")]
            turn = self.start("chair")
            turn.thread.join(5)
        items = self.session["items"]
        card = next(n for n, i in enumerate(items) if i["kind"] == "tool")
        self.assertEqual(items[card + 1], state.item_guard(line, [["Chair_Seat", "Subsurf"]]))
        self.assertEqual(items[card + 1]["action_label"], "Show anyway")
        self.assertIn(line, next(m for m in self.session["messages"] if m["role"] == "tool")["content"])

    def test_a_refused_render_keeps_its_guard_notice_and_gets_the_step_budget_s_note(self):
        from unittest.mock import patch
        refusal = ("Render refused by the cost guard: at render settings this is over its limit of 4M faces per "
                   "object and 12M per scene, and one long render cannot be stopped. Nothing was changed.\n"
                   "- Subsurf on Seat renders at est. 16M faces; lower its render_levels\nLower these, then render again.")

        def run_tool(name, arguments):
            self.ran.append((name, arguments))
            return types.SimpleNamespace(ok=False, image_path=None, guard_notes=[{"text": refusal, "modifiers": []}],
                                         text=f"Traceback (most recent call last):\n  File \"<loopcut>\", line 1, "
                                              f"in <module>\nRuntimeError: {refusal}\n\n{refusal}\n\nScene changes: none")
        self.run_tool = run_tool
        self.session["auto_run"] = self.session["auto_heavy"] = True
        with patch.dict(os.environ, {"LOOPCUT_AUTO_LOOK": "false"}):
            SCRIPT.replies += [tool_reply("run_python", {"code": "bpy.ops.render.render()", "summary": f"Render {i}"})
                               for i in (1, 2, 3)] + [text_reply("The seat is too heavy to render.")]
            self.start("render it").thread.join(5)
        self.assertEqual(len(self.ran), 3, "a refusal is a failure, annotated, never refused itself")
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertTrue(all(results[n].startswith(f"Traceback") and f"\n\n{refusal}\n\nScene changes" in results[n]
                            for n in range(3)), "the guard's report stays whole")
        self.assertIn("(Same error as before.", results[1])
        self.assertIn("This approach is not working", results[2])
        items = self.session["items"]
        cards = [n for n, i in enumerate(items) if i["kind"] == "tool"]
        self.assertEqual([items[n + 1] for n in cards], [state.item_guard(refusal, [])] * 3, "and in the panel")
        self.assertIn("This approach is not working", items[cards[2]]["output"])

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

    def test_file_preview_preserves_origin(self):
        self.session["auto_run"] = True
        shot = Path(self.data_dir.name) / "preview.png"
        shot.write_bytes(b"\x89PNG preview")

        def run_tool(name, arguments):
            what = shot.name if name == "view_image" else "the scene"
            return tools.ToolResult(text=f"Image attached: {what}.", image_path=shot, file_image=name == "view_image")

        self.run_tool = run_tool
        SCRIPT.replies += [tool_reply("view_image", {"path": str(shot)}),
                           tool_reply("capture_viewport", {}), text_reply("Seen.")]
        turn = self.start("check the image")
        turn.thread.join(5)
        self.assertFalse(turn.thread.is_alive())
        preview = next(m for m in SCRIPT.requests[1]["messages"] if isinstance(m.get("content"), list))
        label = preview["content"][0]["text"]
        self.assertTrue(label.startswith("File preview: preview.png."), label)
        self.assertNotIn("Viewport capture:", label)
        self.assertIn("viewing it does not change the open scene", label)
        self.assertTrue(preview["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        stored = [m for m in self.session["messages"] if m.get(context.CAPTURE)]
        self.assertEqual(len(stored), 2)
        self.assertFalse(context.is_turn_start(stored[0]))
        self.assertEqual(stored[0]["content"][0]["text"], label)
        self.assertTrue(stored[0]["content"][1]["image_url"]["url"].startswith(conversations.IMAGE_SCHEME))

        # Once another look replaces the preview, pruning and next-turn folding retain its origin.
        SCRIPT.replies.append(text_reply("Ready."))
        self.start("continue").thread.join(5)
        for request in SCRIPT.requests[2:]:
            records = "\n".join(m["content"] for m in request["messages"]
                                if m["role"] == "assistant" and isinstance(m.get("content"), str))
            self.assertIn("- looked at file: preview.png.", records)
            self.assertNotIn("- looked: preview.png", records)
            images = [p for m in request["messages"] if isinstance(m.get("content"), list)
                      for p in m["content"] if p["type"] == "image_url"]
            self.assertEqual(len(images), 1)

    def test_viewport_capture_preserves_label(self):
        shot = Path(self.data_dir.name) / "viewport.png"
        shot.write_bytes(b"\x89PNG viewport")
        self.run_tool = lambda name, arguments: tools.ToolResult(text="Image attached: the scene.", image_path=shot)
        SCRIPT.replies += [tool_reply("capture_viewport", {}), text_reply("Seen.")]
        self.start("check the scene").thread.join(5)
        capture = next(m for m in SCRIPT.requests[1]["messages"] if isinstance(m.get("content"), list))
        self.assertEqual(capture["content"][0]["text"], "Viewport capture: the scene (after the step above).")
        self.assertTrue(capture["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_scene_inspection_image_preserves_viewport_label(self):
        self.session["auto_run"] = True
        self.session["auto_heavy"] = True
        shot = Path(self.data_dir.name) / "inspection.png"
        shot.write_bytes(b"\x89PNG inspection")
        token = {"generation": 1}
        # Neither result declares file_image; run_python also has no requested capture.
        self.run_tool = lambda name, arguments: types.SimpleNamespace(
            text="Image attached: scene inspection.", ok=True, image_path=shot, evidence_token=token)
        SCRIPT.replies += [tool_reply("inspect_scene", {}), tool_reply("run_python", {}), text_reply("Seen.")]
        with mock.patch.object(agent, "_inspection_current", return_value=True) as current:
            turn = self.start("check the scene")
            turn.thread.join(5)
        self.assertFalse(turn.thread.is_alive())
        current.assert_has_calls([mock.call(token), mock.call(token)])
        self.assertEqual(len(SCRIPT.requests), 3)
        for request in SCRIPT.requests[1:]:
            capture = next(m for m in request["messages"] if isinstance(m.get("content"), list))
            self.assertEqual(capture["content"][0]["text"], "Viewport capture: scene inspection (after the step above).")

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

    def test_the_automatic_look_frames_what_the_step_changed_not_the_first_objects(self):
        """A dungeon run's automatic looks always framed its first twelve floor tiles, whatever it built."""
        os.environ["LOOPCUT_AUTO_LOOK"] = "true"
        self.addCleanup(os.environ.__setitem__, "LOOPCUT_AUTO_LOOK", "false")
        self.session["auto_run"] = True
        shot = Path(self.data_dir.name) / "viewport.png"
        tile = {"type": "MESH", "visible": True, "location": (0, 0, 0), "materials": ()}
        before = {"too_many": False, "object_count": 20, "materials": {}, "collections": {}, "scene": {}, "mode": "OBJECT",
                  "objects": {f"Floor_{n:02d}": dict(tile) for n in range(20)}}
        after = {**before, "objects": {**before["objects"], "Torch": dict(tile), "Floor_07": {**tile, "location": (0, 0, 1)},
                                       "Torch_Light": {**tile, "type": "LIGHT"}}}

        def run_tool(name, arguments):
            self.ran.append((name, json.loads(arguments)))
            if name == "capture_viewport":
                shot.write_bytes(b"\x89PNG look %d" % len(self.ran))
                return types.SimpleNamespace(text="Image attached.", ok=True, image_path=shot)
            return types.SimpleNamespace(text="OK", ok=True, image_path=None, scene_before=before, scene_after=after)

        SCRIPT.replies += [tool_reply("run_python", {"code": "add()", "summary": "Add"}), text_reply("Done.")]
        turn = agent.Turn()
        self.session["items"].append(state.item_user("build it"))
        self.session["messages"].append({"role": "user", "content": "build it"})
        self.session["busy"], self.session["turn"] = True, turn
        agent._run(self.session, turn, run_tool, self.ensure_checkpoint)
        self.assertEqual(self.ran[-1], ("capture_viewport", {"angle": "three_quarter", "focus": ["Floor_07", "Torch"]}))
        self.assertEqual(turn.touched, [], "the next look frames only what changes after this one")

    def test_the_automatic_look_keeps_the_models_last_focus(self):
        """HIGH chest S15 (eval 20260929): a focused final look, then an edit whose result named nothing it
        touched; the automatic look kept the angle but went through the pack's camera."""
        os.environ["LOOPCUT_AUTO_LOOK"] = "true"
        self.addCleanup(os.environ.__setitem__, "LOOPCUT_AUTO_LOOK", "false")
        self.session["auto_run"] = True
        shot = Path(self.data_dir.name) / "viewport.png"
        empty = {"too_many": False, "objects": {}, "object_count": 0, "materials": {}, "collections": {},
                 "scene": {}, "mode": "OBJECT"}

        def run_tool(name, arguments):
            self.ran.append((name, json.loads(arguments)))
            if name == "capture_viewport":
                shot.write_bytes(b"\x89PNG look %d" % len(self.ran))
                return types.SimpleNamespace(text="Image attached.", ok=True, image_path=shot)
            return types.SimpleNamespace(text="OK", ok=True, image_path=None, scene_before=empty,
                                         scene_after={**empty, "object_count": len(self.ran)})

        SCRIPT.replies += [tool_reply("capture_viewport", {"angle": "final", "focus": ["Chest"]}),
                           tool_reply("run_python", {"code": "edit()", "summary": "Edit"}),
                           tool_reply("capture_viewport", {"angle": "front"}),
                           tool_reply("run_python", {"code": "edit()", "summary": "Edit"}), text_reply("Done.")]
        turn = agent.Turn()
        self.session["items"].append(state.item_user("build it"))
        self.session["messages"].append({"role": "user", "content": "build it"})
        self.session["busy"], self.session["turn"] = True, turn
        agent._run(self.session, turn, run_tool, self.ensure_checkpoint)
        looks = [arguments for name, arguments in self.ran if name == "capture_viewport"]
        self.assertEqual(looks, [{"angle": "final", "focus": ["Chest"]}, {"angle": "final", "focus": ["Chest"]},
                                 {"angle": "front"}, {"angle": "front"}], "a look without focus clears it")

    def test_a_specs_after_tool_note_lands_on_the_result(self):
        """LoopSpec.after_tool: the orchestrator's note on a worker's object (P2), on that call's result."""
        import dataclasses
        self.session["auto_run"] = True
        seen = []

        def run_tool(name, arguments):
            return types.SimpleNamespace(text="OK", ok=True, image_path=None)

        def after_tool(session, name, result):
            seen.append(name)
            return "\n\nBarrel was built by worker barrel: send fixes back with delegate continues."

        SCRIPT.replies += [tool_reply("run_python", {"code": "edit()", "summary": "Edit"}), text_reply("Done.")]
        turn = agent.Turn()
        turn.spec = dataclasses.replace(agent.SINGLE_SPEC, after_tool=after_tool)
        self.session["items"].append(state.item_user("fix the barrel"))
        self.session["messages"].append({"role": "user", "content": "fix the barrel"})
        self.session["busy"], self.session["turn"] = True, turn
        agent._run(self.session, turn, run_tool, self.ensure_checkpoint)
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertEqual(seen, ["run_python"])
        self.assertTrue(results[0].endswith("Barrel was built by worker barrel: send fixes back with delegate continues."),
                        results[0])

    def test_a_look_the_model_placed_after_its_change_is_not_doubled(self):
        """A reply of run_python then capture_viewport once got an automatic look between the two:
        two images of the same scene in every lighting step."""
        self._run_batch(("run_python", {"code": "add()", "summary": "Add"}), ("capture_viewport", {"angle": "camera"}))
        self.assertEqual(self.ran, ["run_python", "capture_viewport"])

    def test_the_same_look_after_a_scene_change_in_the_same_reply_runs_again(self):
        """product_reveal: two capture_viewport calls in one reply with a run_python between them that set
        another frame; the second look was dropped as a duplicate of the first."""
        look = ("capture_viewport", {"angle": "camera"})
        self._run_batch(look, ("run_python", {"code": "bpy.context.scene.frame_set(90)", "summary": "Frame 90"}), look)
        self.assertEqual(self.ran, ["capture_viewport", "run_python", "capture_viewport"])
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertFalse([r for r in results if r.startswith("Not run")], results)

    def test_the_same_call_twice_with_nothing_between_still_runs_once(self):
        look = ("capture_viewport", {"angle": "camera"})
        self._run_batch(look, ("get_scene_info", {}), look)
        self.assertEqual(self.ran, ["capture_viewport", "get_scene_info"], "a read between changes nothing")
        self.setUp()
        add = ("run_python", {"code": "add()", "summary": "Add"})
        self._run_batch(add, add, look)
        self.assertEqual(self.ran, ["run_python", "capture_viewport"], "a doubled change runs once")
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertEqual(results[1], "Not run: duplicate of call #1 in this step; see its result.")

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

    def test_an_automatic_look_is_compared_with_the_last_one_as_both_are_stored(self):
        # The last look is stored compact (image_budget): beside the raw PNG, JPEG's noise alone would differ.
        import fake_imbuf
        patcher = mock.patch.dict(sys.modules, {"imbuf": fake_imbuf})
        patcher.start()
        self.addCleanup(patcher.stop)
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
                shot.write_bytes(fake_imbuf.make(640, 360 + len(self.ran)))
                return types.SimpleNamespace(text="Image attached: three_quarter view of Cube.", ok=True, image_path=shot)
            return types.SimpleNamespace(text="OK\n\nScene changes: ~Cube", ok=True, image_path=None,
                                         scene_before=empty, scene_after={**empty, "object_count": len(self.ran)})

        def alike(previous, current):
            compared.append((previous.read_bytes(), current.read_bytes()))
            return False

        step = lambda: tool_reply("run_python", {"code": "tweak()", "summary": "Tweak"})
        SCRIPT.replies += [step(), step(), text_reply("Done.")]
        turn = agent.Turn()
        self.session["items"].append(state.item_user("tweak it"))
        self.session["messages"].append({"role": "user", "content": "tweak it"})
        self.session["busy"], self.session["turn"] = True, turn
        agent._run(self.session, turn, run_tool, self.ensure_checkpoint, alike)
        self.assertEqual(len(compared), 1)
        self.assertEqual([conversations.media_type(data) for data in compared[0]], ["image/jpeg"] * 2)
        refs = [p["image_url"]["url"] for m in self.session["messages"] if context.is_capture(m)
                for p in m["content"] if p.get("type") == "image_url"]
        self.assertEqual(conversations.image_path(self.session["id"], refs[-1]).read_bytes(), compared[0][1],
                         "the look compared is the look stored")

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

    def failing_run(self, errors: list[str]):
        """run_tool that fails the way run_python does, with the next of `errors`."""
        def run_tool(name, arguments):
            self.ran.append((name, arguments))
            error = errors[len(self.ran) - 1]
            return types.SimpleNamespace(ok=False, image_path=None, text=(
                f"Traceback (most recent call last):\n  File \"<loopcut>\", line {len(self.ran)}, in <module>\n"
                f"{error}\n\nScene changes: none"))
        self.run_tool = run_tool

    def test_the_same_call_twice_in_one_reply_runs_once(self):
        # An eval saw 8 of these a step, for 11 steps.
        search = ("search_assets", {"source": "polyhaven", "query": "wood table", "type": "model"})
        SCRIPT.replies += [multi_tool_reply(*[search] * 8, ("inspect_api", {"path": "bpy.types.Object"})),
                           text_reply("Found one.")]
        self.start("find a table").thread.join(5)
        self.assertEqual([name for name, _ in self.ran], ["search_assets", "inspect_api"])
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertEqual(results[0], "cube added")
        self.assertEqual(results[1:8], ["Not run: duplicate of call #1 in this step; see its result."] * 7)
        cards = [i for i in self.session["items"] if i["kind"] == "tool"]
        self.assertEqual([c["status"] for c in cards], ["done"] + ["rejected"] * 7 + ["done"], "not run, not failed")

    def test_the_same_read_is_nudged_then_not_run_until_the_scene_changes(self):
        self.session["auto_run"] = True
        info = lambda: tool_reply("get_object_info", {"names": ["Cube"]})
        SCRIPT.replies += [info(), info(), info(), tool_reply("run_python", {"code": "move()", "summary": "Move"}),
                           info(), text_reply("Done.")]
        self.start("check the cube").thread.join(5)
        self.assertEqual([name for name, _ in self.ran], ["get_object_info"] * 2 + ["run_python", "get_object_info"])
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertEqual(results[0], "cube added")
        self.assertTrue(results[1].startswith("cube added\n\n(Same result as your identical call at step 1"))
        self.assertTrue(results[2].startswith("Not run: you made this same call at steps 1 and 2"))
        self.assertEqual(results[4], "cube added", "the scene changed in between: it runs, and no note")
        self.assertEqual([c["status"] for c in self.session["items"] if c["kind"] == "tool"],
                         ["done", "done", "rejected", "done", "done"])

    def test_an_asset_search_is_a_read_per_source_and_an_import_changes_the_scene(self):
        self.session["auto_run"] = True
        search = lambda source="polyhaven": tool_reply("search_assets", {"source": source, "query": "wood table",
                                                                         "type": "model"})
        imported = tool_reply("import_asset", {"source": "polyhaven", "id": "wooden_table_02"})
        SCRIPT.replies += [search(), search(), search(), search("blenderkit"), imported, search(), text_reply("Done.")]
        from unittest.mock import patch
        from loopcut import tools
        with patch.object(agent, "_changes_scene", lambda name, arguments="": name in tools.CHANGES_SCENE):  # setUpClass stubs it.
            self.start("find a table").thread.join(5)
        ran = [(name, json.loads(arguments).get("source")) for name, arguments in self.ran]
        self.assertEqual(ran, [("search_assets", "polyhaven")] * 2 + [("search_assets", "blenderkit"),
                                                                      ("import_asset", "polyhaven"),
                                                                      ("search_assets", "polyhaven")])
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertIn("(Same result as your identical call at step 1", results[1])
        self.assertTrue(results[2].startswith("Not run: you made this same call at steps 1 and 2"))
        self.assertEqual(results[3], "cube added", "another source is another call")
        self.assertEqual(results[5], "cube added", "the import changed the scene: the search runs, with no note")

    def test_a_render_job_s_status_is_never_refused_or_noted_as_a_repeat(self):
        self.session["auto_heavy"] = True
        status = ("render_job", {"action": "status", "job_id": "j1"})
        start = ("render_job", {"action": "start", "output_dir": "//renders", "frames": [1], "budget_seconds": 60})
        SCRIPT.replies += [multi_tool_reply(start, start, status, status), tool_reply(*status), tool_reply(*status),
                           tool_reply(*status), text_reply("Rendering.")]
        self.start("render it").thread.join(5)
        actions = [json.loads(arguments)["action"] for _, arguments in self.ran]
        self.assertEqual(actions, ["start"] + ["status"] * 5, "status runs every time; a doubled start runs once")
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertEqual(results[1], "Not run: duplicate of call #1 in this step; see its result.")
        self.assertEqual(results[2:], ["cube added"] * 5, "no note, however alike")

    def test_polling_a_running_job_is_pointed_to_wait(self):
        # product_reveal, 2026-09-27: 19 status calls, each followed by run_python sleeping.
        status = ("render_job", {"action": "status", "job_id": "j1"})
        sleep = ("run_python", {"code": "import time\ntime.sleep(20)", "summary": "Wait for the render"})
        self.run_tool = lambda name, arguments: (self.ran.append((name, arguments)) or types.SimpleNamespace(
            ok=True, image_path=None, text='{"state": "running"}' if name == "render_job" else "Scene changes: none"))
        self.session["auto_run"] = True
        SCRIPT.replies += [multi_tool_reply(status, sleep)] * 3 + [text_reply("It is rendering.")]
        self.start("render it").thread.join(5)
        self.assertEqual(len(self.ran), 6, "never refused")
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertTrue(all("Sleeping in run_python holds Blender's main thread" in r for r in results[1::2]))
        self.assertNotIn("wait=", results[0] + results[2])
        self.assertIn("(Status checked 3 times this turn while the job runs.", results[4])

    def test_a_repeated_failure_is_annotated_never_refused(self):
        self.session["auto_run"] = True
        error = "AttributeError: 'NoneType' object has no attribute 'inputs'"
        self.failing_run([error] * 3)
        SCRIPT.replies += [tool_reply("run_python", {"code": f"try_{i}()", "summary": f"Try {i}"}) for i in (1, 2, 3)]
        SCRIPT.replies.append(text_reply("This is not working; here is why."))
        self.start("scatter spheres").thread.join(5)
        self.assertEqual(len(self.ran), 3, "code always runs")
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertTrue(all(f"{error}\n\nScene changes: none" in r for r in results), "the whole error is kept")
        self.assertTrue(results[0].endswith("Scene changes: none"))
        self.assertTrue(results[1].endswith('\n\n(Same error as before. Already tried: step 1 "Try 1".)'))
        self.assertIn('(This error has now come up 3 times. Already tried: step 1 "Try 1", step 2 "Try 2". '
                      'This approach is not working', results[2])
        self.assertIn("not working", SCRIPT.requests[3]["messages"][-1]["content"], "the model sees it")
        self.assertEqual([c["status"] for c in self.session["items"] if c["kind"] == "tool"], ["failed"] * 3)

    def test_failing_runs_in_a_row_get_a_nudge_whatever_the_errors(self):
        self.session["auto_run"] = True
        self.failing_run(["NameError: name 'plane' is not defined", "KeyError: 'Scatter'",
                          "TypeError: enum \"POINTS\" not found", "ValueError: count must be positive"])
        SCRIPT.replies += [tool_reply("run_python", {"code": f"step_{i}()", "summary": f"Step {i}"}) for i in range(4)]
        SCRIPT.replies.append(text_reply("Stepping back."))
        self.start("scatter spheres").thread.join(5)
        results = [m["content"] for m in self.session["messages"] if m["role"] == "tool"]
        self.assertFalse(any("in a row" in r for r in results[:3]))
        self.assertIn("(4 run_python calls in a row have failed. Step back before the next one", results[3])

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

    def test_the_workspace_agent_gets_attached_images_without_a_reference_card(self):
        from loopcut import conversations, context
        picture = Path(self.data_dir.name) / "chest.png"
        picture.write_bytes(b"\x89PNG chest still")
        session = self.session
        session["attachments"] = [{"ref": conversations.store_image(session, picture),
                                   "full": conversations.store_image(session, picture), "name": "chest.png"}]
        SCRIPT.replies.append(text_reply("Looking at the still."))
        agent._scene_context = lambda text, since=None: "<scene_context>\nfile: unsaved\n</scene_context>"
        with mock.patch.dict(os.environ, {"LOOPCUT_ORCHESTRATE": "false", "LOOPCUT_WORKSPACE": "true"}), \
                mock.patch.object(agent, "_workspace_turn"):
            self.assertTrue(agent.send("paint it like this"))
            session["turn"].thread.join(5)
        self.assertEqual(len(SCRIPT.requests), 1, "no card request first")
        self.assertTrue(SCRIPT.requests[0]["messages"][0]["content"].startswith(workspace.PROMPT))
        sent = SCRIPT.requests[0]["messages"][1]["content"]
        self.assertEqual(sent[1]["type"], "image_url")
        self.assertNotIn("reference_card", sent[0]["text"])
        self.assertFalse(session["messages"][0].get(context.REFERENCE_CARD))

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
        calls = [{"index": i, "id": f"call_{i}", "type": "function",  # Two angles: one angle twice runs once.
                  "function": {"name": "capture_viewport", "arguments": json.dumps({"angle": angle})}}
                 for i, angle in enumerate(("front", "side"))]
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
                           tool_reply("run_python", {"code": "bpy.ops.object.bake(type='COMBINED')", "summary": "render"}),
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
                           tool_reply("run_python", {"code": "bpy.ops.object.bake(type='COMBINED')", "summary": "render"}),
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

    def test_a_package_install_asks_every_time_even_with_auto_run_and_always_allow(self):
        # C29: in the RC smoke test, auto-run on, the agent pip-installed imageio-ffmpeg without asking.
        os.environ["LOOPCUT_AUTO_RUN"] = "true"
        self.addCleanup(os.environ.update, {"LOOPCUT_AUTO_RUN": "false"})
        self.session["auto_run"] = self.session["auto_heavy"] = True
        pip = "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pip', 'install', 'imageio-ffmpeg'])"
        SCRIPT.replies += [tool_reply("run_python", {"code": "a()", "summary": "a"}),
                           tool_reply("run_python", {"code": pip, "summary": "Install imageio-ffmpeg"}),
                           tool_reply("run_python", {"code": "import ensurepip", "summary": "pip"}),
                           text_reply("Done.")]
        turn = self.start("check the frames")
        awaiting = lambda: [i for i in self.session["items"] if i.get("status") == "awaiting"]
        self.wait_for(awaiting, "the install's card")
        self.assertEqual([json.loads(a)["code"] for _, a in self.ran], ["a()"], "only the ordinary step ran unasked")
        self.assertTrue(awaiting()[0]["install"])
        self.assertTrue(agent.decide(True, always=True), "Enter or Always on an install allows this one only")
        self.wait_for(lambda: awaiting() and awaiting()[0]["summary"] == "pip", "the next install's card")
        agent.decide(False)
        turn.thread.join(5)
        self.assertEqual([json.loads(a)["code"] for _, a in self.ran], ["a()", pip], "the rejected install never ran")
        shown = state.new_session()
        shown["items"] = [dict(next(i for i in self.session["items"] if i.get("summary") == "pip"), status="awaiting")]
        display = layout.build(shown, 520, 800, 1.0, LayoutTest.measure, "m", None,
                               {"selection": [], "context_budget": 24_000, "account": None})
        text = " ".join(prim.get("text", "") for prim in display["prims"])
        self.assertIn("Loopcut asks every time", text)
        self.assertIn("Run", text)
        self.assertNotIn("Always allow", text, "an install has no Always allow")

    def test_a_worker_cannot_install_and_is_told_so(self):
        import dataclasses
        SCRIPT.replies += [tool_reply("run_python", {"code": "!pip3 install trimesh", "summary": "pip"}),
                           text_reply("Done without it.")]
        turn = self.start("go", spec=dataclasses.replace(agent.SINGLE_SPEC, asks=False))
        turn.thread.join(5)
        self.assertEqual(self.ran, [])
        self.assertEqual(SCRIPT.requests[1]["messages"][-1]["content"], agent.INSTALL_REFUSAL)

    def test_file_reads_inside_the_project_run_unasked_and_writes_ask(self):
        project = Path(self.data_dir.name) / "project"
        project.mkdir(exist_ok=True)
        SCRIPT.replies += [tool_reply("read_file", {"path": str(project / "notes.txt")}),
                           tool_reply("see_render", {}),
                           tool_reply("list_files", {"path": str(Path(self.data_dir.name) / "elsewhere")}),
                           tool_reply("write_file", {"path": str(project / "out.txt"), "content": "hi"}),
                           text_reply("Done.")]
        turn = self.start("look around", roots=(project.resolve(),))
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

    def test_a_conversation_resumed_from_disk_keeps_working_and_counting(self):
        """Reopening a file resumes its saved conversation (conversations.load). Its usage must keep
        every key a new session has: the first turn after a reopen used to die on KeyError('cached')
        (S4 beta RC smoke, 2026-10-09)."""
        usage = {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 8,
                                          "prompt_tokens_details": {"cached_tokens": 100}}}
        SCRIPT.replies += [sse({"choices": [{"delta": {"content": "Hi."}}]}, usage)] * 2
        self.start("hi").thread.join(5)
        conversations.save(self.session)
        self.session = state.replace(conversations.load(self.session["id"]))
        self.start("again").thread.join(5)
        self.assertEqual([i["text"] for i in self.session["items"] if i["kind"] == "error"], [])
        self.assertEqual(set(self.session["usage"]), set(state.new_session()["usage"]))
        self.assertEqual([i["kind"] for i in self.session["items"]], ["user", "assistant", "user", "assistant"])
        self.assertEqual(self.session["usage"], {"input": 240, "cached": 200, "output": 16, "context": 120})

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

    def _short_timeout(self):
        """Patches llm.stream_chat so this test's Hang() replies (which stall past a short
        client-side timeout=, not an HTTP error) read as TimeoutError quickly; agent.py itself
        always asks for the 120s default. Same RETRY_DELAYS trick as the 503 tests above."""
        llm.RETRY_DELAYS, saved = (0.01, 0.01, 0.01), llm.RETRY_DELAYS
        self.addCleanup(setattr, llm, "RETRY_DELAYS", saved)
        real = llm.stream_chat
        patcher = mock.patch.object(llm, "stream_chat", side_effect=lambda **kw: real(**{**kw, "timeout": 0.05}))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_read_timeout_before_any_reply_text_is_retried_and_the_user_never_sees_it(self):
        """The z.ai hiccup this guards against: a whole task ending on its first call with
        TimeoutError('The read operation timed out') and no LLMError to catch it (agent.py's
        'Internal error' fallback). Now it is retried like a 503, invisibly."""
        self._short_timeout()
        SCRIPT.replies += [Hang(delay=0.2), text_reply("Here now.")]
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1]["text"], "Here now.")
        self.assertEqual(len(SCRIPT.requests), 2)
        self.assertNotEqual(self.session["items"][-1]["kind"], "error")

    def test_a_raw_tls_error_reads_as_a_dropped_connection_with_try_again(self):
        """Persona round 1 (2026-10-16): 'Internal error: SSLError(1, ...BAD_RECORD_MAC...)' ended turns. A broken
        connection that reaches the loop unwrapped reads in plain words, and the card offers Try again."""
        import ssl
        failure = ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac")
        with mock.patch.object(llm, "stream_chat", side_effect=failure):
            self.start("hi").thread.join(5)
        item = self.session["items"][-1]
        self.assertEqual(item["kind"], "error")
        self.assertNotIn("Internal error", item["text"])
        self.assertIn("Try again", item["text"])
        self.assertTrue(item.get("retry"))

    def test_a_read_timeout_mid_reply_retries_once_without_doubling_the_card(self):
        """Content already reached the reply card (reply["text"]) before the stream broke; the
        retried request starts from scratch, so agent.py's on_retry must have cleared the stale
        partial text first, or the card would read 'Once upon a Fresh answer.'."""
        self._short_timeout()
        partial = b'data: {"choices":[{"delta":{"content":"Once upon a "}}]}\n\n'
        SCRIPT.replies += [Hang(prefix=partial, delay=0.2), text_reply("Fresh answer.")]
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1]["text"], "Fresh answer.")
        self.assertEqual(len(SCRIPT.requests), 2)

    def test_a_read_timeout_mid_reply_twice_surfaces_as_an_error(self):
        """Only one retry is spent once content has streamed; a second break in a row is shown,
        not silently retried again."""
        self._short_timeout()
        partial = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
        SCRIPT.replies += [Hang(prefix=partial, delay=0.2), Hang(prefix=partial, delay=0.2)]
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1]["kind"], "error")
        self.assertIn("Connection lost while streaming the reply", self.session["items"][-1]["text"])
        self.assertEqual(len(SCRIPT.requests), 2)

    # ------------------------------------------------------------------ the output cap (max_tokens)

    def test_the_configured_output_cap_is_sent_as_max_tokens(self):
        SCRIPT.replies.append(text_reply("hi"))
        self.start("hi").thread.join(5)
        self.assertEqual(SCRIPT.requests[0]["max_tokens"], 32000, "config.py's default")

    def capped_reply(self, usage: dict | None = None) -> bytes:
        chunks = [{"choices": [{"delta": {}, "finish_reason": "length"}]}]
        if usage is not None:
            chunks.append({"choices": [], "usage": usage})
        return sse(*chunks)

    def test_a_capped_reply_with_nothing_usable_is_retried_once(self):
        """The z.ai/GLM failure this guards against: a step burns its whole output cap on reasoning
        and stops with no text and no tool call. Retried once, invisibly, with a short nudge the
        first request did not carry."""
        SCRIPT.replies += [self.capped_reply(), text_reply("Here now.")]
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1]["text"], "Here now.")
        self.assertNotEqual(self.session["items"][-1]["kind"], "error")
        self.assertEqual(len(SCRIPT.requests), 2)
        first, second = SCRIPT.requests[0]["messages"], SCRIPT.requests[1]["messages"]
        self.assertEqual(second[:-1], first, "the retry sends the same messages, plus one more")
        self.assertEqual(second[-1]["role"], "user")
        self.assertIn("Decide now and act", second[-1]["content"])
        # The nudge is not saved: the conversation itself only gained the user turn and its reply.
        self.assertEqual([m["role"] for m in self.session["messages"]], ["user", "assistant"])

    def test_a_capped_tool_call_cut_mid_argument_is_also_retried(self):
        """finish_reason "length" with a tool call whose JSON never closed: not usable either."""
        cut = sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                          "function": {"name": "run_python",
                                                                       "arguments": '{"code": "incomple'}}]}}]},
                  {"choices": [{"delta": {}, "finish_reason": "length"}]})
        SCRIPT.replies += [cut, text_reply("Done.")]
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1]["text"], "Done.")
        self.assertEqual(len(SCRIPT.requests), 2)

    def test_a_capped_reply_with_a_complete_tool_call_is_not_retried(self):
        """finish_reason "length" right after a whole, valid tool call is a usable step, not a
        failure: nothing to retry."""
        whole = sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                            "function": {"name": "run_python",
                                                                         "arguments": '{"code": "x()"}'}}]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "length"}]})
        SCRIPT.replies += [whole, text_reply("Done.")]
        self.start("hi")
        self.awaiting()
        agent.decide(True)
        self.session["turn"].thread.join(5)
        self.assertEqual(len(SCRIPT.requests), 2, "the second request is the next step, not a cap retry")
        self.assertEqual(self.ran, [("run_python", '{"code": "x()"}')])

    def test_a_capped_reply_twice_in_a_row_is_this_steps_error(self):
        SCRIPT.replies += [self.capped_reply(), self.capped_reply()]
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1]["kind"], "error")
        self.assertIn("output cap", self.session["items"][-1]["text"])
        self.assertIn("twice in a row", self.session["items"][-1]["text"])
        self.assertEqual(len(SCRIPT.requests), 2, "no third attempt")

    def empty_reply(self, reasoning: str = "", finish: str = "stop", output_tokens: int = 4) -> bytes:
        """What user D's first turn on Loopcut Fast got on 2026-10-20: a stop with no text and no tool call, maybe
        after a little hidden reasoning (z.ai's delta.reasoning_content)."""
        chunks = [{"choices": [{"delta": {"reasoning_content": reasoning}}]}] if reasoning else []
        chunks += [{"choices": [{"delta": {"content": ""}, "finish_reason": finish}]},
                   {"choices": [], "usage": {"prompt_tokens": 12853, "completion_tokens": output_tokens}}]
        return sse(*chunks)

    def test_an_empty_first_reply_is_retried_once_as_it_was(self):
        SCRIPT.replies += [self.empty_reply(), text_reply("Here it is.")]
        turn = self.start("make a donut")
        turn.thread.join(5)
        self.assertEqual(self.session["items"][-1]["text"], "Here it is.")
        self.assertEqual(len(SCRIPT.requests), 2)
        self.assertEqual(SCRIPT.requests[1]["messages"], SCRIPT.requests[0]["messages"], "the same request again")
        self.assertEqual([m["role"] for m in self.session["messages"]], ["user", "assistant"])
        self.assertEqual(turn.outcome, "completed")

    def test_a_reasoning_only_reply_twice_is_an_error_with_try_again(self):
        """Before this, the panel removed the empty reply card and ended the turn: the user saw nothing at all."""
        SCRIPT.replies += [self.empty_reply("The user"), self.empty_reply("Build a")]
        turn = self.start("make a donut")
        turn.thread.join(5)
        last = self.session["items"][-1]
        self.assertEqual(last["kind"], "error")
        self.assertEqual(last["text"], agent.EMPTY_REPLY_MESSAGE)
        self.assertTrue(last.get("retry"), "the card offers Try again")
        self.assertFalse([i for i in self.session["items"] if i["kind"] == "assistant"], "no blank reply card")
        self.assertEqual(len(SCRIPT.requests), 2, "one retry, no third attempt")
        self.assertEqual((turn.outcome, turn.error_code), ("error", llm.EMPTY_REPLY))
        self.assertEqual(self.session["messages"][-1]["role"], "user", "Try again (agent.resume) can run")

    def test_a_cut_off_first_reply_is_retried_and_replaced(self):
        cut = sse({"choices": [{"delta": {"content": "I'll"}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 12853, "completion_tokens": 4}})
        SCRIPT.replies += [cut, text_reply("Here it is.")]
        self.start("make a donut").thread.join(5)
        replies = [i["text"] for i in self.session["items"] if i["kind"] == "assistant"]
        self.assertEqual(replies, ["Here it is."], "the fragment is replaced, not kept beside the retry")
        self.assertEqual(len(SCRIPT.requests), 2)

    def test_a_short_complete_answer_is_not_a_fragment(self):
        short = sse({"choices": [{"delta": {"content": "Hi!"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 2}})
        SCRIPT.replies.append(short)
        self.start("hi").thread.join(5)
        self.assertEqual(len(SCRIPT.requests), 1)

    def test_a_content_filter_stop_says_so_without_a_retry(self):
        """z.ai ends a filtered stream with finish_reason "sensitive", no text and no usage."""
        SCRIPT.replies += [sse({"choices": [{"delta": {"reasoning_content": "Build a waving flag"}}]},
                               {"choices": [{"delta": {"content": ""}, "finish_reason": "sensitive"}]})]
        turn = self.start("make a flag")
        turn.thread.join(5)
        self.assertEqual(self.session["items"][-1]["text"], agent.FILTERED_REPLY_MESSAGE)
        self.assertEqual(len(SCRIPT.requests), 1)
        self.assertEqual(turn.error_code, llm.CONTENT_FILTER)

    def test_an_empty_reply_after_work_ends_the_turn_as_before(self):
        """After a step that ran tools, an empty last word is retried once for a summary; if it stays empty the
        turn still ends normally, since the work itself is on screen."""
        SCRIPT.replies += [tool_reply("run_python", {"code": "x()"}), self.empty_reply(), self.empty_reply()]
        self.start("hi")
        self.awaiting()
        agent.decide(True)
        turn = self.session["turn"]
        turn.thread.join(5)
        self.assertEqual(turn.outcome, "completed")
        self.assertEqual(len(SCRIPT.requests), 3)
        self.assertFalse([i for i in self.session["items"] if i["kind"] == "error"])

    def test_max_effort_retries_the_capped_step_at_high(self):
        with mock.patch.dict(os.environ, {"LOOPCUT_REASONING_EFFORT": "max"}):
            SCRIPT.replies += [self.capped_reply(), text_reply("ok")]
            self.start("hi").thread.join(5)
        self.assertEqual(SCRIPT.requests[0]["reasoning_effort"], "max")
        self.assertEqual(SCRIPT.requests[1]["reasoning_effort"], "high")

    def test_a_non_max_effort_keeps_its_own_effort_on_retry(self):
        with mock.patch.dict(os.environ, {"LOOPCUT_REASONING_EFFORT": "low"}):
            SCRIPT.replies += [self.capped_reply(), text_reply("ok")]
            self.start("hi").thread.join(5)
        self.assertEqual(SCRIPT.requests[0]["reasoning_effort"], "low")
        self.assertEqual(SCRIPT.requests[1]["reasoning_effort"], "low")

    def test_capped_retry_usage_is_counted_for_both_attempts(self):
        first_usage = {"prompt_tokens": 100, "completion_tokens": 32000, "prompt_tokens_details": {"cached_tokens": 0}}
        second_usage = {"prompt_tokens": 140, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 0}}
        SCRIPT.replies += [self.capped_reply(first_usage), sse({"choices": [{"delta": {"content": "Here."}}]},
                                                               {"choices": [{"delta": {}, "finish_reason": "stop"}],
                                                                "usage": second_usage})]
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["usage"], {"input": 240, "cached": 0, "output": 32005, "context": 140})
        self.assertEqual(self.session["usage_by_role"]["single"],
                         {"input": 240, "cached": 0, "output": 32005, "requests": 2})

    @mock.patch.object(agent.config, "billing_open", lambda: True)
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
        # The card's buttons are on the newest card only, and Upgrade comes first. A ChatGPT plan can carry
        # on where Loopcut's allowance stopped (M7): sign in to it, or switch to it when already signed in.
        display = layout.build(self.session, 400, 600, 1.0, LayoutTest.measure, "m")
        buttons = [h for h in display["hits"] if h["id"].startswith("item") and ".limit." in h["id"]]
        self.assertEqual([h["action"][0] for h in buttons], ["limit_upgrade", "sign_in", "own_key", "limit_retry"])
        self.assertEqual(buttons[1]["action"], ("sign_in", "chatgpt"))
        self.assertIn("Have ChatGPT Plus or Pro? It can run Loopcut now.", str(display["prims"]))
        display = layout.build(self.session, 400, 600, 1.0, LayoutTest.measure, "m", ui={"chatgpt_plan": True})
        buttons = [h for h in display["hits"] if h["id"].startswith("item") and ".limit." in h["id"]]
        self.assertEqual([h["action"][0] for h in buttons], ["limit_upgrade", "limit_chatgpt", "own_key", "limit_retry"])
        # A second 402 from a provider that says nothing structured still gets a card, without a button.
        SCRIPT.replies += [(402, {"error": {"message": "Insufficient credit.", "type": "billing"}})]
        turn = self.start("try again")
        turn.thread.join(5)
        card = self.session["items"][-1]
        self.assertEqual((card["kind"], card["reason"], card["upgrade"]), ("limit", "billing", None))

    def test_hosted_refusal_refreshes_meter_and_still_sends_to_the_server(self):
        reset = "2099-10-21T18:00:00Z"
        SCRIPT.headers.update({"x-loopcut-plan": "free", "x-loopcut-session-used": "1.0",
                               "x-loopcut-session-resets-at": reset})
        SCRIPT.replies += [(402, {"error": {"message": "Chat allowance used.", "code": "session_limit",
                                            "plan": "free", "resets_at": reset}}), text_reply("Allowance is available.")]
        from loopcut import mainthread
        def immediate(fn):
            from concurrent.futures import Future
            future = Future()
            future.set_result(fn())
            return future
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": os.environ["LOOPCUT_BASE_URL"]}), \
                mock.patch.object(mainthread, "run_on_main", side_effect=immediate):
            self.start("make a large model").thread.join(5)
            self.assertEqual(state.ui["account"]["session"], {"used": 1.0, "resets_at": reset})
            self.assertEqual(layout.ring_shares({}, 1000, state.ui["account"])[1][1], 0.0)
            self.assertTrue(agent.resume())
            self.session["turn"].thread.join(5)
            self.assertEqual(len(SCRIPT.requests), 2)
            self.assertNotIn("allowance_notice", state.ui)

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

    def test_a_stopped_chatgpt_plan_is_a_card_offering_loopcut_models_not_an_error(self):
        # M2: the ChatGPT plan's own refusals (by code) become a card that offers Loopcut's models for the same chat.
        SCRIPT.replies += [(403, {"error": {"code": "subscription_sharing_user_not_eligible",
                                            "message": "Not eligible", "type": "invalid_request_error"}})]
        turn = self.start("make a chair")
        turn.thread.join(5)
        card = self.session["items"][-1]
        self.assertEqual((card["kind"], card["nudge"]), ("offer", "chatgpt"), card)
        self.assertIn("continue_loopcut", [a["do"] for a in card["actions"]])
        self.assertFalse(any(i["kind"] == "error" for i in self.session["items"]))
        self.assertFalse(self.session["busy"])

    @mock.patch.object(agent.config, "billing_open", lambda: True)
    def test_usage_headers_update_the_account_and_warn_once_when_the_week_is_nearly_used(self):
        from loopcut import mainthread
        def immediate(fn):
            from concurrent.futures import Future
            future = Future()
            future.set_result(fn())
            return future
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": os.environ["LOOPCUT_BASE_URL"]}), \
                mock.patch.object(mainthread, "run_on_main", side_effect=immediate):
            SCRIPT.headers.update({"x-loopcut-plan": "free", "x-loopcut-session-used": "0.310",
                                   "x-loopcut-week-used": "0.842", "x-loopcut-week-resets-at": "2099-09-23T00:00:00Z"})
            SCRIPT.replies += [text_reply("One."), text_reply("Two.")]
            turn = self.start("hello")
            turn.thread.join(5)
            self.assertEqual(state.ui["account"]["plan"], "free")
            self.assertEqual(state.ui["account"]["week"], {"used": 0.842, "resets_at": "2099-09-23T00:00:00Z"})
            notices = [i for i in self.session["items"] if i["kind"] == "notice"]
            self.assertEqual(len(notices), 1)
            self.assertIn("You've used about 84% of this week's chat allowance.", notices[0]["text"])
            self.assertEqual(notices[0]["url"], "")
            turn = self.start("again")
            turn.thread.join(5)
            self.assertEqual(len([i for i in self.session["items"] if i["kind"] == "notice"]), 1, "warned once")
            self.assertEqual(layout.usage_note(state.ui["account"]), ("Chat 84% used (week)", 0.842))
            self.assertEqual(layout.usage_note({"week": {"used": 0.2}, "session": {"used": 0.5}}), ("", 0.5))

    def test_http_error_is_shown_not_swallowed(self):
        SCRIPT.replies.append((403, {"error": {"message": "This key was revoked."}}))
        self.start("hi").thread.join(5)
        self.assertEqual(self.session["items"][-1],
                         state.item_error("HTTP 403: This key was revoked."))
        self.assertFalse(self.session["busy"])

    # ORCH_SPEC: level inference, the finish gate, usage by role. The wave itself is test_waves'
    # business: here delegate is a fake tool that records what it was handed, as waves.prepare does.
    def orchestrate(self, text: str, job=None) -> agent.Turn:
        patcher = mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": os.environ["LOOPCUT_BASE_URL"]})
        patcher.start()
        self.addCleanup(patcher.stop)
        session = self.session
        session["turn_number"] = session.get("turn_number", 0) + 1
        session["auto_run"] = True  # delegate asks first like any scene change; that is AgentLoopTest's.
        session["items"].append(state.item_user(text))
        session["messages"].append({"role": "user", "content": text})
        turn = agent.Turn()
        turn.spec, self.job = agent.ORCH_SPEC, job

        def run_tool(name, arguments):
            self.ran.append((name, arguments))
            if name == "delegate" and not json.loads(arguments).get("dry_run"):
                wave = json.loads(arguments)["wave"]
                turn.delegations.append([w["deliverable"] for w in wave["workers"]])
                turn.job = self.job
            return types.SimpleNamespace(text="ok", ok=True, image_path=None)
        session["busy"], session["turn"] = True, turn
        turn.thread = threading.Thread(target=agent._run, daemon=True, args=(session, turn, run_tool, self.ensure_checkpoint))
        turn.thread.start()
        turn.thread.join(5)
        return turn

    @staticmethod
    def wave(*deliverables) -> dict:
        return {"wave": {"workers": [{"id": d.lower(), "deliverable": d} for d in deliverables]}}

    def test_orchestrator_levels_come_from_what_the_orchestrator_did(self):
        SCRIPT.replies += [
            text_reply("It has 3 objects."),                                                      # 0
            tool_reply("get_scene_info", {}), text_reply("Two cubes."),                           # 0
            tool_reply("run_python", {"code": "x()"}), text_reply("Moved."),                      # 1
            tool_reply("delegate", self.wave("Chair")), text_reply("Built."),                     # 2
            tool_reply("delegate", {**self.wave("Chair"), "dry_run": True}), text_reply("Fine."),  # 0: a dry run starts nothing
            tool_reply("run_python", {"code": "y()"}), tool_reply("delegate", self.wave("Desk", "Lamp")),
            text_reply("Room done."),                                                              # 1, escalated to 3
            tool_reply("delegate", self.wave("Desk")), tool_reply("delegate", self.wave("Lamp")),
            text_reply("Both done."),                                                              # 2, then 3 in all
        ]
        for text in ("what is here", "describe it", "move it", "a chair", "check a chair", "a room", "desk then lamp"):
            self.orchestrate(text)
        self.assertEqual([(entry["first"], entry["final"]) for entry in self.session["turn_levels"]],
                         [(0, 0), (0, 0), (1, 1), (2, 2), (0, 0), (1, 3), (2, 3)])
        self.assertEqual([entry["turn"] for entry in self.session["turn_levels"]], [1, 2, 3, 4, 5, 6, 7])

    def test_orchestrator_the_finish_gate_bounces_at_most_twice_through_the_loop(self):
        job = FakeJob([{"status": "fail", "detail": "lamp floats 12 mm above"}, {"status": "pass"}])
        SCRIPT.replies += [tool_reply("delegate", self.wave("Lamp")), text_reply("Done."), text_reply("Fixed it."),
                           text_reply("It still floats.")]
        turn = self.orchestrate("a lamp on the desk, keep the mug", job)
        self.assertEqual(turn.outcome, "completed")
        self.assertEqual((len(SCRIPT.requests), job.ran, job.finished), (4, 3, 2))
        bounces = [m for m in self.session["messages"] if m.get(context.STEP_LIMIT)]
        self.assertEqual(len(bounces), 2)
        self.assertIn('- id "lamp c1": contact Lamp rests_on Desk: lamp floats 12 mm above', bounces[0]["content"])
        self.assertEqual(SCRIPT.requests[2]["messages"][-1]["content"], bounces[1]["content"], "sent as the next message")
        final = [i for i in self.session["items"] if i["kind"] == "assistant"][-1]["text"]
        self.assertTrue(final.startswith("It still floats.\n\nVerified by Loopcut on this scene: 1 of 2 checks."), final)
        self.assertEqual(self.session["turn_levels"][-1], {"turn": 1, "first": 2, "final": 2})

    def test_orchestrator_usage_is_kept_by_role_and_still_summed(self):
        usage = {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                          "prompt_tokens_details": {"cached_tokens": 60}}}
        SCRIPT.replies += [sse({"choices": [{"delta": {"content": "Hi."}}]}, usage)]
        self.orchestrate("hi")
        self.assertEqual(self.session["usage_by_role"], {"orchestrator": {"input": 100, "cached": 60, "output": 10,
                                                                          "requests": 1}})
        self.assertEqual(self.session["usage"]["input"], 100)

    def test_orchestrator_send_picks_the_orchestrator_when_workers_are_on(self):
        agent._scene_context = lambda text, since=None: "<scene_context>\nfile: unsaved\n</scene_context>"
        SCRIPT.replies += [text_reply("Hi."), text_reply("Hi.")]
        with mock.patch.dict(os.environ, {"LOOPCUT_ORCHESTRATE": "true", "LOOPCUT_MAX_WORKERS": "2",
                                          "LOOPCUT_SERVICE_URLS": os.environ["LOOPCUT_BASE_URL"]}), \
                mock.patch('loopcut.cloud.prompt_index_available', return_value=True), \
                mock.patch.object(agent, "_launch", wraps=agent._launch) as launched:
            self.assertTrue(agent.send("hi"))
            # A completed worker clears session['turn'] before send() can return.
            turn = launched.call_args.args[1]
            turn.thread.join(5)
            self.assertFalse(turn.thread.is_alive(), "the first turn must finish before the next send")
        self.assertEqual((turn.spec, turn.roles.max_workers), (agent.ORCH_SPEC, 2))
        with mock.patch.object(agent, "_launch", wraps=agent._launch) as launched:
            self.assertTrue(agent.send("hi again"))  # LOOPCUT_ORCHESTRATE=false: today's loop.
            launched.call_args.args[1].thread.join(5)
        orchestrated, single = SCRIPT.requests
        self.assertEqual(orchestrated["_headers"].get("x-loopcut-prompt"), "orchestrator")
        self.assertEqual(orchestrated["messages"][0]["content"], agent._system_tail(self.session))
        self.assertEqual([t["function"]["name"] for t in orchestrated["tools"]], ["delegate", "checks", "accept_quarantine",
                                                                                   "export_glb"])
        self.assertNotIn("## Workers", single["messages"][0]["content"])
        self.assertNotIn("tools", single, "no tools offered (the test stubs them) and none added")
        self.assertEqual([e["turn"] for e in self.session["turn_levels"]], [1, 2])

    def test_a_look_the_router_sends_to_the_workspace_runs_the_workspace_loop_from_then_on(self):
        """LOOPCUT_WORKSPACE with workers on: the router's decision (router.to_workspace) switches the turn to
        WORKSPACE_SPEC before its first request, and the conversation stays there."""
        from loopcut import router
        agent._scene_context = lambda text, since=None: "<scene_context>\nfile: unsaved\n</scene_context>"
        SCRIPT.replies += [text_reply("Painted."), text_reply("Again.")]
        started = []

        def divert(session, turn, cfg, is_cancelled, stream=None, divert=None):
            self.assertIs(divert, router.to_workspace)
            turn.diverted = True
        with mock.patch.dict(os.environ, {"LOOPCUT_ORCHESTRATE": "true", "LOOPCUT_WORKSPACE": "true",
                                          "LOOPCUT_SERVICE_URLS": os.environ["LOOPCUT_BASE_URL"]}), \
                mock.patch.object(router, "start_turn", side_effect=divert) as routed, \
                mock.patch('loopcut.cloud.prompt_index_available', return_value=True), \
                mock.patch.object(agent, "_workspace_turn", side_effect=lambda session, turn: started.append(turn)), \
                mock.patch.object(agent, "_on_main", side_effect=lambda fn, is_cancelled=None, name="": fn()), \
                mock.patch.object(agent, "_launch", wraps=agent._launch) as launched:
            self.assertTrue(agent.send("paint the chest"))
            first = launched.call_args.args[1]
            first.thread.join(5)
            self.assertTrue(agent.send("brighter brass"))
            second = launched.call_args.args[1]
            second.thread.join(5)
        self.assertEqual(routed.call_count, 1, "only the first turn is routed")
        self.assertEqual((first.spec, second.spec), (agent.WORKSPACE_SPEC, agent.WORKSPACE_SPEC))
        self.assertEqual(self.session["mode"], "workspace")
        self.assertEqual(started, [first, second], "the diverted turn is noted by _route, the next by its own on_turn_start")
        self.assertTrue(first.config.keep_context, "diverted, the turn keeps its steps whole")
        self.assertGreaterEqual(first.config.context_budget, context.WORKSPACE_BUDGET)
        for request in SCRIPT.requests:
            self.assertTrue(request["messages"][0]["content"].startswith(workspace.PROMPT))
            self.assertIn("apply_build", [t["function"]["name"] for t in request["tools"]])


class QueuedHelperNoteTest(unittest.TestCase):
    """Exercise the real turn wait and teardown without binding a model server socket."""

    def setUp(self):
        from loopcut import mainthread
        self.session = state.new_session()
        self.turn = agent.Turn()
        self.turn.config = config.Config(api_key="k", base_url=config.SERVICE_BASE_URL, model="fast",
                                         reasoning_effort="low", auto_run=True, auto_look=False,
                                         max_steps=0, context_budget=40_000)
        self.session.update(busy=True, turn=self.turn)
        self.session["messages"].append({"role": "user", "content": "make an object"})
        self.helper = types.SimpleNamespace(id="a", turn=agent.Turn(), thread=None, finished=None, kill=mock.Mock(),
                                            wave=types.SimpleNamespace(id="w1"), brief={"deliverable": "A"})
        self.helper.never_started = lambda: setattr(self.helper, "finished", time.monotonic())
        self.job = types.SimpleNamespace(lock=threading.Lock(), steers=[], stopped=False, crew=[self.helper],
                                         waves=[], log={}, write=mock.Mock(), on_main=None)
        self.turn.job = self.job
        self.entered, self.return_wave = threading.Event(), threading.Event()
        self.requests = []
        self.callbacks = []
        self.resent, self.restored = [], []
        self.resend_on_main = agent._resend_on_main
        self.spec = agent.LoopSpec(role="orchestrator", system=lambda session: "", schemas=lambda: [],
                                   save=lambda session: None, asks=False, checkpoints=False, changes_card=False,
                                   scene_changed=lambda result: False, on_end=waves.end_turn)
        # Keep a private session and callback queue until finish() has stopped the turn, then
        # restore the previous hooks and session even if a race assertion fails.
        for patcher in (mock.patch.object(state, "_session", self.session),
                        mock.patch.object(mainthread, "run_on_main", side_effect=self.callbacks.append),
                        mock.patch.object(agent, "_redraw"),
                        mock.patch.object(agent, "_request", side_effect=self.request),
                        mock.patch.object(agent, "_resend_on_main", side_effect=self.resend),
                        mock.patch.object(agent, "_restore_input_on_main", side_effect=self.restored.append)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.thread = None
        self.addCleanup(self.finish)

    def finish(self):
        self.turn.cancel.set()
        self.return_wave.set()
        if self.thread:
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive(), "turn did not finish during cleanup")

    def request(self, session, turn, cfg, spec):
        self.requests.append([dict(m) for m in session["messages"]])
        if len(self.requests) == 1:
            return llm.Completion(tool_calls=[llm.ToolCall("c1", "delegate", "{}")])
        return llm.Completion(text="done")

    def wait_for_helpers(self, name, arguments):
        self.entered.set()
        waves._wait(self.job, self.turn.cancel.is_set, self.return_wave.is_set)
        return tools.ToolResult("Helpers finished")

    def start(self):
        self.thread = threading.Thread(target=agent.run_loop,
                                       args=(self.session, self.turn, self.spec, self.wait_for_helpers))
        self.thread.start()
        self.assertTrue(self.entered.wait(2), "orchestrator did not reach its helper wait")

    def resend(self, text, note_items):
        self.assertTrue(self.job.stopped)
        self.assertTrue(self.helper.turn.cancel.is_set())
        self.helper.kill.assert_called()
        self.assertFalse(self.session["busy"], "the next turn starts only after cancellation cleanup")
        self.resent.append(text)

    def test_correction_reaches_the_next_orchestrator_request_after_the_wave_returns(self):
        self.start()
        self.assertTrue(agent.send("make it smaller"))
        queued = self.session["items"][-1]
        self.assertEqual(self.job.steers, [("make it smaller", queued)])
        self.assertEqual(queued["helper_deliveries"], [{"wave": "w1", "worker": "a", "deliverable": "A"}])
        with self.job.lock:
            waves._note_delivery(queued, self.helper, 26)
        self.assertTrue(queued["queued"])
        self.assertEqual(len(self.turn.notes), 1, "helper receipt must not consume the main agent's note")
        self.assertEqual(len(self.requests), 1, "the main agent is still waiting for the helper")
        self.return_wave.set()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.turn.outcome, "completed")
        self.assertEqual((self.turn.tool_steps, self.turn.executed_tools, len(self.requests)), (1, 1, 2))
        self.assertFalse(self.turn.unrun_claim, "the helper wave ran before the final 'done' reply")
        self.assertFalse(any(i.get("text") == agent.completion_guard.NOTICE for i in self.session["items"]))
        steers = [m["content"] for m in self.requests[1] if m.get(context.STEER)]
        self.assertEqual(steers, ["<steer>make it smaller</steer>"])
        self.assertNotIn("queued", queued)
        self.assertEqual(queued["helper_deliveries"][0]["step"], 26)
        self.assertEqual(self.resent, [])

    def test_send_now_stops_helpers_then_sends_all_notes_once(self):
        self.start()
        agent.send("smaller")
        agent.send("also red")
        self.assertTrue(agent.send_queued_now())
        self.assertFalse(agent.send_queued_now(), "a second click must not resend the same note")
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.turn.outcome, "cancelled")
        self.assertEqual(self.resent, ["smaller\nalso red"])
        self.assertEqual(self.restored, [])
        self.assertEqual(len(self.requests), 1, "the cancelled turn must not make another model request")
        self.assertFalse(any(i.get("queued") for i in self.session["items"]))
        self.assertEqual(self.session["messages"][-1]["role"], "tool", "cancel closes the delegate call")

    def test_send_now_preserves_original_notes_and_receipts_through_save_and_resend(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": folder}), \
                mock.patch.object(agent, "_resend_on_main", side_effect=self.resend_on_main):
            self.spec = dataclasses.replace(self.spec, save=conversations.save)
            self.start()
            agent.send("smaller")
            agent.send("also red")
            notes = [item for _, item in self.turn.notes]
            waves._note_delivery(notes[0], self.helper, 26)
            receipts = [json.loads(json.dumps(item["helper_deliveries"])) for item in notes]
            self.assertTrue(agent.send_queued_now())
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
            saved = conversations.load(self.session["id"])
            self.assertEqual([i.get("helper_deliveries") for i in saved["items"] if i["kind"] == "user"], receipts)
            with mock.patch.object(agent, "_configure"), \
                    mock.patch.object(agent, "_project_roots", return_value=()), \
                    mock.patch.object(agent, "_scene_context", return_value="scene"), \
                    mock.patch.object(agent, "_scene_snapshot", return_value=None), \
                    mock.patch.object(agent, "_setup_line", return_value=""), \
                    mock.patch.object(agent, "_addons_line", return_value=""), \
                    mock.patch.object(checkpoints, "begin_turn"), \
                    mock.patch.object(agent, "_launch") as launched:
                self.assertEqual(len(self.callbacks), 1)
                self.callbacks.pop()()
                launched.assert_called_once()
            retained = [i for i in self.session["items"] if i["kind"] == "user"]
            self.assertEqual(len(retained), 2)
            for original, item, receipt in zip(notes, retained, receipts):
                self.assertIs(item, original)
                self.assertEqual(item["helper_deliveries"], receipt)
                self.assertNotIn("queued", item)
                self.assertNotIn("sending", item)
            self.assertEqual(self.session["messages"][-1]["content"], "scene\n\nsmaller\nalso red")
            saved = conversations.load(self.session["id"])
            self.assertEqual([i.get("helper_deliveries") for i in saved["items"] if i["kind"] == "user"], receipts)

    def test_ordinary_stop_keeps_returning_the_note_to_the_input(self):
        self.start()
        agent.send("smaller")
        agent.stop()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.turn.outcome, "cancelled")
        self.assertEqual(self.restored, ["smaller"])
        self.assertEqual(self.resent, [])
        self.assertTrue(self.job.stopped)

    def restore_resent_notes(self, texts, *, legacy=False):
        def write_snapshot(filepath):
            Path(filepath).write_bytes(b"scene snapshot")
            return {"FINISHED"}

        blender = types.SimpleNamespace(
            data=types.SimpleNamespace(filepath="/project/scene.blend"),
            app=types.SimpleNamespace(is_job_running=lambda job: False),
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(
                loopcut_snapshot_write=write_snapshot,
                loopcut_snapshot_restore=mock.Mock(return_value={"FINISHED"}))))
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": folder}), \
                mock.patch.dict(sys.modules, {"bpy": blender}), \
                mock.patch.object(config, "checkpoint_budget_mb", return_value=2048), \
                mock.patch.object(agent, "_resend_on_main", side_effect=self.resend_on_main), \
                mock.patch.object(agent, "_configure"), \
                mock.patch.object(agent, "_project_roots", return_value=()), \
                mock.patch.object(agent, "_scene_context", return_value="scene"), \
                mock.patch.object(agent, "_scene_snapshot", return_value=None), \
                mock.patch.object(agent, "_setup_line", return_value=""), \
                mock.patch.object(agent, "_addons_line", return_value=""), \
                mock.patch("loopcut.telemetry.first_use"), \
                mock.patch.object(agent, "_launch") as launched:
            self.session["items"].append(state.item_user("make an object"))
            self.start()
            before_notes = list(self.session["items"])
            for text in texts:
                self.assertTrue(agent.send(text))
            notes = [item for _, item in self.turn.notes]
            waves._note_delivery(notes[0], self.helper, 26)
            receipts = json.loads(json.dumps([item["helper_deliveries"] for item in notes]))
            self.assertTrue(agent.send_queued_now())
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
            before_messages = list(self.session["messages"])
            self.assertEqual(len(self.callbacks), 1)
            self.callbacks.pop()()
            launched.assert_called_once()
            checkpoints.ensure_for_turn(self.session)
            checkpoint_id = notes[0]["checkpoint"]
            store = checkpoints._store(self.session)
            target = store.get(checkpoint_id)
            self.assertEqual(target["item_index"], len(before_notes))
            self.assertEqual(target["message_index"], len(before_messages))
            if legacy:  # Older checkpoints recover their input from the first removed user item.
                target.pop("input")
                store.save_index()
            checkpoints.restore(self.session, checkpoint_id)
            self.assertEqual(self.session["items"][:-1], before_notes)
            self.assertEqual(self.session["messages"], before_messages)
            self.assertEqual(self.session["input"], "\n".join(texts))
            self.assertEqual(self.session["cursor"], len(self.session["input"]))
            self.assertIsNone(self.session["anchor"])
            undo = self.session["items"][-1]["checkpoint"]
            checkpoints.restore(self.session, undo)
            restored_notes = [i for i in self.session["items"] if i.get("helper_deliveries")]
            self.assertEqual([i["text"] for i in restored_notes], texts)
            self.assertEqual([i["helper_deliveries"] for i in restored_notes], receipts)

    def test_send_now_checkpoint_restore_returns_the_note_to_the_input(self):
        self.restore_resent_notes(["smaller"])

    def test_send_now_checkpoint_restore_returns_all_notes_to_the_input(self):
        self.restore_resent_notes(["smaller", "also red"])

    def test_checkpoint_restore_still_reads_older_checkpoints_without_saved_input(self):
        self.restore_resent_notes(["smaller"], legacy=True)

    def test_resend_handoff_queues_original_notes_if_another_turn_started(self):
        agent.send("smaller")
        agent.send("also red")
        notes = [item for _, item in self.turn.notes]
        waves._note_delivery(notes[0], self.helper, 26)
        receipts = json.loads(json.dumps(notes[0]["helper_deliveries"]))
        self.turn.notes.clear()
        next_turn = agent.Turn()
        self.session["turn"] = next_turn
        self.resend_on_main("smaller\nalso red", notes)
        self.callbacks.pop()()
        self.assertEqual(next_turn.notes, [("smaller", notes[0]), ("also red", notes[1])])
        retained = [i for i in self.session["items"] if i["kind"] == "user"]
        self.assertEqual(len(retained), 2)
        for original, item in zip(notes, retained):
            self.assertIs(item, original)
        agent._fold_notes(self.session, next_turn)
        self.assertEqual(notes[0]["helper_deliveries"], receipts)
        self.assertEqual([m["content"] for m in self.session["messages"] if m.get(context.STEER)],
                         ["<steer>smaller</steer>", "<steer>also red</steer>"])

    def test_stale_send_now_action_does_nothing(self):
        self.assertFalse(agent.send_queued_now())
        self.assertFalse(self.turn.cancel.is_set())

    def complete_with_pending_note(self, click_first):
        """A note arrives during the final reply; the real teardown schedules the next turn."""
        def final_reply(*args):
            self.entered.set()
            if not self.return_wave.wait(2):
                raise RuntimeError("final reply was not released")
            return llm.Completion(text="What size should it be?")

        with mock.patch.object(agent, "_request", side_effect=final_reply), \
                mock.patch.object(agent, "_resend_on_main", side_effect=self.resend_on_main) as resent:
            self.start()
            self.assertTrue(agent.send("smaller"))
            note = self.turn.notes[-1][1]
            if click_first:
                self.assertTrue(agent.send_queued_now())
            self.return_wave.set()
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
            self.assertEqual(self.turn.outcome, "cancelled" if click_first else "completed")
            resent.assert_called_once_with("smaller", [note])
            self.assertIsNone(self.session["turn"])
            self.assertFalse(agent.send_queued_now(), "completion already handed the note to automatic resend")
            self.assertEqual(len(self.callbacks), 1)
            with mock.patch.object(agent, "send", return_value=True) as sent:
                self.callbacks[0]()
            sent.assert_called_once_with("smaller", note_items=[note])
        self.assertFalse(any(i.get("queued") for i in self.session["items"]))
        self.assertFalse(self.turn.notes)
        self.assertEqual(self.restored, [])

    def test_completion_before_send_now_click_resends_once(self):
        self.complete_with_pending_note(click_first=False)
        self.assertFalse(self.turn.cancel.is_set())

    def test_send_now_click_before_completion_resends_once(self):
        self.complete_with_pending_note(click_first=True)
        self.assertTrue(self.job.stopped)

    def test_send_captured_before_retirement_retries_normal_sending(self):
        captured, resume = threading.Barrier(2), threading.Barrier(2)
        note_lock = threading.Lock()
        result = []

        class PausedSenderLock:
            def __enter__(self):
                if threading.current_thread() is sender:
                    captured.wait(2)  # send() captured the old turn, but has not validated it.
                    resume.wait(2)
                note_lock.acquire()

            def __exit__(self, *args):
                note_lock.release()

        self.turn.notes_lock = PausedSenderLock()
        sender = threading.Thread(target=lambda: result.append(agent.send("smaller")), daemon=True)
        with mock.patch.object(agent, "_request", return_value=llm.Completion(text="done")), \
                mock.patch.object(agent, "_configure"), \
                mock.patch.object(agent, "_project_roots", return_value=()), \
                mock.patch.object(agent, "_scene_context", return_value="scene"), \
                mock.patch.object(agent, "_scene_snapshot", return_value=None), \
                mock.patch.object(agent, "_setup_line", return_value=""), \
                mock.patch.object(agent, "_addons_line", return_value=""), \
                mock.patch.object(checkpoints, "begin_turn"), \
                mock.patch.object(conversations, "save"), \
                mock.patch.object(agent, "_launch") as launched:
            sender.start()
            try:
                captured.wait(2)
                agent.run_loop(self.session, self.turn, self.spec)
                self.assertIsNone(self.session["turn"])
                self.assertFalse(self.turn.notes)
            finally:
                resume.wait(2)
                sender.join(2)
            self.assertFalse(sender.is_alive())
            self.assertEqual(result, [True])
            launched.assert_called_once()
            self.assertIsNot(launched.call_args.args[1], self.turn)
        self.assertFalse(self.turn.notes, "a retired turn cannot acquire new notes")
        self.assertEqual([i["text"] for i in self.session["items"] if i["kind"] == "user"], ["smaller"])
        self.assertEqual(self.session["messages"][-1]["content"], "scene\n\nsmaller")
        self.assertFalse(any(i.get("queued") for i in self.session["items"]))
        self.assertEqual(self.resent, [])

    def test_send_now_claims_notes_before_a_racing_fold(self):
        agent.send("smaller")
        self.assertTrue(agent.send_queued_now())
        agent._fold_notes(self.session, self.turn)
        self.assertEqual(len(self.turn.notes), 1)
        self.assertFalse(any(m.get(context.STEER) for m in self.session["messages"]))
        agent.run_loop(self.session, self.turn, self.spec)
        self.assertEqual(self.resent, ["smaller"])

    def test_a_folded_note_makes_a_stale_action_harmless(self):
        agent.send("smaller")
        agent._fold_notes(self.session, self.turn)
        self.assertFalse(agent.send_queued_now())
        self.assertFalse(self.turn.cancel.is_set())


class FakeJob:
    """What the finish gate needs of waves.Job: no workers to wait for."""
    crew, on_main = [], None

    def __init__(self, results):
        self.results, self.bounces, self.finished, self.ran = results, 0, None, 0
        self.checker, self.retired = self.check, {}
        self.log = {"level": 0}

    def check(self, checks):
        self.ran += 1
        return self.results[:len(checks)]

    def final_checks(self):
        return [("lamp c1", {"kind": "contact", "a": "Lamp", "relation": "rests_on", "b": "Desk", "id": "c1",
                             "quote": "a lamp on the desk"}),
                ("keep", {"kind": "unchanged", "names": ["Mug"], "id": "k1", "quote": "keep the mug"})]

    def finish(self, bounces, results):
        self.finished = bounces

    def write(self):
        pass


class LimitStateTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        data = mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": folder.name})
        data.start()
        self.addCleanup(data.stop)
        snapshot = mock.patch.dict(state.ui, {"account": None})
        snapshot.start()
        self.addCleanup(snapshot.stop)
        state.ui.pop("allowance_notice", None)
        state.ui.pop("warned_session_allowance", None)
        state.ui.pop("warned_week_allowance", None)
        self.session = state.reset()
        self.cfg = agent.config.Config("test-key", agent.config.SERVICE_BASE_URL, "fast", "low", False, False, 0, 24000)
        from loopcut import mainthread
        def immediate(fn):
            from concurrent.futures import Future
            future = Future()
            future.set_result(fn())
            return future
        for patcher in (mock.patch.object(mainthread, "run_on_main", side_effect=immediate),
                        mock.patch.object(agent, "_update_account", side_effect=lambda info: state.ui.update(account=info)),
                        mock.patch.object(agent, "_redraw")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def configure(self, turn, session):
        turn.config = self.cfg

    def send(self, text, attachments=()):
        session = self.session
        session.update(input=text, attachments=list(attachments))
        with mock.patch.object(agent, "_configure", side_effect=self.configure), \
                mock.patch.object(agent, "_project_roots", return_value=()), \
                mock.patch.object(agent, "_scene_context", return_value="scene"), \
                mock.patch.object(agent, "_scene_snapshot", return_value=None), \
                mock.patch.object(agent, "_setup_line", return_value=""), \
                mock.patch.object(agent, "_addons_line", return_value=""), \
                mock.patch.object(conversations, "save"), mock.patch.object(agent, "_launch") as launch:
            self.assertTrue(agent.send(text))
        turn = launch.call_args.args[1]
        session.update(input="", busy=True, turn=turn)  # host._submit clears the submitted draft.
        return turn

    def refuse(self, turn, reason="session_limit", changes=None):
        info = {"plan": "free", "session": {"used": 0.82, "resets_at": "2099-10-21T18:00:00Z"},
                "week": {"used": 0.52, "resets_at": "2099-10-25T18:00:00Z"}}
        refusal = llm.LLMError("Used up.", 402, reason, {"plan": "free"}, account=info)
        spec = agent.LoopSpec(role="single", system=lambda _: "", schemas=lambda: [], save=mock.Mock(), reference_card=False)
        with mock.patch.object(agent, "_request", side_effect=refusal) as request, \
                mock.patch.object(agent, "_changes_item", return_value=changes):
            agent.run_loop(self.session, turn, spec)
        request.assert_called_once()
        return info

    def test_first_hosted_refusal_restores_exact_draft_and_only_warns_for_other_windows(self):
        for reason in state.HOSTED_LIMITS:
            with self.subTest(reason=reason):
                self.session = state.reset()
                state.clear_allowance_notice()
                state.ui.pop("warned_session_allowance", None)
                old = state.item_assistant()
                old["text"] = "Earlier answer."
                self.session["items"] = [old]
                self.session["messages"] = [{"role": "assistant", "content": old["text"]}]
                attachments = [{"name": "reference.png", "ref": "ref.png", "full": "full.png"},
                               {"name": "clip.mp4", "ref": "sheet.jpg", "video": {"id": "clip", "fps": 24, "in_frame": 1, "out_frame": 48,
                                                                       "size": [1280, 720], "folder": "clip", "name": "clip.mp4",
                                                                       "source": "/tmp/clip.mp4", "frames": 48, "beats_line": ""}}]
                before = (list(self.session["items"]), list(self.session["messages"]), self.session["turn_number"])
                for _ in range(3):
                    turn = self.send("  make more details  ", attachments)
                    self.refuse(turn, reason)
                    self.assertEqual(self.session["input"], "  make more details  ")
                    self.assertEqual(self.session["attachments"], attachments)
                    self.assertEqual(self.session["references"], [])
                    history = [i for i in self.session["items"] if i["kind"] != "notice"]
                    self.assertEqual((history, self.session["messages"], self.session["turn_number"]), before)
                    self.assertEqual(len(self.session["items"]), len(history) + (reason != "session_limit"),
                                     "only an unrefused window adds a percentage warning")
                    self.assertFalse(state.can_resume(self.session))
                    self.assertEqual(state.ui["allowance_notice"]["item"]["reason"], reason)
                self.assertEqual(state.ui["account"]["session"]["used"], 0.82)

    def test_refusal_overrides_only_its_window_until_next_success_with_or_without_headers(self):
        for window in ("session", "week"):
            for success_info in (None, {"plan": "free", "session": {"used": 0.3, "used_with_reservations": 0.5},
                                       "week": {"used": 0.2, "used_with_reservations": 0.4}}):
                with self.subTest(window=window, success_info=success_info):
                    self.session = state.reset()
                    turn = self.send("make a chair")
                    reported = self.refuse(turn, reason=window + "_limit")
                    notice = state.ui["allowance_notice"]
                    self.assertEqual(state.ui["account"], reported, "refusals never overwrite reported usage")
                    self.assertEqual(layout.ring_shares({}, 1, reported, notice)[1][1], 0)
                    self.assertIn(f"{window.capitalize()} chat allowance: 100% used, 0% left", layout.usage_lines({}, 1, reported, notice))
                    other = "Week chat allowance: 52% used, 48% left" if window == "session" else "Session chat allowance: 82% used, 18% left"
                    self.assertIn(other, layout.usage_lines({}, 1, reported, notice))
                    agent._hosted_reply(turn, self.cfg, success_info, success=True)
                    self.assertNotIn("allowance_notice", state.ui)
                    expected = success_info or reported
                    self.assertEqual(layout.usage_lines({}, 1, state.ui["account"], state.ui.get("allowance_notice")),
                                     layout.usage_lines({}, 1, expected))

    def test_later_refusal_keeps_this_turns_heads_up_history_and_other_notices(self):
        for reason in ("session_limit", "week_limit"):
            with self.subTest(reason=reason):
                self.session = state.reset()
                state.ui.pop("warned_session_allowance", None)
                state.ui.pop("warned_week_allowance", None)
                older = state.item_notice("About 18% of this session's chat allowance is left.")
                unrelated = state.item_notice("Scene saved.")
                self.session["items"].append(older)
                turn = self.send("continue")
                info = {"plan": "free", "session": {"used": 0.82, "resets_at": "2099-10-21T18:00:00Z"},
                        "week": {"used": 0.84, "resets_at": "2099-10-25T18:00:00Z"}}
                agent._hosted_reply(turn, self.cfg, info, success=True, session=self.session)
                self.assertEqual(len(turn.allowance_notices), 2)
                self.session["items"].append(unrelated)
                notices = [i for i in self.session["items"] if i["kind"] == "notice"]
                self.refuse(turn, reason)
                self.assertEqual([i for i in self.session["items"] if i["kind"] == "notice"], notices)
                self.assertEqual(self.session["items"][-1]["reason"], reason)

    def test_refusing_the_other_window_keeps_both_overrides_until_success(self):
        turn = self.send("continue")
        turn.hosted_requests = 1
        self.refuse(turn, "session_limit")
        # A resume may reuse the same limit card for the other window.
        retry = agent.Turn()
        retry.config = self.cfg
        info = self.refuse(retry, "week_limit")
        lines = layout.usage_lines({}, 1, info, state.ui["allowance_notice"])
        self.assertIn("Session chat allowance: 100% used, 0% left", lines)
        self.assertIn("Week chat allowance: 100% used, 0% left", lines)
        from datetime import datetime, timezone
        after_session_reset = datetime(2099, 10, 22, tzinfo=timezone.utc).timestamp()
        lines = layout.usage_lines({}, 1, info, state.ui["allowance_notice"], after_session_reset)
        self.assertFalse(any("Session chat allowance" in line for line in lines), "the cached session has expired")
        self.assertIn("Week chat allowance: 100% used, 0% left", lines)
        agent._hosted_reply(retry, self.cfg, success=True)
        self.assertEqual(layout.usage_lines({}, 1, info, state.ui.get("allowance_notice")), layout.usage_lines({}, 1, info))

    def test_worker_refusal_keeps_heads_up_in_the_shared_parent_turn(self):
        parent = self.send("make a model")
        agent._hosted_reply(parent, self.cfg, {"plan": "free", "session": {"used": 0.82}},
                            success=True, session=self.session)
        worker = agent.Turn()
        worker.chat = (self.session, parent)
        agent._hosted_refusal(state.new_session(), worker, state.item_limit("Used up.", "session_limit", "free", "", None))
        self.assertEqual(sum(i["kind"] == "notice" for i in self.session["items"]), 1)

    def test_real_request_path_restores_the_first_refusal_then_clears_notice_on_success(self):
        from prompt_index_fixture import index_patch
        spec = agent.LoopSpec(role="single", system=lambda _: "", schemas=lambda: [], save=mock.Mock(), reference_card=False)
        turn = self.send("  make a chair  ")
        refusal = llm.LLMError("Used up.", 402, "session_limit", {"plan": "free"}, account={"plan": "free"})
        with index_patch(), mock.patch.object(llm, "stream_chat", side_effect=refusal) as stream, \
                mock.patch.object(agent, "_changes_item", return_value=None):
            agent.run_loop(self.session, turn, spec)
            stream.assert_called_once()
        self.assertEqual(self.session["items"], [])
        self.assertEqual(self.session["messages"], [])
        self.assertEqual(self.session["input"], "  make a chair  ")
        turn = self.send(self.session["input"])
        def complete(**kwargs):
            kwargs["on_text"]("What style should it be?")
            return llm.Completion(text="What style should it be?")
        with index_patch(), mock.patch.object(llm, "stream_chat", side_effect=complete) as stream, \
                mock.patch.object(agent, "_changes_item", return_value=None):
            agent.run_loop(self.session, turn, spec)
            stream.assert_called_once()
        self.assertEqual([i["kind"] for i in self.session["items"]], ["user", "assistant"])
        self.assertNotIn("allowance_notice", state.ui)

    def test_reference_request_refusal_restores_the_draft_without_trying_the_main_request(self):
        turn = self.send("copy this", [{"name": "ref.png", "ref": "ref.png"}])
        spec = agent.LoopSpec(role="single", system=lambda _: "", schemas=lambda: [], save=mock.Mock())
        refusal = llm.LLMError("Used up.", 402, "week_limit", {"plan": "free"}, account={"plan": "free"})
        with mock.patch.object(agent, "_reference_card", side_effect=refusal) as reference, \
                mock.patch.object(agent, "_request") as request, mock.patch.object(agent, "_changes_item", return_value=None):
            agent.run_loop(self.session, turn, spec)
        reference.assert_called_once()
        request.assert_not_called()
        self.assertEqual(self.session["input"], "copy this")
        self.assertEqual(self.session["attachments"], [{"name": "ref.png", "ref": "ref.png"}])
        self.assertEqual(self.session["items"], [])

    def test_later_refusal_keeps_limit_and_scene_changes_and_resume_contacts_server(self):
        turn = self.send("make a large model")
        turn.hosted_requests = 1
        changes = state.item_changes("49 objects added", [], "")
        self.refuse(turn, changes=changes)
        self.assertEqual([i["kind"] for i in self.session["items"]], ["user", "limit", "changes"])
        with mock.patch.object(agent, "_configure", side_effect=self.configure), \
                mock.patch.object(agent, "_project_roots", return_value=()), mock.patch.object(agent, "_launch") as launch:
            self.assertTrue(agent.resume())
            launch.assert_called_once()
        # Another refusal before any work reuses the latest card.
        self.refuse(launch.call_args.args[1])
        self.assertEqual(sum(i["kind"] == "limit" for i in self.session["items"]), 1)

    def test_send_records_the_first_attempt_and_an_early_setup_failure(self):
        from loopcut import telemetry
        with mock.patch.object(telemetry, "first_use") as record, \
                mock.patch.object(agent, "_configure", side_effect=config.ConfigError("Model not connected")):
            self.assertFalse(agent.send("make a cabinet"))
        self.assertEqual(record.call_args_list, [mock.call("prompt_sent"), mock.call("result_error")])

    def test_send_records_no_prompt_content_in_telemetry(self):
        from loopcut import telemetry
        with mock.patch.object(telemetry, "first_use") as record:
            self.send("a private prompt")
        record.assert_called_once_with("prompt_sent")

    def test_display_never_blocks_send_with_unknown_reset_or_any_model_or_provider(self):
        for reset in ("", "bad date", "2099-10-21T18:00:00Z"):
            for route, model in ((self.cfg.base_url, "fast"), (self.cfg.base_url, "pro"),
                                 (agent.config.CHATGPT_PLAN_BASE_URL, "gpt"), ("https://own.test/v1", "own")):
                with self.subTest(reset=reset, route=route, model=model):
                    card = state.item_limit("Used up.", "session_limit", "free", reset, None)
                    state.ui["allowance_notice"] = {"item": card, "conversation": "another"}
                    self.cfg = dataclasses.replace(self.cfg, base_url=route, model=model)
                    self.session = state.reset()
                    self.send("continue building")
                    self.assertIs(state.ui["allowance_notice"]["item"], card, "sending never mutates account display")

    def test_success_clears_notice_even_without_usage_headers(self):
        turn = agent.Turn()
        state.ui["allowance_notice"] = {"item": {}, "conversation": self.session["id"]}
        agent._hosted_reply(turn, self.cfg, success=True)
        self.assertNotIn("allowance_notice", state.ui)
        self.assertEqual(turn.hosted_requests, 1)

    def test_late_account_reply_after_sign_out_or_provider_change_cannot_restore_notice_or_meter(self):
        for current_account in (None, {"plan": "free", "email": "another account"}):
            with self.subTest(current_account=current_account):
                self.session = state.reset()
                turn = self.send("make a chair")
                state.clear_allowance_notice(invalidate=True)
                state.ui["account"] = current_account
                self.refuse(turn)
                self.assertNotIn("allowance_notice", state.ui)
                self.assertEqual(state.ui["account"], current_account)
                self.assertEqual(self.session["input"], "make a chair")
                state.ui["allowance_notice"] = {"item": {"text": "New account's notice"}, "conversation": "new"}
                agent._hosted_reply(turn, self.cfg, success=True)
                self.assertIn("allowance_notice", state.ui, "an old account's success cannot clear new account state")

    def test_refusal_without_headers_uses_known_limit_then_refreshes_account(self):
        turn = agent.Turn()
        turn.config = self.cfg
        reset = "2099-10-21T18:00:00Z"
        refusal = llm.LLMError("Used up.", 402, "session_limit", {"plan": "free", "resets_at": reset})
        spec = agent.LoopSpec(role="single", system=lambda _: "", schemas=lambda: [], save=mock.Mock(), reference_card=False)
        account = types.SimpleNamespace(refresh_account=mock.Mock())
        reported = {"plan": "free", "session": {"used": 0.82, "resets_at": reset}}
        state.ui["account"] = reported
        with mock.patch.object(agent, "_request", side_effect=refusal), \
                mock.patch.object(agent, "_changes_item", return_value=None), \
                mock.patch.dict(sys.modules, {"loopcut.account": account}), \
                mock.patch("loopcut.account", account, create=True):
            agent.run_loop(self.session, turn, spec)
        self.assertIn("allowance_notice", state.ui)
        account.refresh_account.assert_called_once()
        self.assertEqual(state.ui["account"], reported)
        self.assertEqual(layout.ring_shares({}, 1, reported, state.ui["allowance_notice"])[1][1], 0)
        agent._hosted_reply(turn, self.cfg, success=True)
        self.assertAlmostEqual(layout.ring_shares({}, 1, reported, state.ui.get("allowance_notice"))[1][1], 0.18)

    def test_new_draft_and_mid_turn_notes_survive_a_first_request_refusal_in_the_original_chat(self):
        turn = self.send("make a chair", [{"name": "ref", "ref": "ref.png"}])
        item = state.item_user("add a cushion")
        turn.notes.append((item["text"], item))
        self.session["items"].append(item)
        self.session["input"] = "and arms"
        original = self.session
        other = state.reset()
        other["input"] = "another conversation's draft"
        self.refuse(turn)
        self.assertEqual(original["input"], "make a chair\nadd a cushion\nand arms")
        self.assertEqual(other["input"], "another conversation's draft")
        self.assertEqual(original["items"], [])
        self.assertEqual(other["items"], [], "the refused send belongs to the original conversation")
        self.assertEqual(original["messages"], [])

    def test_non_hosted_refusal_keeps_normal_history_and_does_not_create_account_notice(self):
        self.cfg = dataclasses.replace(self.cfg, base_url="https://own.test/v1")
        turn = self.send("make a chair")
        self.refuse(turn)
        self.assertEqual([i["kind"] for i in self.session["items"]], ["user", "limit"])
        self.assertNotIn("allowance_notice", state.ui)

    def test_session_heads_up_once_per_reset_across_chats_without_a_purchase(self):
        reset = "2099-10-21T18:00:00Z"
        info = {"plan": "free", "session": {"used": 0.79, "resets_at": reset}}
        agent._warn_low_allowance(self.session, self.session["items"], info)
        self.assertEqual(self.session["items"], [])
        info["session"]["used"] = 0.8
        for billing in (False, True):
            with mock.patch.object(agent.config, "billing_open", return_value=billing):
                agent._warn_low_allowance(self.session, self.session["items"], info)
        self.assertEqual(len(self.session["items"]), 1)
        notice = self.session["items"][0]
        self.assertIn("You've used about 80% of this 5-hour chat allowance.", notice["text"])
        from datetime import datetime
        self.assertIn(datetime.fromisoformat(reset).astimezone().strftime("%a %d %b, %H:%M"), notice["text"])
        self.assertEqual(notice["url"], "")
        fresh = state.reset()
        agent._warn_low_allowance(fresh, fresh["items"], info)
        self.assertEqual(fresh["items"], [], "one warning per server session window, across chats")
        info["session"]["resets_at"] = "2099-10-22T18:00:00Z"
        agent._warn_low_allowance(fresh, fresh["items"], info)
        self.assertEqual(len(fresh["items"]), 1)

    def test_warning_without_session_ids_does_not_match_a_different_live_session(self):
        for live_id, owner_id in ((None, None), (None, "owner"), ("live", None)):
            with self.subTest(live_id=live_id, owner_id=owner_id):
                state.ui.pop("warned_week_allowance", None)
                live, session, items = {"items": []}, {}, []
                if live_id:
                    live["id"] = live_id
                if owner_id:
                    session["id"] = owner_id
                with mock.patch.object(state, "session", return_value=live):
                    agent._warn_low_allowance(session, items, {"week": {"used": .95}})
                self.assertEqual(items, [])
                self.assertEqual(live["items"], [])
                self.assertNotIn("warned_week_allowance", state.ui)

    def test_hosted_turns_can_send_again_without_ever_pumping_main_thread_callbacks(self):
        from concurrent.futures import Future
        from loopcut import mainthread
        from prompt_index_fixture import index_patch
        for spec in (agent.SINGLE_SPEC, agent.ORCH_SPEC, agent.WORKSPACE_SPEC):
            for usage in (None, {"week": {"used": .95}}):
                with self.subTest(spec=spec, usage=usage):
                    self.session = state.reset()
                    callbacks = []

                    def enqueue(fn):
                        future = Future()
                        callbacks.append((fn, future))
                        return future

                    def configure(turn, session):
                        turn.config, turn.spec = self.cfg, spec

                    def complete(**kwargs):
                        kwargs["on_text"]("Done.")
                        return llm.Completion(text="Done.", account=usage)

                    with index_patch(), mock.patch.object(mainthread, "run_on_main", side_effect=enqueue), \
                            mock.patch.object(agent, "_configure", side_effect=configure), \
                            mock.patch.object(agent, "_project_roots", return_value=()), \
                            mock.patch.object(agent, "_scene_context", return_value="scene"), \
                            mock.patch.object(agent, "_scene_snapshot", return_value=None), \
                            mock.patch.object(agent, "_setup_line", return_value=""), \
                            mock.patch.object(agent, "_addons_line", return_value=""), \
                            mock.patch.object(agent, "_session_schemas", return_value=[]), \
                            mock.patch.object(agent, "_route"), mock.patch.object(agent, "_workspace_turn"), \
                            mock.patch.object(llm, "stream_chat", side_effect=complete), \
                            mock.patch.object(agent, "_launch", wraps=agent._launch) as launch:
                        for text in ("hi", "hi again"):
                            launch.reset_mock()
                            self.assertTrue(agent.send(text))
                            launch.assert_called_once()
                            turn = launch.call_args.args[1]
                            turn.thread.join(2)
                            self.assertFalse(turn.thread.is_alive())
                            self.assertEqual(turn.outcome, "completed")
                            self.assertFalse(self.session["busy"])
                            self.assertIsNone(self.session["turn"])
                    self.assertTrue(callbacks)
                    self.assertTrue(all(not future.done() for _, future in callbacks))

    def test_real_streamed_main_and_worker_replies_deliver_reservation_aware_warnings(self):
        from prompt_index_fixture import index_patch
        for role in ("single", "worker"):
            with self.subTest(role=role):
                state.ui.pop("warned_session_allowance", None)
                self.session = state.reset()
                parent = self.send("continue")
                session, turn = self.session, parent
                if role == "worker":
                    session, turn = state.new_session(), agent.Turn()
                    session["messages"] = [{"role": "user", "content": "continue"}]
                    turn.config = self.cfg
                    turn.chat = (self.session, parent)
                info = llm.account_from_headers({"x-loopcut-plan": "free", "x-loopcut-session-used": "0.2",
                    "x-loopcut-session-used-with-reservations": "0.84", "x-loopcut-session-resets-at": "2099-10-21T18:00:00Z"})
                def complete(**kwargs):
                    kwargs["on_text"]("Done.")
                    return llm.Completion(text="Done.", account=info)
                spec = agent.LoopSpec(role=role, system=lambda _: "", schemas=lambda: [], save=mock.Mock(), reference_card=False)
                with index_patch(), mock.patch.object(llm, "stream_chat", side_effect=complete) as stream, \
                        mock.patch.object(agent, "_changes_item", return_value=None):
                    agent.run_loop(session, turn, spec)
                    stream.assert_called_once()
                notices = [i for i in self.session["items"] if i["kind"] == "notice"]
                self.assertEqual(len(notices), 1)
                self.assertIn("84% of this 5-hour chat allowance", notices[0]["text"])
                if role == "worker":
                    self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_queued_main_and_helper_warnings_never_delay_or_repeat_the_final_save(self):
        from concurrent.futures import Future
        from copy import deepcopy
        from loopcut import mainthread
        from prompt_index_fixture import index_patch
        for role in ("single", "worker", "reopened"):
            with self.subTest(role=role):
                state.ui.pop("warned_session_allowance", None)
                self.session = state.reset()
                parent = self.send("continue")
                helper = agent.Turn()
                helper.config, helper.chat = self.cfg, (self.session, parent)
                callbacks = []
                saved = []
                info = {"session": {"used": 0.84, "resets_at": "2099-10-21T18:00:00Z"}}

                def enqueue(fn):
                    future = Future()
                    callbacks.append((fn, future))
                    return future

                def complete(**kwargs):
                    kwargs["on_text"]("Done.")
                    if role == "worker":
                        agent._hosted_reply(helper, self.cfg, info, success=True, session=state.new_session())
                    return llm.Completion(text="Done.", account=info if role != "worker" else None)

                save = conversations.save

                def record_save(session):
                    saved.append((threading.current_thread(), deepcopy(session["items"])))
                    save(session)

                spec = agent.LoopSpec(role="single", system=lambda _: "", schemas=lambda: [],
                                      save=lambda session: conversations.save(session), reference_card=False)
                with index_patch(), mock.patch.object(mainthread, "run_on_main", side_effect=enqueue), \
                        mock.patch.object(llm, "stream_chat", side_effect=complete), \
                        mock.patch.object(conversations, "save", side_effect=record_save), \
                        mock.patch.object(agent, "_changes_item", return_value=None):
                    thread = threading.Thread(target=agent.run_loop, args=(self.session, parent, spec), daemon=True)
                    thread.start()
                    thread.join(2)
                    self.assertFalse(thread.is_alive(), "turn must finish even if main-thread callbacks never run")
                    self.assertFalse(self.session["busy"])
                    self.assertIsNone(self.session["turn"])
                    self.assertEqual(len(saved), 1, "the ordinary final save does not wait for warnings")
                    self.assertIs(saved[0][0], thread)
                    self.assertFalse(any(i["kind"] == "notice" for i in saved[0][1]))
                    self.assertTrue(callbacks)
                    if role == "reopened":
                        reopened = state.replace(deepcopy(self.session))
                        reopened["items"].append(state.item_user("newer message"))
                        reopened["messages"].append({"role": "user", "content": "newer message"})
                        reopened["input"] = "newer draft"
                        save(reopened)
                    for fn, future in callbacks:
                        future.set_result(fn())
                self.assertEqual(parent.outcome, "completed")
                self.assertEqual(len(saved), 1, "warnings never add a separate conversation save")
                loaded = conversations.load(self.session["id"])
                self.assertFalse(any(i["kind"] == "notice" for i in loaded["items"]))
                self.assertEqual(sum(i["kind"] == "notice" for i in state.session()["items"]), 1)
                if role == "reopened":
                    self.assertEqual(loaded["messages"], reopened["messages"])
                    self.assertEqual(loaded["input"], "newer draft")
                    self.assertEqual(reopened["input"], "newer draft")
                    self.assertEqual(reopened["items"][-2], state.item_user("newer message"))

    def test_hosted_refusals_restore_draft_then_save_before_worker_folder_cleanup(self):
        from concurrent.futures import Future
        from loopcut import mainthread
        for stage in ("first", "later", "worker"):
            with self.subTest(stage=stage):
                state.clear_allowance_notice()
                state.ui.pop("warned_week_allowance", None)
                self.session = state.reset()
                parent = self.send("make a chair")
                session, turn = self.session, parent
                if stage == "first":
                    self.assertTrue(agent.send("and a cushion"))
                    session["input"] = "with arms"
                elif stage == "later":
                    turn.hosted_requests = 1
                else:
                    session, turn = state.new_session(), agent.Turn()
                    turn.config, turn.chat = self.cfg, (self.session, parent)
                    session.update(busy=True, turn=turn)
                    worker_folder = Path(os.environ["LOOPCUT_DATA_DIR"]) / "job" / "worker"
                    conversations.FOLDERS[session["id"]] = worker_folder
                    self.addCleanup(conversations.FOLDERS.pop, session["id"], None)
                callbacks = []
                queued = threading.Event()

                def enqueue(fn):
                    future = Future()
                    callbacks.append((fn, future))
                    queued.set()
                    return future

                info = {"plan": "free", "session": {"used": .95}, "week": {"used": .92}}
                refusal = llm.LLMError("Used up.", 402, "session_limit", {"plan": "free"}, account=info)
                save = mock.Mock(wraps=conversations.save)
                turn.spec = agent.LoopSpec(role="worker" if stage == "worker" else "single",
                                          system=lambda _: "", schemas=lambda: [], save=save, reference_card=False)
                with mock.patch.object(mainthread, "run_on_main", side_effect=enqueue), \
                        mock.patch.object(agent, "_request", side_effect=refusal), \
                        mock.patch.object(agent, "_changes_item", return_value=None):
                    turn.thread = threading.Thread(target=agent._run, args=(session, turn), daemon=True)
                    turn.thread.start()
                    self.assertTrue(queued.wait(2))
                    save.assert_not_called()
                    # Base behavior: draft restoration finishes before teardown and saving.
                    fn, future = callbacks.pop(0)
                    future.set_result(fn())
                    turn.thread.join(2)
                    self.assertFalse(turn.thread.is_alive(), "the optional warning must not delay finalisation")
                    self.assertEqual(turn.outcome, "error")
                    self.assertEqual(turn.error_code, "session_limit")
                    save.assert_called_once_with(session)
                    if stage == "worker":
                        self.assertTrue((worker_folder / "conversation.json").is_file())
                        conversations.FOLDERS.pop(session["id"])
                    self.assertTrue(callbacks, "the warning is still queued after the final save")
                    for fn, future in callbacks:
                        future.set_result(fn())
                save.assert_called_once_with(session)
                self.assertFalse(session["busy"])
                self.assertIsNone(session["turn"])
                self.assertEqual(state.ui["allowance_notice"]["item"]["reason"], "session_limit")
                notices = [item for item in self.session["items"] if item["kind"] == "notice"]
                self.assertEqual(len(notices), 1)
                self.assertEqual(notices[0]["allowance_window"], "week")
                if stage == "first":
                    self.assertEqual(session["input"], "make a chair\nand a cushion\nwith arms")
                    self.assertEqual(session["messages"], [])
                    self.assertEqual(session["items"], notices)
                if stage == "later":
                    loaded = conversations.load(session["id"])
                    self.assertEqual(loaded["input"], session["input"])
                    self.assertNotIn(notices[0], loaded["items"])
                elif stage == "worker":
                    self.assertFalse((conversations.root() / session["id"]).exists(),
                                     "late warnings must not write a worker transcript to ordinary storage")

    def test_late_helper_warning_preserves_reopened_messages_and_draft_until_ordinary_save(self):
        from concurrent.futures import Future
        from loopcut import mainthread
        parent = self.send("continue")
        self.session.update(busy=False, turn=None)
        conversations.save(self.session)
        helper = agent.Turn()
        helper.config, helper.chat = self.cfg, (self.session, parent)
        callbacks = []

        def enqueue(fn):
            future = Future()
            callbacks.append((fn, future))
            return future

        with mock.patch.object(mainthread, "run_on_main", side_effect=enqueue):
            agent._hosted_reply(helper, self.cfg, {"session": {
                "used": 0.84, "resets_at": "2099-10-21T18:00:00Z"}}, success=True, session=state.new_session())
        other = state.reset()
        other["input"] = "other draft"
        reopened = state.replace(conversations.load(self.session["id"]))
        reopened["items"].append(state.item_user("newer message"))
        reopened["messages"].append({"role": "user", "content": "newer message"})
        reopened["input"] = "newer draft"
        conversations.save(reopened)
        before = conversations.load(reopened["id"])
        with mock.patch.object(conversations, "save", wraps=conversations.save) as save:
            for fn, future in callbacks:
                future.set_result(fn())
            after = conversations.load(reopened["id"])
            self.assertEqual(after["messages"], before["messages"])
            self.assertEqual(after["input"], "newer draft")
            save.assert_not_called()
        self.assertEqual(after["items"], before["items"], "late warnings wait for the next ordinary save")
        self.assertEqual(reopened["messages"], before["messages"])
        self.assertEqual(reopened["input"], "newer draft")
        self.assertEqual(sum(i["kind"] == "notice" for i in reopened["items"]), 1)
        self.assertEqual(other["input"], "other draft")
        self.assertEqual(other["items"], [])
        conversations.save(reopened)
        loaded = conversations.load(reopened["id"])
        self.assertEqual(loaded["messages"], before["messages"])
        self.assertEqual(loaded["input"], "newer draft")
        self.assertEqual(sum(i["kind"] == "notice" for i in loaded["items"]), 1)

    def test_queued_warnings_require_an_open_conversation_even_without_ids(self):
        from concurrent.futures import Future
        from loopcut import mainthread
        for identity in ("id", "missing", "empty"):
            for closed in (False, True):
                with self.subTest(identity=identity, closed=closed):
                    state.ui.clear()
                    session = state.reset()
                    if identity == "missing":
                        session.pop("id")
                    elif identity == "empty":
                        session["id"] = ""
                    turn = agent.Turn()
                    info = {"session": {"used": .84, "resets_at": "2099-10-21T18:00:00Z"}}
                    callbacks = []

                    def enqueue(fn):
                        callbacks.append(fn)
                        return Future()

                    with mock.patch.object(mainthread, "run_on_main", side_effect=enqueue), \
                            mock.patch.object(conversations, "save") as save:
                        agent._hosted_reply(turn, self.cfg, info, success=True, session=session)
                        if closed:
                            live = state.reset()
                            if identity == "missing":
                                live.pop("id")
                            elif identity == "empty":
                                live["id"] = ""
                            live["input"] = "new draft"
                        for fn in callbacks:
                            fn()
                        save.assert_not_called()
                    if closed:
                        self.assertEqual(session["items"], [])
                        self.assertEqual(live["items"], [])
                        self.assertEqual(live["input"], "new draft")
                        self.assertNotIn("warned_session_allowance", state.ui)
                        state.replace(session)
                        agent._hosted_reply(turn, self.cfg, info, success=True, session=session)
                    self.assertEqual(len(session["items"]), 1)
                    self.assertEqual(session["items"][0]["allowance_window"], "session")

    def test_029_dated_session_notice_matches_the_same_reset_after_upgrade(self):
        reset = "2099-10-21T18:00:00Z"
        # 0.2.9's literal text, without allowance_window or resets_at metadata.
        legacy = state.item_notice("About 16% of this session's chat allowance is left. "
                                   f"Resets {state.reset_label(reset, now=0)}.")
        self.session["items"].append(legacy)
        conversations.save(self.session)
        loaded = state.replace(conversations.load(self.session["id"]))
        agent._warn_low_allowance(loaded, loaded["items"], {"session": {"used": 0.9, "resets_at": reset}})
        self.assertEqual(len(loaded["items"]), 1)
        agent._warn_low_allowance(loaded, loaded["items"], {"session": {
            "used": 0.9, "resets_at": "2099-10-22T18:00:00Z"}})
        self.assertEqual(len(loaded["items"]), 2, "a different dated window still warns")

    def test_029_ambiguous_notices_allow_at_most_one_upgrade_duplicate_per_window(self):
        # Relative session resets and undated weekly text cannot identify a window reliably.
        # Accept one duplicate on upgrade; its new metadata survives subsequent restarts.
        for window, text in (("session", "About 16% of this session's chat allowance is left. Resets in about 2 h."),
                             ("week", "About 16% of this week's allowance is left.")):
            with self.subTest(window=window):
                session = state.reset()
                session["items"].append(state.item_notice(text))
                info = {window: {"used": 0.9, "resets_at": "2099-10-21T18:00:00Z"}}
                for _ in range(3):
                    conversations.save(session)
                    state.ui.clear()
                    session = state.replace(conversations.load(session["id"]))
                    agent._warn_low_allowance(session, session["items"], info)
                    self.assertEqual(len(session["items"]), 2)

    def test_loaded_warning_identity_prevents_duplicates_after_restart_or_sign_in(self):
        for lifecycle in ("restart", "sign_in"):
            with self.subTest(lifecycle=lifecycle):
                state.ui.clear()
                session = state.reset()
                reset = "2099-10-21T18:00:00Z"
                info = {name: {"used": 0.84, "resets_at": reset} for name in ("session", "week")}
                agent._warn_low_allowance(session, session["items"], info)
                conversations.save(session)
                if lifecycle == "restart":
                    state.ui.clear()
                else:
                    state.clear_allowance_notice(invalidate=True)
                    state.ui.pop("warned_session_allowance", None)
                    state.ui.pop("warned_week_allowance", None)
                loaded = state.replace(conversations.load(session["id"]))
                agent._warn_low_allowance(loaded, loaded["items"], info)
                self.assertEqual(len(loaded["items"]), 2, "loaded history remembers both window identities")
                self.assertEqual({i["allowance_window"] for i in loaded["items"]}, {"session", "week"})
                self.assertTrue(all(i["resets_at"] == reset for i in loaded["items"]))
                info["session"]["resets_at"] = "2099-10-22T18:00:00Z"
                agent._warn_low_allowance(loaded, loaded["items"], info)
                self.assertEqual(len(loaded["items"]), 3, "a new window can still warn")
                info["session"]["resets_at"] = reset
                agent._warn_low_allowance(loaded, loaded["items"], info)
                self.assertEqual(len(loaded["items"]), 3, "a delayed old report stays deduplicated")

    def test_expired_reports_do_not_warn_or_consume_the_next_window_marker(self):
        from datetime import datetime
        reset = "2099-10-21T18:00:00Z"
        for role in ("single", "worker"):
            for offset in (0, 1):
                with self.subTest(role=role, offset=offset):
                    state.ui.clear()
                    self.session = state.reset()
                    parent = self.send("continue")
                    session, turn = self.session, parent
                    if role == "worker":
                        session, turn = state.new_session(), agent.Turn()
                        turn.config, turn.chat = self.cfg, (self.session, parent)
                    info = {name: {"used": 0.84, "resets_at": reset} for name in ("session", "week")}
                    with mock.patch.object(agent.allowance.time, "time", return_value=datetime.fromisoformat(reset).timestamp() + offset):
                        agent._hosted_reply(turn, self.cfg, info, success=True, session=session)
                        self.assertFalse(any(i["kind"] == "notice" for i in self.session["items"]))
                        self.assertNotIn("warned_session_allowance", state.ui)
                        self.assertNotIn("warned_week_allowance", state.ui)
                        for window in info.values():
                            window["resets_at"] = "2099-10-22T18:00:00Z"
                        agent._hosted_reply(turn, self.cfg, info, success=True, session=session)
                    self.assertEqual(sum(i["kind"] == "notice" for i in self.session["items"]), 2)

    def test_refusal_headers_only_warn_for_the_other_window_and_keep_history(self):
        from prompt_index_fixture import index_patch
        for role in ("single", "worker"):
            for window in ("session", "week"):
                with self.subTest(role=role, window=window):
                    state.ui.clear()
                    self.session = state.reset()
                    older = state.item_notice("Earlier warning: 81% used.")
                    self.session["items"].append(older)
                    parent = self.send("continue")
                    session, turn = self.session, parent
                    if role == "worker":
                        session, turn = state.new_session(), agent.Turn()
                        session["messages"] = [{"role": "user", "content": "continue"}]
                        turn.config, turn.chat = self.cfg, (self.session, parent)
                    info = {"plan": "free", "session": {"used": 0.82, "resets_at": "2099-10-21T18:00:00Z"},
                            "week": {"used": 0.91, "resets_at": "2099-10-25T18:00:00Z"}}
                    refusal = llm.LLMError("Used up.", 402, window + "_limit", {"plan": "free"}, account=info)
                    spec = agent.LoopSpec(role=role, system=lambda _: "", schemas=lambda: [],
                                          save=conversations.save, reference_card=False)
                    with index_patch(), mock.patch.object(llm, "stream_chat", side_effect=refusal), \
                            mock.patch.object(agent, "_changes_item", return_value=None):
                        agent.run_loop(session, turn, spec)
                    notices = [i for i in self.session["items"] if i["kind"] == "notice"]
                    self.assertEqual(len(notices), 2, "only the other window adds a new percentage warning")
                    self.assertIs(notices[0], older)
                    self.assertIn("91%" if window == "session" else "82%", notices[1]["text"])
                    self.assertNotIn(f"warned_{window}_allowance", state.ui)
                    self.assertEqual(layout.usage_note(info, state.ui["allowance_notice"])[1], 1)
                    if role == "worker":
                        conversations.save(self.session)  # The owning turn's ordinary save persists helper notices.
                    loaded = conversations.load(self.session["id"])
                    self.assertEqual([i for i in loaded["items"] if i["kind"] == "notice"], notices)

    def test_helpers_warn_the_original_conversation_once_per_window_on_every_hosted_plan(self):
        for plan in ("free", "pro", "max"):
            with self.subTest(plan=plan):
                state.ui.pop("warned_session_allowance", None)
                state.ui.pop("warned_week_allowance", None)
                self.session = state.reset()
                parent = self.send("continue")
                helpers = []
                for _ in range(2):
                    worker = agent.Turn()
                    worker.config = self.cfg
                    worker.chat = (self.session, parent)
                    helpers.append((state.new_session(), worker))
                # Changing the visible chat drops helpers' warnings without consuming dedupe keys.
                visible = state.reset()
                info = {"plan": plan, "session": {"used": 0.2, "used_with_reservations": 0.82,
                                                  "resets_at": "2099-10-21T18:00:00Z"},
                        "week": {"used": 0.91, "resets_at": "2099-10-25T18:00:00Z"}}
                for private, worker in helpers:
                    agent._hosted_reply(worker, self.cfg, info, success=True, session=private)
                    self.assertEqual(private["items"], [])
                self.assertEqual(self.session["items"][-1]["kind"], "user")
                self.assertNotIn("warned_session_allowance", state.ui)
                self.assertNotIn("warned_week_allowance", state.ui)
                state.replace(self.session)
                for private, worker in helpers:
                    agent._hosted_reply(worker, self.cfg, info, success=True, session=private)
                agent._hosted_reply(parent, self.cfg, info, success=True, session=self.session)
                notices = [i for i in self.session["items"] if i["kind"] == "notice"]
                self.assertEqual(len(notices), 2)
                self.assertIn("82% of this 5-hour chat allowance", notices[0]["text"])
                self.assertIn("91% of this week's chat allowance", notices[1]["text"])
                self.assertTrue(all("It resets at" in i["text"] for i in notices))
                self.assertEqual(visible["items"], [])
                # A different conversation still shares these server windows.
                state.replace(visible)
                agent._hosted_reply(agent.Turn(), self.cfg, info, success=True, session=visible)
                self.assertEqual(visible["items"], [])
                state.replace(self.session)
                info["week"]["resets_at"] = "2099-11-01T18:00:00Z"
                agent._hosted_reply(parent, self.cfg, info, success=True, session=self.session)
                self.assertEqual(sum(i["kind"] == "notice" for i in self.session["items"]), 3)
                # An older helper finishing late cannot warn for either window again.
                info["week"]["resets_at"] = "2099-10-25T18:00:00Z"
                private, worker = helpers[0]
                agent._hosted_reply(worker, self.cfg, info, success=True, session=private)
                self.assertEqual(sum(i["kind"] == "notice" for i in self.session["items"]), 3)

    def test_full_or_overfull_reports_warn_once_and_refusal_keeps_the_notice(self):
        for window in ("session", "week"):
            for used in (0.8, 1, 1.2):
                with self.subTest(window=window, used=used):
                    state.ui.pop(f"warned_{window}_allowance", None)
                    self.session = state.reset()
                    turn = self.send("continue")
                    info = {"plan": "free", window: {"used": 0.1, "used_with_reservations": used,
                                                     "resets_at": "2099-10-25T18:00:00Z"}}
                    agent._hosted_reply(turn, self.cfg, info, session=self.session)
                    agent._hosted_refusal(self.session, turn, state.item_limit("Used up.", window + "_limit",
                                                                             "free", "", None))
                    notices = self.session["items"]
                    self.assertEqual(len(notices), 1)
                    self.assertEqual(notices[0]["kind"], "notice")
                    self.assertIn(f"about {round(min(used, 1) * 100)}%", notices[0]["text"])
                    self.assertEqual(self.session["input"], "continue")
                    agent._hosted_reply(turn, self.cfg, info, success=True, session=self.session)
                    self.assertEqual(len(notices), 1)

    def test_foreign_providers_and_stale_responses_do_not_emit_warnings(self):
        info = {"plan": "free", "session": {"used": 0.9}, "week": {"used": 0.9}}
        for route in ("https://own.test/v1", config.CHATGPT_PLAN_BASE_URL):
            with self.subTest(route=route):
                parent = agent.Turn()
                parent.config = dataclasses.replace(self.cfg, base_url=route)
                agent._hosted_reply(parent, parent.config, info, success=True, session=self.session)
                worker = agent.Turn()
                worker.chat = (self.session, parent)
                agent._hosted_reply(worker, self.cfg, info, success=True, session=state.new_session())
                self.assertEqual(self.session["items"], [])
        stale = agent.Turn()
        state.clear_allowance_notice(invalidate=True)
        agent._hosted_reply(stale, self.cfg, info, success=True, session=self.session)
        late_helper = agent.Turn()
        late_helper.chat = (self.session, stale)
        agent._hosted_reply(late_helper, self.cfg, info, success=True, session=state.new_session())
        self.assertEqual(self.session["items"], [])
        self.assertNotIn("warned_session_allowance", state.ui)
        self.assertNotIn("warned_week_allowance", state.ui)

    def test_heads_up_uses_reservation_total_and_falls_back_on_malformed_values(self):
        for window in ("session", "week"):
            for reserved in (0.82, "0.82", None, "bad", float("nan"), float("inf"), -1, True, [], {}):
                with self.subTest(window=window, reserved=reserved):
                    state.ui.pop("warned_session_allowance", None)
                    state.ui.pop("warned_week_allowance", None)
                    session = state.reset()
                    valid = reserved in (0.82, "0.82")
                    info = {"plan": "free", window: {"used": 0.62 if valid else 0.82,
                                                     "used_with_reservations": reserved}}
                    agent._warn_low_allowance(session, session["items"], info)
                    self.assertEqual(len(session["items"]), 1)
                    self.assertIn("You've used about 82%", session["items"][0]["text"])
                    self.assertNotIn("resets", session["items"][0]["text"], "no reset time is invented")
            for reported in ({"used": 0.82, "used_with_reservations": 0},
                             {"used": float("nan")}, [], "bad"):
                with self.subTest(window=window, reported=reported):
                    state.ui.pop("warned_session_allowance", None)
                    state.ui.pop("warned_week_allowance", None)
                    session = state.reset()
                    agent._warn_low_allowance(session, session["items"], {window: reported})
                    self.assertEqual(session["items"], [])


class UnrunCompletionTest(unittest.TestCase):
    """The real loop with scripted completions; no Blender or local sockets."""

    def run_replies(self, replies, request="Build a prop", *, previous=False, reject=False,
                    checkpoint_error=False, max_steps=0, role="single", routed=None, on_final=None,
                    session=None, resume=False, base_url="https://example.invalid/v1",
                    tool_names=("run_python", "capture_viewport", "delegate")):
        from loopcut import nudges
        session = session if session is not None else state.new_session()
        if previous:
            session["messages"] = [
                {"role": "user", "content": "Add a cube"},
                {"role": "assistant", "tool_calls": [{"id": "old", "type": "function",
                    "function": {"name": "run_python", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "old", "content": "Created a cube"},
                {"role": "assistant", "content": "Done."}]
            old = state.item_tool("run_python", "Add a cube", "")
            old.update(status="done", output="Created a cube")
            session["items"].append(old)
        if not resume:
            session["items"].append(state.item_user(request))
            session["messages"].append({"role": "user", "content": request})
        turn = agent.Turn()
        turn.config = config.Config("test-key", base_url, "fake", "low", False, False,
                                    max_steps, 24000)
        turn.routed = routed
        saved = []
        spec = agent.LoopSpec(role=role, system=lambda s: "", schemas=lambda: [
                                {"type": "function", "function": {"name": name}} for name in tool_names],
                              save=lambda s: saved.append(json.loads(json.dumps(s["items"]))),
                              asks=reject, checkpoints=checkpoint_error, changes_card=False, on_final=on_final)
        pending = iter(replies)

        def reply(*args):
            completion = next(pending)
            if isinstance(completion, Exception):
                raise completion
            item = state.item_assistant()
            item.update(text=completion.text, streaming=False)
            session["items"].append(item)
            return completion

        run = mock.Mock(return_value=tools.ToolResult("OK"))
        checkpoint = mock.Mock(side_effect=checkpoints.CheckpointError("No checkpoint"))
        with mock.patch.object(agent, "_request", side_effect=reply) as model, \
                mock.patch.object(agent, "_redraw"), mock.patch.object(nudges, "flush"), \
                mock.patch.object(agent, "_wait_for_approval", return_value=False):
            if resume:
                cfg = turn.config
                def configure(fresh, session):
                    fresh.config, fresh.routed = cfg, routed
                def launch(session, fresh):
                    nonlocal turn
                    turn = fresh
                    agent.run_loop(session, fresh, spec, run_tool=run, ensure_checkpoint=checkpoint)
                with mock.patch.object(state, "session", return_value=session), \
                        mock.patch.object(agent, "_configure", side_effect=configure), \
                        mock.patch.object(agent, "_project_roots", return_value=()), \
                        mock.patch.object(agent, "_launch", side_effect=launch):
                    self.assertTrue(agent.resume())
            else:
                session["busy"] = True
                agent.run_loop(session, turn, spec, run_tool=run, ensure_checkpoint=checkpoint)
        self.assertEqual(saved[-1], session["items"], "the final notice is persisted")
        return session, turn, model.call_count, run.call_count

    @staticmethod
    def call():
        return llm.Completion(tool_calls=[llm.ToolCall("c1", "run_python", '{"code":"pass"}')])

    def bounces(self, session):
        return [m for m in session["messages"] if m.get(context.STEP_LIMIT)
                and m["content"] == agent.completion_guard.CORRECTION]

    def test_fabricated_records_bounce_once_then_tools_can_run(self):
        forged = ("I'll build it directly.\n- run_python \"Model the prop\": ok\n"
                  "- looked: three_quarter view\nDone — built by hand.\n"
                  "Verified by Loopcut on this scene: 1 of 1 checks.")
        for role in ("single", "orchestrator"):
            with self.subTest(role=role):
                gate = mock.Mock(return_value=None)
                session, turn, requests, ran = self.run_replies(
                    [llm.Completion(text=forged), self.call(), llm.Completion(text="Done.")],
                    role=role, on_final=gate)
                self.assertEqual((requests, ran, turn.executed_tools, turn.outcome), (3, 1, 1, "completed"))
                self.assertEqual(len(self.bounces(session)), 1)
                self.assertEqual(gate.call_count, 1, "existing finish gate still runs after the correction")
                self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_repeated_record_or_honest_followup_without_execution_ends_with_notice(self):
        for final in ("run_python: done", "Nothing was done."):
            with self.subTest(final=final):
                session, turn, requests, ran = self.run_replies(
                    [llm.Completion(text="run_python: done"), llm.Completion(text=final)], previous=True)
                self.assertEqual((requests, ran, turn.executed_tools), (2, 0, 0))
                self.assertEqual(len(self.bounces(session)), 1)
                self.assertEqual(session["items"][-1], state.item_notice(agent.completion_guard.NOTICE))
                self.assertEqual(session["messages"][-1]["content"], final)

    def test_execution_in_this_turn_allows_completion(self):
        for role, name in (("single", "run_python"), ("single", "capture_viewport"), ("orchestrator", "delegate")):
            with self.subTest(role=role, tool=name):
                call = llm.Completion(tool_calls=[llm.ToolCall("c1", name, "{}")])
                session, turn, requests, ran = self.run_replies(
                    [call, llm.Completion(text="run_python: done")], role=role)
                self.assertEqual((requests, ran, turn.tool_steps, turn.executed_tools), (2, 1, 1, 1))
                self.assertFalse(self.bounces(session))
                self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_skipped_calls_do_not_count_as_execution(self):
        for option in ("reject", "checkpoint_error"):
            with self.subTest(option=option):
                session, turn, requests, ran = self.run_replies(
                    [self.call(), llm.Completion(text="run_python: done"), llm.Completion(text="Done.")],
                    **{option: True})
                self.assertEqual((turn.tool_steps, turn.executed_tools, ran, requests), (1, 0, 0, 3))
                self.assertEqual(len(self.bounces(session)), 1)
                self.assertEqual(session["items"][-1]["text"], agent.completion_guard.NOTICE)

    def test_imitated_runtime_records_are_object_independent(self):
        cases = [("Please add a lamp", "run_python: done"),
                 ("Remove the default cube", 'run_python("remove cube"): done'),
                 ("Could you move it?", "run_python\n```python\nbpy.ops.transform.translate()\n```"),
                 ("Make it red", "**Scene changes**\n1 added / 2 changed"),
                 ("Fix the mesh", "2 added / changed"),
                 ("Fix the mesh", "1 changed"),
                 ("Build a lantern", "4 added, 1 removed, 2 changed"),
                 ("Rename the material", "Scene changes: ~Material"),
                 ("Build a lantern", "Verified by Loopcut on this scene: 1 of 1 checks."),
                 ("Create a prop", '```run_python\n{"code": "pass"}\n```'),
                 ("Create a prop", '```json\n{\n"name": "run_python",\n"status": "ok"\n}\n```')]
        for request, claim in cases:
            with self.subTest(request=request, claim=claim):
                session, _, requests, _ = self.run_replies(
                    [llm.Completion(text=claim), llm.Completion(text="Nothing was done.")], request)
                self.assertEqual(requests, 2)
                self.assertEqual(len(self.bounces(session)), 1)

    def test_questions_plans_answers_refusals_and_examples_pass_through(self):
        replies = ["What size should it be?", "I'll create it after you choose a size.",
                   "Plan: build the mesh, then add materials.", "I can't create that.",
                   "I have not created anything.", "Nothing was done.", "The prop is not built.",
                   "Created nothing; the tools are unavailable.", "Is it done?", "If created, it would be blue.",
                   "Set the scale to 2 in the sidebar.", "Use run_python to create the mesh.",
                   'Example:\n```python\nprint("Done")\n```', '> Done.\nThat is an example.',
                   'Here is an example:\n```run_python\n{"code": "pass"}\n```',
                   'Example:\n- run_python "Create the mesh": ok',
                   "Verified means checked against the live scene.", "Created objects appear in the Outliner.",
                   "The prop is ready to be built.", "The prop is created when you run the script.",
                   "I created it earlier, in the previous turn.", "I created the chest in the previous step.",
                   "I made no changes.", "Done.", "Created the lamp.", "Placed the result.", "I've moved it.",
                   "The material is now changed.", "**Done**, the mesh is repaired.", "All done!",
                   "Verified the lantern exists.", "I made the chest.", "Built it.",
                   "run_python creates meshes.", "The Scene changes card shows what changed.",
                   '```python\nprint("Scene changes")\nrun_python("pass")\n```',
                   '> run_python: done\n> Verified by Loopcut on this scene: 1 of 1 checks.',
                   'In the previous step:\n- run_python "Create the chest": ok',
                   'Plan:\n\nrun_python\n```python\nimport bpy\n```',
                   'I will use this call:\nrun_python({"code": "pass"})',
                   'run_python({"code": "pass"})?',
                   'Here is an example:\n\n```json\n{"name": "run_python", "status": "ok"}\n```']
        for reply in replies:
            with self.subTest(reply=reply):
                session, _, requests, ran = self.run_replies([llm.Completion(text=reply)])
                self.assertEqual((requests, ran), (1, 0))
                self.assertFalse(self.bounces(session))
                self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_non_action_request_and_scene_context_do_not_trigger_guard(self):
        for request in ("How do I build a prop?", "What does Verified mean?", "Create a plan for the scene",
                        "Make a script I can run", "How did you make the chest? Create a mesh first?",
                        "Make a detailed plan", "Create Python code",
                        "<scene_context>\nBuild a prop\n</scene_context>\nHello"):
            with self.subTest(request=request):
                session, _, requests, _ = self.run_replies([llm.Completion(text="run_python: done")], request)
                self.assertEqual(requests, 1)
                self.assertFalse(self.bounces(session))
                self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))
        session, _, requests, _ = self.run_replies([llm.Completion(text="Created a plan.")],
                                                  "Create a plan", routed=types.SimpleNamespace(level=1))
        self.assertEqual(requests, 1)
        self.assertFalse(self.bounces(session))

    def test_routed_action_supports_implicit_requests_and_workers_are_unchanged(self):
        session, _, requests, _ = self.run_replies(
            [llm.Completion(text="run_python: done"), llm.Completion(text="Nothing was done.")],
            "a lamp", routed=types.SimpleNamespace(level=1), role="orchestrator", on_final=agent._orchestrator_final)
        self.assertEqual((requests, len(self.bounces(session))), (2, 1))
        session, _, requests, _ = self.run_replies([llm.Completion(text="run_python: done")], role="worker")
        self.assertEqual(requests, 1)
        self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_correction_does_not_extend_step_budget(self):
        session, turn, requests, ran = self.run_replies(
            [llm.Completion(text="run_python: done"), llm.Completion(text="Built the prop.")], max_steps=1)
        self.assertEqual((turn.outcome, requests, ran), ("step_limit", 2, 0))
        self.assertEqual(len(self.bounces(session)), 1)
        self.assertEqual(session["items"][-1]["text"], agent.completion_guard.NOTICE)

    def test_failed_correction_request_still_leaves_notice(self):
        session, turn, requests, ran = self.run_replies(
            [llm.Completion(text="run_python: done"), llm.LLMError("Connection lost")])
        self.assertEqual((turn.outcome, requests, ran), ("error", 2, 0))
        self.assertEqual(session["items"][-1]["text"], agent.completion_guard.NOTICE)

    def test_fs_fix_7b_item_93_message_113_bounces_once(self):
        # Verbatim final reply from evals/20261023-014909/table.conversation/conversation.json.
        forged = (Path(__file__).parent / "fixtures" / "fs-fix-7b-imitated-records.txt").read_text()
        session, _, requests, ran = self.run_replies(
            [llm.Completion(text=forged), llm.Completion(text="Nothing was done.")], "Build a medieval shovel")
        self.assertEqual((requests, ran, len(self.bounces(session))), (2, 0, 1))

    def test_tool_records_use_the_sessions_tool_list(self):
        for name in ("capture_viewport", "delegate", "custom_scene_tool"):
            for record in (f'{name} "Look at the result": ok', f"{name}\ndone",
                           f'{name}({{"angle": "front"}})',
                           f'```json\n{{"name": "{name}", "arguments": {{}}}}\n```'):
                with self.subTest(name=name, record=record):
                    session, _, requests, _ = self.run_replies(
                        [llm.Completion(text=record), llm.Completion(text="Nothing was done.")], tool_names=(name,))
                    self.assertEqual((requests, len(self.bounces(session))), (2, 1))
                    session, _, requests, _ = self.run_replies([llm.Completion(text=record)], tool_names=())
                    self.assertEqual((requests, len(self.bounces(session))), (1, 0))

    def test_fabrication_is_wrong_on_every_route_so_all_routes_keep_the_narrow_guard(self):
        for base_url in (config.SERVICE_BASE_URL, "chatgpt-plan://", "https://api.openai.com/v1",
                         "http://localhost:1234/v1"):
            with self.subTest(base_url=base_url):
                session, _, requests, _ = self.run_replies(
                    [llm.Completion(text="run_python: done"), llm.Completion(text="Nothing was done.")],
                    base_url=base_url)
                self.assertEqual((requests, len(self.bounces(session))), (2, 1))
                self.assertEqual(session["items"][-1]["text"], agent.completion_guard.NOTICE)
                session, _, requests, _ = self.run_replies([llm.Completion(text="Done.")], base_url=base_url)
                self.assertEqual((requests, len(self.bounces(session))), (1, 0))
                self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_review_questions_plans_and_requested_code_pass_even_when_routed_to_action(self):
        for request, reply in (
                ("How did you make the chest? Create a mesh first?", "I created the chest in the previous step."),
                ("Make a detailed plan", "Plan:\nrun_python: done"),
                ("Create Python code", '```run_python\n{"code": "pass"}\n```'),
                ("Create a plan", "Scene changes\n1 added / 2 changed")):
            with self.subTest(request=request):
                session, _, requests, ran = self.run_replies(
                    [llm.Completion(text=reply)], request, routed=types.SimpleNamespace(level=1))
                self.assertEqual((requests, ran, len(self.bounces(session))), (1, 0, 0))
                self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def saved_session(self, session):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": tmp}):
            conversations.save(session)
            return conversations.load(session["id"])

    def test_resume_after_tools_ran_never_bounces_even_after_save_load_or_steering(self):
        for reload in (False, True):
            for final in ("Done.", "run_python: done"):
                with self.subTest(reload=reload, final=final):
                    session, _, _, _ = self.run_replies([self.call(), llm.LLMError("Connection lost")])
                    session["messages"].append({"role": "user", context.STEER: True, "content": "Make it red"})
                    if reload:
                        session = self.saved_session(session)
                    session, turn, requests, ran = self.run_replies(
                        [llm.Completion(text=final)], session=session, resume=True)
                    self.assertEqual((requests, ran, turn.executed_tools, turn.unrun_claim), (1, 0, 1, False))
                    self.assertFalse(self.bounces(session))
                    self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_failed_correction_is_once_per_original_turn_across_repeated_resumes_and_save_load(self):
        session, _, _, _ = self.run_replies(
            [llm.Completion(text="run_python: done"), llm.LLMError("Connection lost")])
        session = self.saved_session(session)
        session, turn, requests, _ = self.run_replies(
            [llm.LLMError("Still disconnected")], session=session, resume=True)
        self.assertEqual((requests, turn.unrun_claim, len(self.bounces(session))), (1, True, 1))
        session, turn, requests, _ = self.run_replies(
            [llm.Completion(text="run_python: done")], session=session, resume=True)
        self.assertEqual((requests, turn.unrun_claim, len(self.bounces(session))), (1, True, 1))
        # A genuinely new user message gets its own one correction.
        session, turn, requests, _ = self.run_replies(
            [llm.Completion(text="run_python: done"), llm.Completion(text="Nothing was done.")], session=session)
        self.assertEqual((requests, turn.executed_tools, len(self.bounces(session))), (2, 0, 2))

    def test_resume_accumulates_executions_and_suppresses_notice_after_an_earlier_correction(self):
        session, _, _, _ = self.run_replies(
            [llm.Completion(text="run_python: done"), self.call(), llm.LLMError("Connection lost")])
        session, turn, requests, ran = self.run_replies(
            [self.call(), llm.LLMError("Connection lost again")], session=self.saved_session(session), resume=True)
        self.assertEqual((requests, ran, turn.executed_tools, turn.unrun_claim), (2, 1, 2, True))
        session, turn, requests, ran = self.run_replies(
            [llm.Completion(text="run_python: done")], session=self.saved_session(session), resume=True)
        self.assertEqual((requests, ran, turn.executed_tools, len(self.bounces(session))), (1, 0, 2, 1))
        self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_runtime_record_appended_by_finish_hook_is_not_model_fabrication(self):
        def verified(session, turn, completion):
            session["items"][-1]["text"] += "\nVerified by Loopcut on this scene: 1 of 1 checks."
        session, _, requests, _ = self.run_replies([llm.Completion(text="I made no changes.")], on_final=verified)
        self.assertEqual((requests, len(self.bounces(session))), (1, 0))
        self.assertFalse(any(i["kind"] == "notice" for i in session["items"]))

    def test_resume_after_rejected_call_can_correct_and_new_user_turn_forgets_old_execution(self):
        for reject in (False, True):
            with self.subTest(reject=reject):
                session, _, _, _ = self.run_replies([self.call(), llm.LLMError("Connection lost")], reject=reject)
                session, turn, requests, _ = self.run_replies(
                    [llm.Completion(text="run_python: done"), llm.Completion(text="Nothing was done.")],
                    session=session, resume=reject)
                self.assertEqual((requests, turn.executed_tools, len(self.bounces(session))), (2, 0, 1))

    def test_guard_markers_stay_private_on_the_wire(self):
        session, _, _, _ = self.run_replies(
            [llm.Completion(text="run_python: done"), self.call(), llm.Completion(text="Done.")])
        original = session["messages"][0]
        self.assertEqual((original[agent.completion_guard.EXECUTED], original[agent.completion_guard.CORRECTED]),
                         (1, True))
        wired = conversations.wire_messages(session["id"], session["messages"])
        self.assertEqual(wired[0], {"role": "user", "content": "Build a prop"})


class TurnEndTest(unittest.TestCase):
    """Transcript compatibility and correction status with in-process replies, without sockets."""

    def test_terminal_items_keep_their_shape_and_failed_code_stays_red(self):
        stopped = state.item_error("Stopped after 2 steps without finishing.")
        cases = [
            ("revoked", llm.LLMError("HTTP 403: This key was revoked."), "error",
             state.item_error("HTTP 403: This key was revoked.")),
            ("overloaded", llm.LLMError("HTTP 503: Overloaded."), "error",
             state.item_error("HTTP 503: Overloaded.")),
            ("cancelled", llm.Cancelled(), "cancelled", state.item_error("Stopped.")),
            ("step_limit", llm.Completion(text="Some work remains."), "step_limit", stopped),
            ("summary_failed", llm.LLMError("Bad request."), "step_limit", stopped),
            ("summary_tools", llm.Completion(tool_calls=[llm.ToolCall("unused", "get_scene_info", "{}")]),
             "step_limit", stopped),
            ("on_end", llm.Completion(), "completed",
             state.item_error("Internal error while ending the turn: RuntimeError('teardown failed')")),
            ("save", llm.Completion(), "completed", state.item_error("Could not save this conversation: disk full")),
            ("notice", llm.LLMError("Account stopped"), "error", state.item_notice("Account stopped")),
        ]
        from loopcut import nudges
        from loopcut.ui import theme
        for ending, final_reply, outcome, expected in cases:
            with self.subTest(ending=ending), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": tmp}):
                session = state.new_session()
                session["items"].append(state.item_user("Build a chest"))
                session["messages"].append({"role": "user", "content": "Build a chest"})
                session["busy"] = True
                turn = agent.Turn()
                turn.config = config.Config("test-key", "https://example.invalid/v1", "fake", "low", False, False,
                                            2 if outcome == "step_limit" else 0, 24000)

                def save(session):
                    if ending == "save" and not session["busy"]:
                        raise OSError("disk full")
                    conversations.save(session)

                spec = agent.LoopSpec(role="single", system=lambda s: "", schemas=lambda: [], save=save,
                                      asks=False, checkpoints=False,
                                      on_end=mock.Mock(side_effect=RuntimeError("teardown failed"))
                                      if ending == "on_end" else None)
                replies = [llm.Completion(tool_calls=[llm.ToolCall("bad", "run_python", '{"code":"invalid?"}')]),
                           llm.Completion(tool_calls=[llm.ToolCall("later", "run_python", '{"code":"pass"}')]),
                           final_reply]

                def run_tool(name, arguments):
                    if "invalid?" in arguments:
                        return tools.ToolResult("SyntaxError: invalid syntax\n\nScene changes: none.", ok=False)
                    # A folded note is a visible user item, but still belongs to this turn.
                    session["items"].append(state.item_user("Keep the lid open"))
                    return tools.ToolResult("OK")

                with mock.patch.object(agent, "_request", side_effect=replies), \
                        mock.patch.object(agent, "_redraw"), mock.patch.object(agent, "_record_level"), \
                        mock.patch.object(agent, "_changes_item", return_value=state.item_changes("1 added", ["+ Cube"], "")), \
                        mock.patch.object(nudges, "flush"), \
                        mock.patch.object(nudges, "chatgpt_card", return_value=dict(expected) if ending == "notice" else None), \
                        mock.patch("traceback.print_exc"):
                    agent.run_loop(session, turn, spec, run_tool=run_tool)
                self.assertEqual(turn.outcome, outcome)
                terminal = [i for i in session["items"] if i["kind"] in {"error", "limit", "notice"}]
                self.assertEqual(terminal, [expected])
                self.assertEqual(layout._code_repairs(session["items"], False), {})
                # Save again after the simulated disk failure so the final in-memory transcript is checked too.
                conversations.save(session)
                loaded = conversations.load(session["id"])
                self.assertEqual(loaded["items"], session["items"])
                display = layout.build(loaded, 900, 3000, 1.0, lambda f, s, t: len(t) * s * 0.5)
                glyph = next(p for p in display["prims"] if p["id"] == "item1.tool.glyph")
                self.assertEqual(glyph["color"], theme.ERROR)


class PlanRetryTest(unittest.TestCase):
    """Real loop and cards with in-process model replies; no sockets or Blender."""

    def start_helper(self, session, card, error=None, before_start=None, after_start=None):
        """Drive Worker construction, boot and its loop; replace only the child and model I/O."""
        turn = session["turn"]
        turn.roles = config.Roles(orchestrator=turn.config, worker=turn.config, orchestrate=True)
        worker_id, role = card["plan_workers"][0]
        brief = {"id": worker_id, "role": role, "deliverable": "Helper", "checks": [], "owns": [], "prefix": "Helper"}
        runtime = mock.Mock()
        runtime.check.return_value = []
        runtime.finish.return_value = {}

        def start():
            self.assertNotIn("plan_started", card)
            if before_start:
                before_start()
            if error:
                raise error

        def request(*args):
            self.assertTrue(card["plan_started"])
            self.assertEqual(card["status"], "running", "startup is recorded before delegate returns")
            if after_start:
                after_start()
            return llm.Completion(text="done")

        runtime.start.side_effect = start
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(conversations, "save"), \
                mock.patch.object(agent, "_request", side_effect=request) as requested:
            job = types.SimpleNamespace(folder=Path(folder), budget=waves.JobBudget(0, 1800),
                                        lock=threading.Lock(), crew=[], steers=[], stopped=False)
            wave = waves.Wave(session=session, turn=turn, job=job, number=1, id="w1", folder=Path(folder),
                              plan={"workers": [brief]}, spawn=None, openings={worker_id: "Build the helper's object"},
                              item=state.item_wave("w1", 1, [state.wave_row(worker_id, "Helper", role)]),
                              message_index=0, item_index=0, plan_card=card, started=time.monotonic(),
                              make_runtime=lambda wave, worker: runtime)
            worker = waves.Worker(wave, brief, 0)
            job.crew.append(worker)
            worker.started = job.budget.clock()
            worker._work()
            runtime.start.assert_called_once()
            if error:
                requested.assert_not_called()
                self.assertNotIn("plan_started", card)
            else:
                requested.assert_called_once()
                self.assertEqual(worker.turn.outcome, "completed")
        return worker

    def call(self, number, *, name="delegate", helper="A", dry_run=False, **arguments):
        if not arguments:
            arguments = {"wave": {"workers": [{"id": helper.lower(), "deliverable": helper,
                                              "checks": [] if number == 1 else [{"kind": "exists"}]}]},
                         "dry_run": dry_run}
        return llm.ToolCall(str(number), name, json.dumps(arguments))

    def run_calls(self, calls, results, session=None, starts=True, start_error=None):
        session = session or state.new_session()
        session["messages"].append({"role": "user", "content": "Make the requested changes"})
        turn = agent.Turn()
        session.update(busy=True, turn=turn)
        turn.config = config.Config(api_key="k", base_url=config.SERVICE_BASE_URL, model="fast",
                                   reasoning_effort="low", auto_run=True, auto_look=False,
                                   max_steps=0, context_budget=40_000)
        spec = agent.LoopSpec(role="orchestrator", system=lambda session: "", schemas=lambda: [],
                              save=lambda session: None, asks=False, checkpoints=False, changes_card=False,
                              scene_changed=lambda result: False)
        replies = [llm.Completion(tool_calls=[call]) for call in calls] + [llm.Completion(text="Done")]
        execute = mock.Mock(side_effect=results)

        def run_tool(name, arguments):
            result = execute(name, arguments)
            card = next(i for i in reversed(session["items"]) if i["kind"] == "tool")
            if starts and result.ok and card.get("plan_workers") and not card.get("plan_dry_run"):
                self.start_helper(session, card, error=start_error)
            return result

        with mock.patch.object(agent, "_request", side_effect=replies), \
                mock.patch.object(agent, "_redraw"), mock.patch.object(agent, "_note_level"), \
                mock.patch.object(agent, "_record_level"):
            agent.run_loop(session, turn, spec, run_tool=run_tool)
        self.assertEqual(turn.outcome, "completed")
        session.update(busy=False, turn=None)
        return session, [item for item in session["items"] if item["kind"] == "tool"]

    def refusal(self):
        return tools.ToolResult("workers[0].checks[0]: missing kind", ok=False, plan_refused=True)

    def assert_recovery(self, session, card, recovered, working=False):
        from loopcut.ui import theme
        before = dict(card)
        display = layout.build(session, 900, 2000, 1.0, lambda f, s, t: len(t) * s * 0.5)
        self.assertEqual(card, before, "rendering must not rewrite the original attempt")
        prefix = f"item{session['items'].index(card)}.tool."
        text = {p["id"]: p["text"] for p in display["prims"] if p["t"] == "text"}
        self.assertEqual(text[prefix + "summary"], "Helper started" if recovered else
                         "Adjusting the helper plan…" if working else "Helper plan needs adjustment")
        self.assertEqual(text[prefix + "glyph"], "✓" if recovered else "…" if working else "✕")
        glyph = next(p for p in display["prims"] if p["id"] == prefix + "glyph")
        self.assertEqual(glyph["color"], theme.TEXT_MUTED if recovered or working else theme.ERROR)
        self.assertNotIn("Bad arguments", " ".join(value for key, value in text.items() if key.startswith(prefix)))

    def test_shared_deliverable_does_not_resolve_different_workers_or_roles(self):
        first = {"id": "chair", "role": "modeling", "deliverable": "Chair"}
        for second in ({**first, "id": "other"}, {**first, "role": "materials"}):
            with self.subTest(second=second):
                session, cards = self.run_calls(
                    [self.call(1, wave={"workers": [first]}), self.call(2, wave={"workers": [second]})],
                    [self.refusal(), tools.ToolResult("Plan accepted")])
                self.assert_recovery(session, cards[0], False)

    def test_repaired_deliverable_and_reordered_workers_still_resolve(self):
        first = [{"id": "chair", "role": "modeling"},
                 {"id": "table", "role": "modeling", "deliverable": "Old table"}]
        second = [{**first[1], "deliverable": "Table"}, {**first[0], "deliverable": "Chair"}]
        session, cards = self.run_calls(
            [self.call(1, wave={"workers": first}), self.call(2, wave={"workers": second})],
            [self.refusal(), tools.ToolResult("Plan accepted")])
        self.assert_recovery(session, cards[0], True)

    def test_retry_matches_the_orchestrators_normalized_worker_role(self):
        for fields, role in (({}, "modeling"), ({"role": None}, "modeling"), ({"role": ""}, "modeling"),
                             ({"role": "sculpting"}, "modeling"), ({"role": " lights "}, "lighting"),
                             ({"role": "shot"}, "lighting")):
            with self.subTest(fields=fields):
                first = {"id": "chair", "deliverable": "Chair", **fields}
                second = {**first, "role": role}
                session, cards = self.run_calls(
                    [self.call(1, wave={"workers": [first]}), self.call(2, wave={"workers": [second]})],
                    [self.refusal(), tools.ToolResult("Plan accepted")])
                self.assert_recovery(session, cards[0], True)
                self.assertEqual(cards[0]["plan_workers"], [["chair", role]])
                self.assertEqual(cards[1]["plan_workers"], cards[0]["plan_workers"])

    def test_retry_matches_the_role_inherited_from_the_live_thread(self):
        for fields in ({}, {"role": None}, {"role": ""}):
            with self.subTest(fields=fields):
                session = state.new_session()
                session["worker_threads"] = {"key": {"role": "lighting"}}
                first = {"id": "key", "continues": "key", **fields}
                second = {**first, "role": "lighting"}
                session, cards = self.run_calls(
                    [self.call(1, wave={"workers": [first]}), self.call(2, wave={"workers": [second]})],
                    [self.refusal(), tools.ToolResult("Plan accepted")], session=session)
                self.assertEqual(cards[0]["plan_workers"], [["key", "lighting"]])
                self.assertEqual(cards[1]["plan_workers"], cards[0]["plan_workers"])
                self.assert_recovery(session, cards[0], True)

    def test_continuation_identity_is_recorded_before_the_retry_replaces_its_thread(self):
        session = state.new_session()
        session["worker_threads"] = {"key": {"role": "lighting"}}
        first = {"id": "renamed", "continues": "key"}
        second = {**first, "deliverable": "Key light"}

        def delegate(name, arguments):
            if "deliverable" not in json.loads(arguments)["wave"]["workers"][0]:
                return self.refusal()
            # waves.remember moves the continued thread to its new worker id before returning.
            session["worker_threads"]["renamed"] = session["worker_threads"].pop("key")
            return tools.ToolResult("Started")

        session, cards = self.run_calls(
            [self.call(1, wave={"workers": [first]}), self.call(2, wave={"workers": [second]})],
            delegate, session=session)
        self.assertEqual(cards[0]["plan_workers"], [["renamed", "lighting"]])
        self.assertEqual(cards[1]["plan_workers"], cards[0]["plan_workers"])
        self.assert_recovery(session, cards[0], True)

    def test_restore_reload_and_undo_recompute_recovery_from_remaining_items(self):
        session, cards = self.run_calls([self.call(1), self.call(2, dry_run=False)],
                                        [self.refusal(), tools.ToolResult("Started")])
        self.assert_recovery(session, cards[0], True)
        # Real checkpoint restore/save/reload and undo, with only Blender's snapshot I/O replaced.
        fake = types.SimpleNamespace(data=types.SimpleNamespace(filepath="/work/scene.blend"),
                                     ops=types.SimpleNamespace(wm=types.SimpleNamespace(
                                         loopcut_snapshot_restore=lambda **kwargs: {"FINISHED"})))
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": folder}), \
                mock.patch.dict(sys.modules, {"bpy": fake}), \
                mock.patch.object(checkpoints, "_blocking_job", return_value=None), \
                mock.patch.object(checkpoints, "in_place_supported", return_value=True):
            def snapshot(store, kind, label, **extra):
                entry = {"id": f"{len(store.entries) + 1:032x}", "kind": kind, "label": label,
                         "source": fake.data.filepath, "in_place": True, "size": 1, "expired": False, **extra}
                store.folder.mkdir(parents=True, exist_ok=True)
                store.snapshot_path(entry["id"]).write_bytes(b"snapshot stand-in")
                store.add(entry, 10_000)
                return entry

            with mock.patch.object(checkpoints, "_write_snapshot", side_effect=snapshot):
                message_index = next(i for i, m in enumerate(session["messages"])
                                     if any(c["id"] == "2" for c in m.get("tool_calls", [])))
                # prepare() checkpoints while delegate is running, just before appending its wave card.
                with mock.patch.dict(cards[1], {"status": "running"}):
                    checkpoint = waves._wave_checkpoint(session, 1, message_index,
                                                        session["items"].index(cards[1]) + 1)
                checkpoints.restore(session, checkpoint)
                self.assert_recovery(session, cards[0], False)
                self.assertFalse(any(m.get("tool_call_id") == "2" for m in session["messages"]))
                session = conversations.load(session["id"])
                refused = next(i for i in session["items"] if i.get("plan_repair"))
                self.assert_recovery(session, refused, False)
                checkpoints.restore(session, session["items"][-1]["checkpoint"])
                self.assert_recovery(session, refused, True)
                session = conversations.load(session["id"])
                self.assert_recovery(session, next(i for i in session["items"] if i.get("plan_repair")), True)

    def test_only_start_resolves_the_original_card_and_keeps_diagnostics(self):
        for dry_run in (True, False):
            session, cards = self.run_calls([self.call(1), self.call(2, dry_run=dry_run)],
                                            [self.refusal(), tools.ToolResult("Plan accepted")])
            self.assert_recovery(session, cards[0], not dry_run)
            self.assertEqual(cards[0]["plan_repair"], "needed", "rendering must not rewrite history")
            self.assertEqual(cards[0]["status"], "failed", "retain the actual attempt's outcome")
            self.assertIn("workers[0].checks[0]", cards[0]["output"])
            message = next(m for m in session["messages"] if m.get("tool_call_id") == "1")
            self.assertIn("workers[0].checks[0]", message["content"])
            self.assertEqual(cards[1]["status"], "done")

    def test_plan_is_neutral_during_repair_and_recovers_before_the_retry_returns(self):
        session = state.new_session()

        def delegate(name, arguments):
            cards = [i for i in session["items"] if i["kind"] == "tool"]
            if len(cards) == 1:
                return self.refusal()
            self.assert_recovery(session, cards[0], False, working=True)
            self.assertEqual(cards[1]["status"], "running")
            self.start_helper(session, cards[1],
                              before_start=lambda: self.assert_recovery(session, cards[0], False, working=True),
                              after_start=lambda: self.assert_recovery(session, cards[0], True))
            return tools.ToolResult("Started")

        session, cards = self.run_calls([self.call(1), self.call(2)], delegate, session=session, starts=False)
        self.assert_recovery(session, cards[0], True)

    def test_failed_startup_does_not_recover_a_completed_delegate_card(self):
        from loopcut.worker_host import WorkerUnavailable
        session, cards = self.run_calls([self.call(1), self.call(2)],
                                       [self.refusal(), tools.ToolResult("No helper could boot")],
                                       start_error=WorkerUnavailable("handshake failed"))
        self.assertEqual(cards[1]["status"], "done")
        self.assert_recovery(session, cards[0], False)

    def test_completion_without_confirmed_start_does_not_recover_the_plan(self):
        session, cards = self.run_calls([self.call(1), self.call(2)],
                                       [self.refusal(), tools.ToolResult("No helper could boot")], starts=False)
        self.assert_recovery(session, cards[0], False)

    def test_reloaded_running_retry_retains_the_confirmed_start(self):
        session, cards = self.run_calls([self.call(1), self.call(2)],
                                       [self.refusal(), tools.ToolResult("Started")])
        cards[1]["status"] = "running"
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": folder}):
            conversations.save(session)
            loaded = conversations.load(session["id"])
        refused = next(i for i in loaded["items"] if i.get("plan_repair"))
        self.assert_recovery(loaded, refused, True)

    def test_unrelated_success_wait_or_failed_retry_does_not_resolve_a_plan(self):
        for call, result in ((self.call(2, helper="B"), tools.ToolResult("Other plan accepted")),
                             (self.call(2, wait=True), tools.ToolResult("Collected results")),
                             (self.call(2, name="get_scene_info"), tools.ToolResult("Scene info")),
                             (self.call(2), tools.ToolResult("Could not save the copy", ok=False))):
            session, cards = self.run_calls([self.call(1), call], [self.refusal(), result])
            self.assert_recovery(session, cards[0], False)

    def test_runtime_failures_and_previous_turns_are_not_marked_as_repaired(self):
        _, cards = self.run_calls([self.call(1), self.call(2)],
                                 [tools.ToolResult("Could not save the copy", ok=False), tools.ToolResult("Started")])
        self.assertNotIn("plan_repair", cards[0])
        session, cards = self.run_calls([self.call(1)], [self.refusal()])
        self.run_calls([self.call(2)], [tools.ToolResult("Plan accepted")], session=session)
        self.assert_recovery(session, cards[0], False)

    def test_all_refused_attempts_for_a_plan_are_resolved_by_its_successful_retry(self):
        # Different arguments make these real attempts, not identical calls suppressed by the ledger.
        last = self.call(3, dry_run=False)
        session, cards = self.run_calls([self.call(1, dry_run=True), self.call(2, dry_run=True), last],
                                       [self.refusal(), self.refusal(), tools.ToolResult("Started")])
        for card in cards[:2]:
            self.assert_recovery(session, card, True)
        self.assertNotIn("plan_repair", cards[2])

    def test_argument_rejection_is_neutral_then_recovers_at_matching_startup(self):
        # The persona call: workers was a JSON string at the top level instead of inside wave.
        crew = [{"id": "a", "role": "modeling", "deliverable": "A"}]
        for arguments in ({"workers": json.dumps(crew)}, {"workers": crew},
                          {"wave": {"workers": crew}, "unexpected": True}):
            with self.subTest(arguments=arguments):
                session = state.new_session()

                def delegate(name, raw):
                    cards = [i for i in session["items"] if i["kind"] == "tool"]
                    if len(cards) == 1:
                        return tools.execute(name, raw)  # Real signature validation, before plan validation.
                    self.assert_recovery(session, cards[0], False, working=True)
                    self.start_helper(session, cards[1],
                                      before_start=lambda: self.assert_recovery(session, cards[0], False, working=True),
                                      after_start=lambda: self.assert_recovery(session, cards[0], True))
                    return tools.ToolResult("Started")

                session, cards = self.run_calls([self.call(1, **arguments), self.call(2)], delegate,
                                                session=session, starts=False)
                self.assert_recovery(session, cards[0], True)
                self.assertEqual(cards[0]["plan_workers"], [["a", "modeling"]])
                self.assertIn("Bad arguments for delegate:", cards[0]["output"])
                self.assertIn("Bad arguments for delegate:", next(m["content"] for m in session["messages"]
                                                                 if m.get("tool_call_id") == "1"))
                cards[1]["plan_workers"] = [["other", "modeling"]]
                self.assert_recovery(session, cards[0], False)
                cards[1]["plan_workers"] = [["a", "materials"]]
                self.assert_recovery(session, cards[0], False)

    def test_missing_ids_still_require_matching_deliverable_and_role(self):
        for identity in ({"deliverable": "Chair", "role": "modeling"},
                         {"deliverable": "Chair"}, {"role": "modeling"},
                         {"id": [], "deliverable": "Chair", "role": "modeling"}):
            for started in ({"id": "chair", "deliverable": "Chair", "role": "modeling"},
                            {"id": "lamp", "deliverable": "Lamp", "role": "modeling"},
                            {"id": "chair", "deliverable": "Chair", "role": "materials"}):
                with self.subTest(identity=identity, started=started):
                    session, cards = self.run_calls(
                        [self.call(1, workers=[identity]), self.call(2, wave={"workers": [started]})],
                        [self.refusal(), tools.ToolResult("Started")])
                    matches = all(started[key] == value for key, value in identity.items() if key != "id")
                    self.assert_recovery(session, cards[0], matches)
                    with mock.patch.dict(session, busy=True,
                                         turn=types.SimpleNamespace(plan_turn=cards[0]["plan_turn"])):
                        self.assert_recovery(session, cards[0], matches, working=not matches)
                    # Saved cards must carry the same evidence, without live helper state.
                    restored = {**session, "items": json.loads(json.dumps(session["items"]))}
                    self.assert_recovery(restored, restored["items"][session["items"].index(cards[0])], matches)
                    # Earlier saved rejections had raw arguments but no name metadata.
                    cards[0].pop("plan_names")
                    self.assert_recovery(session, cards[0], matches)

    def test_missing_ids_match_normalized_roles_in_recorded_and_saved_arguments(self):
        for supplied, normalized in (("sculpting", "modeling"), ("lights", "lighting")):
            for saved_arguments in (False, True):
                with self.subTest(role=supplied, saved_arguments=saved_arguments):
                    first = {"deliverable": "Chair", "role": supplied}
                    second = {"id": "chair", "deliverable": "Chair", "role": normalized}
                    session, cards = self.run_calls(
                        [self.call(1, workers=[first]), self.call(2, wave={"workers": [second]})],
                        [self.refusal(), tools.ToolResult("Started")])
                    if saved_arguments:
                        cards[0].pop("plan_names")
                    else:
                        self.assertEqual(cards[0]["plan_names"], [["Chair", normalized]])
                    self.assert_recovery(session, cards[0], True)

    def test_unidentified_attempt_cannot_skip_a_delegate_to_claim_a_later_start(self):
        for intervening, result in ((self.call(2, dry_run=True), tools.ToolResult("Plan accepted")),
                                    (self.call(2, wait=True), tools.ToolResult("Collected")),
                                    (self.call(2), self.refusal())):
            with self.subTest(intervening=intervening):
                session, cards = self.run_calls(
                    [llm.ToolCall("1", "delegate", "{}"), intervening, self.call(3, helper="Lamp")],
                    [self.refusal(), result, tools.ToolResult("Started")])
                self.assert_recovery(session, cards[0], False)
                with mock.patch.dict(session, busy=True,
                                     turn=types.SimpleNamespace(plan_turn=cards[0]["plan_turn"])):
                    self.assert_recovery(session, cards[0], False, working=True)

    def test_unidentified_arguments_require_a_real_start_in_the_same_turn(self):
        for raw in ("", "{}", "[]", "null", "{broken", '{"unexpected": true}', '{"workers": [{}]}'):
            with self.subTest(raw=raw):
                enabled = {"turn": types.SimpleNamespace(roles=types.SimpleNamespace(orchestrate=True))}
                with mock.patch.object(state, "session", return_value=enabled):
                    refused = tools.execute("delegate", raw)
                self.assertFalse(refused.ok)
                self.assertTrue(refused.plan_refused)
                call = llm.ToolCall("1", "delegate", raw)
                session, cards = self.run_calls([call, self.call(2)], [refused, tools.ToolResult("Started")])
                self.assert_recovery(session, cards[0], True)
                # The result is derived from saved cards, including after removing a retry.
                session["items"].remove(cards[1])
                self.assert_recovery(session, cards[0], False)
                with mock.patch.dict(session, busy=True, turn=types.SimpleNamespace(plan_turn=cards[0]["plan_turn"])):
                    self.assert_recovery(session, cards[0], False, working=True)
                self.run_calls([self.call(3)], [tools.ToolResult("Started")], session=session)
                self.assert_recovery(session, cards[0], False)
                for retry in (self.call(2, dry_run=True), self.call(2, wait=True),
                              self.call(2, name="get_scene_info")):
                    session, cards = self.run_calls([call, retry], [refused, tools.ToolResult("OK")])
                    self.assert_recovery(session, cards[0], False)
                session, cards = self.run_calls([call, self.call(2)], [refused, tools.ToolResult("No boot")],
                                                starts=False)
                self.assert_recovery(session, cards[0], False)

    def test_argument_validation_does_not_reclassify_internal_or_other_tool_errors(self):
        from loopcut import orchestrator

        def broken(wave=None, dry_run=False, wait=False):
            raise TypeError("internal failure")

        with mock.patch.dict(orchestrator.DISPATCH, {"delegate": broken}):
            result = tools.execute("delegate", "{}")
        self.assertFalse(result.ok)
        self.assertFalse(result.plan_refused)
        for raw in ('{"unexpected": true}', "{broken", "[]"):
            result = tools.execute("get_scene_info", raw)
            self.assertFalse(result.ok)
            self.assertFalse(result.plan_refused)


class ToolCardLabelTest(unittest.TestCase):
    def test_empty_or_invalid_delegate_plan_has_a_plain_title_and_keeps_arguments(self):
        for arguments in ({}, {"workers": "[]"}, {"wave": "bad"}, {"wave": {}},
                          *({"wave": {"workers": crew}} for crew in ([], None, 5, "bad", [None]))):
            with self.subTest(arguments=arguments):
                title, details = agent._describe(llm.ToolCall("1", "delegate", json.dumps(arguments)))
                self.assertEqual(title, "Helper plan is empty or invalid")
                self.assertEqual(json.loads(details), arguments)
        for raw in ("[]", "null", "{broken"):
            self.assertEqual(agent._describe(llm.ToolCall("1", "delegate", raw)),
                             ("Helper plan is empty or invalid", raw))

    def test_undescribed_tool_uses_registered_title(self):
        for name in ("get_scene_info", "inspect_api"):
            with self.subTest(name=name):
                call = llm.ToolCall("c1", name, "{}")
                title, code = agent._describe(call)
                registered_title = next(s["function"]["title"] for s in tools.SCHEMAS
                                   if s["function"]["name"] == name)
                self.assertNotEqual(title, name)
                self.assertNotIn("_", title)
                self.assertEqual(title, registered_title)
                self.assertEqual(code, "")
                self.assertEqual((call.name, call.arguments), (name, "{}"))

    def test_explicit_summary_is_preserved(self):
        for name in ("get_scene_info", "run_python"):
            with self.subTest(name=name):
                arguments = {"summary": "Read the current scene", "code": "pass"}
                call = llm.ToolCall("c1", name, json.dumps(arguments))
                self.assertEqual(agent._describe(call), (arguments["summary"], "pass"))

    def test_described_tool_keeps_its_title(self):
        call = llm.ToolCall("c1", "see_render", "{}")
        self.assertEqual(agent._describe(call), ("Show the last render", ""))

    def test_registered_title_takes_precedence_over_description(self):
        schema = {"type": "function", "function": {"name": "get_scene_info",
                  "title": "Scene information", "description": "Read the current scene."}}
        with mock.patch.object(agent, "_tool_schemas", return_value=[schema]):
            self.assertEqual(agent._describe(llm.ToolCall("c1", "get_scene_info", "{}")),
                             ("Scene information", ""))

    def test_missing_metadata_uses_readable_identifier(self):
        with mock.patch.object(agent, "_tool_schemas", return_value=[]):
            self.assertEqual(agent._describe(llm.ToolCall("c1", "read_custom_data", "{}")),
                             ("Read custom data", ""))

    def test_missing_title_never_falls_back_to_a_description(self):
        schema = {"type": "function", "function": {"name": "read_custom_data",
                  "description": "Read additional data. Returns the stored values."}}
        self.assertEqual(agent._describe(llm.ToolCall("c1", "read_custom_data", "{}"), lambda: [schema]),
                         ("Read custom data", ""))

    def test_invalid_arguments_keep_the_title_and_technical_detail(self):
        title = agent._describe(llm.ToolCall("c1", "get_scene_info", "{}"))[0]
        for arguments in ("{", "[]"):
            with self.subTest(arguments=arguments):
                self.assertEqual(agent._describe(llm.ToolCall("c1", "get_scene_info", arguments)),
                                 (title, arguments))


class LayoutTest(unittest.TestCase):
    @staticmethod
    def measure(font, size, text):
        return len(text) * size * 0.5

    def test_thinking_line_shimmers_and_pulses_its_ellipsis_with_the_clock(self):
        session = state.new_session()
        session["items"] += [state.item_user("hi"), state.item_assistant()]
        session["busy"] = True

        def prims_at(now):
            display = layout.build(session, 400, 600, 1.0, self.measure, "m", None, {"now": now})
            return [p for p in display["prims"] if p.get("id", "").startswith("item1.thinking")]

        first, later = prims_at(0.0), prims_at(1.0)
        # Any still frame reads "Thinking…", never "Thinking." (a screenshot is a still frame).
        self.assertEqual("".join(p["text"] for p in first), "Thinking…")
        self.assertEqual("".join(p["text"] for p in later), "Thinking…")
        self.assertNotEqual(first[-1]["color"], later[-1]["color"], "the ellipsis pulses")
        brightest = lambda prims: max(prims[:-1], key=lambda p: p["color"][0])["text"]
        self.assertNotEqual(brightest(first), brightest(later), "the highlight moves along the word")
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
