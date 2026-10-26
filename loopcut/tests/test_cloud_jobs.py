"""Managed jobs, SSE/retry semantics and result provenance, without running Blender.

Placement uses NumPy; when absent, run those cases with Blender's bundled Python, as paint_tools does.
"""

import contextlib
import copy
import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

try:
    import numpy  # Load before patch.dict(sys.modules) so each Blender mock does not reload NumPy.
except ImportError:
    numpy = None

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).parent))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import cloud, cloud_account, cloud_http, cloud_jobs, state  # noqa: E402
from mock_service import MockService  # noqa: E402

PROJECT = {"id": "unit-test", "no_generation": False}
DESCRIPTOR = {"name": "image_generate", "title": "Generate image", "version": 1, "apply": "add_image_reference",
              "file_inputs": [{"field": "reference", "required": False, "max_mb": 20, "accept": ["image/png"]}],
              "cost": {"quote_required": False}, "latency": "seconds"}
DATA = b"verified result"


def output(data=DATA):
    return {"name": "../../image", "type": "image", "mime": "image/png", "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data), "url": "https://storage.test/output?signature=test", "expires_at": "later"}


def job(status="succeeded"):
    return {"id": "job_test", "tool": "image_generate", "status": status, "progress": {"pct": 100, "message": "Done"},
            "credits": {"reserved": 2, "charged": 2 if status == "succeeded" else 0, "refunded": 0},
            "outputs": [output()] if status == "succeeded" else [],
            "provenance": {"job_id": "job_test", "tool": "image_generate", "vendor": "test", "model": "test-1",
                           "kind": "gen", "created_at": "2026-10-03T18:00:00Z"}, "error": None}


def event(name, data, id):
    return f"id: {id}\nevent: {name}\ndata: {json.dumps(data)}\n\n".encode()


class Reply(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(json.dumps(data).encode() if isinstance(data, dict) else data)
        self.status, self.headers = status, headers or {}


class Transport:
    def __init__(self, streams=(), final=None):
        self.calls, self.streams = [], list(streams)
        self.final = final or job()
        self.posts, self.lose_post = [], False

    def __call__(self, request, *, timeout):
        headers = {k.lower(): v for k, v in request.header_items()}
        self.calls.append((request.method, request.full_url, headers))
        url = request.full_url
        if url.endswith("/blobs/check"):
            return Reply({"missing": json.loads(request.data)["sha256"]})
        if url.endswith("/blobs/upload-urls"):
            return Reply({"items": [{"sha256": item["sha256"], "put_url": "https://storage.test/upload",
                                      "expires_at": "later"} for item in json.loads(request.data)["items"]]})
        if request.method == "PUT":
            request.data.read()
            return Reply(b"")
        if url.endswith("/jobs"):
            payload = json.loads(request.data)
            self.posts.append(payload)
            if self.lose_post and len(self.posts) == 1:
                raise urllib.error.URLError("lost reply after acceptance")
            return Reply(job("queued") if self.streams else self.final)
        if url.endswith("/events"):
            response = self.streams.pop(0)
            if isinstance(response, Exception):
                raise response
            return Reply(response, headers={"Content-Type": "text/event-stream"})
        if url.endswith("/cancel"):
            result = job("canceled")
            result["credits"]["refunded"] = 2
            return Reply(result)
        if url.startswith("https://storage.test"):
            return Reply(DATA)
        if url.endswith("/job_test"):
            return Reply(self.final)
        raise AssertionError(url)

    def client(self):
        return cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=self,
                                 client_headers={"x-loopcut-contract": "0.1", "x-loopcut-client": "test"})


class Busy(Transport):
    """The first fails[(method, url suffix)] requests get a 5xx (0.2.19's gate: a 500 after the job was charged)."""

    def __init__(self, fails, streams=(), final=None, status=500, code="server_error"):
        super().__init__(streams, final)
        self.fails, self.status, self.code = dict(fails), status, code

    def __call__(self, request, *, timeout):
        for (method, suffix), left in self.fails.items():
            if request.method == method and request.full_url.endswith(suffix) and left:
                self.fails[(method, suffix)] = left - 1
                self.calls.append((request.method, request.full_url, {}))
                if suffix == "/jobs":
                    self.posts.append(json.loads(request.data))
                return Reply({"error": {"message": "The job service is temporarily unavailable. Try again shortly.",
                                        "code": self.code, "type": "server_error"}}, status=self.status)
        return super().__call__(request, timeout=timeout)


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        cloud_jobs._registry.clear()
        redraw = patch.object(cloud_jobs, "_redraw")
        redraw.start()
        self.addCleanup(redraw.stop)

    def run_job(self, transport, arguments=None, descriptor=None, files=None, **options):
        with patch.object(cloud_jobs, "_client", return_value=transport.client()), \
                patch.object(cloud_jobs.tempfile, "mkdtemp", return_value=str(self.root)):
            return cloud_jobs.run(descriptor or DESCRIPTOR, arguments or {"prompt": "prop"}, files or {}, project=PROJECT, **options)

    def test_sse_parser_ping_crlf_multiline_and_incomplete_eof(self):
        data = b': ping\r\n\r\nretry: 10\nid: 4\nevent: progress\ndata: {"pct": 42,\ndata: "message": "working"}\n\n'
        data += b'id: 5\nevent: progress\ndata: {"pct": 50, "message": "partial"}\n'
        self.assertEqual(list(cloud_jobs.parse_sse(io.BytesIO(data))), [("progress", {"pct": 42, "message": "working"}, "4")])

    def test_parser_rejects_bad_id_json_and_oversized_events(self):
        for data in (b'id: x\ndata: {}\n\n', b'data: wrong\n\n', b'data: []\n\n', b'data: \xff\n\n'):
            with self.subTest(data=data), self.assertRaises(cloud_http.CloudError):
                list(cloud_jobs.parse_sse(io.BytesIO(data)))
        with patch.object(cloud_jobs, "MAX_EVENT", 5), self.assertRaises(cloud_http.CloudError):
            list(cloud_jobs.parse_sse(io.BytesIO(b'data: {}\n\n')))

    def test_sse_reconnect_uses_last_event_id_then_downloads_without_auth(self):
        initial = b': ping\n\n' + event("status", {"status": "running"}, 1)
        initial += event("progress", {"pct": 42, "message": "Making image"}, 2)
        final = event("output", output(), 3) + event("done", job(), 4)
        transport, progress = Transport([initial, final]), []
        result = self.run_job(transport, on_progress=progress.append)
        self.assertEqual(Path(result["outputs"][0]["path"]).read_bytes(), DATA)
        self.assertTrue(Path(result["outputs"][0]["path"]).is_relative_to(self.root))
        streams = [headers for method, url, headers in transport.calls if url.endswith("/events")]
        self.assertNotIn("last-event-id", streams[0])
        self.assertEqual(streams[1]["last-event-id"], "2")
        self.assertEqual(streams[1]["accept"], "text/event-stream")
        self.assertIn({"pct": 42, "message": "Making image"}, progress)
        self.assertNotIn("authorization", transport.calls[-1][2])
        self.assertEqual(cloud_jobs.ui_rows(), [])

    def test_unsupported_stream_and_dropped_streams_fall_back_to_polling(self):
        for streams in ([urllib.error.HTTPError("events", 405, "unsupported", {}, io.BytesIO())],
                        [urllib.error.URLError("gone"), urllib.error.URLError("again")], [b"", b""]):
            with self.subTest(streams=streams):
                cloud_jobs._registry.clear()  # Each independent transport uses the fixture ID job_test.
                transport = Transport(streams)
                self.run_job(transport)
                self.assertTrue(any(url.endswith("/job_test") for _, url, _ in transport.calls))
                for path in self.root.iterdir():
                    path.unlink()

    def test_non_sse_content_type_polls(self):
        transport = Transport([b"unused"])
        original = transport.__call__
        def send(request, **options):
            if request.full_url.endswith("/events"):
                return Reply(b"{}", headers={"Content-Type": "application/json"})
            return original(request, **options)
        with patch.object(cloud_jobs, "_client", return_value=cloud_http.Client(
                base_url="https://service.test", api_key="lc_test", transport=send, client_headers={})), \
                patch.object(cloud_jobs.tempfile, "mkdtemp", return_value=str(self.root)):
            result = cloud_jobs.run(DESCRIPTOR, {}, {}, project=PROJECT)
        self.assertEqual(result["status"], "succeeded")

    def test_lost_submit_reply_retries_with_identical_body_and_key(self):
        transport = Transport()
        transport.lose_post = True
        self.run_job(transport)
        self.assertEqual(len(transport.posts), 2)
        self.assertEqual(transport.posts[0], transport.posts[1])
        self.assertTrue(transport.posts[0]["idempotency_key"])

    def run_busy(self, transport, **options):
        slept = []
        with patch.object(cloud_jobs, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=slept.append)):
            return self.run_job(transport, **options), round(sum(slept))

    def test_a_500_creating_a_job_asks_again_with_the_same_key_and_gets_the_one_job(self):
        transport = Busy({("POST", "/jobs"): 1})
        result, slept = self.run_busy(transport)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(Path(result["outputs"][0]["path"]).read_bytes(), DATA)
        self.assertEqual(len(transport.posts), 2)
        self.assertEqual(transport.posts[0], transport.posts[1], "the identical body, so the server replays its job")
        self.assertEqual(len({post["idempotency_key"] for post in transport.posts}), 1)
        self.assertEqual(slept, cloud_jobs.SERVER_RETRY_SECONDS[0])

    def test_500s_creating_a_job_throughout_say_it_may_exist_never_a_plain_failure(self):
        transport = Busy({("POST", "/jobs"): 99})
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_busy(transport)
        error = caught.exception
        self.assertEqual(error.code, cloud_jobs.UNCONFIRMED)
        self.assertEqual(len(transport.posts), 1 + len(cloud_jobs.SERVER_RETRY_SECONDS))
        keys = {post["idempotency_key"] for post in transport.posts}
        self.assertEqual(len(keys), 1, "every retry reuses the one key")
        self.assertEqual(error.extra["idempotency_key"], keys.pop())
        text = cloud.error_text(error)
        for said in ("may have been created", "do not start another", error.extra["idempotency_key"],
                     "never charges twice"):
            self.assertIn(said, text)
        self.assertNotIn("Try again shortly", text)
        self.assertFalse(text.startswith("server_error"))

    def test_backoff_on_5xx_is_bounded_to_about_a_minute(self):
        slept = []
        with patch.object(cloud_jobs, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=slept.append)), \
                self.assertRaises(cloud_http.CloudError):
            self.run_job(Busy({("POST", "/jobs"): 99}, status=503, code="http_error"))
        self.assertTrue(55 <= sum(slept) <= 65, sum(slept))
        self.assertEqual(sum(cloud_jobs.SERVER_RETRY_SECONDS), 60)

    def test_a_4xx_or_a_refunded_502_creating_a_job_is_definite_and_not_asked_again(self):
        for status, code in ((402, "insufficient_credits"), (409, "quote_changed"), (422, "invalid_input"),
                             (502, "vendor_failed")):
            with self.subTest(code=code):
                transport = Busy({("POST", "/jobs"): 99}, status=status, code=code)
                with self.assertRaises(cloud_http.CloudError) as caught:
                    _, slept = self.run_busy(transport)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(transport.posts), 1)

    def test_a_503_on_the_event_stream_polls_and_succeeds(self):
        transport = Busy({("GET", "/events"): 1}, streams=[b"unused"], status=503, code="http_error")
        result, slept = self.run_busy(transport)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(Path(result["outputs"][0]["path"]).read_bytes(), DATA)
        self.assertTrue(any(method == "GET" and url.endswith("/job_test") for method, url, _ in transport.calls))
        self.assertEqual(len(transport.posts), 1)
        self.assertEqual(slept, 0, "polled at once")

    def test_503s_on_the_polling_get_are_asked_again_then_succeed(self):
        transport = Busy({("GET", "/events"): 1, ("GET", "/job_test"): 2}, streams=[b"unused"], status=503,
                         code="server_error")
        result, slept = self.run_busy(transport)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(Path(result["outputs"][0]["path"]).read_bytes(), DATA)
        self.assertEqual(sum(url.endswith("/job_test") and m == "GET" for m, url, _ in transport.calls), 3)
        self.assertEqual(slept, sum(cloud_jobs.SERVER_RETRY_SECONDS[:2]))
        self.assertEqual(len(transport.posts), 1)

    def test_5xx_polling_throughout_names_the_job_that_may_still_run(self):
        transport = Busy({("GET", "/events"): 1, ("GET", "/job_test"): 99}, streams=[b"unused"], status=500)
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_busy(transport)
        error = caught.exception
        self.assertEqual(error.code, cloud_jobs.UNCONFIRMED)
        self.assertEqual(error.extra["job"]["id"], "job_test")
        self.assertEqual(error.extra["job"]["status"], "queued")
        text = cloud.error_text(error)
        for said in ("job job_test", "may still be running", "do not start another", error.extra["idempotency_key"]):
            self.assertIn(said, text)
        # The same key again picks the job up anew, rather than repeating the stored answer.
        transport.fails.clear()
        transport.streams = [urllib.error.HTTPError("events", 405, "unsupported", {}, io.BytesIO())]
        result, _ = self.run_busy(transport, arguments={"prompt": "prop",
                                                        "idempotency_key": error.extra["idempotency_key"]})
        self.assertEqual(result["status"], "succeeded")

    def test_a_long_jobs_5xx_polls_throughout_leave_job_status_naming_its_key(self):
        transport = Busy({("GET", "/events"): 1, ("GET", "/job_test"): 99}, streams=[b"unused"], status=503)
        slept = []
        with patch.object(cloud_jobs, "time", types.SimpleNamespace(monotonic=time.monotonic, sleep=slept.append)):
            started = self.run_job(transport, descriptor={**DESCRIPTOR, "latency": "long"})
            for thread in [t for t in threading.enumerate() if t.name == "Loopcut cloud job"]:
                thread.join(timeout=5)
        self.assertEqual(started["status"], "queued")
        with self.assertRaises(cloud_http.CloudError) as caught:
            cloud_jobs.job_status("job_test")
        error = caught.exception
        self.assertEqual(error.code, cloud_jobs.UNCONFIRMED)
        self.assertEqual(error.extra["idempotency_key"], transport.posts[0]["idempotency_key"])
        self.assertIn(f'idempotency_key "{transport.posts[0]["idempotency_key"]}"', cloud.error_text(error))

    def test_cancel_during_progress_posts_cancel_and_exposes_refund(self):
        cancel = threading.Event()
        stream = event("progress", {"pct": 10, "message": "Working"}, 1)
        transport = Transport([stream])
        def progress(value):
            if value["pct"] == 10:
                self.assertTrue(cloud_jobs.cancel_job("job_test"))
                self.assertFalse(cloud_jobs.ui_rows()[0]["cancelable"])
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_job(transport, cancel=cancel, on_progress=progress)
        self.assertEqual(caught.exception.code, "canceled")
        self.assertEqual(caught.exception.extra["refunded"], 2)
        self.assertTrue(cancel.is_set())
        self.assertTrue(any(method == "POST" and url.endswith("/cancel") for method, url, _ in transport.calls))

    def test_a_cancel_the_server_never_confirms_ends_60_s_after_stop(self):
        class Unconfirmed(Transport):  # Takes the cancel, but the job stays running.
            def __call__(self, request, *, timeout):
                if request.full_url.endswith("/cancel"):
                    self.calls.append((request.method, request.full_url, {}))
                    if len(self.calls) > 100:
                        raise AssertionError("still asking: the call never gave up")
                    return Reply(job("running"))
                return super().__call__(request, timeout=timeout)

        cancel, now, stopped = threading.Event(), [1000.0], []
        transport = Unconfirmed([event("progress", {"pct": 10, "message": "Working"}, 1)])

        def monotonic():  # A poll every 5 s of the fake clock.
            now[0] += 5
            return now[0]

        def stop(value):
            if not cancel.is_set():
                cancel.set()
                stopped.append(now[0])
        slept = []
        with patch.object(cloud_jobs, "time", types.SimpleNamespace(monotonic=monotonic, sleep=slept.append)), \
                self.assertRaises(cloud_http.CloudError) as caught:
            self.run_job(transport, cancel=cancel, on_progress=stop)
        self.assertEqual(caught.exception.code, "canceled")
        self.assertEqual(caught.exception.extra["job"]["status"], "running", "not claimed as canceled or refunded")
        self.assertTrue(60 <= now[0] - stopped[0] <= 75, now[0] - stopped[0])
        self.assertGreater(sum(url.endswith("/cancel") for _, url, _ in transport.calls), 5, "asked again meanwhile")
        self.assertEqual(set(slept), {cloud_jobs.CANCEL_POLL_SECONDS}, "every 2 s, not every poll's 0.25 s")

    def test_a_replay_of_a_job_whose_cancel_is_never_confirmed_ends_60_s_after_stop(self):
        cloud_jobs._remember(job("running"), "Generate image", threading.Event())  # Its own call follows it.
        cancel, now, slept = threading.Event(), [1000.0], []
        cancel.set()

        def monotonic():
            now[0] += 5
            return now[0]

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) > 1000:
                raise AssertionError("still waiting: the replay never gave up")
        with patch.object(cloud_jobs, "time", types.SimpleNamespace(monotonic=monotonic, sleep=sleep)), \
                self.assertRaises(cloud_http.CloudError) as caught:
            cloud_jobs._wait_known("job_test", cancel, None)
        self.assertEqual((caught.exception.code, caught.exception.extra["job"]["status"]), ("canceled", "running"))
        self.assertTrue(cloud_jobs._registry["job_test"]["cancel"].is_set(), "the job itself was asked to cancel")
        self.assertLessEqual(len(slept), 60 / 5 + 1)

    def test_every_answer_of_the_server_is_heard_events_keepalives_and_downloads(self):
        """cloud._Cancel.heard: progress for the stall clock of the turn that made the call (agent.check_stall), so a
        queued, long or downloading job never stalls while the server answers."""
        stream = b": ping\n\n" + event("status", {"status": "running"}, 1) + event("output", output(), 2)
        stream += event("done", job(), 3)
        cancel, heard = threading.Event(), []
        cancel.heard = lambda: heard.append(1)
        result = self.run_job(Transport([stream]), cancel=cancel)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(heard), stream.count(b"\n") + 1, "each line read, then the output's one chunk")

    def test_precanceled_call_makes_no_http(self):
        cancel, transport = threading.Event(), Transport()
        cancel.set()
        with self.assertRaises(cloud_http.CloudError):
            self.run_job(transport, cancel=cancel)
        self.assertEqual(transport.calls, [])

    def test_hash_mismatch_rejects_output_and_removes_partial_folder(self):
        final = job()
        final["outputs"][0]["sha256"] = "0" * 64
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_job(Transport(final=final))
        self.assertEqual(caught.exception.code, "hash_mismatch")
        self.assertFalse(self.root.exists())
        self.root.mkdir()  # Cleanup owns the test folder.

    def test_invalid_job_progress_output_and_provenance_are_rejected(self):
        for field, value in (("progress", {"pct": True, "message": "wrong"}), ("provenance", {}),
                             ("id", "../../escape"), ("status", [])):
            final = job()
            final[field] = value
            with self.subTest(field=field), self.assertRaises(cloud_http.CloudError):
                self.run_job(Transport(final=final))
        final = job()
        final["outputs"][0]["url"] = "file:///etc/passwd"
        with self.assertRaises(cloud_http.CloudError):
            self.run_job(Transport(final=final))

    def test_independent_calls_use_distinct_keys_and_call_metadata_is_not_input(self):
        transport = Transport()
        self.run_job(transport, {"prompt": "prop", "quote_id": "qt_test", "bundle": "a" * 64})
        for path in self.root.iterdir():
            path.unlink()
        self.run_job(transport)
        first, second = transport.posts
        self.assertNotEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(first["input"], {"prompt": "prop"})
        self.assertEqual(first["quote_id"], "qt_test")
        self.assertEqual(first["bundle"], "a" * 64)
        self.assertEqual(first["project"], PROJECT)

    def test_worker_calls_run_without_bpy_access(self):
        transport = Transport()
        source = self.root / "input.png"
        source.write_bytes(b"input bytes")
        result, errors = [], []
        def work():
            try:
                result.append(self.run_job(transport, files={"reference": source}))
            except Exception as ex:
                errors.append(ex)
        # The HTTP settings are already snapshotted by _client on main. The entire run is pure.
        with patch.dict(sys.modules, {"bpy": None}):
            worker = threading.Thread(target=work)
            worker.start()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]["status"], "succeeded")
        self.assertTrue(any(method == "PUT" for method, _, _ in transport.calls))
        for method, url, headers in transport.calls:
            if method == "PUT" or url.startswith("https://storage.test"):
                self.assertNotIn("authorization", headers)

    def test_failed_job_preserves_vendor_error_and_refund_without_mock(self):
        final = job("failed")
        final["error"] = {"code": "vendor_failed", "type": "vendor_failed", "message": "Vendor failed", "refunded": True}
        final["credits"]["refunded"] = 2
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_job(Transport(final=final))
        self.assertEqual((caught.exception.code, caught.exception.status), ("vendor_failed", None))
        self.assertTrue(caught.exception.extra["refunded"])
        self.assertEqual(caught.exception.extra["job"]["credits"]["refunded"], 2)

    def test_submission_http_errors_keep_scope_and_retry_after_without_mock(self):
        for code, status, extra in (("generation_disabled", 403, {"scope": "project"}),
                                    ("rate_limited", 429, {"retry_after": 9}),
                                    ("insufficient_credits", 402, {"balance": 0, "needed": 2})):
            transport = Transport()
            def send(request, **options):
                if request.full_url.endswith("/jobs"):
                    return Reply({"error": {"message": "Cannot submit", "code": code, "type": code, **extra}}, status)
                return transport(request, **options)
            client = cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=send, client_headers={})
            with self.subTest(code=code), patch.object(cloud_jobs, "_client", return_value=client), \
                    self.assertRaises(cloud_http.CloudError) as caught:
                cloud_jobs.run(DESCRIPTOR, {}, {}, project=PROJECT)
            self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
            self.assertTrue(extra.items() <= caught.exception.extra.items())
            self.assertIn("idempotency_key", caught.exception.extra)

    def test_quote_handoff_does_not_submit_until_approved_without_mock(self):
        transport = Transport()
        quote = {"quote_id": "qt_test", "credits": 5, "expires_at": "later"}
        def send(request, **options):
            if request.full_url.endswith("/quotes"):
                self.assertNotIn("project", json.loads(request.data))
                return Reply(quote)
            return transport(request, **options)
        client = cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=send, client_headers={})
        descriptor = {**DESCRIPTOR, "cost": {"quote_required": True}}
        with patch.object(cloud_jobs, "_client", return_value=client):
            with self.assertRaises(cloud_http.CloudError) as caught:
                cloud_jobs.run(descriptor, {"prompt": "prop"}, {}, project=PROJECT)
            self.assertEqual(caught.exception.code, "quote_required")
            self.assertEqual(caught.exception.extra, quote)
            self.assertEqual(transport.posts, [])
            result = cloud_jobs.run(descriptor, {"prompt": "prop", "quote_id": "qt_test"}, {}, project=PROJECT)
            self.addCleanup(shutil.rmtree, Path(result["outputs"][0]["path"]).parent)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(transport.posts[0]["quote_id"], "qt_test")

    def test_long_job_returns_handle_and_status_waits_for_verified_downloads(self):
        started, finish = threading.Event(), threading.Event()
        transport = Transport([event("done", job(), 1)])
        original = transport.__call__
        quote = {"quote_id": "qt_long", "credits": 5, "expires_at": "later"}
        def send(request, **options):
            if request.full_url.endswith("/quotes"):
                return Reply(quote)
            if request.full_url.startswith("https://storage.test"):
                started.set()
                finish.wait(timeout=2)
            return original(request, **options)
        client = cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=send, client_headers={})
        runtime = cloud.Runtime(client, self.root / "cache", signed_in=True)
        descriptor = {**DESCRIPTOR, "backend": "managed", "latency": "long", "cost": {"quote_required": True}}
        cancel, approvals = threading.Event(), []
        def consent(details):
            self.assertEqual(transport.posts, [])
            approvals.append(details)
            return True
        with patch.object(cloud_jobs, "_client", side_effect=AssertionError("use captured client")), \
                cloud_account.approval_context(consent), \
                patch.object(cloud_jobs.tempfile, "mkdtemp", return_value=str(self.root)):
            arguments = cloud_account.authorize(cloud_jobs, descriptor, {}, {}, project=PROJECT,
                                                cancel=cancel, runtime=runtime, client=client)
            handle = cloud_account.submit(cloud_jobs, descriptor, arguments, {}, project=PROJECT,
                                           cancel=cancel, on_progress=None, runtime=runtime, client=client)
            self.assertEqual(handle["status"], "queued")
            self.assertEqual(list(cloud_jobs._registry), [handle["id"]])
            self.assertIs(cloud_jobs._registry[handle["id"]]["cancel"], cancel)
            self.assertEqual([row["id"] for row in cloud_jobs.ui_rows()], [handle["id"]])
            self.assertEqual(transport.posts[0]["quote_id"], "qt_long")
            self.assertEqual(runtime.quotes, {"qt_long": quote})
            self.assertEqual([detail["credits"] for detail in approvals], [5])
            self.assertTrue(started.wait(timeout=2))
            self.assertEqual(cloud_jobs.job_status(handle["id"])["status"], "running")
            finish.set()
            until = time.monotonic() + 2
            while cloud_jobs.job_status(handle["id"])["status"] != "succeeded" and time.monotonic() < until:
                time.sleep(.01)
            result = cloud_jobs.job_status(handle["id"])
        self.assertTrue(Path(result["outputs"][0]["path"]).is_file())

    def test_progress_rows_are_pure_and_only_show_active_jobs(self):
        registry = {"job_test": {"job": job("running"), "title": "Generate image"},
                    "job_done": {"job": job(), "title": "Finished"}}
        before = copy.deepcopy(registry)
        self.assertEqual(cloud_jobs.progress_rows(registry), [{"id": "job_test", "title": "Generate image", "pct": 100,
                                                             "message": "Done", "cancelable": True,
                                                             "note": "2 credits held. Cancel returns them."}])
        self.assertEqual(registry, before)

    def test_status_raises_terminal_failure_before_worker_records_exception(self):
        failed = job('failed')
        failed['error'] = {'code': 'tool_disabled', 'type': 'service_error', 'message': 'Disabled', 'refunded': True}
        # The done event updates the row before _finish catches and stores its exception.
        cloud_jobs._remember(failed, 'Generate image', threading.Event())
        with self.assertRaises(cloud_http.CloudError) as caught:
            cloud_jobs.job_status(failed['id'])
        self.assertEqual(caught.exception.code, 'tool_disabled')
        self.assertEqual(caught.exception.error_type, 'service_error')
        self.assertTrue(caught.exception.extra['refunded'])

    def test_progress_and_cancel_draw_in_chat_and_background_jobs_panel(self):
        from loopcut.ui import layout
        rows = [{"id": "job_test", "title": "Generate image", "pct": 42,
                 "message": "Making image", "cancelable": True}]
        for view in ("chat", "jobs"):
            display = layout.build(state.new_session(), 420, 600, 1, lambda font, size, text: len(text) * size * .6,
                                   ui={"view": view, "cloud_jobs": rows})
            texts = " ".join(p.get("text", "") for p in display["prims"])
            by_id = {p['id']: p.get('text', '') for p in display['prims']}
            self.assertEqual(by_id['cloud.job_test.title'], 'Generate image')
            self.assertEqual(by_id['cloud.job_test.pct'], '42%')
            self.assertIn("Making image", texts)
            cancel = next(hit for hit in display["hits"] if hit["id"] == "cloud.job_test.cancel")
            self.assertEqual(cancel["action"], ("cloud_job_cancel", "job_test"))


