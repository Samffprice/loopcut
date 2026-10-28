"""Goal/quality behavior with scripted judgments; these do not benchmark model taste or realism."""

import copy
import json
import unittest
from unittest.mock import patch

from test_controller import ControllerFixture, answer, paid
from loopcut import agent, controller, llm, spending, state


def human_quality(*, rigged=False, optional=False):
    return {
        "goal": "A convincing human character that is useful in the user's game.",
        "intended_use": "A character that can be posed and animated in a game.", "artifact": True,
        "inferences": [{"intent": "The character should be poseable", "confidence": "high",
                        "basis": "A game character is likely to need animation, though the user did not say rig."}],
        "dimensions": [
            {"name": "appearance", "kind": "visual", "target": "Convincing proportions and materials",
             "status": "meets", "evidence": ["read1"], "observation": "The viewed character has consistent proportions."},
            {"name": "usability", "kind": "functional", "target": "A rig with tested deformation",
             "status": "meets" if rigged else "not_applicable" if optional else "needs_work",
             "evidence": ["read2"], "observation": "Pose tests passed." if rigged else
             "No armature exists; rigging could make this character useful for animation."}],
        "improvements": [] if rigged else [
            {"dimension": "usability", "basis": "inferred", "support": "The character should be poseable",
             "change": "Add a simple rig and test its deformation.", "benefit": "The user can pose and animate it.",
             "confidence": "high", "risk": "low", "effort": "medium", "cost": "none", "scope": "capability",
             "disposition": "ask" if optional else "do"}],
    }


def human_review_reply(kwargs, *, rigged):
    """A scripted reviewer asks for two kinds of evidence, then judges the simulated character."""
    packet = json.loads(kwargs["messages"][1]["content"])
    value = human_quality(rigged=rigged)
    if packet["event"] == "intent":
        value["improvements"] = []
        for row in value["dimensions"]:
            row.update(status="unverified", evidence=[], observation="Inspect this as the work progresses.")
        return llm.Completion(text=json.dumps(answer(packet, quality=value)))
    if len(kwargs["messages"]) == 2:
        return llm.Completion(tool_calls=[llm.ToolCall("view", "review_view", '{"angle":"sheet"}'),
                                          llm.ToolCall("objects", "review_objects", '{"names":["Human"]}')])
    action = ("present" if packet["event"] == "finish" else "continue") if rigged else "improve"
    return llm.Completion(text=json.dumps(answer(packet, action, quality=value)))


