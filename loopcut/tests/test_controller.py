"""Selective reviews with scripted models: no Blender, credentials, network or paid evaluations."""

import dataclasses
import json
import os
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import agent, cloud_http, config, controller, llm, spending, state, tools, waves


def model():
    return config.Config("test-key", "https://example.invalid/v1", "fast", "low", True, False, 10, 32000)


def answer(packet, action=None, **extra):
    return {"event": packet["event"], "action": action if action is not None else {
        "paid_action": "approve", "finish": "present", "repeated_failure": "continue"}[packet["event"]],
        "scene_version": packet["scene_version"], "instruction_version": packet["instruction_version"],
        "reason": "The evidence supports this choice.", "guidance": "Inspect the current object before repairing it.",
        "findings": [], "advice": "", **extra}


def paid(credits=20):
    return {"text": f"{credits} credits", "credits": credits, "vendor": None, "required": False,
            "threshold": 100, "balance": 500}


class ControllerFixture:
    def setUp(self):
        self.session = state.new_session()
        self.session["items"].append(state.item_user("Make a red cube."))
        self.session["messages"].append({"role": "user", "content": "Make a red cube."})
        self.turn = agent.Turn()
        self.turn.config = model()
        self.turn.spending = spending.Settings()
        self.session["turn"] = self.turn
        self.scene = {"version": 1}
        self.requests = []
        self.inspect = Mock(return_value=("Cube is blue.", None))
        self.settings = config.Controller(model())
        self.review = self.make_review()

    def stream(self, **kwargs):
        self.requests.append(kwargs)
        packet = json.loads(kwargs["messages"][1]["content"])
        return llm.Completion(text=json.dumps(answer(packet)), usage={"input": 100, "cached": 0, "output": 50})

    def make_review(self, stream=None, **settings):
        review = controller.Controller(self.session, self.turn, dataclasses.replace(self.settings, **settings),
                                       snapshot=lambda: dict(self.scene), inspect=self.inspect, stream=stream or self.stream)
        self.review = self.turn.controller = review
        return review

    def packet(self):
        return self.review.packet("finish", "Done.", None)


