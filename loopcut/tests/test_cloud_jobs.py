"""Managed jobs, SSE/retry semantics and result provenance, without running Blender."""

import copy
import hashlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

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

    def test_report_handler_keeps_text_and_conversation_and_returns_report(self):
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
            summary = cloud_jobs.apply_result({"apply": "attach_report"}, self.local_job())
        self.assertIn('"ok": true', summary)
        self.assertIn("QA report", summary)
        self.assertEqual(texts[0]["content"], self.path.read_text())
        self.assertEqual(json.loads(texts[0]["loopcut_provenance"])["backend"], "byok")
        self.assertEqual(session["items"][0]["kind"], "notice")
        # A full notice item: the panel's notice layout reads its button fields (KeyError 'checkpoint' before).
        self.assertEqual(session["items"][0], state.item_notice(session["items"][0]["text"]))
        save.assert_called_once_with(session)

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
            image = types.SimpleNamespace()
            image.save = lambda: Path(image.filepath_raw).write_bytes(b"png")
            copied.append(image)
            return image
        image = types.SimpleNamespace(copy=copy_image, filepath_raw="original", file_format="JPEG")
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
                         matrix_world=FakeMatrix(), bound_box=[(0, 0, 0), (1, 1, 1)])

    def select_set(self, value):
        self.selected = value


class FakeMatrix:
    def __matmul__(self, other):
        return other

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
                                           cursor=types.SimpleNamespace(location=(0, 0, 0)))
        self.window = types.SimpleNamespace(screen="screen")
        self.overrides = []
        layer_objects = FakeList()
        layer_objects.active = None
        self.data = types.SimpleNamespace(objects=FakeList(FakeObject), collections=FakeList(FakeCollection),
                                          images=FakeList(), materials=FakeList(), scenes=[self.scene])
        self.calls = []
        context = types.SimpleNamespace(scene=self.scene, window=None, temp_override=self.temp_override,
                                        window_manager=types.SimpleNamespace(windows=[self.window]),
                                        view_layer=types.SimpleNamespace(update=lambda: None, objects=layer_objects))
        self.ops = types.SimpleNamespace(wm=types.SimpleNamespace(fbx_import=self.importer("wm.fbx_import")),
                                         import_scene=types.SimpleNamespace(fbx=self.importer("import_scene.fbx"),
                                                                            gltf=self.importer("import_scene.gltf")))
        self.bpy = types.SimpleNamespace(data=self.data, context=context, ops=self.ops,
                                         path=types.SimpleNamespace(abspath=lambda value: value))
        mathutils = types.SimpleNamespace(Vector=tuple)
        modules = patch.dict(sys.modules, {"bpy": self.bpy, "mathutils": mathutils})
        modules.start()
        self.addCleanup(modules.stop)

    def temp_override(self, **values):
        import contextlib
        self.overrides.append(values)
        return contextlib.nullcontext()

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