class QualityTest(ControllerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.session["items"][0]["text"] = "Make a human character for my game."
        self.session["messages"][0]["content"] = self.session["items"][0]["text"]

    def decision(self, value, action="improve", event="finish", visual=None):
        packet = self.review.packet(event, "Candidate", None)
        return controller.parse(json.dumps(answer(packet, action, quality=value)), packet,
                                {"read1", "read2"}, visual_ids={"read1"} if visual is None else visual)

    def test_inferred_rigging_can_drive_improvement_without_a_literal_rig_request(self):
        decision = self.decision(human_quality())
        self.assertEqual(decision.action, "improve")
        self.assertIn("Inferred (high confidence)", decision.note())
        self.assertIn("Add a simple rig", decision.note())
        self.assertIn("Pursue within existing limits", decision.note())

    def test_object_existence_or_a_reference_image_does_not_certify_appearance(self):
        for evidence in (set(), {"reference_image"}):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(controller.ReviewError, "current result"):
                self.decision(human_quality(rigged=True), "present", visual=evidence)

    def test_looking_good_or_having_bones_is_not_enough_to_claim_ready(self):
        value = human_quality(rigged=True)
        value["dimensions"][1].update(status="unverified", observation="An armature exists, but no pose was tested.")
        with self.assertRaisesRegex(controller.ReviewError, "unresolved quality"):
            self.decision(value, "present")
        self.assertEqual(self.decision(value, "incomplete").action, "incomplete")

    def test_both_visual_and_functional_quality_must_be_considered_for_an_asset(self):
        value = human_quality(rigged=True)
        value["dimensions"].pop()
        with self.assertRaisesRegex(controller.ReviewError, "fitness for use"):
            self.decision(value, "present")

    def test_unknown_or_costly_scope_cannot_become_an_automatic_improvement(self):
        for field, choice in (("risk", "high"), ("effort", "large"), ("cost", "paid"), ("cost", "unknown"),
                              ("scope", "deliverable"), ("confidence", "medium")):
            value = human_quality()
            value["improvements"][0][field] = choice
            with self.subTest(field=field, choice=choice), self.assertRaisesRegex(controller.ReviewError, "user decision"):
                self.decision(value)
        value = human_quality()
        value["inferences"][0]["confidence"] = "low"
        with self.assertRaisesRegex(controller.ReviewError, "user decision"):
            self.decision(value)

    def test_uncertain_capability_is_a_proposal_and_not_a_requirement_to_finish(self):
        value = human_quality(optional=True)
        value["intended_use"] = "A static portrait; future animation is uncertain."
        value["inferences"][0]["confidence"] = "low"
        value["improvements"][0].update(cost="paid", risk="medium", confidence="low")
        decision = self.decision(value, "present")
        self.assertIn("Propose to user; do not execute", decision.note())

    def test_quality_cannot_be_omitted_and_improvements_need_an_explained_basis(self):
        for change in (lambda q: q.update(inferences=[]),
                       lambda q: q["improvements"][0].update(basis="explicit", support="rig it"),
                       lambda q: q["improvements"][0].update(dimension="invented")):
            value = human_quality()
            change(value)
            with self.assertRaises(controller.ReviewError):
                self.decision(value)
        with self.assertRaises(controller.ReviewError):
            self.decision(None, "present")

    def test_progress_cannot_hide_an_unbudgeted_improvement_in_continue(self):
        with self.assertRaisesRegex(controller.ReviewError, "explicitly choose"):
            self.decision(human_quality(), "continue", "progress")

    def test_review_must_actually_receive_the_scene_image_it_cites(self):
        value = human_quality(rigged=True)
        for supplied_image in (None, {"role": "user", "content": "scripted image payload"}):
            with self.subTest(image=supplied_image):
                def stream(**kw):
                    packet = json.loads(kw["messages"][1]["content"])
                    if len(kw["messages"]) == 2:
                        return llm.Completion(tool_calls=[llm.ToolCall("view", "review_view", '{"angle":"sheet"}'),
                                                          llm.ToolCall("objects", "review_objects", '{"names":["Human"]}')])
                    return llm.Completion(text=json.dumps(answer(packet, "present", quality=value)))
                self.inspect.side_effect = lambda name, args: ("Observed evidence", supplied_image if name == "review_view" else None)
                review = self.make_review(stream)
                if supplied_image:
                    self.assertEqual(review.review("finish").action, "present")
                else:
                    with self.assertRaisesRegex(controller.ReviewError, "current result"):
                        review.review("finish")

    def test_quality_targets_improvement_history_and_limits_survive_resume(self):
        decision = self.decision(human_quality())
        self.assertTrue(self.review.take_improvement(decision))
        self.assertFalse(self.review.take_improvement(decision), "Do not repeat the same optional polish")
        self.review.goal_state = {"assessment": decision.quality, "scene_version": decision.scene_version,
                                  "instruction_version": decision.instruction_version,
                                  "evidence": ["read1", "read2"], "visual": ["read1"]}
        self.review.save()
        resumed = self.make_review(max_improvements=1)
        self.assertEqual(resumed.packet("finish", "", None)["previous_quality"]["assessment"], decision.quality)
        self.assertIn("Historical quality context", resumed.goal_context())
        self.assertNotIn("Pursue within existing limits", resumed.goal_context())
        changed = copy.deepcopy(decision.quality)
        changed["improvements"][0]["change"] = "Add hand controls."
        self.assertFalse(resumed.take_improvement(self.decision(changed)))
        self.session["items"].append(state.item_user("Keep it static; no rig."))
        self.assertEqual(resumed.goal_context(), "")

    def test_goal_is_revisited_on_steering_and_progress_is_bounded_with_finish_reserve(self):
        review = self.make_review(max_calls=6, check_every=2, max_checkpoints=1)
        self.assertTrue(review.checkpoint_due())
        self.assertFalse(review.checkpoint_due())
        self.assertFalse(review.checkpoint_due(progress=True))
        self.assertTrue(review.checkpoint_due(progress=True))
        self.assertFalse(review.checkpoint_due(progress=True))
        self.assertFalse(review.checkpoint_due(progress=True))
        self.session["items"].append(state.item_user("Keep it static. Don't add a rig."))
        self.assertTrue(review.checkpoint_due())
        review.review("intent")
        review.review("intent")
        self.assertFalse(review.checkpoint_due(), "Preserve four calls for final review")
        self.assertEqual(review.review("finish").action, "present")

    def test_multi_read_checkpoints_cannot_consume_the_final_review_reserve(self):
        review = self.make_review(max_calls=5)
        review.stream = lambda **kw: llm.Completion(tool_calls=[llm.ToolCall("read", "review_scene", "{}")])
        with self.assertRaisesRegex(controller.ReviewError, "limit"):
            review.review("progress")
        self.assertEqual(review.totals["calls"], 1)

    def test_optional_paid_rigging_choice_reaches_the_final_user_message(self):
        value = human_quality(optional=True)
        value["improvements"][0]["cost"] = "paid"
        self.turn.pending_reply = {**state.item_assistant(), "text": "The static character is ready."}
        self.session["messages"].append({"role": "assistant", "content": "The static character is ready."})
        with patch.object(self.review, "review", return_value=self.decision(value, "present")):
            self.assertIsNone(agent._review_finish(self.session, self.turn, llm.Completion(text="Done")))
        agent._publish_reply(self.session, self.turn)
        self.assertIn("Add a simple rig", self.session["items"][-1]["text"])
        self.assertIn("go-ahead", self.session["items"][-1]["text"])

    def test_inferred_improvement_is_executed_at_a_checkpoint_and_stops_at_its_limit(self):
        self.make_review(check_every=1, max_improvements=1)
        decision = self.decision(human_quality(), event="progress")
        with patch.object(self.review, "review", return_value=decision):
            first = agent._review_goal(self.session, self.turn, progress=True)
            second = agent._review_goal(self.session, self.turn, progress=True)
        self.assertIn("Add a simple rig", first)
        self.assertIn("Do not repeat optional polish", second)
        self.assertNotIn("Add a simple rig", second)

    def test_a_supposedly_free_inferred_improvement_cannot_silently_charge_even_below_review_threshold(self):
        self.review.take_improvement(self.decision(human_quality()))
        consent = agent._Consent(self.session, self.turn, state.item_tool("image_generate", "Improve skin", "{}"))
        with patch.object(consent, "_ask", return_value=False) as ask:
            self.assertFalse(consent(paid(6)))
        self.assertEqual(ask.call_args.args[0], spending.VALUE)
        self.assertEqual(ask.call_args.args[1]["credits"], 6)
        self.assertIn("inferred", ask.call_args.args[1]["value_reason"])
        self.assertEqual(spending.ledger(self.session)["credits"], 0)

    def test_one_pass_records_all_attempted_changes_and_does_not_repeat_a_partial_bundle(self):
        value = human_quality()
        extra = copy.deepcopy(value["improvements"][0])
        extra["change"] = "Add hand controls."
        value["improvements"].append(extra)
        self.assertTrue(self.review.take_improvement(self.decision(value)))
        self.assertEqual(self.review.improvement_passes, 1)
        self.assertEqual(len(self.review.improvements), 2)
        value["improvements"] = [extra]
        self.assertFalse(self.review.take_improvement(self.decision(value)))


if __name__ == "__main__":
    unittest.main()
