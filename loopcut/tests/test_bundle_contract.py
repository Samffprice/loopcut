"""Check real client payloads against the sibling platform's schemas when available.

The validator stays owned by the platform; no schema copy or dependency enters the add-on.
platform_contract finds it (LOOPCUT_CONTRACT_DIR or a sibling checkout). HTTP is mocked.
"""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
sys.path.insert(0, str(REPO / "loopcut" / "harness"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import blobs, bundles, cloud_http  # noqa: E402
from fake_blobs import FakeBlobs, Reply  # noqa: E402
import platform_contract  # noqa: E402


class BundleContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        contract = platform_contract.require()  # Skips the class without the private contract.
        spec = importlib.util.spec_from_file_location("loopcut_contract_validate", contract / "conformance/validate.py")
        cls.validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.validator)
        cls.schemas = cls.validator.load_schemas(contract / "schemas")

    def assert_conforms(self, value, schema):
        self.assertEqual(self.validator.validate(value, schema, self.schemas), [])

    def test_uploaded_manifest_and_batched_blob_requests_conform(self):
        service = FakeBlobs()
        info = {"blender": "5.2.0", "created_at": "2026-10-03T18:00:00Z",
                "scene": {"name": "Scene", "camera": None, "frame_start": 1, "frame_end": 2, "fps": 24},
                "missing": [], "unsupported": [], "system": ["/SYSTEM/brush.blend"], "extra": "local"}
        source = {"kind": "checkpoint", "session": "session", "checkpoint": "checkpoint", "extra": "local"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scene.blend").write_bytes(b"scene")
            (root / "texture.png").write_bytes(b"texture")
            value = bundles.manifest(root, info, source)
            self.assert_conforms(value, "bundle-manifest")
            digest = bundles.upload(root, value, client=service.client())
            self.assert_conforms(json.loads(service.stored[digest]), "bundle-manifest")
            # Direct callers can supply local descriptors; only contract keys go on the wire.
            items = [{**item, "mime": "application/octet-stream", "local": True} for item in value["files"]]
            blobs.check([item["sha256"] for item in items] * 2, client=service.client(), batch_size=1)
            rows = blobs.upload_urls(items, client=service.client(), batch_size=1)
            self.assert_conforms({"items": rows}, "upload-urls-response")
        checks, uploads = 0, 0
        for call in service.calls:
            if call["url"].endswith("/check"):
                checks += 1
                self.assert_conforms(call["payload"], "blobs-check-request")
            elif call["url"].endswith("/upload-urls"):
                uploads += 1
                self.assert_conforms(call["payload"], "upload-urls-request")
        self.assertGreater(checks, 1)
        self.assertGreater(uploads, 1)

    def test_blob_request_schema_boundaries_and_empty_check_response(self):
        payloads = []

        def transport(request, *, timeout):
            payload = json.loads(request.data)
            payloads.append(payload)
            if request.full_url.endswith("/check"):
                reply = {"missing": []}
                self.assert_conforms(payload, "blobs-check-request")
                self.assert_conforms(reply, "blobs-check-response")
            else:
                self.assert_conforms(payload, "upload-urls-request")
                reply = {"items": [{"sha256": item["sha256"], "put_url": "https://storage.test/blob",
                                    "expires_at": "2099-01-01T00:00:00Z"} for item in payload["items"]]}
                self.assert_conforms(reply, "upload-urls-response")
            return Reply(reply)

        client = cloud_http.Client(api_key="lc_" + "fake-for-tests", transport=transport)
        self.assertEqual(blobs.check(["a" * 64], client=client), [])
        for size in (0, 209715200):
            blobs.upload_urls([{"sha256": "a" * 64, "size": size, "mime": "application/octet-stream"}], client=client)
        with self.assertRaises(ValueError):
            blobs.upload_urls([{"sha256": "a" * 64, "size": 209715201, "mime": "application/octet-stream"}], client=client)
        self.assertEqual(len(payloads), 3)

    def test_schema_valid_error_envelope_is_preserved(self):
        envelope = {"error": {"message": "Upgrade client", "type": "contract_unsupported",
                              "code": "contract_unsupported", "min_client": "0.2"}}
        self.assert_conforms(envelope, "error")
        client = cloud_http.Client(api_key="lc_" + "fake-for-tests",
                                   transport=lambda *a, **kw: Reply(envelope, status=426))
        with self.assertRaises(cloud_http.CloudError) as caught:
            blobs.check(["a" * 64], client=client)
        error = caught.exception
        self.assertEqual((error.status, error.code, error.error_type), (426, "contract_unsupported", "contract_unsupported"))
        self.assertEqual(error.extra, {"min_client": "0.2"})


if __name__ == "__main__":
    unittest.main()
