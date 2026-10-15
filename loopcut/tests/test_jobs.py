"""Durability, ownership and corrupted-output regressions; real Blender runs jobs_check.py."""
import json
import os
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
fake_bpy = types.ModuleType("bpy")  # The tool modules import bpy; the wait itself never touches it.
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)
from loopcut import jobs  # noqa: E402


def spec():
    return {"version": 1, "id": "a" * 32, "frames": [1, 2, 3], "width": 8, "height": 8,
            "samples": 1, "budget_seconds": 60, "fps": 24, "fps_base": 1.0, "format": "PNG",
            "engine": "CYCLES", "camera": "Camera", "scene": "Scene", "source": "", "session_id": "b" * 32,
            "output": "/tmp/owned-output", "binary": "/fake/blender", "existing_files": "verify_owned_frames"}


def png(width=8, height=8):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\0\0\xff" * width) * height)) + chunk(b"IEND", b""))


class JobTests(unittest.TestCase):
    def test_render_attempts_always_require_scoped_approval(self):
        from loopcut import agent
        for action in ("start", "resume"):
            self.assertEqual(agent._approval("render_job", json.dumps({"action": action}), ()), "heavy")
        for action in ("status", "cancel"):
            self.assertEqual(agent._approval("render_job", json.dumps({"action": action, "job_id": "a"}), ()), "")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = jobs.folder(self.root, "a" * 32)
        self.path.mkdir(parents=True)
        jobs.atomic_json(self.path / "spec.json", spec())
        jobs.atomic_json(self.path / "status.json", {"state": "running", "updated": time.time()})

    def test_rejects_invalid_specs_before_launch(self):
        for field, value in (("width", True), ("height", 0), ("samples", 0), ("fps_base", float("nan")),
                             ("frames", [2, 1]), ("frames", [1, 1]), ("frames", []), ("frames", [True]),
                             ("budget_seconds", 86401), ("format", "AVI"), ("binary", "relative"),
                             ("existing_files", "overwrite_everything")):
            invalid = {**spec(), field: value}
            with self.subTest(field=field, value=value), self.assertRaises(jobs.JobError):
                jobs.validate_spec(invalid)
        jobs.validate_spec(spec())

    def test_an_output_folder_is_the_jobs_by_identity_not_a_marker_among_the_renders(self):
        output = self.root / "renders" / "turntable"
        output.mkdir(parents=True)
        record = {**spec(), "output": str(output), "created": 100.0, "output_identity": jobs.identity(output)}
        jobs.check_output(self.path, record)
        self.assertEqual(list(output.iterdir()), [], "nothing of the job's in the folder")
        # The same name, another folder: its files are not the job's to replace. The old folder is
        # moved aside, not removed, so Linux cannot hand its inode number to the new one.
        output.rename(output.with_name("turntable-old"))
        output.mkdir()
        with self.assertRaisesRegex(jobs.JobError, "no longer belongs"):
            jobs.check_output(self.path, record)
        record["output_identity"] = jobs.identity(output)
        later = jobs.folder(self.root, "c" * 32)
        later.mkdir()
        jobs.atomic_json(later / "spec.json", {**record, "id": "c" * 32, "created": 200.0})
        with self.assertRaisesRegex(jobs.JobError, "belongs to job c+, started later"):
            jobs.check_output(self.path, record)
        jobs.check_output(later, {**record, "id": "c" * 32, "created": 200.0})
        output.rmdir()
        with self.assertRaisesRegex(jobs.JobError, "no longer belongs"):
            jobs.check_output(self.path, record)

    def test_a_named_folder_must_be_new_or_empty(self):
        folder = self.root / "renders"
        jobs.ensure_empty(folder)  # Not there yet.
        folder.mkdir()
        (folder / ".DS_Store").write_bytes(b"")
        jobs.ensure_empty(folder)  # Hidden files are not the user's work.
        (folder / "frame_0000001.png").write_bytes(png())
        with self.assertRaisesRegex(jobs.JobError, "not empty \\(frame_0000001.png\\)"):
            jobs.ensure_empty(folder)
        with self.assertRaisesRegex(jobs.JobError, "a file, not a folder"):
            jobs.ensure_empty(folder / "frame_0000001.png")

    def test_start_takes_a_folder_and_a_frame_range(self):
        from loopcut import job_tools
        scene = types.SimpleNamespace(frame_start=10, frame_end=14)
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(context=types.SimpleNamespace(scene=scene))}):
            self.assertEqual(job_tools.frame_range(), [10, 11, 12, 13, 14])
            self.assertEqual(job_tools.frame_range(start=12), [12, 13, 14])
            self.assertEqual(job_tools.frame_range(start=1, end=3), [1, 2, 3])
            self.assertEqual(job_tools.frame_range([5, 9]), [5, 9])
            for bad in (dict(frames=[1], start=1), dict(start=5, end=4), dict(start=1, end=10001), dict(start=True)):
                with self.subTest(bad=bad), self.assertRaises(jobs.JobError):
                    job_tools.frame_range(**bad)
        describe = job_tools.describe
        self.assertEqual(describe("render_job", {"action": "start", "folder": "renders/spin", "start": 1, "end": 48,
                                                 "budget_seconds": 600})[0],
                         "Background PNG render · frames 1-48 · 600s budget")
        self.assertEqual(describe("render_job", {"action": "start", "folder": "f", "budget_seconds": 60})[0],
                         "Background PNG render · the scene's frames · 60s budget")
        properties = job_tools.SCHEMAS[0]["function"]["parameters"]["properties"]
        self.assertTrue({"folder", "start", "end"} <= set(properties))
        self.assertIn("folder (by the .blend; new or empty)", job_tools.SCHEMAS[0]["function"]["description"])

    def test_mp4_requires_even_dimensions(self):
        with self.assertRaises(jobs.JobError):
            jobs.validate_spec({**spec(), "format": "MP4", "width": 7})

    def test_path_traversal_rejected(self):
        for value in ("../other", "a" * 32 + "/x", None, ""):
            with self.assertRaises(jobs.JobError):
                jobs.folder(self.root, value)

    def test_job_id_must_match_folder(self):
        jobs.atomic_json(self.path / "spec.json", {**spec(), "id": "c" * 32})
        with self.assertRaises(jobs.JobError):
            jobs.spec_at(self.path)

    def test_cancel_is_durable_and_does_not_claim_completion(self):
        jobs.cancel(self.path)
        self.assertEqual(jobs.status(self.path)["state"], "cancelling")
        self.assertTrue((self.path / "cancel").is_file())
        jobs.atomic_json(self.path / "status.json", {"state": "cancelled", "updated": time.time()})
        self.assertEqual(jobs.status(self.path)["state"], "cancelled")

    def test_cancel_complete_job_does_not_change_it(self):
        jobs.atomic_json(self.path / "status.json", {"state": "complete", "updated": time.time()})
        jobs.cancel(self.path)
        self.assertFalse((self.path / "cancel").exists())

    def test_stale_heartbeat_reports_interruption_without_removing_lease(self):
        lease = self.path / "lease"
        lease.mkdir()
        (lease / "heartbeat").touch()
        stale = time.time() - 60
        os.utime(lease / "heartbeat", (stale, stale))
        self.assertEqual(jobs.status(self.path)["state"], "interrupted")
        self.assertTrue(lease.exists())

    def test_duplicate_worker_is_refused(self):
        (self.path / "lease").mkdir()
        with self.assertRaisesRegex(jobs.JobError, "already has a worker"):
            jobs.launch(self.path)

    def test_orphan_renderer_blocks_resume(self):
        lease = self.path / "lease"
        lease.mkdir()
        jobs.atomic_json(lease / "child.json", {"pid": os.getpid()})
        stale = time.time() - 60
        os.utime(lease, (stale, stale))
        with self.assertRaisesRegex(jobs.JobError, "renderer is still alive"):
            jobs.launch(self.path)

    def test_failed_launch_releases_lease_and_records_failure(self):
        with patch.object(jobs.subprocess, "Popen", side_effect=OSError("test launch failure")):
            with self.assertRaises(OSError):
                jobs.launch(self.path)
        self.assertFalse((self.path / "lease").exists())
        self.assertEqual(jobs.status(self.path)["state"], "failed")

    def test_records_survive_new_store_reader(self):
        jobs.atomic_json(self.path / "progress.json", {"completed": 2, "files": [{"file": "f.png"}]})
        row = jobs.list_jobs(Path(str(self.root)))[0]
        self.assertEqual(row["progress"]["completed"], 2)
        self.assertEqual(row["id"], "a" * 32)

    def test_png_requires_complete_pixels_crc_dimensions_and_end(self):
        path = self.root / "frame.png"
        valid = png()
        path.write_bytes(valid)
        jobs.verify_png(path, 8, 8)
        for damaged in (valid[:33], valid[:-1], valid + b"junk", valid[:48] + b"!" + valid[49:], png(4, 8)):
            path.write_bytes(damaged)
            with self.assertRaises(jobs.JobError):
                jobs.verify_png(path, 8, 8)

    def test_atomic_record_rejects_nan_and_preserves_old_state(self):
        path = self.root / "record.json"
        jobs.atomic_json(path, {"value": 1})
        with self.assertRaises(ValueError):
            jobs.atomic_json(path, {"value": float("nan")})
        self.assertEqual(jobs.read_json(path), {"value": 1})
        self.assertFalse(list(self.root.glob("*.tmp")))