class Block(dict):
    def __init__(self, name, **attributes):
        super().__init__()
        self.name = name
        self.__dict__.update(attributes)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "report.json"
        self.path.write_text('{"ok": true}')

    def local_job(self):
        result = job()
        result["id"] = result["provenance"]["job_id"] = "byok_test"
        result["provenance"]["backend"] = "byok"
        result["outputs"][0].update(type="report", name="QA report", path=str(self.path))
        return result

    def test_only_new_datablocks_and_embedded_nodes_get_stamped(self):
        old = Block("existing")
        data = types.SimpleNamespace(objects=[old], meshes=[], materials=[], images=[])
        before = cloud_jobs.datablock_snapshot(data)
        nodes, mesh, image = Block("nodes"), Block("mesh"), Block("image")
        material = Block("material", node_tree=nodes)
        data.meshes.append(mesh)
        data.images.append(image)
        data.materials.append(material)
        new = cloud_jobs.created_datablocks(before, cloud_jobs.datablock_snapshot(data))
        cloud_jobs.stamp_provenance(new, job()["provenance"])
        self.assertEqual({block.name for block in new}, {"nodes", "mesh", "image", "material"})
        self.assertNotIn("loopcut_provenance", old)
        for block in new:
            self.assertEqual(json.loads(block["loopcut_provenance"]), job()["provenance"])

    def attach(self, descriptor, details=None):
        texts = []
        def new(name):
            text = Block(name)
            text.write = lambda value: text.update(content=value)
            texts.append(text)
            return text
        class Texts(list):
            def __iter__(self):
                return iter(texts)
        collection = Texts()
        collection.new = new
        fake = types.SimpleNamespace(data=types.SimpleNamespace(texts=collection))
        from loopcut import conversations
        session = state.new_session()
        with patch.dict(sys.modules, {"bpy": fake}), patch.object(state, "session", return_value=session), \
                patch.object(conversations, "save") as save:
            summary = cloud_jobs.apply_result(descriptor, self.local_job(), details=details)
        # Not a notice in the chat as well: that was the whole report again, unfolded (0.2.17's "wall of text").
        self.assertEqual(session["items"], [])
        save.assert_not_called()
        return summary, texts

    def test_report_handler_keeps_text_and_returns_report(self):
        summary, texts = self.attach({"apply": "attach_report"})
        self.assertIn('"ok": true', summary)
        self.assertIn("QA report", summary)
        self.assertEqual(texts[0]["content"], self.path.read_text())
        self.assertEqual(json.loads(texts[0]["loopcut_provenance"])["backend"], "byok")

    def search_report(self):
        from loopcut import asset_report
        row = lambda title, source="polyhaven": {
            "id": f"{source}:{title.lower().replace(' ', '_')}",
            "dedupe_keys": [f"{source}:{title.lower().replace(' ', '_')}"],
            "source": source, "source_id": title.lower().replace(" ", "_"), "kind": "model", "title": title,
            "license": {"id": "CC0-1.0", "name": "CC0 1.0", "url": "https://creativecommons.org/publicdomain/zero/1.0/"},
            "attribution": {"required": False, "text": f"{title} by Someone (CC0).", "authors": ["Someone"]},
            "page_url": "https://polyhaven.com/a/x", "preview_url": "https://cdn.polyhaven.com/x.png",
            "import": {"source": source, "id": title.lower().replace(" ", "_")}, "score": 1.0,
            "tags": ["wood", "box"], "downloads": [{"label": "blend", "url": "https://dl.polyhaven.org/x.blend",
                                                     "format": "blend", "size": 1000}]}
        return {"schema": asset_report.SCHEMA, "query": "treasure chest", "mode": "hybrid",
                "generated_at": "2026-10-24T12:00:00Z",
                "filters": {"kind": "model", "license": "cc0", "sources": ["polyhaven", "ambientcg"], "limit": 10,
                            "max_polycount": None, "min_resolution": None},
                "results": [row("Treasure Chest"), *(row(f"Crate {i}", "ambientcg") for i in range(9))],
                "sources": [{"id": "polyhaven", "name": "Poly Haven", "url": "https://polyhaven.com",
                             "credit": "Powered by Poly Haven", "credit_required": True, "license_ids": ["CC0-1.0"]}],
                "notices": []}

    SEARCH = {"name": "asset_search", "apply": "attach_report", "title": "Search assets"}
    HEAD = "Attached reports: QA report. Job byok_test; provenance recorded.\nQA report:\n"

    def test_an_asset_search_report_s_card_line_is_its_count_and_best_match_and_the_model_gets_its_table(self):
        # The owner, 0.2.17 walkthrough: a search's report, 10 rows of about 18 fields, as a "massive wall of text".
        report = json.dumps(self.search_report(), indent=2)
        self.path.write_text(report)
        details = {}
        summary, texts = self.attach(self.SEARCH, details)
        self.assertEqual(details["outcome"], "Searched libraries: 10 matches; best: Treasure Chest (Poly Haven, CC0)")
        self.assertNotIn("{", details["outcome"])
        self.assertEqual(details["card_output"], self.HEAD + report, "the JSON, behind the card's toggle")
        self.assertEqual(texts[0]["content"], report, "and in its Text datablock")
        # The model's copy: numbered rows as search_assets shows them, each with its import handle.
        rows = summary.removeprefix(self.HEAD).splitlines()
        self.assertEqual(rows[0], "10 asset(s) for 'treasure chest':")
        self.assertEqual(rows[1], "#1 Treasure Chest (polyhaven); kind: model; score 1; CC0-1.0; id treasure_chest")
        self.assertEqual(rows[10], "#10 Crate 8 (ambientcg); kind: model; score 1; CC0-1.0; id crate_8")
        self.assertTrue(rows[11].startswith('Sources: [{"id": "polyhaven", "name": "Poly Haven", '), rows[11])
        self.assertEqual(rows[12:], ["Import by number: import_asset(pick=N)."])
        for bulk in ("page_url", "preview_url", "/a/x", "x.png", "x.blend", "dedupe", "wood", "generated_at"):
            self.assertNotIn(bulk, summary)
        self.assertLess(len(summary), len(report) / 4)

    def test_loopcut_asset_report_full_or_a_row_import_asset_cannot_take_gives_the_model_the_json(self):
        unknown = self.search_report()
        unknown["results"][4].update(source="example", id="example:crate_3", dedupe_keys=["example:crate_3"],
                                     **{"import": {"source": "example", "id": "crate_3"}})
        for env, report in (({cloud_jobs.ASSET_REPORT: "full"}, self.search_report()),
                            ({cloud_jobs.ASSET_REPORT: " full\n"}, self.search_report()), ({}, unknown)):
            with self.subTest(env=env), patch.dict(os.environ, env):
                report = json.dumps(report, indent=2)
                self.path.write_text(report)
                details = {}
                summary, _ = self.attach(self.SEARCH, details)
                self.assertEqual(summary, self.HEAD + report, "the report as it was before the table")
                self.assertNotIn("card_output", details)
                self.assertTrue(details["outcome"].startswith("Searched libraries: 10 matches"))

    def test_an_asset_search_line_reads_only_the_fields_that_are_there(self):
        report = self.search_report()
        line = lambda report: cloud_jobs.report_line("Search assets", [report])
        best = report["results"][0]
        self.assertEqual(line({**report, "sources": None}), "Searched libraries: 10 matches; best: Treasure Chest "
                                                            "(polyhaven, CC0)")
        best.pop("license")
        self.assertEqual(line(report), "Searched libraries: 10 matches; best: Treasure Chest (Poly Haven)")
        best.update(source=["polyhaven"], license={"id": None})
        self.assertEqual(line(report), "Searched libraries: 10 matches; best: Treasure Chest")
        for broken in ({}, {"title": ""}, {"title": "Two\nlines"}, {"title": {"en": "Chest"}}):
            report["results"][0] = broken
            self.assertEqual(line(report), "Searched libraries: 10 matches", broken)
        self.assertEqual(line({**report, "results": [None]}), "Searched libraries: 1 match")
        self.assertEqual(line({"schema": report["schema"]}), "Searched libraries: 0 matches")

    def test_another_report_s_card_line_names_the_tool_and_counts_its_items_never_its_json(self):
        descriptor = {"name": "render", "apply": "attach_report", "title": "Render scene",
                      "display": {"title": "Cloud rendering"}}
        for report, line in (('{"checks": {"frames": 2}, "passed": true}', "Cloud rendering: report attached"),
                             ('{"schema": "loopcut.qa/2", "results": [1, 2, 3]}',
                              "Cloud rendering: report attached (3 items)"),
                             ('[{"frame": 1}]', "Cloud rendering: report attached (1 item)"),
                             ("Frames 1-2 rendered.", "Cloud rendering: report attached")):
            with self.subTest(report=report):
                self.path.write_text(report)
                details = {}
                summary, _ = self.attach(descriptor, details)
                self.assertEqual(details["outcome"], line)
                self.assertTrue(summary.endswith(f"QA report:\n{report}"))
        self.assertEqual(cloud_jobs.report_line("Cloud rendering", [{}, None]), "Cloud rendering: 2 reports attached")

    def test_summary_none_and_unimplemented_material(self):
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(data=types.SimpleNamespace())}):
            summary = cloud_jobs.apply_result({"apply": "none"}, self.local_job())
            self.assertIn(str(self.path), summary)
            summary = cloud_jobs.apply_result({"apply": "import_material"}, self.local_job())
            self.assertIn("not yet available", summary)

    def test_partial_import_is_stamped_even_on_handler_error(self):
        created, old = Block("new"), Block("old")
        data = types.SimpleNamespace(objects=[old])
        def failed(job):
            data.objects.append(created)
            raise RuntimeError("import failed")
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(data=data)}), \
                patch.object(cloud_jobs, "_model_import", side_effect=failed), self.assertRaises(cloud_http.CloudError):
            cloud_jobs.apply_result({"apply": "import_model"}, self.local_job())
        self.assertIn("loopcut_provenance", created)
        self.assertNotIn("loopcut_provenance", old)

    def test_gltf_rejects_external_file_uris(self):
        path = Path(self.temp.name) / "asset.gltf"
        for uri in ("../secret", "https://other.test/asset.bin", "file:///tmp/file", "%2e%2e/secret", "..\\secret"):
            path.write_text(json.dumps({"buffers": [{"uri": uri}]}))
            with self.subTest(uri=uri), self.assertRaises(cloud_http.CloudError):
                cloud_jobs._check_gltf(path)

    def test_resolve_paths_export_image_copy_and_object_tree_without_mutating_source(self):
        copied = []
        def copy_image():
            image = types.SimpleNamespace(size=(4, 4))
            image.save = lambda: Path(image.filepath_raw).write_bytes(b"png")
            copied.append(image)
            return image
        image = types.SimpleNamespace(copy=copy_image, filepath_raw="original", file_format="JPEG",
                                      get=lambda key: None)
        ob = types.SimpleNamespace(children_recursive=["child"])
        class Images(dict):
            def remove(self, image):
                copied.remove(image)
        fake = types.SimpleNamespace(data=types.SimpleNamespace(images=Images(Image=image), objects={"Object": ob}),
                                     path=types.SimpleNamespace(abspath=lambda path: path))
        descriptor = {"file_inputs": [{"field": name} for name in ("a", "b", "c")]}
        from loopcut import glb
        with patch.dict(sys.modules, {"bpy": fake}), patch.object(glb, "_export") as export:
            found = cloud_jobs.resolve_files(descriptor, {"a": "Image", "b": "Object", "c": str(self.path)}, Path(self.temp.name))
        self.assertEqual(found["a"].read_bytes(), b"png")
        self.assertEqual(found["c"], self.path.resolve())
        export.assert_called_once_with(found["b"], [ob, "child"])
        self.assertEqual((image.filepath_raw, image.file_format, copied), ("original", "JPEG", []))

    def test_a_name_not_found_gets_the_images_attached_last_newest_first(self):
        """2026-10-25: Luna passed "character reference" to photo→3D, and after a restore an old photo's name."""
        from loopcut import conversations, files
        attached = lambda *refs: {"role": "user", conversations.ATTACHED: True, "content": [
            {"type": "text", "text": "go"}, *({"type": "image_url", "image_url": {"url": r}} for r in refs)]}
        session = {"id": "c1", "messages": [attached("old"), {"role": "assistant", "content": "ok"},
                                            attached("hero", "sheet", "cape")],
                   "references": [{"ref": r, "name": f"{r}.png", "pinned": True, **({"video": {"id": "clip"}} if r == "sheet" else {})}
                                  for r in ("old", "hero", "sheet", "cape")]}
        fake = types.SimpleNamespace(data=types.SimpleNamespace(images={}, objects={}))
        descriptor = {"file_inputs": [{"field": "image", "required": True, "accept": ["image/png"]}]}
        with patch.object(state, "_session", session), patch.dict(sys.modules, {"bpy": fake}), \
                patch.object(files, "resolve", return_value=Path(self.temp.name) / "character reference"):
            with self.assertRaises(cloud_http.CloudError) as raised:
                cloud_jobs.resolve_files(descriptor, {"image": "character reference"}, Path(self.temp.name))
            self.assertEqual(str(raised.exception), "Scene reference or file was not found. Images the user attached "
                                                    "last, newest first: cape.png, hero.png")
            session["messages"] = session["references"] = []
            with self.assertRaises(cloud_http.CloudError) as raised:
                cloud_jobs.resolve_files(descriptor, {"image": "character reference"}, Path(self.temp.name))
            self.assertEqual(str(raised.exception), "Scene reference or file was not found")

    def test_resolve_refuses_loopcut_previews_and_images_without_pixels(self):
        """A user's log (0.2.12): image_to_3d got a chat preview whose copy had no pixels yet; saving it raised a
        RuntimeError that ended the turn. A preview is refused and an unsavable image is an input error."""
        def copy(size, save=None):
            def make():
                image = types.SimpleNamespace(size=size, save=save or (lambda: None))
                copied.append(image)
                return image
            return make
        copied = []
        class Images(dict):
            def remove(self, image):
                copied.remove(image)

        def broken():
            raise RuntimeError("Error: Image 'x' does not have any image data")
        images = Images(preview=types.SimpleNamespace(copy=copy((4, 4)), get={"lc_temp": True}.get),
                        empty=types.SimpleNamespace(copy=copy((0, 0)), get=lambda key: None),
                        unsaved=types.SimpleNamespace(copy=copy((4, 4), broken), get=lambda key: None))
        fake = types.SimpleNamespace(data=types.SimpleNamespace(images=images, objects={}))
        descriptor = {"file_inputs": [{"field": "image"}]}
        for name, words in (("preview", "Loopcut's own preview"), ("empty", "no pixels"),
                            ("unsaved", "could not be saved")):
            with self.subTest(name), patch.dict(sys.modules, {"bpy": fake}), \
                    self.assertRaises(cloud_http.CloudError) as raised:
                cloud_jobs.resolve_files(descriptor, {"image": name}, Path(self.temp.name))
            self.assertEqual(raised.exception.code, "invalid_input")
            self.assertIn(words, str(raised.exception))
        self.assertEqual(copied, [], "every copy was removed")
        # Refused, the model gets the names it can pass, as for a name that is not found.
        from loopcut import conversations
        session = {"id": "c1", "references": [{"ref": "cat", "name": "cat.png", "pinned": True}],
                   "messages": [{"role": "user", conversations.ATTACHED: True, "content": [
                       {"type": "text", "text": "go"}, {"type": "image_url", "image_url": {"url": "cat"}}]}]}
        with patch.object(state, "_session", session), patch.dict(sys.modules, {"bpy": fake}), \
                self.assertRaises(cloud_http.CloudError) as raised:
            cloud_jobs.resolve_files(descriptor, {"image": "preview"}, Path(self.temp.name))
        self.assertEqual(str(raised.exception), "That image is Loopcut's own preview, not the user's; images the user "
                                                "attached last, newest first: cat.png")


