"""Contract headers, envelope errors and missing-only streaming uploads, without HTTP or Blender."""

import contextlib
import errno
import hashlib
import http.client
import io
import json
import socket
import ssl
import sys
import tempfile
import threading
import traceback
import types
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
sys.path.insert(0, str(REPO / "loopcut" / "harness"))
sys.path.insert(0, str(Path(__file__).parent))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import bl_info, blobs, cloud_http, config  # noqa: E402
from fake_blobs import FakeBlobs, Reply  # noqa: E402
from mock_service import MockService, MockTransport  # noqa: E402


class BlobsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = FakeBlobs()
        self.client = self.service.client()
        fake = types.SimpleNamespace(app=types.SimpleNamespace(version=(5, 2, 0)))
        context = patch.dict(sys.modules, {"bpy": fake})
        context.start()
        self.addCleanup(context.stop)

    def file(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        return {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                "mime": "application/octet-stream"}

    def test_hash_streams_all_chunks(self):
        item = self.file("large", b"0123456789" * 1000)
        with patch.object(blobs, "CHUNK_SIZE", 17):
            self.assertEqual(blobs.sha256(item["path"]), item["sha256"])

    def test_upload_twice_puts_n_then_zero_and_has_headers_everywhere(self):
        files = [self.file(str(n), bytes([n]) * 10) for n in range(3)]
        self.assertEqual(set(blobs.upload_missing(files, client=self.client)), {f["sha256"] for f in files})
        self.assertEqual(len(self.service.puts), 3)
        self.assertEqual(blobs.upload_missing(files, client=self.client), [])
        self.assertEqual(len(self.service.puts), 3)
        for call in self.service.calls:
            version = ".".join(map(str, bl_info["version"]))
            self.assertTrue(call["headers"]["x-loopcut-client"].startswith(f"loopcut-addon/{version} blender/5.2.0 os/"))
            self.assertEqual(call["headers"]["x-loopcut-contract"], "0.7")
            self.assertEqual("authorization" in call["headers"], call["method"] != "PUT")
        self.assertEqual(self.service.calls[-1]["url"], "https://service.test/v1/blobs/check")

    def test_only_the_missing_subset_is_put(self):
        files = [self.file(str(n), bytes([n])) for n in range(4)]
        for item in files[::2]:
            self.service.stored[item["sha256"]] = item["path"].read_bytes()
        uploaded = blobs.upload_missing(files, client=self.client)
        self.assertEqual(set(uploaded), {files[1]["sha256"], files[3]["sha256"]})
        self.assertEqual(self.service.puts, uploaded)

    def test_long_lists_are_batched_and_identical_content_is_uploaded_once(self):
        files = [self.file(str(n), bytes([n])) for n in range(7)]
        files.append(self.file("duplicate", b"\0"))
        blobs.upload_missing(files, client=self.client, batch_size=3)
        checks = [c["payload"]["sha256"] for c in self.service.calls if c["url"].endswith("/check")]
        urls = [c["payload"]["items"] for c in self.service.calls if c["url"].endswith("/upload-urls")]
        self.assertEqual([len(batch) for batch in checks], [3, 3, 1])
        self.assertEqual([len(batch) for batch in urls], [3, 3, 1])
        self.assertEqual(len(self.service.puts), 7)

    def test_check_and_upload_urls_also_batch_when_called_directly(self):
        files = [self.file(str(n), bytes([n])) for n in range(5)]
        hashes = [item["sha256"] for item in files]
        self.assertEqual(blobs.check(hashes, client=self.client, batch_size=2), hashes)
        rows = blobs.upload_urls(files, client=self.client, batch_size=2)
        self.assertEqual([row["sha256"] for row in rows], hashes)

    def test_changed_file_is_rejected_before_http(self):
        item = self.file("changed", b"first")
        item["path"].write_bytes(b"other")
        with self.assertRaises(ValueError):
            blobs.upload_missing([item], client=self.client)
        self.assertEqual(self.service.calls, [])

    def test_invalid_hashes_batch_sizes_and_upload_metadata(self):
        for value in ("X" * 64, "../bad", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                blobs.check([value], client=self.client)
        with self.assertRaises(ValueError):
            blobs.check([], client=self.client, batch_size=0)
        item = self.file("file", b"x")
        for field, bad in (("size", -1), ("size", True), ("size", 209715201), ("mime", "text/plain\r\nX: bad")):
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                blobs.upload_urls([{**item, field: bad}], client=self.client)
        self.assertEqual(self.service.calls, [])

    def test_invalid_check_and_url_responses_fail_before_put(self):
        item = self.file("file", b"x")
        for payload in ({"missing": ["not-requested"]}, {"missing": [item["sha256"]] * 2}, {"missing": None}):
            client = cloud_http.Client(api_key=self.service.key, transport=lambda *a, **k: Reply(payload))
            with self.subTest(payload=payload), self.assertRaises(cloud_http.CloudError):
                blobs.check([item["sha256"]], client=client)
        for rows in ([{"sha256": item["sha256"], "put_url": "file:///etc/passwd", "expires_at": "later"}],
                     [{"sha256": "0" * 64, "put_url": "https://storage.test/x", "expires_at": "later"}]):
            client = cloud_http.Client(api_key=self.service.key,
                                       transport=lambda *a, **k: Reply({"items": rows}))
            with self.subTest(rows=rows), self.assertRaises(cloud_http.CloudError):
                blobs.upload_urls([item], client=client)
        self.assertEqual(self.service.puts, [])

    def test_ready_hashes_omitted_from_upload_urls_are_not_put(self):
        item = self.file("file", b"x")
        def transport(request, *, timeout):
            if request.full_url.endswith("/check"):
                return Reply({"missing": [item["sha256"]]})
            return Reply({"items": []})  # SPEC 3.3: became ready between check and upload-urls.
        client = cloud_http.Client(base_url="https://service.test", api_key=self.service.key, transport=transport)
        self.assertEqual(blobs.upload_missing([item], client=client), [])


def broken_pipe():
    return urllib.error.URLError(BrokenPipeError(32, "Broken pipe"))


class Flaky:
    """FakeBlobs whose first `failures` PUTs send part of the body, then fail as `error()` does."""

    def __init__(self, service, failures, error=broken_pipe):
        self.service, self.failures, self.error, self.tries, self.urls = service, failures, error, 0, []

    def __call__(self, request, *, timeout):
        if request.get_method() == "PUT":
            self.tries += 1
            self.urls.append(request.full_url)
            if self.tries <= self.failures:
                request.data.read(7)
                raise self.error()
        return self.service(request, timeout=timeout)


class PutRetryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = FakeBlobs()
        fake = types.SimpleNamespace(app=types.SimpleNamespace(version=(5, 2, 0)))
        self.console = io.StringIO()
        for context in (patch.dict(sys.modules, {"bpy": fake}), patch.object(blobs, "RETRY_DELAYS", (0, 0, 0, 0)),
                        contextlib.redirect_stdout(self.console)):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.item = self.file("scene.blend", b"frozen scene bytes " * 4000)

    def file(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        return {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}

    def client(self, transport):
        return cloud_http.Client(base_url="https://service.test", api_key=self.service.key, transport=transport)

    def test_put_broken_mid_body_is_retried_from_the_start_with_progress(self):
        errors = [broken_pipe,
                  lambda: ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac"),
                  lambda: http.client.RemoteDisconnected("Remote end closed connection without response"),
                  lambda: TimeoutError("timed out"), lambda: ConnectionResetError(54, "Connection reset by peer")]
        for error in errors:
            with self.subTest(error=type(error()).__name__):
                self.service.stored.clear()
                flaky, steps = Flaky(self.service, 2, error), []
                uploaded = blobs.upload_missing([self.item], client=self.client(flaky), progress=steps.append)
                self.assertEqual(uploaded, [self.item["sha256"]])
                self.assertEqual(self.service.stored[self.item["sha256"]], self.item["path"].read_bytes())
                self.assertEqual(flaky.tries, 3)
                self.assertEqual(steps, ["Upload interrupted, retrying (2/5)…", "Upload interrupted, retrying (3/5)…"])
        self.assertIn("0.1 MB upload interrupted (SSLError SSLV3_ALERT_BAD_RECORD_MAC); retrying (2/5)",
                      self.console.getvalue())

    def test_a_put_cut_off_mid_upload_is_sent_again_from_the_start(self):
        # A 25 MB scene bundle's PUT broke part-way with TLS "bad record mac" (live test 2026-10-17).
        item = self.file("bundle", b"scene bytes" * 1000)
        drops = []

        def flaky(request, *, timeout):
            if request.get_method() == "PUT" and len(drops) < blobs.PUT_ATTEMPTS - 1:
                request.data.read(5000)  # part of the body went out before the connection broke
                drops.append(request.full_url)
                raise BrokenPipeError(32, "Broken pipe")
            return self.service(request, timeout=timeout)

        client = cloud_http.Client(base_url="https://service.test", api_key=self.service.key, transport=flaky)
        self.assertEqual(blobs.upload_missing([item], client=client), [item["sha256"]])
        self.assertEqual(len(drops), blobs.PUT_ATTEMPTS - 1)
        self.assertEqual(self.service.stored[item["sha256"]], item["path"].read_bytes())

    def test_a_put_that_keeps_breaking_or_is_refused_is_not_retried_forever(self):
        item = self.file("bundle", b"scene bytes")
        puts = []

        def broken(request, *, timeout):
            if request.get_method() == "PUT":
                puts.append(1)
                raise BrokenPipeError(32, "Broken pipe")
            return self.service(request, timeout=timeout)

        client = cloud_http.Client(base_url="https://service.test", api_key=self.service.key, transport=broken)
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.upload_missing([item], client=client)
        self.assertEqual((caught.exception.code, len(puts)), ("connection_failed", blobs.PUT_ATTEMPTS))
        puts.clear()

        def refused(request, *, timeout):
            if request.get_method() == "PUT":
                puts.append(1)
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}"))
            return self.service(request, timeout=timeout)

        client = cloud_http.Client(base_url="https://service.test", api_key=self.service.key, transport=refused)
        with self.assertRaises(cloud_http.CloudError):
            blobs.upload_missing([item], client=client)
        self.assertEqual(len(puts), 1)

    def test_client_errors_are_final_while_408_429_and_5xx_are_retried(self):
        def status(code):
            return lambda: urllib.error.HTTPError("https://storage.test/x", code, "error", {}, io.BytesIO(b"<Error/>"))
        for code, retried in ((403, False), (400, False), (404, False), (408, True), (429, True),
                              (500, True), (503, True), (501, False)):
            with self.subTest(code=code):
                self.service.stored.clear()
                flaky = Flaky(self.service, 1, status(code))
                if retried:
                    blobs.upload_missing([self.item], client=self.client(flaky))
                    self.assertEqual(flaky.tries, 2)
                    continue
                with self.assertRaises(cloud_http.CloudError) as caught:
                    blobs.upload_missing([self.item], client=self.client(flaky))
                self.assertEqual(flaky.tries, 1)
                self.assertEqual((caught.exception.code, caught.exception.status), ("http_error", code))
                self.assertEqual(str(caught.exception), f"Cloud storage refused the 0.1 MB upload (HTTP {code})")

    def test_exhausted_5xx_retries_say_storage_did_not_accept(self):
        flaky = Flaky(self.service, 99, lambda: urllib.error.HTTPError("https://storage.test/x", 503, "", {},
                                                                       io.BytesIO(b"")))
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.upload_missing([self.item], client=self.client(flaky))
        self.assertEqual((flaky.tries, caught.exception.status), (5, 503))
        self.assertEqual(str(caught.exception), "Cloud storage did not accept the 0.1 MB upload: HTTP 503 after 5 "
                                                "tries. Try again later.")

    def test_exhausted_retries_name_the_cause_size_and_tries_never_the_url_or_key(self):
        marker = "synthetic" + "-marker"
        query = "X-Amz-Expires=900&X-Amz-" + "Signature=" + marker
        url = f"https://storage.test/{self.item['sha256']}?{query}"
        def error():
            return urllib.error.URLError(BrokenPipeError(32, f"Broken pipe sending to {url} {self.service.key}"))
        flaky = Flaky(self.service, 99, error)
        def transport(request, *, timeout):
            reply = flaky(request, timeout=timeout)
            if request.full_url.endswith("/upload-urls"):
                rows = json.loads(reply.getvalue())["items"]
                reply = Reply({"items": [{**row, "put_url": url} for row in rows]})
            return reply
        try:
            blobs.upload_missing([self.item], client=self.client(transport))
        except cloud_http.CloudError as ex:
            caught, text = ex, traceback.format_exc()
        else:
            self.fail("Expected the upload to fail")
        self.assertEqual(flaky.tries, blobs.PUT_ATTEMPTS)
        self.assertEqual(str(caught), "Upload to cloud storage was interrupted: 0.1 MB, 5 tries, last "
                                      "BrokenPipeError [Errno 32 EPIPE]. Check the connection and try again.")
        self.assertEqual((caught.code, caught.detail), ("connection_failed", "BrokenPipeError [Errno 32 EPIPE]"))
        for leak in (query, marker, "storage.test", self.service.key, "Broken pipe sending"):
            self.assertNotIn(leak, text + self.console.getvalue())

    def test_expired_url_is_refreshed_before_a_retry(self):
        clock = [1_000_000.0]
        def stamp(seconds):
            return datetime.fromtimestamp(clock[0] + seconds, timezone.utc).isoformat().replace("+00:00", "Z")
        flaky = Flaky(self.service, 1)
        issued = []
        def transport(request, *, timeout):
            if request.get_method() == "PUT" and flaky.tries == 0:
                clock[0] += 900  # The first try stalls until its URL has expired.
            reply = flaky(request, timeout=timeout)
            if request.full_url.endswith("/upload-urls"):
                issued.append(1)
                rows = json.loads(reply.getvalue())["items"]
                reply = Reply({"items": [{**row, "put_url": f"{row['put_url']}?try={len(issued)}",
                                          "expires_at": stamp(900)} for row in rows]})
            return reply
        with patch.object(blobs.time, "time", lambda: clock[0]):
            blobs.upload_missing([self.item], client=self.client(transport))
        self.assertEqual([url.rsplit("=", 1)[1] for url in flaky.urls], ["1", "2"])
        self.assertIn(self.item["sha256"], self.service.stored)

    def test_url_near_expiry_is_refreshed_and_a_blob_stored_meanwhile_is_not_put(self):
        for rows, puts in ((None, 1), ([], 0)):
            with self.subTest(rows=rows):
                self.service.stored.clear()
                issued = []
                def transport(request, *, timeout):
                    reply = self.service(request, timeout=timeout)
                    if request.full_url.endswith("/upload-urls"):
                        issued.append(1)
                        items = json.loads(reply.getvalue())["items"]
                        if len(issued) == 1:  # Issued long ago in a batch: past its expiry.
                            items = [{**row, "expires_at": "2000-01-01T00:00:00.000Z"} for row in items]
                        reply = Reply({"items": items if len(issued) == 1 or rows is None else rows})
                    return reply
                blobs.upload_missing([self.item], client=self.client(transport))
                self.assertEqual(len(issued), 2)
                self.assertEqual(len(self.service.stored), puts)

    def test_cancel_stops_retries_and_a_body_mid_stream(self):
        cancel = threading.Event()
        def stop():
            cancel.set()
            return broken_pipe()
        flaky = Flaky(self.service, 99, stop)
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.upload_missing([self.item], client=self.client(flaky), cancel=cancel)
        self.assertEqual((caught.exception.code, flaky.tries), ("canceled", 1))
        cancel.clear()
        def midway(request, *, timeout):
            if request.get_method() == "PUT":
                flaky.tries += 1
                request.data.read(8192)
                cancel.set()
                request.data.read(8192)
            return self.service(request, timeout=timeout)
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.upload_missing([self.item], client=self.client(midway), cancel=cancel)
        self.assertEqual((caught.exception.code, flaky.tries), ("canceled", 2))
        self.assertEqual(self.service.stored, {})


class MultipartTest(unittest.TestCase):
    """Contract 0.6 multipart: large files go in parts, each with the whole-file PUT's retries."""
    PART = 64 * 1024

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = FakeBlobs(part_size=self.PART)
        fake = types.SimpleNamespace(app=types.SimpleNamespace(version=(5, 2, 0)))
        self.console = io.StringIO()
        for context in (patch.dict(sys.modules, {"bpy": fake}), patch.object(blobs, "RETRY_DELAYS", (0, 0, 0, 0)),
                        contextlib.redirect_stdout(self.console)):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.capability = {"min_size": 2 * self.PART}
        self.scene = self.file("scene.blend", bytes(range(256)) * (self.PART * 7 // 2 // 256))  # 3.5 parts
        self.small = self.file("wood.png", b"small texture")

    def file(self, name, content):
        path = self.root / name
        path.write_bytes(content)
        return {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
                "mime": "application/octet-stream"}

    def client(self, transport=None):
        return cloud_http.Client(base_url="https://service.test", api_key=self.service.key,
                                 transport=transport or self.service)

    def posts(self):
        return [call["url"].removeprefix("https://service.test") for call in self.service.calls if call["method"] == "POST"]

    def test_large_files_go_in_parts_and_small_ones_in_one_put(self):
        steps = []
        uploaded = blobs.upload_missing([self.scene, self.small], client=self.client(), progress=steps.append,
                                        multipart=self.capability)
        self.assertEqual(sorted(uploaded), sorted([self.scene["sha256"], self.small["sha256"]]))
        self.assertEqual(self.service.stored[self.scene["sha256"]], self.scene["path"].read_bytes())
        self.assertEqual([n for _, n in self.service.part_puts], [1, 2, 3, 4])
        self.assertEqual(self.service.puts, [self.small["sha256"]])
        self.assertEqual(steps, [f"Uploading scene.blend: part {n}/4…" for n in (1, 2, 3, 4)])
        self.assertEqual(self.posts(), ["/v1/blobs/check", "/v1/blobs/multipart/start", "/v1/blobs/multipart/complete",
                                        "/v1/blobs/upload-urls"])
        for call in self.service.calls:
            self.assertEqual("authorization" in call["headers"], call["method"] != "PUT")
        self.service.calls.clear()
        self.assertEqual(blobs.upload_missing([self.scene, self.small], client=self.client(), multipart=self.capability), [])
        self.assertEqual(self.posts(), ["/v1/blobs/check"])

    def test_without_the_capability_or_below_its_min_size_one_put_is_used(self):
        for capability in (None, {"min_size": 10 ** 9}, {"min_size": 0}, {"min_size": True}, {"min_size": "1"}, "yes"):
            with self.subTest(capability=capability):
                self.service.stored.clear()
                self.service.puts.clear()
                blobs.upload_missing([self.scene], client=self.client(), multipart=capability)
                self.assertEqual((self.service.puts, self.service.part_puts), ([self.scene["sha256"]], []))
        self.assertEqual(blobs.multipart_capability({"min_size": 5, "extra": 1}), {"min_size": 5})

    def test_a_server_with_multipart_switched_off_gets_one_put(self):
        self.service.part_size = None
        self.assertEqual(blobs.upload_missing([self.scene], client=self.client(), multipart=self.capability),
                         [self.scene["sha256"]])
        self.assertEqual(self.service.puts, [self.scene["sha256"]])
        self.assertEqual(self.posts(), ["/v1/blobs/check", "/v1/blobs/multipart/start", "/v1/blobs/upload-urls"])

    def test_a_hash_stored_meanwhile_needs_no_parts(self):
        self.service.stored[self.scene["sha256"]] = self.scene["path"].read_bytes()
        def transport(request, *, timeout):
            if request.full_url.endswith("/v1/blobs/check"):
                self.service(request, timeout=timeout)
                return Reply({"missing": [self.scene["sha256"]]})  # Stored between check and start.
            return self.service(request, timeout=timeout)
        self.assertEqual(blobs.upload_missing([self.scene], client=self.client(transport), multipart=self.capability), [])
        self.assertEqual((self.service.part_puts, self.service.puts), ([], []))

    def test_a_broken_part_is_sent_again_on_its_own(self):
        tries = []
        def transport(request, *, timeout):
            if request.get_method() == "PUT" and request.full_url.endswith("/2"):
                tries.append(1)
                if len(tries) <= 2:
                    request.data.read(5000)  # Part of the body went out before the connection broke.
                    raise broken_pipe()
            return self.service(request, timeout=timeout)
        steps = []
        blobs.upload_missing([self.scene], client=self.client(transport), progress=steps.append, multipart=self.capability)
        self.assertEqual(self.service.stored[self.scene["sha256"]], self.scene["path"].read_bytes())
        self.assertEqual(([n for _, n in self.service.part_puts], len(tries)), ([1, 2, 3, 4], 3))
        self.assertEqual(steps[1:4], ["Uploading scene.blend: part 2/4…", "Part 2/4 interrupted, retrying (2/5)…",
                                      "Part 2/4 interrupted, retrying (3/5)…"])
        self.assertIn("Loopcut: 0.2 MB upload (part 2/4) interrupted (BrokenPipeError [Errno 32 EPIPE]); retrying (2/5)",
                      self.console.getvalue())

    def test_a_part_that_keeps_breaking_fails_naming_the_part_and_aborts(self):
        marker = "synthetic" + "-signature"
        def transport(request, *, timeout):
            if request.get_method() == "PUT" and request.full_url.endswith("/3"):
                raise urllib.error.URLError(BrokenPipeError(32, f"Broken pipe to {request.full_url}?X-Amz-Signature={marker}"))
            return self.service(request, timeout=timeout)
        try:
            blobs.upload_missing([self.scene], client=self.client(transport), multipart=self.capability)
        except cloud_http.CloudError as ex:
            caught, text = ex, traceback.format_exc()
        else:
            self.fail("Expected the upload to fail")
        self.assertEqual(str(caught), "Upload to cloud storage was interrupted: 0.2 MB (part 3/4), 5 tries, last "
                                      "BrokenPipeError [Errno 32 EPIPE]. Check the connection and try again.")
        self.assertEqual(caught.code, "connection_failed")
        self.assertEqual(self.service.aborted, ["upload-1"])
        self.assertNotIn("/v1/blobs/multipart/complete", self.posts())
        self.assertEqual(self.service.stored, {})
        for leak in (marker, "storage.test", self.service.key):
            self.assertNotIn(leak, text + self.console.getvalue())

    def test_a_refused_part_is_final_and_aborts(self):
        puts = []
        def transport(request, *, timeout):
            if request.get_method() == "PUT":
                puts.append(1)
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"<Error/>"))
            return self.service(request, timeout=timeout)
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.upload_missing([self.scene], client=self.client(transport), multipart=self.capability)
        self.assertEqual(str(caught.exception), "Cloud storage refused the 0.2 MB upload (part 1/4) (HTTP 403)")
        self.assertEqual((len(puts), self.service.aborted), (1, ["upload-1"]))

    def test_cancel_mid_part_stops_and_aborts_without_completing(self):
        cancel = threading.Event()
        def transport(request, *, timeout):
            if request.get_method() == "PUT" and request.full_url.endswith("/2"):
                request.data.read(8192)
                cancel.set()
                request.data.read(8192)
            return self.service(request, timeout=timeout)
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.upload_missing([self.scene], client=self.client(transport), cancel=cancel, multipart=self.capability)
        self.assertEqual(caught.exception.code, "canceled")
        self.assertEqual(([n for _, n in self.service.part_puts], self.service.aborted), ([1], ["upload-1"]))
        self.assertNotIn("/v1/blobs/multipart/complete", self.posts())

    def test_part_urls_near_expiry_are_refreshed_once_for_the_rest(self):
        def transport(request, *, timeout):
            reply = self.service(request, timeout=timeout)
            if request.full_url.endswith("/multipart/start"):
                reply = Reply({**json.loads(reply.getvalue()), "expires_at": "2000-01-01T00:00:00Z"})
            return reply
        blobs.upload_missing([self.scene], client=self.client(transport), multipart=self.capability)
        refreshes = [call["payload"] for call in self.service.calls if call["url"].endswith("/multipart/urls")]
        self.assertEqual(refreshes, [{"sha256": self.scene["sha256"], "upload_id": "upload-1", "part_numbers": [1, 2, 3, 4]}])
        self.assertIn(self.scene["sha256"], self.service.stored)

    def test_multipart_calls_ride_out_a_rate_limit_and_a_dropped_connection(self):
        answers = [Reply({"error": {"message": "Too many requests.", "type": "rate_limited", "code": "rate_limited",
                                    "retry_after": 0}}, status=429), broken_pipe()]
        def transport(request, *, timeout):
            if request.full_url.endswith("/multipart/complete") and answers:
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return self.service(request, timeout=timeout)
        self.assertEqual(blobs.upload_missing([self.scene], client=self.client(transport), multipart=self.capability),
                         [self.scene["sha256"]])
        self.assertEqual((answers, self.service.aborted), ([], []))
        self.assertIn("multipart complete failed (rate_limited); retrying (2/3)", self.console.getvalue())

    def test_unusable_replies_fail_before_or_after_parts_and_abort(self):
        def drop_part(request, *, timeout):
            reply = self.service(request, timeout=timeout)
            if request.full_url.endswith("/multipart/start"):
                data = json.loads(reply.getvalue())
                reply = Reply({**data, "parts": data["parts"][:-1]})
            return reply
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.upload_missing([self.scene], client=self.client(drop_part), multipart=self.capability)
        self.assertEqual((caught.exception.code, self.service.part_puts, self.service.aborted),
                         ("invalid_response", [], ["upload-1"]))
        def no_etag(request, *, timeout):
            reply = self.service(request, timeout=timeout)
            if request.get_method() == "PUT":
                reply.headers = {}
            return reply
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.upload_missing([self.scene], client=self.client(no_etag), multipart=self.capability)
        self.assertEqual(str(caught.exception), "Cloud storage did not confirm part 1/4 of the 0.2 MB upload")
        self.assertEqual(self.service.aborted, ["upload-1", "upload-2"])

    def test_a_file_changed_since_hashing_is_refused_before_any_call(self):
        with self.assertRaises(ValueError):
            blobs.put_multipart(self.scene["path"], "0" * 64, self.scene["size"], "application/x-blender", client=self.client())
        self.assertEqual(self.service.calls, [])


class MockMultipartTest(unittest.TestCase):
    def test_parts_through_the_contract_mock(self):
        try:
            mock = MockTransport(multipart_min_size=2 * 1024, multipart_part_size=1024)
        except TypeError:
            self.skipTest("the contract mock predates multipart (0.6 blob_multipart)")
        with tempfile.TemporaryDirectory() as directory:
            client = cloud_http.Client(base_url="http://127.0.0.1:3000", api_key="lc_demo", transport=mock,
                                       client_headers={"x-loopcut-contract": "0.5", "x-loopcut-client": "test"})
            manifest = client.request("GET", "/v1/manifest")
            capability = blobs.multipart_capability(manifest["capabilities"]["blob_multipart"])
            path = Path(directory) / "scene.blend"
            path.write_bytes(bytes(range(256)) * 10)
            digest = blobs.sha256(path)
            self.assertEqual(blobs.upload_missing([{"path": path}], client=client, multipart=capability), [digest])
            self.assertEqual([(c["method"], c["path"].rsplit("/", 1)[-1]) for c in mock.calls][1:],
                             [("POST", "check"), ("POST", "start"), ("PUT", "1"), ("PUT", "2"), ("PUT", "3"),
                              ("POST", "complete")])
            self.assertEqual(client.request("POST", "/v1/blobs/check", {"sha256": [digest]}), {"missing": []})


class CloudHttpTest(unittest.TestCase):
    def test_transport_failures_are_described_without_their_text(self):
        private = "https://storage.test/x?X-Amz-" + "Signature=synthetic " + "lc_" + "synthetic"
        for error, expected in (
                (urllib.error.URLError(BrokenPipeError(32, private)), "BrokenPipeError [Errno 32 EPIPE]"),
                (ssl.SSLError(1, "[SSL: SSLV3_ALERT_BAD_RECORD_MAC] " + private), "SSLError SSLV3_ALERT_BAD_RECORD_MAC"),
                (ssl.SSLError(1, private), "SSLError"),
                (urllib.error.URLError(socket.timeout(private)), "TimeoutError (timed out)"),
                (socket.gaierror(8, private), "gaierror (name lookup failed)"),
                (http.client.RemoteDisconnected(private), "RemoteDisconnected"),
                (http.client.IncompleteRead(private.encode()), "IncompleteRead"),
                (urllib.error.URLError(private), "URLError")):
            with self.subTest(expected=expected):
                self.assertEqual(cloud_http.describe(error), expected)

    def test_service_connection_errors_keep_the_cause(self):
        def send(request, *, timeout):
            raise urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, "reset " + "lc_" + "synthetic"))
        with contextlib.redirect_stdout(io.StringIO()) as console, self.assertRaises(cloud_http.CloudError) as caught:
            cloud_http.Client(api_key="lc_" + "fake-for-tests", transport=send).request("GET", "/v1/me")
        cause = f"ConnectionResetError [Errno {errno.ECONNRESET} ECONNRESET]"  # 54 on macOS, 104 on Linux CI.
        self.assertEqual((str(caught.exception), caught.exception.detail, caught.exception.code),
                         (f"Could not reach the Loopcut service ({cause})", cause, "connection_failed"))
        self.assertEqual(console.getvalue(), f"Loopcut: GET request failed in transport: {cause}\n")

    def test_contract_error_envelopes_preserve_status_code_type_and_extra_fields(self):
        for status, code, extra in ((402, "insufficient_credits", {"balance": 1, "needed": 2, "upgrade": "/pricing"}),
                                    (426, "contract_unsupported", {"min_client": "0.2"}),
                                    (502, "vendor_failed", {"refunded": True})):
            for raised in (True, False):
                envelope = {"error": {"message": "Test error", "type": "contract_error", "code": code, **extra}}

                def send(request, *, timeout):
                    if raised:
                        raise urllib.error.HTTPError(request.full_url, status, "error", {},
                                                     io.BytesIO(json.dumps(envelope).encode()))
                    return Reply(envelope, status=status)

                client = cloud_http.Client(api_key="lc_" + "fake-for-tests", transport=send)
                with self.subTest(code=code, raised=raised), self.assertRaises(cloud_http.CloudError) as caught:
                    client.request("POST", "/v1/blobs/check", {"sha256": []})
                error = caught.exception
                self.assertEqual((str(error), error.status, error.code, error.error_type),
                                 ("Test error", status, code, "contract_error"))
                self.assertEqual(error.extra, extra)

    def test_default_url_and_stored_key_are_used(self):
        service = FakeBlobs()
        with patch.object(config, "SERVICE_URL", "https://service.test"), \
                patch.object(cloud_http.credentials, "api_key", return_value=service.key) as stored:
            self.assertEqual(cloud_http.Client(transport=service).request("POST", "/v1/blobs/check",
                                                                         {"sha256": []}), {"missing": []})
        stored.assert_called_once_with("https://service.test/v1")

    def test_signed_out_get_preserves_etag_and_304(self):
        def send(request, *, timeout):
            headers = {k.lower(): v for k, v in request.header_items()}
            self.assertEqual(headers["x-loopcut-contract"], "0.7")
            self.assertNotIn("authorization", headers)
            self.assertEqual(headers["if-none-match"], "cached")
            return Reply(status=304, headers={"ETag": "cached"})
        reply = cloud_http.Client(api_key="", transport=send).request_response(
            "GET", "/v1/manifest", auth=False, headers={"If-None-Match": "cached"})
        self.assertEqual((reply.data, reply.status, reply.headers), (None, 304, {"ETag": "cached"}))

    def test_urllib_304_is_a_cache_hit_even_when_raised_as_http_error(self):
        def send(request, *, timeout):
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {"ETag": "cached"}, io.BytesIO())
        reply = cloud_http.Client(transport=send).request_response("GET", "/v1/manifest", auth=False)
        self.assertEqual((reply.data, reply.status, reply.headers), (None, 304, {"ETag": "cached"}))

    def test_bad_json_and_non_object_responses_are_errors(self):
        for body in (b"not json", b"[]"):
            def send(request, *, timeout):
                reply = Reply()
                reply.write(body)
                reply.seek(0)
                return reply
            with self.subTest(body=body), self.assertRaises(cloud_http.CloudError) as caught:
                cloud_http.Client(transport=send).request("GET", "/v1/manifest", auth=False)
            self.assertEqual(caught.exception.code, "invalid_response")

    def test_url_path_and_header_validation_keeps_auth_on_the_service(self):
        client = cloud_http.Client(api_key="lc_" + "fake-for-tests")
        for path in ("https://other.test/path", "//other.test/path", "/bad\r\nheader", "/bad\\path"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.request("GET", path)
        with self.assertRaises(ValueError):
            client.request("GET", "/v1/me", headers={"AUTHORIZATION": "override"})
        with self.assertRaises(cloud_http.CloudError) as caught:
            cloud_http.Client(api_key="").request("GET", "/v1/me")
        self.assertEqual(caught.exception.code, "sign_in_required")


class MockBlobsTest(unittest.TestCase):
    def test_mock_check_upload_urls_put_then_no_second_upload(self):
        with MockService() as service, tempfile.TemporaryDirectory() as directory:
            calls = []
            def transport(request, **options):
                calls.append((request.method, request.full_url, dict(request.header_items())))
                return cloud_http.urllib_transport(request, **options)
            client = cloud_http.Client(base_url=service.url, api_key="lc_demo", transport=transport,
                                       client_headers={"x-loopcut-contract": "0.1", "x-loopcut-client": "test"})
            path = Path(directory) / "image.png"
            path.write_bytes(b"mock blob bytes")
            digest = blobs.sha256(path)
            self.assertEqual(blobs.upload_missing([{"path": path}], client=client), [digest])
            self.assertEqual([method for method, _, _ in calls], ["POST", "POST", "PUT"])
            self.assertTrue(calls[0][1].endswith("/v1/blobs/check"))
            self.assertTrue(calls[1][1].endswith("/v1/blobs/upload-urls"))
            self.assertNotIn("authorization", {k.lower(): v for k, v in calls[2][2].items()})
            calls.clear()
            self.assertEqual(blobs.upload_missing([{"path": path}], client=client), [])
            self.assertEqual([(method, url) for method, url, _ in calls], [("POST", service.url + "/v1/blobs/check")])


if __name__ == "__main__":
    unittest.main()
