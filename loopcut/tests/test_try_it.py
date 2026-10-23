"""Try it tools run before the model, through the ordinary loop, without Blender or sockets."""

import dataclasses
import io
import json
import os
import sys
import tempfile
import types
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).parent))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import (agent, anthropic_messages, chatgpt_responses, checkpoints, cloud, cloud_account,
                     cloud_http, cloud_jobs, config, conversations, llm, router, state, tools, try_it, try_samples)
from loopcut.byok import catalog
from prompt_index_fixture import index_patch


def descriptors():
    photo = deepcopy(next(d for d in catalog._TOOLS if d["name"] == "image_to_3d"))
    photo["file_inputs"][0]["field"] = "image"
    motion = {**deepcopy(photo), "name": "motion_from_text", "title": "Motion from text", "apply": "apply_motion",
              "file_inputs": [], "backends": ["managed"], "byok": None,
              "input_schema": {"type": "object", "additionalProperties": False, "required": ["prompt"],
                               "properties": {"prompt": {"type": "string"},
                                              "duration_s": {"type": "number", "minimum": 1, "maximum": 10}}}}
    for descriptor in (photo, motion):
        descriptor.update(status="live", available=False, unavailable_reason="credits",
                          cost={"credits_estimate": 80, "quote_required": True})
    return [photo, motion]