def remove_identical(items, block):
    """Blender removes by identity; Block's dict equality would match any unstamped block."""
    del items[next(n for n, item in enumerate(items) if item is block)]


class FakeList(list):
    """bpy.data's collections: iterable, with new/remove."""
    def __init__(self, make=None):
        super().__init__()
        self.make = make

    def new(self, *args):
        block = self.make(*args)
        self.append(block)
        return block

    def remove(self, block):
        remove_identical(self, block)


class FakeObject(Block):
    def __init__(self, name, data=None, type="MESH"):
        super().__init__(name, type=type, data=data, parent=None, users_collection=[], selected=False,
                         _matrix=FakeMatrix(), location=FakeVector((0, 0, 0)), scale=(1, 1, 1),
                         bound_box=[(0, 0, 0), (1, 1, 1)])

    @property
    def matrix_world(self):
        local = FakeMatrix(tuple(tuple(self.scale[i] if i == j else self.location[i] if j == 3 else 0
                                       for j in range(4)) for i in range(3)) + ((0, 0, 0, 1),)) @ self._matrix
        return self.parent.matrix_world @ local if self.parent is not None else local

    @matrix_world.setter
    def matrix_world(self, matrix):
        # Imports reparent their top-level objects to the newly created identity root.
        assert self.parent is None or self.parent.matrix_world.rows == FakeMatrix().rows
        self._matrix = matrix
        self.location, self.scale = FakeVector((0, 0, 0)), (1, 1, 1)

    def evaluated_get(self, graph):
        bounds = getattr(self, "evaluated_bound_box", self.bound_box)
        return types.SimpleNamespace(matrix_world=self.matrix_world,
                                     bound_box=bounds,
                                     data=types.SimpleNamespace(vertices=FakeVertices(
                                         getattr(self, "evaluated_vertices", bounds))))

    def visible_get(self, *, view_layer, viewport):
        return not any(space is viewport for space in getattr(self, "hidden_in", ()))

    def select_set(self, value):
        self.selected = value

    def select_get(self):
        return self.selected