class ControllerTest(ControllerFixture, unittest.TestCase):
    def test_requires_grounded_correction_and_observed_evidence(self):
        packet = self.packet()
        for quote, evidence in (("make it shiny", "e1"), ("red cube", "invented")):
            with self.subTest(quote=quote), self.assertRaises(controller.ReviewError):
                controller.parse(json.dumps(answer(packet, "correct", findings=[
                    {"quote": quote, "issue": "Wrong colour", "evidence": evidence}])), packet, {"e1"})
        decision = controller.parse(json.dumps(answer(packet, "correct", findings=[
            {"quote": "red cube", "issue": "The cube is blue", "evidence": "e1"}])), packet, {"e1"})
        self.assertEqual(decision.action, "correct")
        self.assertEqual(controller.parse(json.dumps(answer(packet, advice="A bevel might help.")), packet, set()).action,
                         "present")

    def test_structured_decision_rejects_invalid_types_actions_and_versions(self):
        packet = self.packet()
        for fields in ({"action": []}, {"action": "approve"}, {"scene_version": "old"}, {"reason": 7},
                       {"findings": {}}, {"action": "correct"}, {"guidance": "", "action": "correct"},
                       {"unexpected": True}):
            with self.subTest(fields=fields), self.assertRaises(controller.ReviewError):
                controller.parse(json.dumps(answer(packet, **fields)), packet, set())

    def test_mutating_or_malformed_tool_never_reaches_dispatch(self):
        for name, arguments in (("run_python", '{"code":"delete()"}'), ("review_scene", '{"code":"delete()"}'),
                                ("review_objects", '{"names":[7]}'), ("review_image", '{"ref":{}}'),
                                ("review_result", '{"id":"../../secret"}'), ("review_scene", "[]")):
            with self.subTest(name=name), self.assertRaises(controller.ReviewError):
                self.review._read(llm.ToolCall("bad", name, arguments), self.packet())
        self.inspect.assert_not_called()

    def test_read_only_inspection_can_ground_a_correction(self):
        calls = []
        def stream(**kwargs):
            calls.append(kwargs)
            packet = json.loads(kwargs["messages"][1]["content"])
            if len(calls) == 1:
                return llm.Completion(tool_calls=[llm.ToolCall("read", "review_objects", '{"names":["Cube"]}')])
            return llm.Completion(text=json.dumps(answer(packet, "correct", findings=[
                {"quote": "red cube", "issue": "The cube is blue.", "evidence": "read1"}])))
        review = self.make_review(stream)
        self.assertEqual(review.review("finish").action, "correct")
        self.inspect.assert_called_once_with("review_objects", {"names": ["Cube"]})
        self.assertIn("Evidence read1: Cube is blue.", calls[-1]["messages"][-1]["content"])

    def test_scene_edits_and_user_steering_invalidate_decisions(self):
        for change in (lambda: self.scene.update(version=2),
                       lambda: self.session["items"].append(state.item_user("Make it green instead.")),
                       lambda: self.turn.notes.append(("make it green", {}))):
            def stream(**kwargs):
                completion = self.stream(**kwargs)
                change()
                return completion
            self.scene = {"version": 1}
            with self.subTest(change=change), self.assertRaises(controller.StaleReview):
                self.make_review(stream).review("finish")

    def test_only_meaningful_paid_actions_qualify(self):
        self.assertFalse(self.review.meaningful(paid(0)))
        self.assertFalse(self.review.meaningful(paid(6)))
        self.assertTrue(self.review.meaningful(paid(20)))
        self.assertTrue(self.review.meaningful({"credits": None}))
        self.assertTrue(self.review.meaningful({"vendor": "OpenAI", "credits": 0}))

    def test_unknown_worker_cost_and_inflight_usage_are_not_zero_dollars(self):
        budget = waves.JobBudget(0.4, 100, clock=lambda: 0)
        budget.take_step(0)
        self.assertIsNone(budget.snapshot()["used"])
        budget.add("unknown-model", {"input": 100})
        self.assertIsNone(budget.snapshot()["used"])
        self.assertEqual(budget.snapshot()["unknown_requests"], 1)

    def test_stored_result_pointers_survive_packet_compaction(self):
        from loopcut import result_store
        self.session["messages"].append({"role": "tool", "tool_call_id": "old",
            "content": "Partial result" + result_store.pointer("r12", 5000, 10, 20)})
        packet = self.packet()
        self.assertEqual(packet["stored_results"][0]["id"], "r12")
        self.review._read(llm.ToolCall("read", "review_result", '{"id":"r12","lines":"10-20"}'), packet)
        self.inspect.assert_called_once()
        with self.assertRaises(controller.ReviewError):
            self.review._read(llm.ToolCall("read", "review_result", '{"id":"r1"}'), packet)

    def test_omitted_human_requirements_remain_retrievable(self):
        for index in range(10):
            self.session["items"].append(state.item_user(f"Correction {index}: keep the handle."))
        packet = self.packet()
        self.assertNotIn("Correction 0", str(packet["instructions"]))
        text, image = self.review._read(llm.ToolCall("read", "review_instruction", '{"index":1}'), packet)
        self.assertIn("Correction 0: keep the handle.", text)
        self.assertIsNone(image)
        self.inspect.assert_not_called()
        with self.assertRaises(controller.ReviewError):
            self.review._read(llm.ToolCall("read", "review_instruction", '{"index":true}'), packet)

    def test_inspection_loop_has_a_hard_bound(self):
        self.review.stream = lambda **kw: llm.Completion(tool_calls=[llm.ToolCall("read", "review_scene", "{}")])
        with self.assertRaisesRegex(controller.ReviewError, "inspection limit"):
            self.review.review("finish")
        self.assertEqual(self.inspect.call_count, controller.MAX_READS)
        self.assertLessEqual(self.review.totals["calls"], controller.MAX_READS + 1)

    def test_cancel_propagates_and_a_late_reply_does_not_become_a_decision(self):
        def stream(**kw):
            self.turn.cancel.set()
            raise llm.Cancelled()
        self.review.stream = stream
        with self.assertRaises(llm.Cancelled):
            self.review.review("finish")
        self.assertEqual(self.review.totals["calls"], 1)
        self.assertEqual(self.review.totals["unknown_calls"], 1)

    def test_all_executor_paths_load_the_review_configuration(self):
        from loopcut import cloud
        cfg = model()
        review = config.Controller(cfg)
        with patch.object(cloud, "sync_settings"), patch.object(config, "spending", return_value=spending.Settings()), \
                patch.object(config, "stall_seconds", return_value=0), \
                patch.object(agent, "_turn_config", return_value=cfg), \
                patch.object(agent, "_keep_mode", side_effect=lambda c: c), \
                patch.object(cloud, "prompt_index_available", return_value=True), \
                patch.object(config, "controller", return_value=review):
            for orchestrated, workspace, app, expected in (
                    (False, False, None, agent.SINGLE_SPEC), (False, True, None, agent.WORKSPACE_SPEC),
                    (True, False, None, agent.ORCH_SPEC), (False, False, config.CodexApp("codex"), agent.CODEX_SPEC)):
                session, turn = state.new_session(), agent.Turn()
                with patch.object(config, "codex_app", return_value=app), patch.object(config, "load_roles", return_value=
                        config.Roles(cfg, cfg, orchestrated, workspace=workspace)):
                    agent._configure(turn, session)
                self.assertIs(turn.spec, expected)
                self.assertIs(turn.review_config, review)

    def test_stable_prefix_compact_state_and_separate_budgets(self):
        self.session["messages"].append({"role": "assistant", "content": "private reasoning " * 2000})
        self.turn.job = types.SimpleNamespace(budget=waves.JobBudget(0.4, 100))
        self.turn.job.budget.add("fast", {"input": 1000, "output": 500})
        for event in ("finish", "paid_action"):
            self.review.review(event, "candidate", paid())
        first, second = self.requests
        self.assertEqual(first["messages"][0], second["messages"][0])
        self.assertEqual(first["tools"], second["tools"])
        packet = json.loads(second["messages"][1]["content"])
        self.assertNotIn("private reasoning", json.dumps(packet))
        self.assertEqual(packet["budget"]["paid_tool_credits"]["credits"], 0)
        self.assertEqual(packet["budget"]["worker_inference_usd"]["limit"], 0.4)
        self.assertEqual(packet["budget"]["review_inference"]["calls"], 1)
        self.assertIsNone(packet["budget"]["executor_own_key_usage"]["remaining_usd"])
        self.assertEqual(self.session["usage_by_role"]["controller"]["requests"], 2)

    def test_spending_call_time_and_inspection_limits(self):
        self.make_review(max_calls=1).review("finish")
        with self.assertRaises(controller.ReviewError):
            self.review.review("finish")
        self.assertEqual(len(self.requests), 1)
        # Resume retains usage; it cannot turn an exhausted review budget into another round of calls.
        resumed = self.make_review(max_calls=1)
        with self.assertRaises(controller.ReviewError):
            resumed.review("finish")
        self.assertEqual(len(self.requests), 1)

    def test_reported_charge_unknown_charge_and_timeout_are_recorded(self):
        def stream(**kwargs):
            completion = self.stream(**kwargs)
            completion.usage = {"cost": 0.2, "input": 100, "output": 50}
            return completion
        review = self.make_review(stream)
        review.review("finish")
        self.assertEqual(review.totals["reported_usd"], 0.2)
        with self.assertRaises(controller.ReviewError):
            review.review("finish")
        review.totals.update(calls=0, reported_usd=0)
        review.stream = Mock(side_effect=llm.LLMError("private provider details"))
        with self.assertRaisesRegex(controller.ReviewError, "unavailable"):
            review.review("finish")
        self.assertEqual(review.totals["unknown_calls"], 1)
        review.stream = Mock(side_effect=llm.Cancelled())
        with self.assertRaisesRegex(controller.ReviewError, "time limit"):
            review.review("finish")

    def test_corrupt_resume_budget_does_not_reset(self):
        self.session["messages"][0][controller.RECORD] = '{"totals":{}}'
        with self.assertRaises(config.ConfigError):
            self.make_review()

    def test_config_is_opt_in_with_bounded_same_provider_defaults(self):
        with patch.object(config, "_getter", return_value=(None, lambda k, default="": default)):
            self.assertIsNone(config.controller(model()))
        def configured(**extra):
            values = {"LOOPCUT_CONTROLLER": "on", **extra}
            return patch.object(config, "_getter", return_value=(None, lambda k, default="": values.get(k, default)))
        with configured():
            settings = config.controller(model())
            self.assertEqual(settings.model.base_url, model().base_url)
            self.assertEqual(settings.model.max_output_tokens, 1200)
        for value in ("NaN", "inf", "0", "-1"):
            with configured(LOOPCUT_CONTROLLER_MAX_USD=value), self.assertRaises(config.ConfigError):
                config.controller(model())


