"""Contract headers, envelope errors and missing-only streaming uploads, without HTTP or Blender."""

import hashlib
import io
import json
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
sys.path.insert(0, str(REPO / "loopcut" / "harness"))
sys.path.insert(0, str(Path(__file__).parent))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import bl_info, blobs, cloud_http, config  # noqa: E402
from fake_blobs import FakeBlobs, Reply  # noqa: E402
from mock_service import MockService  # noqa: E402


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
            self.assertEqual(call["headers"]["x-loopcut-client"], f"loopcut-addon/{version} blender/5.2.0")
            self.assertEqual(call["headers"]["x-loopcut-contract"], "0.5")
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
        for rows in ([], [{"sha256": item["sha256"], "put_url": "file:///etc/passwd", "expires_at": "later"}]):
            client = cloud_http.Client(api_key=self.service.key,
                                       transport=lambda *a, **k: Reply({"items": rows}))
            with self.subTest(rows=rows), self.assertRaises(cloud_http.CloudError):
                blobs.upload_urls([item], client=client)
        self.assertEqual(self.service.puts, [])


class CloudHttpTest(unittest.TestCase):
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
            self.assertEqual(headers["x-loopcut-contract"], "0.5")
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
