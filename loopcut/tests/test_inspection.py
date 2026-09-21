"""Inspection boundaries, asynchronous waiting and attachment-time freshness."""
import sys
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import agent, inspection, jobs, llm, tools


class InspectionTests(unittest.TestCase):
    def test_request_limits_bound_the_total_work(self):
        inspection.validate_request("final_lighting", ["A", "B"], [1, 60, 120], ["Cube"], 384, 16, 90)
        invalid = [dict(purpose="unknown"), dict(cameras=["a"] * 2), dict(frames=[True]),
                   dict(frames=[1, 1]), dict(frames=[]), dict(focus="Cube"), dict(samples=65),
                   dict(max_side=641), dict(budget_seconds=181),
                   dict(cameras=["A", "B", "C", "D"], frames=[1, 2, 3, 4])]
        defaults = dict(purpose="geometry", cameras=[], frames=[1], focus=[], max_side=384, samples=16, budget_seconds=90)
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(jobs.JobError):
                inspection.validate_request(**(defaults | override))

    def test_inspection_requires_one_bounded_approval(self):
        self.assertEqual(agent._approval("inspect_scene", '{"purpose":"geometry"}', ()), "heavy")
        self.assertFalse(agent._changes_scene("inspect_scene"))

    def test_cancel_wait_sends_cancel_without_blocking_the_main_thread(self):
        with patch.object(jobs, "cancel") as cancel, self.assertRaises(llm.Cancelled):
            inspection.wait_result(Path("job"), lambda: True, lambda fn: self.fail("must not run on main"))
        cancel.assert_called_once_with(Path("job"))

    def test_wait_schedules_only_final_freshness_check_on_main(self):
        result = tools.ToolResult("verified")
        calls = []
        def on_main(fn):
            calls.append(fn)
            future = Future()
            future.set_result(fn())
            return future
        with patch.object(jobs, "status", return_value={"state": "complete"}), patch.object(inspection, "finish", return_value=result):
            self.assertIs(inspection.wait_result(Path("job"), lambda: False, on_main), result)
        self.assertEqual(len(calls), 1)

    def test_later_mutation_in_same_batch_omits_old_inspection_image(self):
        card = {"status": "done", "output": "inspection from revision 1"}
        message = {"role": "tool", "content": "inspection from revision 1"}
        with patch.object(agent, "_inspection_current", return_value=False):
            images = agent._fresh_images([("image", "look", {"generation": 1}, card, message)])
        self.assertEqual(images, [])
        self.assertEqual(card["status"], "failed")
        self.assertIn("omitted", message["content"])

    def test_fresh_and_historical_noninspection_images_are_kept(self):
        with patch.object(agent, "_inspection_current", return_value=True):
            self.assertEqual(agent._fresh_images([("a", "look", {}, {}, {}), ("b", "last render", None, {}, {})]),
                             [("a", "look"), ("b", "last render")])

    def test_inspection_does_not_change_automatic_viewport_angle(self):
        self.assertEqual(agent._asked_angle("inspect_scene", '{"cameras":["Side"]}'), "")


if __name__ == "__main__":
    unittest.main()