class TryItTest(unittest.TestCase):
    def patched(self, *args, **kwargs):
        p = patch.object(*args, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"LOOPCUT_DATA_DIR": self.tmp.name, "LOOPCUT_SERVICE_URLS": "https://service.test/v1",
                                      "LOOPCUT_TELEMETRY": "0"})
        env.start()
        self.addCleanup(env.stop)
        fake = types.SimpleNamespace(
            app=types.SimpleNamespace(driver_namespace={}, version=(5, 2, 0)),
            data=types.SimpleNamespace(images={}, objects={}, filepath=""),
            context=types.SimpleNamespace(scene=None, view_layer=types.SimpleNamespace(
                objects=types.SimpleNamespace(active=None)), selected_objects=[]),
            ops=types.SimpleNamespace(wm=types.SimpleNamespace(loopcut_snapshot_restore=lambda **kw: {"FINISHED"})))
        p = patch.dict(sys.modules, {"bpy": fake})
        p.start()
        self.addCleanup(p.stop)
        self.patched(tools, "bpy", fake)
        self.patched(state, "_session", None)
        self.patched(state, "ui", {})
        self.events, self.requests, self.applied = [], [], []
        self.reply = "Placed the result."
        self.transport = Mock(side_effect=AssertionError("A free sample must never contact a cloud service"))
        client = cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=self.transport)
        self.runtime = cloud.Runtime(client, self.root / "cloud", signed_in=True)
        self.runtime.manifest = {"account": {"credits": {"balance": 0}, "settings": {}},
                                 "flags": {"services": True}, "tools": descriptors(), "skills": []}
        self.patched(cloud, "_runtime", self.runtime)
        self.patched(cloud, "sync_settings")
        self.patched(cloud, "_apply_to_scene", side_effect=self.apply)
        self.play = self.patched(try_samples, "play", return_value=True)
        self.confirm = self.patched(cloud_account, "confirm", side_effect=AssertionError("No paid approval for samples"))
        self.patched(agent, "_redraw")
        self.patched(agent, "_on_main", side_effect=lambda fn, *a, **kw: fn())
        self.patched(agent, "_tool_schemas", side_effect=cloud.tool_schemas)
        self.patched(agent, "_system_prompt", return_value="Use the tool result to finish the user's request.")
        self.patched(agent, "_system_tail", return_value="")
        self.patched(agent, "_hosted_reply")
        self.reference = self.patched(agent, "_reference_card", return_value=[])
        self.route = self.patched(router, "start_turn")
        self.patched(checkpoints, "_write_snapshot", side_effect=self.snapshot)
        p = index_patch()
        p.start()
        self.addCleanup(p.stop)
        self.patched(llm, "_open", side_effect=self.chat_request)
        self.patched(chatgpt_responses, "stream", side_effect=self.responses_request)
        self.patched(anthropic_messages, "stream", side_effect=self.anthropic_request)
        self.cfg = config.Config(api_key="test", base_url="https://service.test/v1", model="test-model",
                                 reasoning_effort="low", auto_run=False, auto_look=False,
                                 max_steps=4, context_budget=40000)
        self.session = state.reset()

    def snapshot(self, store, kind, label, **extra):
        self.events.append("checkpoint")
        entry = {"id": uuid.uuid4().hex, "kind": kind, "label": label, "created": 0,
                 "size": 1, "expired": False, "source": "", "in_place": True, **extra}
        store.folder.mkdir(parents=True, exist_ok=True)
        store.snapshot_path(entry["id"]).write_bytes(b"stand-in for Blender snapshot")
        store.add(entry, 1024 * 1024)
        return entry

    def apply(self, module, descriptor, job, scene):
        self.events.append("apply")
        self.applied.append(deepcopy(job))
        if descriptor["apply"] == "import_model":
            return tools.ToolResult(cloud_jobs.result_summary("import_model", job,
                ["Loopcut try_photo_3d model; placed on the ground at the 3D cursor, 1.8 m tall"]))
        return tools.ToolResult("Applied " + job["id"] + ". Continue placing the result in the scene.")

    def run_tool(self, name, arguments):
        self.events.append("tool")
        result = tools.execute(name, arguments)
        if result.then:
            return result.then(self.turn.cancel.is_set, lambda fn: fn())
        return result

    def record_request(self, body, provider):
        self.events.append("model")
        self.requests.append((provider, deepcopy(body)))

    def chat_request(self, build, *args):
        self.record_request(json.loads(build().data), "chat")
        chunk = {"choices": [{"delta": {"content": self.reply}, "finish_reason": "stop"}]}
        response = io.BytesIO(b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n")
        response.headers = {}
        return response

    def responses_request(self, **kwargs):
        body = chatgpt_responses.request_body(**{k: kwargs[k] for k in ("model", "messages", "tools", "reasoning_effort")})
        self.record_request(body, "responses")
        kwargs["on_text"](self.reply)
        return llm.Completion(text=self.reply)

    def anthropic_request(self, **kwargs):
        body, _ = anthropic_messages.request_body(**{k: kwargs[k] for k in ("model", "messages", "tools", "reasoning_effort")})
        self.record_request(body, "anthropic")
        kwargs["on_text"](self.reply)
        return llm.Completion(text=self.reply)

    def prepare(self, card_id="photo_3d", *, attached=True, source=None, route="hosted", orchestrate=False):
        self.session = state.reset()
        self.events.clear()
        self.requests.clear()
        self.applied.clear()
        self.runtime.keys_set = {"FAL_KEY": True} if route == "vendor_key" else {}
        self.runtime.preferred = {"image_to_3d": "byok"} if route == "vendor_key" else {}
        base = {"hosted": "https://service.test/v1", "chatgpt": "chatgpt-plan://account",
                "own_key": "https://api.example.test/v1", "local": "http://localhost:1234/v1",
                "anthropic": "anthropic://api.anthropic.com", "vendor_key": "https://api.example.test/v1"}[route]
        self.turn = agent.Turn()
        self.turn.config = dataclasses.replace(self.cfg, base_url=base)
        self.turn.spec = agent.ORCH_SPEC if orchestrate else agent.SINGLE_SPEC
        self.turn.try_it = card_id
        card = next(c for c in try_it.CARDS if c["id"] == card_id)
        item = state.item_user(card["prompt"])
        checkpoints.begin_turn(self.session, item)
        self.session["items"].append(item)
        message = {"role": "user", "content": card["prompt"]}
        if attached and card.get("photo"):
            path = source or try_samples.SAMPLE_PHOTO
            ref = conversations.store_image(self.session, path)
            self.session["references"] = [{"ref": ref, "full": ref, "name": "attached-clock.jpg", "pinned": True,
                                            "source": str(path)}]
            message.update({conversations.ATTACHED: True, "content": [
                {"type": "text", "text": card["prompt"]}, {"type": "image_url", "image_url": {"url": ref}}]})
        self.session["messages"].append(message)
        self.session["busy"], self.session["turn"] = True, self.turn
        return self.turn

    def run_turn(self):
        agent.run_loop(self.session, self.turn, self.turn.spec, run_tool=self.run_tool,
                       ensure_checkpoint=checkpoints.ensure_for_turn)
        self.assertEqual(self.turn.outcome, "completed", self.session["items"])

    def assert_history(self):
        messages = self.session["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "assistant"])
        call = messages[1]["tool_calls"][0]
        self.assertEqual(messages[2]["tool_call_id"], call["id"])
        provider, body = self.requests[0]
        if provider == "chat":
            wired = body["messages"]
            index = next(i for i, m in enumerate(wired) if m.get("tool_calls"))
            self.assertEqual(wired[index]["tool_calls"], [call])
            self.assertEqual(wired[index + 1], messages[2])
            # OpenAI-compatible routes include local/custom Mistral V3/V7/V11 validators.
            self.assertRegex(wired[index]["tool_calls"][0]["id"], r"\A[a-zA-Z0-9]{9}\Z")
        elif provider == "responses":
            wired = body["input"]
            index = next(i for i, m in enumerate(wired) if m.get("type") == "function_call")
            self.assertEqual(wired[index]["call_id"], call["id"])
            self.assertRegex(wired[index]["call_id"], r"\A[a-zA-Z0-9_-]+\Z")
            self.assertEqual(wired[index]["arguments"], call["function"]["arguments"])
            self.assertEqual(wired[index + 1], {"type": "function_call_output", "call_id": call["id"],
                                                "output": messages[2]["content"]})
        else:
            wired = body["messages"]
            index = next(i for i, m in enumerate(wired) if m["role"] == "assistant")
            use, result = wired[index]["content"][0], wired[index + 1]["content"][0]
            self.assertRegex(use["id"], r"\A[a-zA-Z0-9_-]+\Z")
            self.assertEqual((use["type"], use["id"], use["input"]),
                             ("tool_use", call["id"], json.loads(call["function"]["arguments"])))
            self.assertEqual((wired[index + 1]["role"], result["type"], result["tool_use_id"], result["content"]),
                             ("user", "tool_result", call["id"], messages[2]["content"]))
        return call

    def test_cards_run_first_for_each_model_route_and_provider_format(self):
        for card in ("photo_3d", "motion"):
            for route in ("hosted", "chatgpt", "own_key", "local", "anthropic"):
                with self.subTest(card=card, route=route):
                    self.prepare(card, route=route)
                    self.run_turn()
                    self.assertEqual(self.events, ["checkpoint", "tool", "apply", "model"])
                    self.assertEqual((self.turn.tool_steps, self.turn.executed_tools), (1, 1))
                    self.assertFalse(self.turn.unrun_claim, "the synthetic first tool ran before the claiming reply")
                    self.assertFalse(any(i.get("text") == agent.completion_guard.NOTICE for i in self.session["items"]))
                    call = self.assert_history()
                    arguments = json.loads(call["function"]["arguments"])
                    expected = {"image": "attached-clock.jpg"} if card == "photo_3d" else {
                        "prompt": "a figure jumps for joy", "duration_s": 3}
                    self.assertEqual(arguments, expected)
                    self.assertEqual(self.applied[0]["input"], expected)
                    self.assertEqual(self.applied[0]["id"], "try_" + card)
                    self.assertEqual(self.applied[0]["credits"], {"reserved": 0, "charged": 0, "refunded": 0})
                    self.assertEqual(self.session["spend"]["credits"], 0)
                    self.assertEqual(self.turn.paid_calls, 0)
                    tool_card = next(i for i in self.session["items"] if i["kind"] == "tool")
                    self.assertEqual((tool_card["status"], tool_card["cost"]), ("done", "Free"))
                    saved = conversations.load(self.session["id"])
                    self.assertEqual(saved["messages"], self.session["messages"])
                    self.assertEqual(saved["items"][0]["checkpoint"], self.session["items"][0]["checkpoint"])
        self.confirm.assert_not_called()
        self.transport.assert_not_called()
        self.reference.assert_not_called()

    def test_orchestrator_uses_the_same_first_step_without_a_router_request(self):
        for card in ("photo_3d", "motion"):
            with self.subTest(card=card):
                self.prepare(card, orchestrate=True)
                self.run_turn()
                self.assertEqual(self.events, ["checkpoint", "tool", "apply", "model"])
                self.assertEqual((self.turn.tool_steps, self.turn.executed_tools), (1, 1))
                self.assertFalse(self.turn.unrun_claim)
                self.assert_history()
        self.route.assert_not_called()

    def test_own_vendor_key_photo_uses_the_offered_reference_field(self):
        self.prepare(route="vendor_key")
        self.run_turn()
        call = self.assert_history()
        self.assertEqual(json.loads(call["function"]["arguments"]), {"reference": "attached-clock.jpg"})
        self.assertEqual(self.applied[0]["credits"]["charged"], 0)
        self.confirm.assert_not_called()
        self.transport.assert_not_called()

    def test_a_build_missing_the_sample_does_not_force_a_paid_call(self):
        # This tests sample availability and paid-call fallback, not an unearned completion claim.
        self.reply = "The sample is unavailable."
        for card in ("photo_3d", "motion"):
            with self.subTest(card=card):
                self.prepare(card)
                with patch.object(try_samples, "SAMPLES", self.root / "missing"):
                    self.run_turn()
                self.assertEqual(self.events, ["model"])
        self.confirm.assert_not_called()
        self.transport.assert_not_called()

    def test_model_tool_steps_continue_after_the_sample_without_repeating_it(self):
        self.prepare("motion")
        count = 0
        def request(**kwargs):
            nonlocal count
            self.events.append("model")
            count += 1
            if count == 1:
                self.assertEqual([m["role"] for m in kwargs["messages"]][-3:], ["user", "assistant", "tool"])
                return llm.Completion(tool_calls=[llm.ToolCall(id="call_place", name="run_python",
                    arguments=json.dumps({"code": "place_result()", "summary": "Place the generated result"}))])
            return llm.Completion(text="Done.")
        execute = tools.execute
        def dispatch(name, arguments):
            if name == "run_python":
                return tools.ToolResult("Placed result; removed the default cube.")
            return execute(name, arguments)
        self.session["auto_run"] = True
        with patch.object(llm, "stream_chat", side_effect=request), patch.object(tools, "execute", side_effect=dispatch):
            self.run_turn()
        self.assertEqual(self.events, ["checkpoint", "tool", "apply", "model", "tool", "model"])
        self.assertEqual(self.turn.tool_steps, 2)
        self.assertEqual(self.turn.executed_tools, 2)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual([m["role"] for m in self.session["messages"]],
                         ["user", "assistant", "tool", "assistant", "tool", "assistant"])

    def test_resuming_saved_history_does_not_repeat_the_sample(self):
        self.prepare()
        self.run_turn()
        original = deepcopy(self.session["messages"])
        self.session = state._session = conversations.load(self.session["id"])
        self.turn = agent.Turn()  # resume() creates a new turn without the once-only try_it marker.
        self.turn.config, self.turn.spec = self.cfg, agent.SINGLE_SPEC
        self.session["busy"], self.session["turn"] = True, self.turn
        self.events.clear()
        self.run_turn()
        self.assertEqual(self.events, ["model"])
        self.assertEqual(self.session["messages"][:-1], original)

    def test_photo_card_can_remove_default_cube_after_grounded_import(self):
        self.prepare("photo_3d")
        code = "bpy.data.objects.remove(bpy.data.objects['Cube'], do_unlink=True)"
        def request(**kwargs):
            self.events.append("model")
            if self.events.count("model") == 1:
                result = kwargs["messages"][-1]
                self.assertEqual(result["role"], "tool")
                self.assertIn("placed on the ground at the 3D cursor, 1.8 m tall", result["content"])
                return llm.Completion(tool_calls=[llm.ToolCall(id="call_remove", name="run_python",
                    arguments=json.dumps({"code": code, "summary": "Remove the default cube"}))])
            return llm.Completion(text="Done.")
        execute, removed = tools.execute, []
        def dispatch(name, arguments):
            if name == "run_python":
                removed.append(json.loads(arguments)["code"])
                return tools.ToolResult("Removed the default cube.")
            return execute(name, arguments)
        self.session["auto_run"] = True
        with patch.object(llm, "stream_chat", side_effect=request), patch.object(tools, "execute", side_effect=dispatch):
            self.run_turn()
        self.assertEqual(removed, [code])
        self.assertEqual(self.events, ["checkpoint", "tool", "apply", "model", "tool", "model"])
        self.assertEqual(len(self.applied), 1)

    def test_unmarked_missing_tool_and_missing_or_different_photo_leave_the_loop_alone(self):
        # These card eligibility cases do no work; leave the ordinary model reply non-claiming.
        self.reply = "What size should it be?"
        other = self.root / "other.jpg"
        other.write_bytes(b"not the card photo")
        for case in ("unmarked", "missing_tool", "missing_photo", "different_photo", "old_photo", "no_tool_card"):
            with self.subTest(case=case):
                self.prepare("model" if case == "no_tool_card" else "photo_3d",
                             attached=case != "missing_photo", source=other if case == "different_photo" else None,
                             orchestrate=True)
                if case == "unmarked":
                    self.turn.try_it = ""
                if case == "old_photo":
                    self.session["messages"].append({"role": "user", "content": "a later request without an image"})
                with patch.object(agent, "_tool_schemas", return_value=[] if case == "missing_tool" else cloud.tool_schemas()):
                    self.run_turn()
                self.assertEqual(self.events, ["model"])
                self.assertFalse(any(m.get("tool_calls") for m in self.session["messages"]))
        self.assertEqual(self.route.call_count, 6)

    def test_send_marks_only_the_unedited_card_prompt(self):
        card = next(c for c in try_it.CARDS if c["id"] == "motion")
        def configure(turn, session):
            turn.config, turn.spec = self.cfg, agent.SINGLE_SPEC
        launched = []
        def launch(session, turn):
            self.turn = turn
            session["busy"], session["turn"] = True, turn
            launched.append(turn)
            self.run_turn()
        with patch.object(agent, "_configure", side_effect=configure), \
                patch.object(agent, "_scene_context", return_value="<scene_context/>"), \
                patch.object(agent, "_scene_snapshot", return_value=None), \
                patch.object(agent, "_project_roots", return_value=()), \
                patch.object(agent, "_addons_line", return_value=""), patch.object(agent, "_launch", side_effect=launch):
            for edited in (True, False):
                self.session = state.reset()
                self.events.clear()
                # Editing the prompt removes the synthetic call; this test only checks that marker.
                self.reply = "What size should it be?" if edited else "Placed the result."
                self.session["try_it"] = (card["id"], card["prompt"])
                self.assertTrue(agent.send(card["prompt"] + (" Make it blue." if edited else "")))
                self.assertEqual(launched[-1].try_it, "" if edited else "motion")
                self.assertEqual(self.events, ["model"] if edited else ["checkpoint", "tool", "apply", "model"])
                self.assertNotIn("try_it", self.session)

    def test_a_failed_checkpoint_prevents_the_sample_from_running(self):
        self.prepare()
        with patch.object(checkpoints, "_write_snapshot", side_effect=checkpoints.CheckpointError("disk full")):
            self.run_turn()
        self.assertEqual(self.events, ["model"])
        self.assertIn("NOT run", self.session["messages"][2]["content"])
        self.assert_history()

    def test_cancel_closes_the_synthetic_call_without_a_model_request(self):
        self.prepare()
        def cancel(*args):
            self.turn.cancel.set()
            return False
        self.play.side_effect = cancel
        agent.run_loop(self.session, self.turn, self.turn.spec, run_tool=self.run_tool,
                       ensure_checkpoint=checkpoints.ensure_for_turn)
        self.assertEqual(self.turn.outcome, "cancelled")
        self.assertEqual(self.events, ["checkpoint", "tool"])
        self.assertEqual([m["role"] for m in self.session["messages"]], ["user", "assistant", "tool"])
        self.assertEqual(self.session["messages"][1]["tool_calls"][0]["id"], self.session["messages"][2]["tool_call_id"])
        self.assertFalse(self.applied)

    def test_first_hosted_refusal_keeps_the_sample_and_checkpoint(self):
        self.prepare()
        # send() keeps a snapshot so a refusal before any work returns the request to the input.
        self.turn.submission = {"items": [], "messages": [], "references": [], "text": "draft", "attachments": [],
                                "scene_seen": None, "scene_turn_start": None, "pending_checkpoint": None, "turn_number": 0}
        error = llm.LLMError("Weekly limit", status=402, code="week_limit", account={"plan": "free"})
        with patch.object(llm, "_open", side_effect=error), \
                patch.object(agent, "_update_account", lambda info: None):  # account.py needs bpy.
            agent.run_loop(self.session, self.turn, self.turn.spec, run_tool=self.run_tool,
                           ensure_checkpoint=checkpoints.ensure_for_turn)
        self.assertEqual(self.turn.outcome, "error")
        self.assertFalse(self.turn.returned_submission)
        self.assertEqual([m["role"] for m in self.session["messages"]], ["user", "assistant", "tool"])
        self.assertTrue(self.session["items"][0]["checkpoint"])
        saved = conversations.load(self.session["id"])
        self.assertEqual(saved["messages"], self.session["messages"])

    def test_restore_and_undo_preserve_the_whole_synthetic_step(self):
        self.prepare()
        self.run_turn()
        checkpoint = self.session["items"][0]["checkpoint"]
        original = deepcopy(self.session["messages"])
        with patch.object(checkpoints, "_blocking_job", return_value=""):
            checkpoints.restore(self.session, checkpoint)
            self.assertEqual(self.session["messages"], [])
            undo = self.session["items"][-1]["checkpoint"]
            checkpoints.restore(self.session, undo)
        self.assertEqual(self.session["messages"], original)


if __name__ == "__main__":
    unittest.main()
