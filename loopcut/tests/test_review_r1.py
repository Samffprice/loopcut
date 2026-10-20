"""R1 regressions for secrets, immutable uploads, policy and filesystem boundaries.

All HTTP and Blender work is simulated; only temporary files and Python workers run.
"""

import contextlib
import errno
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
import types
import unittest
import urllib.error
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts/addons_core"))
sys.path.insert(0, str(REPO / "loopcut/harness"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import blobs, bundles, checkpoints, cloud_http, config, credentials  # noqa: E402
from loopcut.byok import catalog, runner  # noqa: E402
from loopcut.byok.http import ByokError, Client, Response  # noqa: E402
from fake_blobs import FakeBlobs, Reply  # noqa: E402


class ReviewTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {"LOOPCUT_CONFIG_DIR": str(self.root),
                                              **{name: "" for name in credentials.VENDOR_KEY_NAMES}})
        environment.start()
        self.addCleanup(environment.stop)

    def test_concurrent_account_and_vendor_saves_keep_both_sections(self):
        original_read = credentials._read
        barrier = threading.Barrier(2)

        def read(*args, **kwargs):
            data = original_read(*args, **kwargs)
            try:
                barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
            return data

        with patch.object(credentials, "_read", side_effect=read), ThreadPoolExecutor(2) as pool:
            first = pool.submit(credentials.store, "https://account.example/v1", "test-account")
            second = pool.submit(credentials.set_vendor_key, "OPENAI_API_KEY", "test-vendor")
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual(credentials.api_key("https://account.example/v1"), "test-account")
        self.assertEqual(credentials.vendor_key("OPENAI_API_KEY"), "test-vendor")

    def test_separate_python_processes_keep_both_credential_sections(self):
        code = '''
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from loopcut import credentials
root, number = Path(sys.argv[2]), sys.argv[3]
original_read = credentials._read
def read(*args, **kwargs):
    data = original_read(*args, **kwargs)
    (root / (number + '.ready')).touch()
    deadline = time.monotonic() + 0.5
    while len(list(root.glob('*.ready'))) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    return data
credentials._read = read
if number == '1':
    credentials.store('https://account.example/v1', 'test-account')
else:
    credentials.set_vendor_key('OPENAI_API_KEY', 'test-vendor')
'''
        processes = [subprocess.Popen([sys.executable, "-c", code, str(REPO / "scripts/addons_core"),
                                       str(self.root), str(number)], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) for number in (1, 2)]
        for process in processes:
            _, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertEqual(credentials.api_key("https://account.example/v1"), "test-account")
        self.assertEqual(credentials.vendor_key("OPENAI_API_KEY"), "test-vendor")

    def test_unreadable_or_corrupt_credentials_are_not_replaced(self):
        path = self.root / credentials.FILE_NAME
        for raw in ('{"api_keys":', '[]', '{"api_keys": null}'):
            path.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(OSError):
                credentials.set_vendor_key("OPENAI_API_KEY", "test-vendor")
            self.assertEqual(path.read_text(), raw)
        path.write_text('{"api_keys": {"https://account.example/v1": "test-account"}}')
        before = path.read_bytes()
        with patch.object(Path, "read_text", side_effect=PermissionError("unreadable")):
            with self.assertRaises(OSError):
                credentials.set_vendor_key("OPENAI_API_KEY", "test-vendor")
        self.assertEqual(path.read_bytes(), before)

    def test_cloud_error_redacts_echoed_auth_in_all_fields_and_tracebacks(self):
        key = "lc_" + "not-a-real-secret"
        for raised in (False, True):
            def transport(request, *, timeout):
                payload = {"error": {"code": "invalid_api_key", "type": "invalid_api_key",
                                      "message": "Bad token " + key, "debug": {key: [key]}}}
                if raised:
                    raise urllib.error.HTTPError(request.full_url, 401, key, {},
                                                 io.BytesIO(json.dumps(payload).encode()))
                return Reply(payload, status=401)

            try:
                cloud_http.Client(api_key=key, transport=transport).request("GET", "/v1/me")
            except cloud_http.CloudError as error:
                self.assertNotIn(key, str(error) + repr(vars(error)) + traceback.format_exc())
            else:
                self.fail("Expected an HTTP error")

    def test_connection_errors_do_not_expose_raw_exception_text(self):
        secret = "synthetic-private-details"
        for client in (cloud_http.Client(api_key="lc_test", transport=lambda *a, **k: self.fail_transport(secret)),
                       Client(lambda *a, **k: self.fail_transport(secret))):
            try:
                if isinstance(client, cloud_http.Client):
                    client.request("GET", "/v1/me")
                else:
                    client.request("POST", "https://api.openai.com/v1/images/generations")
            except (cloud_http.CloudError, ByokError):
                self.assertNotIn(secret, traceback.format_exc())
            else:
                self.fail("Expected a connection error")

    def test_connection_errors_name_what_failed_without_its_text(self):
        # A cloud render's upload failed as a bare "Could not reach the Loopcut service" while the
        # service answered (live test 2026-10-17): say which kind of failure, never its message.
        import ssl
        secret = "https://storage.example/bundle?X-Amz-Signature=synthetic-private-details"
        tls = ssl.SSLError(1, secret)
        tls.reason = "BAD_RECORD_MAC"
        odd = ssl.SSLError(1, secret)
        odd.reason = secret
        # cloud_http.describe() is the one format: the class, then the TLS reason or errno name.
        for raised, reason in ((urllib.error.URLError(tls), "SSLError BAD_RECORD_MAC"),
                               (urllib.error.URLError(odd), "SSLError"),
                               (urllib.error.URLError(TimeoutError(secret)), "TimeoutError (timed out)"),
                               (ConnectionResetError(errno.ECONNRESET, secret),
                                f"ConnectionResetError [Errno {errno.ECONNRESET} ECONNRESET]"),
                               (urllib.error.URLError(secret), "URLError"),
                               (BrokenPipeError(secret), "BrokenPipeError")):
            def transport(*args, raised=raised, **kwargs):
                raise raised
            with self.subTest(reason=reason), contextlib.redirect_stdout(io.StringIO()) as console, \
                    self.assertRaises(cloud_http.CloudError) as caught:
                cloud_http.Client(api_key="lc_test", transport=transport).request("GET", "/v1/me")
            self.assertEqual(caught.exception.code, "connection_failed")
            self.assertEqual(str(caught.exception), f"Could not reach the Loopcut service ({reason})")
            # The reason rides on the error as detail (blobs.put names it when it gives up).
            self.assertEqual((caught.exception.detail, caught.exception.extra), (reason, {}))
            self.assertEqual(console.getvalue(), f"Loopcut: GET request failed in transport: {reason}\n")
            self.assertNotIn("synthetic-private-details", str(caught.exception) + repr(vars(caught.exception))
                             + console.getvalue() + traceback.format_exc())

    @staticmethod
    def fail_transport(message):
        raise urllib.error.URLError(message)

    def test_cloud_truncated_http_response_is_a_safe_error(self):
        client = cloud_http.Client(api_key="lc_test", transport=lambda *a, **kw:
                                   self.raise_incomplete())
        with self.assertRaises(cloud_http.CloudError) as caught:
            client.request("GET", "/v1/me")
        self.assertEqual(caught.exception.code, "connection_failed")

    @staticmethod
    def raise_incomplete():
        import http.client
        raise http.client.IncompleteRead(b"private bytes")

    def test_mutation_while_requesting_upload_url_never_puts_wrong_hash(self):
        path = self.root / "asset"
        path.write_bytes(b"before")
        digest = blobs.sha256(path)
        service = FakeBlobs()

        def transport(request, *, timeout):
            reply = service(request, timeout=timeout)
            if request.full_url.endswith("/upload-urls"):
                path.write_bytes(b"after!")
            return reply

        client = cloud_http.Client(base_url="https://service.test", api_key=service.key, transport=transport)
        with self.assertRaises(ValueError):
            blobs.upload_missing([{"path": path, "sha256": digest}], client=client)
        self.assertFalse(service.puts)
        self.assertFalse(any(call["method"] == "PUT" for call in service.calls))

    def test_mutation_during_put_cannot_change_uploaded_bytes(self):
        path = self.root / "asset"
        path.write_bytes(b"before")
        digest = blobs.sha256(path)
        service = FakeBlobs()

        def transport(request, *, timeout):
            if request.get_method() == "PUT":
                path.write_bytes(b"after!")
            return service(request, timeout=timeout)

        client = cloud_http.Client(base_url="https://service.test", api_key=service.key, transport=transport)
        blobs.upload_missing([{"path": path, "sha256": digest}], client=client)
        self.assertEqual(service.stored[digest], b"before")

    def test_oversized_blobs_fail_before_any_http_even_if_already_uploaded(self):
        path = self.root / "asset"
        path.write_bytes(b"1234")
        service = FakeBlobs()
        service.stored[blobs.sha256(path)] = b"1234"
        with patch.object(blobs, "MAX_BLOB_SIZE", 3), self.assertRaises(ValueError):
            blobs.upload_missing([{"path": path}], client=service.client())
        self.assertEqual(service.calls, [])

    def test_private_mode_has_contract_reason_and_cannot_choose_backend(self):
        tool = next(t for t in catalog.descriptors(no_generation=False, private_mode=True,
                        keys_set={"ELEVENLABS_API_KEY": True}) if t["name"] == "sfx")
        self.assertEqual(tool["unavailable_reason"], "private_mode")
        for signed in (False, True):
            self.assertEqual(catalog.choose_backend(tool, signed_in_with_credits=signed,
                             preferred_backend="byok", has_key=True),
                             {"backend": None, "unavailable_reason": "private_mode"})
        job = runner.run("sfx", {"prompt": "test"}, {}, self.root, no_generation=False, private_mode=True)
        self.assertEqual(job["error"]["code"], "private_mode")
        self.assertEqual(job["error"]["data_class"], "C")

    def test_explicit_byok_without_key_never_falls_back_to_paid_managed(self):
        tool = catalog.descriptors(no_generation=False, private_mode=False, keys_set={})[0]
        result = catalog.choose_backend(tool, signed_in_with_credits=True,
                                        preferred_backend="byok", has_key=False)
        self.assertIsNone(result["backend"])

    def test_symlink_output_directory_is_refused_before_paid_submit(self):
        outside = self.root / "outside"
        outside.mkdir()
        output = self.root / "outputs"
        output.symlink_to(outside, target_is_directory=True)
        credentials.set_vendor_key("OPENAI_API_KEY", "test-vendor")
        with patch.object(runner.openai, "execute", return_value=[
                {"name": "image_1", "mime": "image/png", "data": b"image", "meta": {}}]) as execute:
            job = runner.run("image_generate", {"prompt": "test"}, {}, output,
                             no_generation=False, private_mode=False)
        self.assertEqual(job["status"], "failed")
        execute.assert_not_called()
        self.assertFalse(list(outside.iterdir()))

    def test_unusable_output_parent_is_refused_before_paid_submit(self):
        parent = self.root / "not-a-directory"
        parent.write_bytes(b"keep")
        credentials.set_vendor_key("OPENAI_API_KEY", "test-vendor")
        with patch.object(runner.openai, "execute", return_value=[
                {"name": "image_1", "mime": "image/png", "data": b"image", "meta": {}}]) as execute:
            job = runner.run("image_generate", {"prompt": "test"}, {}, parent / "outputs",
                             no_generation=False, private_mode=False)
        self.assertEqual(job["status"], "failed")
        execute.assert_not_called()
        self.assertEqual(parent.read_bytes(), b"keep")

    def test_symlink_bundle_root_is_refused_before_child_and_manifest_read(self):
        outside = self.root / "outside"
        outside.mkdir()
        stage = self.root / "stage"
        stage.symlink_to(outside, target_is_directory=True)
        snapshot = self.root / "snapshot.blend"
        snapshot.write_bytes(b"checkpoint")
        fake = types.SimpleNamespace(app=types.SimpleNamespace(binary_path="/not-executable"))
        with patch.dict(sys.modules, {"bpy": fake}), patch.object(bundles.subprocess, "run") as child:
            with self.assertRaisesRegex(bundles.BundleError, "[Ss]ymlink"):
                bundles.collect(snapshot, stage)
        child.assert_not_called()
        (outside / "scene.blend").write_bytes(b"scene")
        info = {"blender": "5.2.0", "scene": {"name": "Scene", "camera": None,
                "frame_start": 1, "frame_end": 2, "fps": 24}}
        with self.assertRaisesRegex(bundles.BundleError, "[Ss]ymlink"):
            bundles.manifest(stage, info, {"kind": "checkpoint", "session": "s", "checkpoint": "c"})

    def test_bundle_child_rejects_preexisting_output_symlink(self):
        from test_bundles import CollectionTest
        helper = CollectionTest()
        snapshot, stage = self.root / "snapshot.blend", self.root / "stage"
        snapshot.write_bytes(b"checkpoint")
        stage.mkdir()
        (stage / "scene.blend").symlink_to(snapshot)
        scene = helper.scene()
        fake = helper.blender(types.SimpleNamespace(objects=[], scenes=[scene], libraries=[]),
                              scene, stage, lambda **kw: [])
        with patch.dict(sys.modules, {"bpy": fake}), self.assertRaises(bundles.BundleError):
            bundles._collect_opened(snapshot, stage)
        self.assertEqual(snapshot.read_bytes(), b"checkpoint")

    def on_worker(self, operation):
        """Pump real worker handoffs on the test's main thread, without a Blender timer."""
        from loopcut import mainthread
        pending = queue.Queue()

        def dispatch(fn):
            future = Future()
            pending.put((fn, future))
            return future

        with patch.object(mainthread, "run_on_main", side_effect=dispatch), ThreadPoolExecutor(1) as pool:
            result = pool.submit(operation)
            while not result.done():
                try:
                    fn, future = pending.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    future.set_result(fn())
                except Exception as error:
                    future.set_exception(error)
            return result.result(timeout=2)

    def require_main(self, value):
        self.assertIs(threading.current_thread(), threading.main_thread(), "bpy accessed off main thread")
        return value

    def test_worker_credential_lookup_dispatches_bpy_folder_access(self):
        credentials.set_vendor_key("OPENAI_API_KEY", "test-vendor")
        fake = types.SimpleNamespace(utils=types.SimpleNamespace(user_resource=lambda *a, **kw:
                                     self.require_main(str(self.root))))
        with patch.dict(os.environ, {"LOOPCUT_CONFIG_DIR": ""}), patch.dict(sys.modules, {"bpy": fake}):
            self.assertEqual(self.on_worker(lambda: credentials.vendor_key("OPENAI_API_KEY")), "test-vendor")

    def test_worker_contract_headers_dispatch_bpy_version_access(self):
        owner = self

        class App:
            @property
            def version(self):
                return owner.require_main((5, 2, 0))

        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(app=App())}):
            headers = self.on_worker(cloud_http.contract_headers)
        self.assertIn(" blender/5.2.0 os/", headers["x-loopcut-client"])  # os and arch follow since the activation work.

    def test_worker_policy_snapshot_dispatches_preference_access(self):
        with patch.object(config, "_getter", side_effect=lambda:
                self.require_main((None, lambda key, default: default))):
            self.assertEqual(self.on_worker(config.service_policy), {"no_generation": False, "private_mode": True})

    def test_worker_collection_dispatches_binary_access_but_runs_child_on_worker(self):
        owner = self

        class App:
            @property
            def binary_path(self):
                return owner.require_main("/not-executable")

        def child(command, **kw):
            self.assertIsNot(threading.current_thread(), threading.main_thread())
            spec = json.loads(Path(command[-2]).read_text())
            (Path(spec["staging"]) / "scene.blend").write_bytes(b"bundle")
            Path(command[-1]).write_text("{}")
            return types.SimpleNamespace(returncode=0)

        snapshot = self.root / "snapshot.blend"
        snapshot.write_bytes(b"checkpoint")
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(app=App())}), \
                patch.object(bundles.subprocess, "run", side_effect=child):
            self.assertEqual(self.on_worker(lambda: bundles.collect(snapshot, self.root / "stage")), {})

    def test_worker_checkpoint_handoff_resolves_store_on_main(self):
        store = types.SimpleNamespace(get=lambda key: {"expired": False, "created": 0},
                                      snapshot_path=lambda key: self.root / "snapshot.blend")
        store.snapshot_path("").write_bytes(b"checkpoint")
        with patch.object(checkpoints, "_store", side_effect=lambda session: self.require_main(store)), \
                patch.object(bundles, "collect", return_value={"missing": [], "unsupported": [], "system": []}), \
                patch.object(bundles, "manifest", return_value={}):
            self.assertEqual(self.on_worker(lambda: checkpoints.bundle_from_checkpoint(
                {"id": "session"}, "checkpoint", self.root / "stage"))["manifest"], {})

    def test_public_plaintext_urls_are_rejected_but_loopback_development_works(self):
        for url in ("http://service.example", "http://storage.example/blob", "https://service.example:bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                cloud_http.validate_url(url)
        for url in ("http://localhost:3000", "http://127.0.0.1:8765", "http://[::1]:8765"):
            self.assertEqual(cloud_http.validate_url(url).scheme, "http")

    def test_deeply_nested_json_is_a_safe_transport_error(self):
        body = b"[" * 2000 + b"]" * 2000
        for status in (200, 400):
            def transport(request, *, timeout):
                response = io.BytesIO(body)
                response.status, response.headers = status, {}
                return response

            with self.subTest(kind="cloud", status=status), self.assertRaises(cloud_http.CloudError):
                cloud_http.Client(api_key="lc_test", transport=transport).request("GET", "/v1/me")
            with self.subTest(kind="vendor", status=status), self.assertRaises(ByokError):
                Client(lambda *a: Response(status, {}, body)).json("POST", "https://api.openai.com/test")

    def test_gemini_invalid_pcm_rate_returns_failed_job_without_resubmitting(self):
        credentials.set_vendor_key("GEMINI_API_KEY", "test-vendor")
        payload = {"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;rate=" + "9" * 5000, "data": "AAAAAA=="}}]}}]}
        calls = []

        def transport(*args):
            calls.append(args[0])
            return Response(200, {}, json.dumps(payload).encode())

        job = runner.run("voice_line", {"text": "test"}, {}, self.root / "outputs", transport=transport,
                         no_generation=False, private_mode=False)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "vendor_failed")
        self.assertEqual(calls, ["POST"])

    def test_failed_collection_reads_only_log_tail(self):
        snapshot = self.root / "snapshot.blend"
        snapshot.write_bytes(b"checkpoint")

        def child(command, **kw):
            kw["stdout"].write(b"x" * 65536 + b"final failure")
            return types.SimpleNamespace(returncode=1)

        read_text = Path.read_text

        def bounded(path, *args, **kw):
            self.assertNotEqual(path.name, "blender.log", "whole log was read into memory")
            return read_text(path, *args, **kw)

        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(app=types.SimpleNamespace(binary_path="/fake"))}), \
                patch.object(bundles.subprocess, "run", side_effect=child), patch.object(Path, "read_text", bounded):
            with self.assertRaisesRegex(bundles.BundleError, "final failure"):
                bundles.collect(snapshot, self.root / "stage")


if __name__ == "__main__":
    unittest.main()