class FakeVector(tuple):
    def __sub__(self, other):
        return FakeVector(a - b for a, b in zip(self, other))


class FakeVertices:
    """Only bulk coordinate access, like Mesh.vertices.foreach_get."""
    def __init__(self, coordinates):
        self.coordinates = coordinates

    def __len__(self):
        return len(self.coordinates)

    def foreach_get(self, attribute, buffer):
        assert attribute == "co"
        buffer[:] = [value for co in self.coordinates for value in co]


class FakeMatrix:
    def __init__(self, rows=((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0), (0, 0, 0, 1))):
        self.rows = rows

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        return self.rows[index]

    def __matmul__(self, other):
        if isinstance(other, FakeMatrix):
            return FakeMatrix(tuple(tuple(sum(self.rows[i][k] * other.rows[k][j] for k in range(4))
                                          for j in range(4)) for i in range(4)))
        return FakeVector(sum(row[i] * other[i] for i in range(3)) + row[3] for row in self.rows[:3])

    def copy(self):
        return self


class FakeCollection(Block):
    def __init__(self, name):
        super().__init__(name)
        owner = self
        class Objects(list):
            def link(self, ob):
                self.append(ob)
                ob.users_collection.append(owner)
            def unlink(self, ob):
                remove_identical(self, ob)
                remove_identical(ob.users_collection, owner)
        self.objects, self.children = Objects(), FakeList()
        self.children.link = self.children.append


