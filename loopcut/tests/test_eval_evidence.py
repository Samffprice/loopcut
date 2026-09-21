"""Regression evidence must compare the same brief/checks, and only continue finished turns."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness" / "evals"))
import evidence
import run


class EvidenceTest(unittest.TestCase):
    def test_source_fingerprint_detects_content_and_path_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tasks.py"
            source.write_text("original brief")
            old = evidence.fingerprint([source], root)
            source.write_text("new brief")
            self.assertNotEqual(old, evidence.fingerprint([source], root))
            moved = root / "showcase.py"
            source.rename(moved)
            self.assertNotEqual(old, evidence.fingerprint([moved], root))

    def test_unknown_or_changed_contract_is_not_called_a_regression(self):
        previous = {"started": "before", "model": "same", "label": "single turn",
                    "results": [{"id": "perfume_ad", "passed": True}]}
        current = {"model": "same", "label": "two turns", "wall_seconds": 1,
                   "results": [{"id": "perfume_ad", "passed": False,
                                "manifest": {"task_contract": "two-turn-contract"}}]}
        report = run.summarize(current, previous)
        self.assertIn("Not comparable", report)
        self.assertIn("lost nothing", report)
        previous["results"][0]["manifest"] = {"task_contract": "two-turn-contract"}
        self.assertIn("lost ['perfume_ad']", run.summarize(current, previous))

    def test_failed_first_turn_never_starts_followup(self):
        self.assertTrue(evidence.follow_up_allowed(True, "completed"))
        for outcome in ("error", "step_limit", "cancelled", "timeout", "approval_required", "unknown"):
            self.assertFalse(evidence.follow_up_allowed(True, outcome), outcome)
        self.assertFalse(evidence.follow_up_allowed(False, "completed"))

    def test_manifest_never_serializes_credentials(self):
        from types import SimpleNamespace as NS
        package = Path(__file__).resolve().parents[2] / "scripts" / "addons_core" / "loopcut"
        cfg = NS(api_key="secret-key", base_url="https://user:password@example.com", model="model",
                 reasoning_effort="medium", max_steps=25, context_budget=24000, auto_run=True, auto_look=True)
        manifest = evidence.manifest(NS(id="test", prompt="brief", follow_up=""), cfg,
                                     NS(app=NS(version_string="5.2", build_hash=b"abc", binary_path="blender")),
                                     package, 60, False)
        self.assertNotIn("secret-key", str(manifest))
        self.assertNotIn("password", str(manifest))
        self.assertEqual(manifest["config"]["max_steps"], 25)

    def test_comparison_rejects_different_and_unknown_task_revisions(self):
        import json
        from types import SimpleNamespace as NS
        task = NS(id="perfume", prompt="two cameras", follow_up="make it pink")
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with self.assertRaisesRegex(ValueError, "no task manifest"):
                evidence.validate_arm(folder, task)
            self.assertIn("UNVERIFIED", evidence.validate_arm(folder, task, True))
            (folder / "perfume.prompt.txt").write_text("one camera")
            with self.assertRaisesRegex(ValueError, "prompt differs"):
                evidence.validate_arm(folder, task, True)
            manifest = evidence.task_manifest(task)
            (folder / "perfume.manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "prompt differs"):
                evidence.validate_arm(folder, task)
            (folder / "perfume.prompt.txt").write_text(task.prompt)
            self.assertEqual(evidence.validate_arm(folder, task), "")
            task.follow_up = "make it blue"
            with self.assertRaisesRegex(ValueError, "differ"):
                evidence.validate_arm(folder, task, True)


if __name__ == "__main__":
    unittest.main()