class ConsentReviewTest(ControllerFixture, unittest.TestCase):
    # Keep these independent of Blender's UI and use the actual permission and reservation code.
    def setUp(self):
        super().setUp()
        p = patch.object(agent, "_redraw")
        p.start()
        self.addCleanup(p.stop)

    def consent(self, turn=None):
        return agent._Consent(self.session, turn or self.turn, state.item_tool("image_generate", "Make a picture", "{}"))

    def test_alternative_cannot_spend_and_parent_reviews_worker_cost(self):
        self.review.stream = lambda **kw: llm.Completion(text=json.dumps(answer(
            json.loads(kw["messages"][1]["content"]), "alternative", reason="The existing asset fits.")))
        worker = agent.Turn()
        worker.chat, worker.worker = (self.session, self.turn), "Model worker"
        with self.assertRaises(cloud_http.CloudError) as raised:
            self.consent(worker)(paid())
        self.assertEqual(raised.exception.code, "controller_alternative")
        self.assertEqual(self.turn.paid_calls, 0)
        self.assertEqual(spending.ledger(self.session)["credits"], 0)

    def test_review_approval_preserves_permission_and_only_asked_refusal(self):
        self.turn.spending = spending.Settings(ask_every=True)
        consent = self.consent()
        with patch.object(consent, "_ask", return_value=False) as ask:
            self.assertFalse(consent(paid()))
        self.assertEqual(ask.call_args.args[0], spending.COST)
        self.assertEqual(self.turn.paid_calls, 0)
        self.turn.spending = spending.Settings(extras=spending.ONLY_ASKED)
        self.review.stream = Mock(side_effect=AssertionError("forbidden calls must not be reviewed"))
        with self.assertRaises(cloud_http.CloudError):
            self.consent()(paid())
        self.review.stream.assert_not_called()

    def test_value_question_is_priced_and_does_not_duplicate_cost_approval(self):
        self.review.stream = lambda **kw: llm.Completion(text=json.dumps(answer(
            json.loads(kw["messages"][1]["content"]), "user_decision", reason="A library asset may be enough.")))
        for settings, kind in ((spending.Settings(), spending.VALUE), (spending.Settings(ask_every=True), spending.COST)):
            self.turn.spending = settings
            consent = self.consent()
            with patch.object(consent, "_ask", return_value=True) as ask:
                self.assertTrue(consent(paid()))
            ask.assert_called_once()
            self.assertEqual(ask.call_args.args[0], kind)
            self.assertEqual(ask.call_args.args[1]["credits"], 20)
            self.assertIn("library asset", ask.call_args.args[1]["value_reason"])

    def test_stale_approval_reserves_nothing(self):
        self.turn.spending = spending.Settings(ask_every=True)
        consent = self.consent()
        def approve(*args):
            self.scene["version"] += 1
            return True
        with patch.object(consent, "_ask", side_effect=approve), self.assertRaises(cloud_http.CloudError) as raised:
            consent(paid())
        self.assertEqual(raised.exception.code, "controller_stale")
        self.assertEqual(spending.ledger(self.session)["credits"], 0)

    def test_concurrent_reviews_recheck_and_reserve_atomically(self):
        barrier = threading.Barrier(2)
        self.turn.spending = spending.Settings(chat_budget=30)
        self.turn.controller = types.SimpleNamespace(meaningful=lambda d: True, current=lambda d: True,
            review=lambda *args: (barrier.wait(timeout=3), types.SimpleNamespace(action="approve"))[1])
        outcomes, errors = [], []
        def run():
            try:
                consent = self.consent()
                with patch.object(consent, "_ask", return_value=False):
                    outcomes.append(consent(paid()))
            except Exception as ex:
                errors.append(ex)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(spending.ledger(self.session)["credits"], 20)
        self.assertEqual(self.turn.paid_calls, 1)