def fake_glb():
    import struct
    body = json.dumps({"asset": {"version": "2.0"}}).encode()
    body += b" " * (-len(body) % 4)
    chunk = struct.pack("<II", len(body), 0x4E4F534A) + body
    return struct.pack("<III", 0x46546C67, 2, 12 + len(chunk)) + chunk


class ModelImportTest(unittest.TestCase):
    """import_model's FBX branch beside the GLB one, against a scripted bpy (no Blender)."""
    FBX = b"Kaydara FBX Binary  \x00\x1a\x00\xe8\x1c\x00\x00quads"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        scene_collection = FakeCollection("Scene Collection")
        self.scene = types.SimpleNamespace(render=types.SimpleNamespace(fps=24, fps_base=1.0),
                                           collection=scene_collection, unit_settings=types.SimpleNamespace(scale_length=1),
                                           cursor=types.SimpleNamespace(location=FakeVector((0, 0, 0))))
        self.window = types.SimpleNamespace(screen="screen")
        self.overrides = []
        layer_objects = FakeList(lambda name, data: FakeObject(name, data, type="EMPTY"))
        layer_objects.active = None
        self.data = types.SimpleNamespace(objects=layer_objects, collections=FakeList(FakeCollection),
                                          images=FakeList(), materials=FakeList(), scenes=[self.scene])
        self.calls = []
        context = types.SimpleNamespace(scene=self.scene, window=None, temp_override=self.temp_override,
                                        window_manager=types.SimpleNamespace(windows=[self.window]),
                                        evaluated_depsgraph_get=lambda: None,
                                        view_layer=types.SimpleNamespace(update=lambda: None, objects=layer_objects))
        self.ops = types.SimpleNamespace(wm=types.SimpleNamespace(fbx_import=self.importer("wm.fbx_import")),
                                         import_scene=types.SimpleNamespace(fbx=self.importer("import_scene.fbx"),
                                                                            gltf=self.importer("import_scene.gltf")))
        self.bpy = types.SimpleNamespace(data=self.data, context=context, ops=self.ops,
                                         app=types.SimpleNamespace(background=True),
                                         path=types.SimpleNamespace(abspath=lambda value: value))
        mathutils = types.SimpleNamespace(Vector=FakeVector)
        modules = patch.dict(sys.modules, {"bpy": self.bpy, "mathutils": mathutils})
        modules.start()
        self.addCleanup(modules.stop)

    @contextlib.contextmanager
    def temp_override(self, **values):
        self.overrides.append(values)
        missing = object()
        previous = {key: getattr(self.bpy.context, key, missing) for key in values}
        try:
            for key, value in values.items():
                setattr(self.bpy.context, key, value)
            yield
        finally:
            for key, value in previous.items():
                if value is missing:
                    delattr(self.bpy.context, key)
                else:
                    setattr(self.bpy.context, key, value)

    def importer(self, name):
        """An import operator that adds what a real file would: an object, a packed (embedded) image,
        an image named by a path outside the job folder, and a material sampling both. It also sets
        the scene's frame rate, as both FBX importers do."""
        def run(**options):
            self.calls.append((name, options))
            mesh = FakeObject("Remeshed")
            self.data.objects.append(mesh)
            self.scene.collection.objects.link(mesh)
            embedded = Block("baked.png", packed_file=object(), filepath="baked.png")
            outside = Block("secret.png", packed_file=None, filepath="/Users/someone/secret.png")
            self.data.images.extend([embedded, outside])
            nodes = FakeList()
            nodes.extend(Block(image.name, type="TEX_IMAGE", image=image) for image in (embedded, outside))
            self.data.materials.append(Block("Material", node_tree=Block("tree", nodes=nodes)))
            if name != "import_scene.gltf":
                self.scene.render.fps, self.scene.render.fps_base = 30, 1.001
            return {"FINISHED"}
        return run

    def apply(self, suffix, data, kind="assist"):
        if kind == "gen" and numpy is None:
            self.skipTest("NumPy placement cases run in NumpyModelImportTest with Blender's Python")
        path = self.folder / f"0000-model{suffix}"
        path.write_bytes(data)
        result = job()
        result["id"] = result["provenance"]["job_id"] = "byok_test"
        result["provenance"].update(backend="byok", kind=kind)
        result["outputs"][0].update(type="model_3d", name="model", path=str(path), size=len(data),
                                    sha256=hashlib.sha256(data).hexdigest(),
                                    mime="model/vnd.fbx" if suffix == ".fbx" else "model/gltf-binary")
        return cloud_jobs.apply_result({"apply": "import_model"}, result)

    def assert_imported(self, summary, note):
        self.assertEqual(summary, f"Imported and selected models: Loopcut byok_test model{note}. "
                                  "Job byok_test; provenance recorded.")
        collection = next(c for c in self.data.collections if c.name == "Loopcut byok_test model")
        mesh, root = self.data.objects
        self.assertTrue(any(child is collection for child in self.scene.collection.children))
        self.assertEqual([ob.name for ob in collection.objects], [root.name, mesh.name])
        self.assertFalse(any(ob is mesh for ob in self.scene.collection.objects))
        self.assertIs(mesh.parent, root)
        self.assertTrue(mesh.selected and root.selected)
        self.assertIs(self.bpy.context.view_layer.objects.active, root)
        self.assertIn("loopcut_provenance", mesh)

    def import_objects(self, *objects):
        def gltf(**options):
            for ob in objects:
                self.data.objects.append(ob)
                self.scene.collection.objects.link(ob)
            return {"FINISHED"}
        self.ops.import_scene.gltf = gltf

    def box(self, low, high):
        return [(x, y, z) for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]

    def assert_bounds(self, objects, low, high):
        points = [ob.matrix_world @ corner for ob in objects
                  for corner in getattr(ob, "evaluated_vertices", getattr(ob, "evaluated_bound_box", ob.bound_box))]
        for axis in range(3):
            self.assertAlmostEqual(min(p[axis] for p in points), low[axis])
            self.assertAlmostEqual(max(p[axis] for p in points), high[axis])

    def test_offset_generated_mesh_rests_on_ground_and_reports_actual_height(self):
        mesh = FakeObject("Character")
        mesh.bound_box = self.box((8, -7, -4), (10, -3, -.4))
        self.import_objects(mesh)
        summary = self.apply(".glb", fake_glb(), kind="gen")
        self.assert_bounds([mesh], (-.5, -1, 0), (.5, 1, 1.8))
        self.assertEqual(mesh.parent.scale, (.5,) * 3)
        self.assertEqual(summary, "Imported and selected models: Loopcut byok_test model; "
                                 "placed on the ground at the 3D cursor, 1.8 m tall. "
                                 "Job byok_test; provenance recorded.")

    def test_rotated_import_uses_world_bounds_and_nonzero_cursor(self):
        mesh = FakeObject("Rotated")
        mesh.bound_box = self.box((-1, -2, -3), (1, 2, 3))
        mesh.matrix_world = FakeMatrix(((1, 0, 0, 12), (0, .6, -.8, -7), (0, .8, .6, 5), (0, 0, 0, 1)))
        self.scene.cursor.location = FakeVector((7, -4, 3))
        self.import_objects(mesh)
        self.apply(".glb", fake_glb(), kind="gen")
        self.assert_bounds([mesh], (7 - 5 / 18, -5, 3), (7 + 5 / 18, -3, 3 + 17 / 9))
        for scale in mesh.parent.scale:
            self.assertAlmostEqual(scale, 5 / 18)

    def test_rotated_tetrahedron_grounds_real_vertices_and_reports_half_metre_height(self):
        mesh = FakeObject("Tetrahedron")
        mesh.bound_box = self.box((0, 0, 0), (1, 1, 1))
        mesh.evaluated_vertices = [(0, 0, 0), (1, 0, 1), (0, 1, 0), (0, 0, .5)]
        c = 2 ** -.5
        mesh.matrix_world = FakeMatrix(((c, 0, c, 0), (0, 1, 0, 0), (-c, 0, c, 0), (0, 0, 0, 1)))
        self.import_objects(mesh)
        summary = self.apply(".glb", fake_glb(), kind="gen")
        self.assert_bounds([mesh], (-1, -c, 0), (1, c, .5))
        self.assertIn("0.5 m tall", summary)
        for scale in mesh.parent.scale:
            self.assertAlmostEqual(scale, 2 ** .5)  # Keep the existing bounding-box size fit.

    def test_parent_rotated_deformed_vertices_are_centred_on_cursor_xy(self):
        parent, mesh = FakeObject("Rig", type="ARMATURE"), FakeObject("Deformed tetrahedron")
        mesh.parent = parent
        mesh.bound_box = self.box((-20, -20, -20), (20, 20, 20))
        mesh.data = types.SimpleNamespace(vertices=FakeVertices(mesh.bound_box))
        mesh.evaluated_bound_box = self.box((0, 0, 0), (1, 1, 1))
        mesh.evaluated_vertices = [(0, 0, 0), (1, 1, 0), (0, 0, 1), (0, .5, 0)]
        c = 2 ** -.5
        parent.matrix_world = FakeMatrix(((c, -c, 0, 12), (c, c, 0, -7), (0, 0, 1, 5), (0, 0, 0, 1)))
        self.scene.cursor.location = FakeVector((7, -4, 3))
        self.import_objects(parent, mesh)
        summary = self.apply(".glb", fake_glb(), kind="gen")
        self.assert_bounds([mesh], (6.75, -5, 3), (7.25, -3, 3 + 2 ** .5))
        self.assertIn("1.41 m tall", summary)
        self.assertIs(mesh.parent, parent)

    def test_empty_evaluated_mesh_does_not_claim_ground_placement(self):
        mesh = FakeObject("Empty result")
        mesh.evaluated_vertices = []
        self.import_objects(mesh)
        summary = self.apply(".glb", fake_glb(), kind="gen")
        self.assertNotIn("placed on the ground", summary)
        self.assertTrue(mesh.selected and mesh.parent.selected)

    def test_parented_import_fits_all_evaluated_meshes_and_keeps_hierarchy(self):
        parent, first, second = FakeObject("Rig", type="ARMATURE"), FakeObject("Body"), FakeObject("Boots")
        parent.matrix_world = FakeMatrix(((0, -1, 0, 50), (2, 0, 0, -20), (0, 0, 1, 7), (0, 0, 0, 1)))
        first.parent = second.parent = parent
        first.location, second.location = (10, 0, -5), (10, 3, -4)
        second.evaluated_bound_box = self.box((0, 0, 0), (2, 1, 3))  # Deformed geometry differs from source.
        self.scene.cursor.location = FakeVector((4, 8, 2.5))
        self.import_objects(parent, first, second)
        self.apply(".glb", fake_glb(), kind="gen")
        self.assert_bounds([first, second], (3, 7, 2.5), (5, 9, 4.5))
        self.assertIs(first.parent, parent)
        self.assertIs(second.parent, parent)
        self.assertEqual(parent.parent.scale, (.5,) * 3)
        self.assertEqual(len(self.scene.collection.objects), 0)
        self.assertTrue(all(ob.users_collection[0] is parent.parent.users_collection[0]
                            for ob in (parent, first, second)))

    def test_scene_units_keep_two_metre_fit_and_height_in_metres(self):
        for units in (.01, 0):  # Zero retains the existing fallback to metres.
            with self.subTest(units=units):
                mesh = FakeObject("Character")
                mesh.bound_box = self.box((-1, -2, -1), (1, 2, 2.6))
                self.scene.unit_settings.scale_length = units
                self.scene.cursor.location = FakeVector((10, 20, 30))
                self.import_objects(mesh)
                summary = self.apply(".glb", fake_glb(), kind="gen")
                unit = units or 1
                self.assert_bounds([mesh], (10 - .5 / unit, 20 - 1 / unit, 30),
                                   (10 + .5 / unit, 20 + 1 / unit, 30 + 1.8 / unit))
                self.assertIn("1.8 m tall", summary)

    def test_assist_mesh_keeps_source_size_and_placement(self):
        mesh = FakeObject("Retopologized")
        mesh.location = (10, -20, 30)
        self.import_objects(mesh)
        summary = self.apply(".glb", fake_glb())
        self.assert_bounds([mesh], (10, -20, 30), (11, -19, 31))
        self.assertNotIn("placed on the ground", summary)

    def viewport(self):
        self.bpy.app.background = False
        space = types.SimpleNamespace(lock_camera=True)
        region = types.SimpleNamespace(type="WINDOW")
        area = types.SimpleNamespace(type="VIEW_3D", regions=[region], spaces=types.SimpleNamespace(active=space))
        self.window.screen = types.SimpleNamespace(areas=[area])
        self.window.scene, self.window.view_layer = self.scene, self.bpy.context.view_layer
        self.ops.view3d = types.SimpleNamespace(view_selected=unittest.mock.Mock(return_value={"FINISHED"}))
        self.ops.view3d.view_selected.poll.return_value = True
        return space, area, region

    def test_framing_tries_second_view_when_first_hides_imported_geometry(self):
        hidden_space, hidden_area, _ = self.viewport()
        visible_space, visible_area, _ = self.viewport()
        self.window.screen.areas = [hidden_area, visible_area]
        mesh = FakeObject("Character")
        mesh.hidden_in = [hidden_space]
        self.import_objects(mesh)
        framed = []
        def frame(*args, **kwargs):
            if self.bpy.context.area is visible_area:
                framed.append(visible_area)
            return {"FINISHED"}  # Blender also returns FINISHED when everything is hidden.
        self.ops.view3d.view_selected.side_effect = frame
        summary = self.apply(".glb", fake_glb(), kind="gen")
        self.assertEqual(framed, [visible_area])
        self.ops.view3d.view_selected.assert_called_once()
        self.assertTrue(hidden_space.lock_camera and visible_space.lock_camera)
        self.assertIn("placed on the ground", summary)

    def test_framing_polls_inside_each_override_and_continues_after_failures(self):
        for failure in ("poll_false", "poll_error", "operator_error", "cancelled"):
            with self.subTest(failure=failure):
                first_space, first_area, _ = self.viewport()
                second_space, second_area, second_region = self.viewport()
                self.window.screen.areas = [first_area, second_area]
                mesh = FakeObject("Character")
                self.import_objects(mesh)
                polled, framed = [], []
                def poll():
                    area = self.bpy.context.area
                    self.assertIs(self.bpy.context.window, self.window)
                    self.assertIs(self.bpy.context.region, area.regions[0])
                    polled.append(area)
                    if area is first_area:
                        if failure == "poll_error":
                            raise RuntimeError("poll context disappeared")
                        return failure != "poll_false"
                    return True
                def frame(*args, **kwargs):
                    area = self.bpy.context.area
                    self.assertFalse(area.spaces.active.lock_camera)
                    if area is first_area:
                        if failure == "operator_error":
                            raise RuntimeError("viewport closed")
                        return {"CANCELLED"}
                    framed.append(area)
                    return {"FINISHED"}
                self.ops.view3d.view_selected.poll.side_effect = poll
                self.ops.view3d.view_selected.side_effect = frame
                summary = self.apply(".glb", fake_glb(), kind="gen")
                self.assertEqual(polled, [first_area, second_area])
                self.assertEqual(framed, [second_area])
                self.assertEqual(self.ops.view3d.view_selected.call_count,
                                 1 if failure.startswith("poll_") else 2)
                self.assertIs(self.overrides[-1]["region"], second_region)
                self.assertTrue(first_space.lock_camera and second_space.lock_camera)
                self.assertTrue(mesh.selected and mesh.parent.selected)
                self.assertIn("placed on the ground", summary)

    def test_frames_only_imported_meshes_without_camera_lock_then_restores_selection(self):
        space, area, region = self.viewport()
        cube = FakeObject("Cube")
        cube.selected = True
        self.data.objects.append(cube)
        parent, mesh = FakeObject("Offset parent", type="EMPTY"), FakeObject("Character")
        mesh.parent = parent
        self.import_objects(parent, mesh)
        observed = []
        def frame(*args, **kwargs):
            observed.append(([ob.name for ob in self.data.objects if ob.selected], space.lock_camera))
            return {"FINISHED"}
        self.ops.view3d.view_selected.side_effect = frame
        self.apply(".glb", fake_glb(), kind="gen")
        self.ops.view3d.view_selected.assert_called_once_with("EXEC_DEFAULT", use_all_regions=False)
        self.assertEqual(observed, [(["Character"], False)])
        self.assertEqual(self.overrides, [{"window": self.window, "screen": self.window.screen,
                                          "area": area, "region": region}])
        self.assertTrue(space.lock_camera)
        self.assertTrue(all(ob.selected for ob in (parent, mesh, parent.parent)))
        self.assertIs(self.bpy.context.view_layer.objects.active, parent.parent)
        self.assertEqual(cube.matrix_world.rows, FakeMatrix().rows)
        self.assertFalse(cube.selected)

    def test_background_skips_framing_even_with_a_saved_viewport(self):
        self.viewport()
        self.bpy.app.background = True
        summary = self.apply(".glb", fake_glb(), kind="gen")
        self.assertIn("placed on the ground", summary)
        self.ops.view3d.view_selected.assert_not_called()
        self.assertEqual(self.overrides, [])

    def test_no_viewport_or_wrong_scene_or_layer_skips_framing(self):
        for missing in ("windows", "area", "region", "scene", "view_layer"):
            with self.subTest(missing=missing):
                _, area, _ = self.viewport()
                self.bpy.context.window_manager.windows = [self.window]
                if missing == "windows":
                    self.bpy.context.window_manager.windows = []
                elif missing == "area":
                    area.type = "CONSOLE"
                elif missing == "region":
                    area.regions = []
                else:
                    setattr(self.window, missing, object())
                self.apply(".glb", fake_glb(), kind="gen")
                self.ops.view3d.view_selected.assert_not_called()
                self.assertEqual(self.overrides, [])

    def test_framing_failure_does_not_fail_import_and_restores_selection_and_lock(self):
        space, _, _ = self.viewport()
        self.ops.view3d.view_selected.side_effect = RuntimeError("viewport closed")
        summary = self.apply(".glb", fake_glb(), kind="gen")
        self.assertIn("placed on the ground", summary)
        self.assertTrue(space.lock_camera)
        self.assertTrue(all(ob.selected for ob in self.data.objects))
        self.assertTrue(all("loopcut_provenance" in ob for ob in self.data.objects))

    def test_fbx_uses_the_native_importer_with_a_borrowed_window(self):
        summary = self.apply(".fbx", self.FBX)
        self.assertEqual([name for name, _ in self.calls], ["wm.fbx_import"])
        self.assertEqual(self.calls[0][1], {"filepath": str(self.folder / "0000-model.fbx"), "use_custom_props": False})
        self.assertEqual(self.overrides, [{"window": self.window, "screen": "screen"}])
        self.assert_imported(summary, " (FBX; left off 1 texture(s) not embedded in the file)")
        self.assertEqual((self.scene.render.fps, self.scene.render.fps_base), (24, 1.0))
        # The embedded texture is kept; the one named by an outside path is gone, node and image.
        self.assertEqual([image.name for image in self.data.images], ["baked.png"])
        self.assertEqual([node.name for node in self.data.materials[0].node_tree.nodes], ["baked.png"])

    def test_fbx_falls_back_to_the_python_importer(self):
        self.ops.wm = types.SimpleNamespace()  # A build without WITH_IO_FBX.
        summary = self.apply(".fbx", self.FBX)
        self.assertEqual(self.calls, [("import_scene.fbx", {"filepath": str(self.folder / "0000-model.fbx"),
                                                            "use_custom_props": False, "use_image_search": False})])
        self.assertEqual(self.overrides, [])
        self.assert_imported(summary, " (FBX; left off 1 texture(s) not embedded in the file)")
        self.assertEqual((self.scene.render.fps, self.scene.render.fps_base), (24, 1.0))

    def test_fbx_without_a_window_uses_the_python_importer(self):
        self.bpy.context.window_manager.windows = []  # Background Blender.
        self.apply(".fbx", self.FBX)
        self.assertEqual([name for name, _ in self.calls], ["import_scene.fbx"])

    def test_fbx_textures_beside_the_download_are_kept(self):
        def importer(**options):
            self.calls.append(("wm.fbx_import", options))
            beside = self.folder / "albedo.png"
            beside.write_bytes(b"png")
            self.data.objects.append(FakeObject("Remeshed"))
            self.data.images.append(Block("albedo.png", packed_file=None, filepath=str(beside)))
            return {"FINISHED"}
        self.ops.wm.fbx_import = importer
        summary = self.apply(".fbx", self.FBX)
        self.assertTrue(summary.startswith("Imported and selected models: Loopcut byok_test model (FBX)."), summary)
        self.assertEqual([image.name for image in self.data.images], ["albedo.png"])

    def test_native_fbx_reusing_an_existing_image_is_redone_with_the_python_importer(self):
        # Blender's native importer takes an existing image of the same name instead of the file's
        # embedded one: a second retopology result showed the first one's textures.
        earlier = Block("retopo_normal.png", packed_file=object(), filepath="retopo_normal.png")
        self.data.images.append(earlier)
        def native(**options):
            self.calls.append(("wm.fbx_import", options))
            mesh = FakeObject("Remeshed")
            nodes = FakeList()
            nodes.append(Block("normal", type="TEX_IMAGE", image=earlier))
            material = Block("Material", node_tree=Block("tree", nodes=nodes))
            mesh.material_slots = [types.SimpleNamespace(material=material)]
            self.data.objects.append(mesh)
            self.data.materials.append(material)
            self.data.collections.append(FakeCollection("FBX import"))
            self.scene.render.fps = 30
            return {"FINISHED"}
        self.ops.wm.fbx_import = native
        summary = self.apply(".fbx", self.FBX)
        self.assertEqual([name for name, _ in self.calls], ["wm.fbx_import", "import_scene.fbx"])
        self.assert_imported(summary, " (FBX; left off 1 texture(s) not embedded in the file)")
        # Nothing the native import made survives; the earlier image is untouched and not shared.
        self.assertFalse(any(c.name == "FBX import" for c in self.data.collections))
        self.assertEqual([m.name for m in self.data.materials], ["Material"])
        self.assertIsNot(self.data.materials[0].node_tree.nodes[0].image, earlier)
        self.assertTrue(any(image is earlier for image in self.data.images))
        self.assertEqual((self.scene.render.fps, self.scene.render.fps_base), (24, 1.0))

    def test_native_fbx_with_fresh_images_is_kept(self):
        self.data.images.append(Block("other.png", packed_file=object(), filepath="other.png"))
        self.apply(".fbx", self.FBX)
        self.assertEqual([name for name, _ in self.calls], ["wm.fbx_import"])

    def test_imported_objects_get_their_own_materials(self):
        class Material(Block):
            def copy(self):
                return Material(self.name + ".001", node_tree=self.node_tree)
        shared = Material("Retopo", node_tree=Block("tree", nodes=FakeList()))
        self.data.materials.append(shared)
        def gltf(**options):
            self.calls.append(("import_scene.gltf", options))
            mesh = FakeObject("Remeshed")
            mesh.material_slots = [types.SimpleNamespace(material=shared)]
            self.data.objects.append(mesh)
            return {"FINISHED"}
        self.ops.import_scene.gltf = gltf
        self.apply(".glb", fake_glb())
        mesh = next(ob for ob in self.data.objects if ob.name == "Remeshed")
        self.assertIsNot(mesh.material_slots[0].material, shared)
        self.assertEqual(mesh.material_slots[0].material.name, "Retopo.001")

    def vendor_export(self):
        """A glTF import named the way Tripo exports: node, material and textures tagged with its UUID."""
        uid = "3b97ab15-5c1f-4366-9b92-d244544ff68d"
        def gltf(**options):
            self.calls.append(("import_scene.gltf", options))
            self.data.objects.append(FakeObject(f"tripo_node_{uid}", type="EMPTY"))  # EMPTY: no bounds to fit
            self.data.materials.append(Block(f"tripo_material_{uid}", node_tree=Block("Shader Nodetree")))
            self.data.images.extend([Block(f"Color_{uid}", packed_file=object(), filepath="c.png"),
                                     Block("Wood.png", packed_file=object(), filepath="Wood.png")])
            return {"FINISHED"}
        self.ops.import_scene.gltf = gltf

    def test_generated_models_drop_the_vendor_export_names(self):
        self.vendor_export()
        self.apply(".glb", fake_glb(), kind="gen")
        label = "Loopcut byok_test model"
        self.assertEqual([ob.name for ob in self.data.objects], [f"{label} mesh", label])
        self.assertEqual([m.name for m in self.data.materials], [f"{label} material"])
        self.assertEqual([i.name for i in self.data.images], [f"{label} Color", "Wood.png"])
        self.assertEqual(self.data.materials[0].node_tree.name, "Shader Nodetree")

    def test_assist_results_keep_their_names(self):
        self.vendor_export()
        self.apply(".glb", fake_glb())
        self.assertTrue(self.data.objects[0].name.startswith("tripo_node_"))
        self.assertTrue(self.data.materials[0].name.startswith("tripo_material_"))

    def test_neutral_name_leaves_ordinary_names_alone(self):
        for name, expected in (("Chair", None), ("ORM_3b97ab15-5c1f-4366-9b92-d244544ff68d", "L ORM"),
                               ("rodin_mesh", "L mesh"), ("Tripod legs", None), ("geometry_0", None)):
            with self.subTest(name=name):
                self.assertEqual(cloud_jobs._neutral_name(name, "L"), expected)

    def test_not_binary_fbx_is_refused_before_import(self):
        for data in (b"; FBX 7.4.0 project file", fake_glb()):
            with self.subTest(data=data[:8]), self.assertRaisesRegex(cloud_http.CloudError, "not a binary FBX"):
                self.apply(".fbx", data)
        self.assertEqual(self.calls, [])

    def test_glb_import_is_unchanged(self):
        summary = self.apply(".glb", fake_glb())
        self.assertEqual(self.calls, [("import_scene.gltf", {"filepath": str(self.folder / "0000-model.glb"),
                                                             "import_scene_extras": False,
                                                             "import_scene_as_collection": False})])
        self.assert_imported(summary, "")

    def test_hosted_and_byok_save_fbx_with_its_suffix(self):
        from loopcut.byok import runner
        self.assertEqual(cloud_jobs._SUFFIXES["model/vnd.fbx"], ".fbx")
        self.assertEqual(runner._SUFFIXES["model/vnd.fbx"], ".fbx")
        self.assertEqual(cloud_jobs._SUFFIXES["model/gltf-binary"], ".glb")
        self.assertEqual(runner._SUFFIXES["model/gltf-binary"], ".glb")
        final = job()
        final["outputs"][0].update(type="model_3d", name="model", mime="model/vnd.fbx")
        with patch.object(cloud_jobs, "_redraw"), patch.object(cloud_jobs, "_registry", {}), \
                patch.object(cloud_jobs.tempfile, "mkdtemp", return_value=str(self.folder)):
            result = cloud_jobs.run(DESCRIPTOR, {"prompt": "prop"}, {}, project=PROJECT,
                                    client=Transport(final=final).client())
        self.assertEqual(Path(result["outputs"][0]["path"]).suffix, ".fbx")
        self.assertEqual(Path(result["outputs"][0]["path"]).read_bytes(), DATA)


