"""A blind report must keep its identity key and not reveal the arm in image URLs."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness" / "evals"))
import compare


class BlindReportTest(unittest.TestCase):
    def test_judge_receives_camera_and_frame_labels_for_every_image(self):
        import judge
        with tempfile.TemporaryDirectory() as directory:
            arm = Path(directory)
            payload = {"renders": ["hero.png", "close.png"], "render_evidence": [
                {"file": "hero.png", "camera": "Hero", "frame": 1, "engine": "CYCLES"},
                {"file": "close.png", "camera": "CloseUp", "frame": 60, "engine": "CYCLES"}]}
            (arm / "test.grade.json").write_text(json.dumps(payload))
            _, frames = judge.load(arm, "test")
            self.assertEqual(len(frames), 2)
            self.assertIn("camera Hero, frame 1, CYCLES", frames[0][0])
            self.assertIn("camera CloseUp, frame 60, CYCLES", frames[1][0])

    def test_multiple_tasks_have_a_key_and_anonymous_images(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            arms, tasks = ["loopcut", "competitor"], ["task_one", "task_two"]
            grades = {}
            for arm in arms:
                (out / arm).mkdir()
                grades[arm] = {}
                for task in tasks:
                    file = f"{task}.png"
                    (out / arm / file).write_bytes(arm.encode())
                    grades[arm][task] = {"renders": [file], "stage_renders": [file]}
            with patch.object(compare, "prompt_of", return_value="brief"):
                compare.write_blind(out, arms, tasks, grades)
            key = json.loads((out / "blind_key.json").read_text())
            self.assertEqual(set(key), set(tasks))
            report = (out / "blind.md").read_text()
            for arm in arms:
                self.assertNotIn(f"({arm}/", report)
            for task, columns in key.items():
                for label, arm in columns.items():
                    artifact = out / "blind_assets" / f"{task}.{label.lower()}.renders.1.png"
                    self.assertEqual(artifact.read_bytes(), arm.encode())


if __name__ == "__main__":
    unittest.main()
