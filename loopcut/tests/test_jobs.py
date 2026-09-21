"""Durability, ownership and corrupted-output regressions; real Blender runs jobs_check.py."""
import os
import struct
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut import jobs


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
        for name in ("start_render_job", "resume_render_job"):
            self.assertEqual(agent._approval(name, "{}", ()), "heavy")
        for name in ("render_job_status", "cancel_render_job"):
            self.assertEqual(agent._approval(name, "{}", ()), "")

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


if __name__ == "__main__":
    unittest.main()