@unittest.skipIf(numpy is not None, "NumPy is available: ModelImportTest ran in this Python")
class NumpyModelImportTest(unittest.TestCase):
    def test_model_import_with_bundled_numpy(self):
        blender = os.environ.get("LOOPCUT_BLENDER") or str(REPO.parent / "tools/Blender.app/Contents/MacOS/Blender")
        root = Path(blender).resolve().parent
        found = sorted(glob.glob(str(root.parent / "Resources" / "*" / "python" / "bin" / "python3*")))
        found += sorted(glob.glob(str(root / "*" / "python" / "bin" / "python3*")))
        if not found:
            self.skipTest("no NumPy or Blender Python found (set LOOPCUT_BLENDER)")
        result = subprocess.run([found[0], "-B", "-m", "unittest", "-q", "test_cloud_jobs.ModelImportTest"],
                                cwd=Path(__file__).parent, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("skipped", result.stderr)


class MockJobsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = MockService("--live", "--job-seconds", ".5")
        cls.service.__enter__()
        cls.addClassCleanup(cls.service.__exit__, None, None, None)
        cls.client = cloud_http.Client(base_url=cls.service.url, api_key="lc_demo",
                                      client_headers={"x-loopcut-contract": "0.1", "x-loopcut-client": "test"})

    def setUp(self):
        self.client.request("POST", "/mock/control", {"reset": True, "settings": {"private_mode": False}})
        self.output_directories = set()
        cloud_jobs._registry.clear()
        for target, value in (("_client", self.client), ("_redraw", None)):
            context = patch.object(cloud_jobs, target, return_value=value)
            context.start()
            self.addCleanup(context.stop)
        self.descriptors = {row["name"]: row for row in self.client.request("GET", "/v1/manifest")["tools"]}

    def run_tool(self, name="image_generate", arguments=None, files=None, **options):
        result = cloud_jobs.run(self.descriptors[name], arguments or {"prompt": "test prop"}, files or {}, project=PROJECT, **options)
        for directory in {str(Path(o["path"]).parent) for o in result["outputs"]}:
            if directory not in self.output_directories:
                self.output_directories.add(directory)
                self.addCleanup(shutil.rmtree, directory)
        return result

    def test_upload_submit_sse_and_hash_checked_download_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.png"
            path.write_bytes(b"mock upload bytes")
            progress = []
            calls = []
            transport = self.client.transport
            def record(request, **options):
                calls.append((request.method, request.full_url, dict(request.header_items())))
                return transport(request, **options)
            with patch.object(self.client, "transport", record):
                result = self.run_tool(arguments={"prompt": "prop", "reference": str(path)},
                                       files={"reference": path}, on_progress=progress.append)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(any(0 < p["pct"] < 100 for p in progress))
        for output in result["outputs"]:
            self.assertEqual(hashlib.sha256(Path(output["path"]).read_bytes()).hexdigest(), output["sha256"])
        self.assertTrue(any(method == "PUT" for method, _, _ in calls))
        self.assertTrue(any(url.endswith("/events") for _, url, _ in calls))
        for _, url, headers in calls:
            headers = {k.lower(): v for k, v in headers.items()}
            self.assertIn("x-loopcut-contract", headers)
            if "/mock-files/" in url or "/mock-upload/" in url:
                self.assertNotIn("authorization", headers)

    def test_insufficient_credits_and_rate_limit_preserve_contract_extras(self):
        for code, status, fields in (("insufficient_credits", 402, {"balance", "needed", "upgrade"}),
                                    ("rate_limited", 429, {"retry_after"})):
            self.client.request("POST", "/mock/control", {"force_error": code})
            with self.subTest(code=code), self.assertRaises(cloud_http.CloudError) as caught:
                self.run_tool()
            self.assertEqual((caught.exception.code, caught.exception.status), (code, status))
            self.assertTrue(fields.issubset(caught.exception.extra))

    def test_generation_disabled_account_and_project_scope(self):
        for scope in ("account", "project"):
            self.client.request("POST", "/mock/control", {"settings": {"no_generation": scope == "account"}})
            project = {**PROJECT, "no_generation": scope == "project"}
            with self.subTest(scope=scope), self.assertRaises(cloud_http.CloudError) as caught:
                cloud_jobs.run(self.descriptors["image_generate"], {"prompt": "prop"}, {}, project=project)
            self.assertEqual((caught.exception.code, caught.exception.status), ("generation_disabled", 403))
            self.assertEqual(caught.exception.extra["scope"], scope)

    def test_vendor_failure_refunds_and_keeps_failed_job(self):
        self.client.request("POST", "/mock/control", {"fail_jobs": True})
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_tool()
        error = caught.exception
        self.assertEqual(error.code, "vendor_failed")
        self.assertTrue(error.extra["refunded"])
        self.assertEqual(error.extra["job"]["status"], "failed")
        self.assertEqual(error.extra["job"]["credits"]["refunded"], error.extra["job"]["credits"]["reserved"])

    def test_cancel_posts_to_mock_and_refunds(self):
        cancel = threading.Event()
        def progress(row):
            if row["pct"] > 0:
                cancel.set()
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_tool(cancel=cancel, on_progress=progress)
        self.assertEqual(caught.exception.code, "canceled")
        self.assertEqual(caught.exception.extra["job"]["status"], "canceled")
        self.assertGreater(caught.exception.extra["refunded"], 0)

    def test_idempotent_replay_returns_same_job_and_one_charge(self):
        args = {"prompt": "prop", "idempotency_key": "same-call"}
        first = self.run_tool(arguments=args)
        second = self.run_tool(arguments=args)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.client.request("GET", "/mock/state")["jobs"], 1)

    def test_quote_is_returned_before_job_then_quote_id_allows_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text('{"schema":"loopcut.bundle/0.1"}')
            from loopcut import blobs
            blobs.upload_missing([{"path": path}], client=self.client)
            args = {"bundle": blobs.sha256(path), "frame_start": 1, "frame_end": 2}
            with self.assertRaises(cloud_http.CloudError) as caught:
                self.run_tool("render", args)
            self.assertEqual(caught.exception.code, "quote_required")
            self.assertGreater(caught.exception.extra["credits"], 0)
            self.assertEqual(self.client.request("GET", "/mock/state")["jobs"], 0)
            result = self.run_tool("render", {**args, "quote_id": caught.exception.extra["quote_id"]})
            until = time.monotonic() + 3
            while result["status"] not in cloud_jobs.TERMINAL and time.monotonic() < until:
                time.sleep(.01)
                result = cloud_jobs.job_status(result["id"])
            for directory in {str(Path(o["path"]).parent) for o in result["outputs"]}:
                self.addCleanup(shutil.rmtree, directory)
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue(any(row["type"] == "report" for row in result["outputs"]))


if __name__ == "__main__":
    unittest.main()
