"""The setup's free Try it calls (try_samples): which calls they take over, the job they apply and how they play."""
import hashlib
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import cloud, cloud_account, cloud_jobs, try_samples  # noqa: E402

MOTION = {"name": "motion_from_text", "kind": "gen", "apply": "apply_motion",
          "apply_args": {"type": "object", "additionalProperties": False,
                         "properties": {"target_armature": {"type": "string"}}}}
MODEL = {"name": "image_to_3d", "kind": "gen", "apply": "import_model"}


class SampleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.photo = self.tmp / "upload.jpg"
        shutil.copyfile(try_samples.SAMPLE_PHOTO, self.photo)

    def test_the_shipped_outputs_are_there(self):
        for sample in try_samples.CARDS.values():
            for output in sample["outputs"]:
                self.assertTrue((try_samples.SAMPLES / output[3]).is_file(), output[3])
        # Ships in every installer and update: the 300K-triangle dwarf (owner 2026-10-23) is 10.4 MiB.
        self.assertLess((try_samples.SAMPLES / "photo_3d.glb").stat().st_size, 12 * 1024 * 1024)

    def test_only_the_cards_own_tool_and_photo_are_taken_over(self):
        self.assertEqual(try_samples.match("photo_3d", "image_to_3d", {"image": str(self.photo)})["card"], "photo_3d")
        self.assertEqual(try_samples.match("motion", "motion_from_text", {})["tool"], "motion_from_text")
        other = self.tmp / "other.jpg"
        other.write_bytes(b"not the sample photo")
        self.assertIsNone(try_samples.match("photo_3d", "image_to_3d", {"image": str(other)}))
        self.assertIsNone(try_samples.match("photo_3d", "image_to_3d", {}))
        self.assertIsNone(try_samples.match("photo_3d", "motion_from_text", {}))  # Another tool in the same turn.
        self.assertIsNone(try_samples.match("", "image_to_3d", {"image": str(self.photo)}))  # Not a card's turn.
        self.assertIsNone(try_samples.match("model", "image_to_3d", {"image": str(self.photo)}))

    def test_the_attached_photo_is_passed_by_its_name(self):
        # The model sees an attachment's name, never its path (0.2.8: "Scene reference or file was not found", then a
        # search of the disk and a paid text-only call). The name resolves to the attached file, which the card matches.
        from loopcut import conversations, state
        stored = self.tmp / "images" / "abc.png"
        stored.parent.mkdir()
        stored.write_bytes(b"stored copy")
        session = {"id": "c1", "references": [
            {"ref": "loopcut-image:abc.png", "full": "loopcut-image:abc.png", "name": "dwarf-warrior.jpg", "pinned": True,
             "source": str(try_samples.SAMPLE_PHOTO)},
            {"ref": "loopcut-image:abc.png", "name": "gone.jpg", "pinned": True, "source": str(self.tmp / "gone.jpg")}]}
        fake = types.SimpleNamespace(data=types.SimpleNamespace(images={}, objects={}))
        with patch.object(state, "_session", session), patch.dict(sys.modules, {"bpy": fake}), \
                patch.object(conversations, "image_path", return_value=stored):
            self.assertEqual(cloud_jobs.attached_image("dwarf-warrior.jpg"), try_samples.SAMPLE_PHOTO)
            self.assertEqual(cloud_jobs.attached_image("gone.jpg"), stored)  # The original moved: the stored copy.
            self.assertIsNone(cloud_jobs.attached_image("other.jpg"))
            files = cloud_jobs.resolve_files({"file_inputs": [{"field": "image", "required": False,
                                                               "accept": ["image/jpeg", "image/png"]}]},
                                             {"image": "dwarf-warrior.jpg"}, self.tmp / "job")
        self.assertEqual(try_samples.match("photo_3d", "image_to_3d", files)["card"], "photo_3d")

    def test_a_build_without_the_files_runs_the_ordinary_call(self):
        with patch.object(try_samples, "SAMPLES", self.tmp / "missing"):
            self.assertIsNone(try_samples.match("motion", "motion_from_text", {}))

    def test_the_job_passes_the_local_job_checks_with_nothing_charged(self):
        sample = try_samples.match("motion", "motion_from_text", {})
        job = try_samples.job(sample, "gen", {"prompt": "jump"}, {"target_armature": "Armature"})
        clean = cloud_jobs._job(job, local=True)  # What apply_result checks first.
        self.assertEqual(clean["credits"], {"reserved": 0, "charged": 0, "refunded": 0})
        self.assertEqual(clean["status"], "succeeded")
        self.assertEqual([o["name"] for o in clean["outputs"]], ["motion.bvh", "motion.json"])
        bvh = clean["outputs"][0]
        self.assertEqual(bvh["sha256"], hashlib.sha256(Path(bvh["path"]).read_bytes()).hexdigest())
        self.assertEqual(job["input"], {"prompt": "jump"})
        self.assertEqual(job["apply_args"], {"target_armature": "Armature"})

    def test_runs_show_the_servers_messages_and_the_photo_sample_lasts_30_seconds(self):
        final = try_samples.timeline("image_to_3d", {"quality": "final"})
        self.assertEqual(final[0], (0.0, 5, "Queued for model processing."))
        self.assertEqual(final[-1][0], 30.0)
        self.assertGreaterEqual(final[-2][1], 90)  # The climb still nearly finishes before Done.
        running = final[1:-1]
        self.assertTrue(all(m == "Generating model." and 10 <= p <= 95 for _, p, m in running))
        self.assertEqual([p for _, p, _ in running], sorted(p for _, p, _ in running))
        self.assertEqual(try_samples.timeline("image_to_3d", {"quality": "draft"})[-1][0], 30.0)
        self.assertEqual([m for _, _, m in try_samples.timeline("motion_from_text", {})],
                         ["Queued", "Generating motion", "Done"])

    def test_play_shows_each_step_at_its_time_and_stops_when_cancelled(self):
        now, shown = [0.0], []

        def sleep(seconds):
            now[0] += seconds
        steps = [(0.0, 0, "Queued"), (2.0, 5, "Generating motion"), (3.0, 100, "Done")]
        self.assertTrue(try_samples.play(steps, shown.append, lambda: False, lambda: now[0], sleep))
        self.assertEqual(shown, [{"pct": 0, "message": "Queued"}, {"pct": 5, "message": "Generating motion"}])
        self.assertGreaterEqual(now[0], 3.0)
        now[0], shown[:] = 0.0, []
        self.assertFalse(try_samples.play(steps, shown.append, lambda: now[0] > 1.0, lambda: now[0], sleep))
        self.assertEqual(shown, [{"pct": 0, "message": "Queued"}])

    def test_the_call_says_free_on_its_card_and_applies_the_sample_like_a_job(self):
        sample = try_samples.match("motion", "motion_from_text", {})
        card = {"name": "loopcut_cloud.motion_from_text"}
        consent = types.SimpleNamespace(shown=[card])
        applied = []
        with patch.object(try_samples, "play", return_value=True), \
                patch.object(cloud, "_apply_to_scene", side_effect=lambda m, d, job, s: applied.append(job) or "done"):
            result = cloud._sample_call("loopcut_cloud.motion_from_text", MOTION, cloud_jobs,
                                        {"prompt": "jump", "target_armature": "Armature"}, sample, None)
            self.assertEqual(result.text, "Preparing cloud job…")
            with cloud_account.approval_context(consent):
                self.assertEqual(result.then(lambda: False, lambda f: f()), "done")
        self.assertEqual(card["cost"], "Free")
        self.assertEqual(applied[0]["input"], {"prompt": "jump"})  # Client-only arguments stay apply arguments.
        self.assertEqual(applied[0]["apply_args"], {"target_armature": "Armature"})
        self.assertEqual(applied[0]["credits"]["charged"], 0)

    def test_a_cancelled_call_applies_nothing(self):
        sample = try_samples.match("photo_3d", "image_to_3d", {"image": str(self.photo)})
        with patch.object(try_samples, "play", return_value=False), \
                patch.object(cloud, "_apply_to_scene") as apply:
            result = cloud._sample_call("loopcut_cloud.image_to_3d", MODEL, cloud_jobs, {}, sample, None)
            outcome = result.then(lambda: True, lambda f: f())
        self.assertFalse(outcome.ok)
        apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