class DeliveryReviewTest(ControllerFixture, unittest.TestCase):
    def test_final_text_is_private_until_accepted_but_progress_is_published(self):
        spec = agent.LoopSpec("single", lambda s: "", lambda: [], lambda s: None)
        def stream(**kw):
            kw["on_text"]("Done before review.")
            self.assertFalse(any(i["kind"] == "assistant" for i in self.session["items"]))
            return llm.Completion(text="Done before review.", usage={"input": 10, "cached": 0, "output": 5})
        with patch.object(llm, "stream_chat", side_effect=stream), patch.object(agent, "_redraw"):
            completion = agent._request(self.session, self.turn, model(), spec)
        self.assertFalse(any(i["kind"] == "assistant" for i in self.session["items"]))
        self.session["messages"].append(completion.as_message())
        self.assertIsNone(agent._review_finish(self.session, self.turn, completion))
        agent._publish_reply(self.session, self.turn)
        self.assertEqual(self.session["items"][-1]["text"], "Done before review.")

    def test_rejected_completion_cannot_leak_through_error_cancel_or_limit(self):
        self.turn.pending_reply = {**state.item_assistant(), "text": "Everything is perfect."}
        self.session["messages"].append({"role": "assistant", "content": "Everything is perfect."})
        self.review.stream = Mock(side_effect=llm.LLMError("unavailable"))
        agent._review_finish(self.session, self.turn, llm.Completion(text="Everything is perfect."))
        agent._publish_reply(self.session, self.turn)
        self.assertEqual(self.turn.outcome, "incomplete")
        self.assertNotIn("Everything is perfect", str(self.session["items"]))
        self.assertIn("unavailable", self.session["items"][-1]["text"])
        self.turn.pending_reply = {**state.item_assistant(), "text": "Another draft."}
        self.turn.cancel.set()
        agent._publish_reply(self.session, self.turn)
        self.assertNotIn("Another draft", str(self.session["items"]))

    def test_recovery_can_change_approach_once_without_resetting_hard_limits(self):
        self.review.stream = lambda **kw: llm.Completion(text=json.dumps(answer(
            json.loads(kw["messages"][1]["content"]), "change_approach", reason="This API failed repeatedly.")))
        note = agent._review_failure(self.session, self.turn)
        self.assertIn("API failed repeatedly", note)
        self.assertIsNone(agent._review_failure(self.session, self.turn, hard_stop=True))
        self.assertEqual(self.review.failures, 1)

    def test_correction_limit_reports_incomplete_without_publishing_claim(self):
        self.session["items"].append({"kind": "tool", "name": "get_scene_info", "output": "Cube is blue."})
        self.turn.pending_reply = {**state.item_assistant(), "text": "The red cube is done."}
        self.review.stream = lambda **kw: llm.Completion(text=json.dumps(answer(
            json.loads(kw["messages"][1]["content"]), "correct", reason="The cube is blue.", findings=[
                {"quote": "red cube", "issue": "Wrong colour", "evidence": "e1"}])))
        self.review.corrections = self.review.settings.max_corrections
        self.assertIsNone(agent._review_finish(self.session, self.turn, llm.Completion(text="The red cube is done.")))
        agent._publish_reply(self.session, self.turn)
        self.assertEqual(self.turn.outcome, "incomplete")
        self.assertNotIn("red cube is done", self.session["items"][-1]["text"])


if __name__ == "__main__":
    unittest.main()