class StatusWaitTests(unittest.TestCase):
    """render_job status with wait, through the agent's own path: the main-thread call hands back
    the rest, and the agent thread waits between main-thread calls. A single-worker executor
    stands in for Blender's main thread and its timer pump."""

    ID = "a" * 32

    def setUp(self):
        from loopcut import agent, job_tools, llm, mainthread
        self.agent, self.job_tools, self.llm = agent, job_tools, llm
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"LOOPCUT_DATA_DIR": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.path = jobs.folder(Path(temporary.name), self.ID)
        self.path.mkdir(parents=True)
        jobs.atomic_json(self.path / "spec.json", spec())
        self.set_state("running")
        jobs.atomic_json(self.path / "progress.json", {"phase": "rendering", "completed": 1, "total": 3,
                                                       "rendered": 1, "reused": 0, "elapsed_seconds": 2.0, "files": []})
        self.main = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blender-main")
        self.addCleanup(self.main.shutdown)
        self.on_main = []  # What ran on the main thread.

        def run_on_main(fn):
            self.on_main.append(fn)
            return self.main.submit(fn)

        for target, name, value in ((mainthread, "run_on_main", run_on_main), (job_tools, "WAIT_READ", 0.05)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_state(self, state, **fields):
        jobs.atomic_json(self.path / "status.json", {"state": state, "updated": time.time(), **fields})

    def later(self, seconds, fn):
        timer = threading.Timer(seconds, fn)
        timer.start()
        self.addCleanup(timer.cancel)

    def status(self, is_cancelled=lambda: False, **arguments):
        started = time.monotonic()
        result = self.agent._run_tool_on_main("render_job", json.dumps(
            {"action": "status", "job_id": self.ID, **arguments}), is_cancelled)
        return result, time.monotonic() - started

    def finish(self):
        jobs.atomic_json(self.path / "progress.json", {"phase": "verified", "completed": 3, "total": 3, "rendered": 3,
                                                       "reused": 0, "elapsed_seconds": 6.0, "files": []})
        self.set_state("complete", elapsed_seconds=7.5)

    def test_render_job_preview_declares_file_origin(self):
        from loopcut import attachments
        source = self.path / "preview.png"
        source.write_bytes(png())
        self.finish()
        with patch.object(self.job_tools, "preview_path", return_value=source), \
                patch.object(attachments, "_to_png", return_value=(8, 8)):
            result, _ = self.status(preview=True, wait=1)
        self.assertTrue(result.ok, result.text)
        self.assertIsNotNone(result.image_path)
        self.assertTrue(result.file_image)

    def test_returns_when_the_job_ends(self):
        self.later(.3, self.finish)
        result, took = self.status(wait=120)
        self.assertLess(took, 3)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(json.loads(result.text.partition("\n")[0])["state"], "complete")
        self.assertIn("Waited 0s; the job is now complete: 3/3 frames in 8s.", result.text)
        self.assertEqual(len(self.on_main), 2, "the call and the read after the wait; nothing in between")

    def test_a_failure_or_the_user_s_cancel_ends_it_too(self):
        for end in (lambda: self.set_state("failed", error="Time budget exhausted"),
                    lambda: (self.path / "cancel").touch()):
            with self.subTest(end=end):
                self.set_state("running")
                self.later(.2, end)
                result, took = self.status(wait=120)
                self.assertLess(took, 3)
                self.assertRegex(result.text, r"the job is now (failed|cancelling): 1/3 frames")
                (self.path / "cancel").unlink(missing_ok=True)

    def test_returns_at_the_end_of_the_wait_while_the_job_still_runs(self):
        result, took = self.status(wait=1)
        self.assertGreaterEqual(took, 1)
        self.assertLess(took, 3)
        self.assertIn("Waited 1s; still running: 1/3 frames, 1s into this attempt, about 4s left.", result.text)
        self.assertIn("otherwise finish the turn: it goes on without you", result.text)

    def test_a_stop_ends_the_wait_at_once_but_never_the_job(self):
        stop = threading.Event()
        self.later(.2, stop.set)
        started = time.monotonic()
        with self.assertRaises(self.llm.Cancelled):
            self.status(stop.is_set, wait=120)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse((self.path / "cancel").exists())
        self.assertEqual(jobs.status(self.path)["state"], "running")

    def test_the_wait_is_not_on_the_main_thread(self):
        read_on = []  # The thread of every read of the job's state file.
        current = jobs.current

        def read(path):
            read_on.append(threading.current_thread().name)
            return current(path)

        probes = []  # The UI pump's own work, queued mid-wait: it must not wait for the wait.
        self.later(.3, lambda: probes.append(self.main.submit(time.monotonic)))
        self.later(.8, self.finish)
        started = time.monotonic()
        with patch.object(jobs, "current", read):
            result, took = self.status(wait=120)
        self.assertIn("now complete", result.text)
        self.assertGreater(took, .8)
        self.assertEqual(read_on.count("blender-main_0"), 2, "one in each of the two main-thread calls")
        self.assertGreater(read_on.count(threading.current_thread().name), 5, "the polling, on the agent thread")
        self.assertLess(probes[0].result(timeout=1) - started, .6, "the main thread was free during the wait")

    def test_status_says_how_to_wait_and_start_says_not_to_poll(self):
        from loopcut import tools
        result = tools.execute("render_job", json.dumps({"action": "status", "job_id": self.ID}))
        self.assertIsNone(result.then, "no wait asked: nothing to wait for")
        self.assertIn("Still running: 1/3 frames", result.text)
        self.assertIn("call status with wait=<seconds> (up to 240) rather than polling", result.text)
        with patch.object(jobs, "launch", lambda path: jobs.status(path)):
            resumed = tools.execute("render_job", json.dumps({"action": "resume", "job_id": self.ID}))
        self.assertEqual(json.loads(resumed.text.partition("\n")[0])["id"], self.ID)
        self.assertIn("goes on after this turn ends: do not poll it", resumed.text)
        self.assertIn("wait=<seconds>; otherwise finish the turn", resumed.text)

    def test_the_wait_is_capped_and_checked(self):
        from loopcut import tools
        self.assertEqual(self.job_tools.render_job_status(self.ID, wait=10000).then.args[-1], 240)
        self.finish()
        self.assertIsNone(self.job_tools.render_job_status(self.ID, wait=60).then, "a job that ended: no wait")
        for arguments, error in (({"job_id": self.ID, "wait": "soon"}, "wait must be a number of seconds, up to 240."),
                                 ({"job_id": self.ID, "wait": -1}, "wait must be a number of seconds, up to 240."),
                                 ({"wait": 60}, "wait needs job_id.")):
            result = tools.execute("render_job", json.dumps({"action": "status", **arguments}))
            self.assertEqual((result.ok, result.text), (False, error))


if __name__ == "__main__":
    unittest.main()
