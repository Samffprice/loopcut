"""Offline BYOK contracts: exact HTTP requests, file results, policy and credential isolation.

Every HTTP call goes through a scripted transport; no vendor keys or Blender are required.
The settings import/registration test runs in a subprocess to keep the suite's shared bpy intact.
"""

import base64
import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import wave
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

ADDONS = Path(__file__).resolve().parents[2] / "scripts" / "addons_core"
sys.path.insert(0, str(ADDONS))

from loopcut import config, credentials  # noqa: E402
from loopcut.byok import catalog, choose_backend, descriptors, run  # noqa: E402
from loopcut.byok import elevenlabs, fal, gemini, openai  # noqa: E402
from loopcut.byok.http import ByokError, Client, Response, urllib_transport  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nmock image payload"
GLB = b"glTF\x02\x00\x00\x00mock model payload"
FBX = b"Kaydara FBX Binary  \x00\x1a\x00\xe8\x1c\x00\x00mock quad payload"
MP3 = b"ID3mock sound payload"
PCM = b"\x00\x00\x01\x00\xff\x7f"


def reply(payload, status=200, headers=None):
    return Response(status, headers or {}, json.dumps(payload).encode())


def image_reply():
    return reply({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})


def voice_reply():
    return reply({"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(PCM).decode()}}]}}]})


class FakeTransport:
    """Record wire requests and consume replies. Extra calls fail instead of reaching urllib."""
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout})
        if not self.replies:
            raise AssertionError("Unexpected HTTP call")
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ByokTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {"LOOPCUT_CONFIG_DIR": str(self.root / "config"),
                                                   **{k: "" for k in credentials.VENDOR_KEY_NAMES}})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in credentials.VENDOR_KEY_NAMES:
            credentials.set_vendor_key(name, "test-" + name)
        self.reference = self.root / "reference.png"
        self.reference.write_bytes(PNG)
        self.model = self.root / "input.glb"
        self.model.write_bytes(GLB)
        self.sleep = patch.object(fal.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def job(self, tool, transport, inputs=None, files=None, **policy):
        defaults = {"image_generate": {"prompt": "a chair"}, "image_edit": {"prompt": "make it green"},
                    "image_to_3d": {}, "retopology": {}, "voice_line": {"text": "Hello"},
                    "sfx": {"prompt": "a bell"}}
        selected = {"image_edit": {"reference": self.reference}, "image_to_3d": {"reference": self.reference},
                    "retopology": {"model": self.model}}
        return run(tool, defaults.get(tool, {}) if inputs is None else inputs,
                   selected.get(tool, {}) if files is None else files, self.root / "outputs",
                   transport=transport, no_generation=policy.get("no_generation", False),
                   private_mode=policy.get("private_mode", False))

    def assert_output(self, job, expected, output_type, mime, vendor, model):
        self.assertEqual(job["status"], "succeeded", job["error"])
        self.assertIsNone(job["error"])
        self.assertEqual(job["credits"], {"reserved": 0, "charged": 0, "refunded": 0})
        self.assertEqual(job["progress"]["pct"], 100)
        self.assertEqual(len(job["outputs"]), 1)
        output = job["outputs"][0]
        self.assertEqual(set(output), {"name", "type", "mime", "sha256", "size", "path", "meta"})
        self.assertEqual(output["type"], output_type)
        self.assertEqual(output["mime"], mime)
        self.assertEqual(output["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(output["size"], len(expected))
        self.assertEqual(Path(output["path"]).read_bytes(), expected)
        self.assertEqual(Path(output["path"]).parent, (self.root / "outputs").resolve())
        provenance = job["provenance"]
        self.assertEqual(provenance["job_id"], job["id"])
        self.assertEqual(provenance["tool"], job["tool"])
        self.assertEqual(provenance["vendor"], vendor)
        self.assertEqual(provenance["model"], model)
        self.assertEqual(provenance["backend"], "byok")
        self.assertTrue(provenance["created_at"].endswith("Z"))
        self.assertNotIn("test-", json.dumps(job))

    def test_openai_generate_request_and_saved_output(self):
        fake = FakeTransport(image_reply())
        job = self.job("image_generate", fake, {"prompt": "a chair", "size": "1536x1024", "quality": "high"})
        self.assert_output(job, PNG, "image", "image/png", "openai", "gpt-image-2.5")
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://api.openai.com/v1/images/generations")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-OPENAI_API_KEY")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(call["body"]), {"model": "gpt-image-2.5", "prompt": "a chair", "size": "1536x1024",
                                                    "quality": "high", "output_format": "png"})
        self.assertEqual(call["timeout"], 30)

    def test_openai_edit_multipart_request_and_saved_output(self):
        fake = FakeTransport(image_reply())
        job = self.job("image_edit", fake)
        self.assert_output(job, PNG, "image", "image/png", "openai", "gpt-image-2.5")
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://api.openai.com/v1/images/edits")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-OPENAI_API_KEY")
        message = BytesParser(policy=policy.default).parsebytes(
            ("Content-Type: " + call["headers"]["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + call["body"])
        parts = {p.get_param("name", header="content-disposition"): p for p in message.iter_parts()}
        self.assertEqual(set(parts), {"model", "prompt", "size", "quality", "output_format", "image"})
        self.assertEqual(parts["model"].get_content().strip(), "gpt-image-2.5")
        self.assertEqual(parts["prompt"].get_content().strip(), "make it green")
        self.assertEqual(parts["image"].get_content_type(), "image/png")
        self.assertEqual(parts["image"].get_payload(decode=True), PNG)
        self.assertNotIn(str(self.reference), call["body"].decode(errors="replace"))

    def fal_fake(self, field, data=GLB, result=None):
        result = {field: {"url": "https://v3.fal.media/files/result.glb"}} if result is None else result
        return FakeTransport(reply({"request_id": "req_123", "status_url": "https://queue.fal.run/vendor/requests/req_123/status",
                                    "response_url": "https://queue.fal.run/vendor/requests/req_123"}),
                             reply({"status": "IN_QUEUE"}), reply({"status": "IN_PROGRESS"}),
                             reply({"status": "COMPLETED"}), reply(result), Response(200, {}, data))

    def assert_fal_calls(self, fake, endpoint, payload):
        self.assertEqual(len(fake.calls), 6)
        self.assertFalse(fake.replies)
        self.assertEqual(fake.calls[0]["url"], "https://queue.fal.run/" + endpoint)
        self.assertEqual(fake.calls[0]["method"], "POST")
        self.assertEqual(json.loads(fake.calls[0]["body"]), payload)
        for call in fake.calls[:-1]:
            self.assertEqual(call["headers"]["Authorization"], "Key test-FAL_KEY")
            self.assertEqual(call["headers"]["X-Fal-Store-IO"], "0")
            self.assertEqual(json.loads(call["headers"]["X-Fal-Object-Lifecycle-Preference"]),
                             {"expiration_duration_seconds": 3600})
        for call in fake.calls[1:4]:
            self.assertEqual(call["url"], "https://queue.fal.run/vendor/requests/req_123/status")
            self.assertEqual(call["method"], "GET")
            self.assertIsNone(call["body"])
        self.assertEqual(fake.calls[4]["url"], "https://queue.fal.run/vendor/requests/req_123")
        self.assertEqual(fake.calls[-1]["url"], "https://v3.fal.media/files/result.glb")
        self.assertEqual(fake.calls[-1]["method"], "GET")
        self.assertEqual(fake.calls[-1]["headers"], {})

    def test_fal_rodin_submit_poll_result_download(self):
        fake = self.fal_fake("model_mesh")
        job = self.job("image_to_3d", fake)
        self.assert_output(job, GLB, "model_3d", "model/gltf-binary", "fal", "fal-ai/hyper3d/rodin/v2.5")
        self.assert_fal_calls(fake, "fal-ai/hyper3d/rodin/v2.5",
                              {"input_image_urls": ["data:image/png;base64," + base64.b64encode(PNG).decode()],
                               "geometry_file_format": "glb"})
        self.assertEqual(job["provenance"]["kind"], "gen")

    def test_fal_remesh_submit_poll_result_download_under_no_generation(self):
        fake = self.fal_fake("model_mesh")
        job = self.job("retopology", fake, {"face_limit": 2000}, no_generation=True)
        self.assert_output(job, GLB, "model_3d", "model/gltf-binary", "fal", "tripo3d/tripo/remesh")
        self.assertEqual(Path(job["outputs"][0]["path"]).suffix, ".glb")
        self.assert_fal_calls(fake, "tripo3d/tripo/remesh",
                              {"mesh_url": "data:model/gltf-binary;base64," + base64.b64encode(GLB).decode(),
                               "face_limit": 2000, "quad": False})
        self.assertEqual(job["provenance"]["kind"], "assist")

    def test_fal_remesh_quads_come_back_as_fbx(self):
        # The FBX is labelled by its bytes, whatever the vendor's file name or content type says.
        result = {"model_mesh": {"url": "https://v3.fal.media/files/result.glb", "content_type": "model/gltf-binary",
                                 "file_name": "result.glb", "file_size": len(FBX)}}
        fake = self.fal_fake(None, FBX, result)
        job = self.job("retopology", fake, {"face_limit": 2000, "quad": True}, no_generation=True)
        self.assert_output(job, FBX, "model_3d", "model/vnd.fbx", "fal", "tripo3d/tripo/remesh")
        self.assertEqual(job["outputs"][0]["name"], "model")
        self.assertEqual(Path(job["outputs"][0]["path"]).suffix, ".fbx")
        self.assert_fal_calls(fake, "tripo3d/tripo/remesh",
                              {"mesh_url": "data:model/gltf-binary;base64," + base64.b64encode(GLB).decode(),
                               "face_limit": 2000, "quad": True})

    def test_fal_remesh_falls_back_to_the_matching_model_urls_entry(self):
        for quad, data, chosen in ((True, FBX, "fbx"), (False, GLB, "glb")):
            urls = {name: {"url": f"https://v3.fal.media/files/{name}-result"} for name in ("glb", "fbx")}
            result = {"model_mesh": None, "model_urls": {**urls, "base_model": None, "pbr_model": None}}
            with self.subTest(quad=quad):
                fake = self.fal_fake(None, data, result)
                job = self.job("retopology", fake, {"quad": quad})
                self.assert_output(job, data, "model_3d", fal.model_mime(data), "fal", "tripo3d/tripo/remesh")
                self.assertEqual(fake.calls[-1]["url"], f"https://v3.fal.media/files/{chosen}-result")
        fake = self.fal_fake(None, FBX, {"model_urls": {"glb": None, "fbx": {"url": "https://v3.fal.media/files/only"}}})
        self.assert_output(self.job("retopology", fake), FBX, "model_3d", "model/vnd.fbx", "fal", "tripo3d/tripo/remesh")
        fake = self.fal_fake(None, FBX, {"model_mesh": None, "model_urls": {"glb": None, "fbx": None}})
        self.assertEqual(self.job("retopology", fake)["error"]["code"], "vendor_failed")
        self.assertEqual(len(fake.calls), 5, "no download without a model URL")

    def test_fal_remesh_bytes_that_are_neither_fbx_nor_glb_fail_unsaved(self):
        for data in (b"; FBX 7.4.0 project file\nascii", b"<html>expired</html>", b"PK\x03\x04zip"):
            result = {"model_mesh": {"url": "https://v3.fal.media/files/result.fbx", "file_name": "result.fbx"}}
            with self.subTest(data=data):
                job = self.job("retopology", self.fal_fake(None, data, result), {"quad": True})
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["error"]["code"], "vendor_failed")
                self.assertIn("labelled FBX", job["error"]["message"])
                self.assertEqual(job["outputs"], [])
        self.assertFalse(list((self.root / "outputs").glob("*")))
        self.assertEqual(fal.model_mime(FBX), "model/vnd.fbx")
        self.assertEqual(fal.model_mime(GLB), "model/gltf-binary")
        with self.assertRaises(ByokError) as caught:
            fal.model_mime(b"plain", {"content_type": "application/octet-stream"})
        self.assertNotIn("labelled", caught.exception.error["message"])

    def test_fal_remesh_face_limit_ranges(self):
        model = {"model": (GLB, "model/gltf-binary")}
        for quad, faces in ((True, 10001), (True, 499), (False, 20001), (False, 499)):
            fake = FakeTransport()
            with self.subTest(quad=quad, faces=faces), self.assertRaises(ByokError) as caught:
                fal.execute("retopology", {"quad": quad, "face_limit": faces}, model, Client(fake), "test-fal")
            self.assertEqual(caught.exception.error["field"], "face_limit")
            self.assertEqual(fake.calls, [])
        for quad, faces in ((True, 10000), (True, 500), (False, 20000), (False, 500)):
            with self.subTest(quad=quad, faces=faces):
                fake = self.fal_fake("model_mesh", FBX if quad else GLB)
                job = self.job("retopology", fake, {"quad": quad, "face_limit": faces})
                self.assertEqual(job["status"], "succeeded", job["error"])
                self.assertEqual(json.loads(fake.calls[0]["body"])["face_limit"], faces)

    def test_gemini_request_pcm_saved_as_wav(self):
        fake = FakeTransport(voice_reply())
        job = self.job("voice_line", fake, {"text": "Hello", "voice": "Puck"})
        self.assertEqual(job["status"], "succeeded", job["error"])
        data = Path(job["outputs"][0]["path"]).read_bytes()
        with wave.open(io.BytesIO(data), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, 24000))
            self.assertEqual(audio.readframes(audio.getnframes()), PCM)
        self.assert_output(job, data, "audio", "audio/wav", "gemini", "gemini-3.8-flash-tts")
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash-tts:generateContent")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["x-goog-api-key"], "test-GEMINI_API_KEY")
        self.assertNotIn("Authorization", call["headers"])
        self.assertNotIn("test-GEMINI", call["url"])
        self.assertEqual(json.loads(call["body"]),
                         {"contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
                          "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                              "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Puck"}}}}})

    def test_elevenlabs_request_and_saved_mp3(self):
        fake = FakeTransport(Response(200, {"Content-Type": "audio/mpeg"}, MP3))
        job = self.job("sfx", fake, {"prompt": "a bell", "duration_seconds": 2, "prompt_influence": 0.5, "loop": True})
        self.assert_output(job, MP3, "audio", "audio/mpeg", "elevenlabs", "eleven_text_to_sound_v2")
        call = fake.calls[0]
        self.assertEqual(call["url"], "https://api.elevenlabs.io/v1/sound-generation?output_format=mp3_44100_128")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["xi-api-key"], "test-ELEVENLABS_API_KEY")
        self.assertEqual(call["headers"]["Accept"], "audio/mpeg")
        self.assertEqual(json.loads(call["body"]), {"model_id": "eleven_text_to_sound_v2", "text": "a bell",
                                                    "duration_seconds": 2, "prompt_influence": 0.5, "loop": True})

    def test_every_adapter_maps_http_errors_and_timeout_to_failed_jobs(self):
        for tool in ("image_generate", "image_edit", "image_to_3d", "retopology", "voice_line", "sfx"):
            for failure, code in ((reply({}, 401), "byok_key_invalid"),
                                  (reply({}, 429, {"Retry-After": "13"}), "rate_limited"),
                                  (reply({}, 400), "invalid_input"), (reply({}, 422), "invalid_input"),
                                  (reply({}, 500), "vendor_failed"), (TimeoutError("private details"), "vendor_failed")):
                with self.subTest(tool=tool, code=code, failure=failure):
                    job = self.job(tool, FakeTransport(failure))
                    self.assertEqual(job["status"], "failed")
                    self.assertEqual(job["error"]["code"], code)
                    self.assertEqual(job["outputs"], [])
                    if code == "rate_limited":
                        self.assertEqual(job["error"]["retry_after"], 13)
                    self.assertNotIn("private details", json.dumps(job))
        self.assertFalse(list((self.root / "outputs").glob("*")))

    def test_fal_queue_failure_and_completed_error(self):
        for status in ({"status": "FAILED", "error": "test-FAL_KEY"},
                       {"status": "COMPLETED", "error": "test-FAL_KEY"}, {"status": "UNKNOWN"}):
            fake = FakeTransport(reply({"request_id": "job1"}), reply(status))
            job = self.job("image_to_3d", fake)
            self.assertEqual(job["error"]["code"], "vendor_failed")
            self.assertNotIn("test-FAL_KEY", json.dumps(job))
            self.assertEqual(len(fake.calls), 2)

    def test_fal_polling_is_bounded(self):
        fake = FakeTransport(reply({"request_id": "job1"}), reply({"status": "IN_QUEUE"}), reply({"status": "IN_PROGRESS"}))
        with self.assertRaises(ByokError) as caught:
            fal.execute("image_to_3d", {}, {"reference": (PNG, "image/png")}, Client(fake), "test-fal",
                        max_polls=2, sleep=lambda _: None)
        self.assertEqual(caught.exception.error["code"], "vendor_failed")
        self.assertEqual(len(fake.calls), 3)

    def test_fal_queue_failure_http_mapping(self):
        for response, code in ((reply({}, 422), "invalid_input"), (reply({}, 500), "vendor_failed")):
            fake = FakeTransport(reply({"request_id": "job1"}), response)
            self.assertEqual(self.job("retopology", fake)["error"]["code"], code)

    def test_fal_does_not_send_credentials_to_an_untrusted_queue_host(self):
        fake = FakeTransport(reply({"request_id": "job1", "status_url": "https://attacker.example/status"}))
        self.assertEqual(self.job("image_to_3d", fake)["error"]["code"], "vendor_failed")
        self.assertEqual(len(fake.calls), 1)

    def test_malformed_vendor_outputs_fail_before_saving(self):
        cases = [("image_generate", reply({"data": [{"b64_json": "%%%"}]})),
                 ("image_edit", Response(200, {}, b"not json")),
                 ("voice_line", reply({"candidates": [{"content": {"parts": []}}]})),
                 ("voice_line", reply({"candidates": [None]})), ("sfx", Response(200, {}, b""))]
        for tool, response in cases:
            with self.subTest(tool=tool, response=response):
                self.assertEqual(self.job(tool, FakeTransport(response))["error"]["code"], "vendor_failed")
        self.assertFalse(list((self.root / "outputs").glob("*")))

    def test_multiple_image_outputs_and_unique_job_paths(self):
        response = reply({"data": [{"b64_json": base64.b64encode(PNG).decode()}] * 2})
        first = self.job("image_generate", FakeTransport(response))
        second = self.job("image_generate", FakeTransport(response))
        paths = [output["path"] for job in (first, second) for output in job["outputs"]]
        self.assertEqual(len(set(paths)), 4)
        self.assertTrue(all(Path(path).read_bytes() == PNG for path in paths))

    def test_generation_policy_blocks_all_gen_tools_before_http(self):
        for tool in ("image_generate", "image_edit", "image_to_3d", "voice_line", "sfx"):
            fake = FakeTransport()
            job = self.job(tool, fake, no_generation=True)
            self.assertEqual(job["error"]["code"], "generation_disabled")
            self.assertEqual(fake.calls, [])

    def test_private_mode_refuses_class_c_before_http(self):
        fake = FakeTransport()
        job = self.job("sfx", fake, private_mode=True)
        self.assertEqual(job["error"]["code"], "private_mode")
        self.assertEqual(job["error"]["data_class"], "C")
        self.assertEqual(fake.calls, [])

    def test_missing_keys_and_invalid_key_characters_fail_locally(self):
        fake = FakeTransport()
        credentials.clear_vendor_key("OPENAI_API_KEY")
        self.assertEqual(self.job("image_generate", fake)["error"]["code"], "byok_key_missing")
        for key in ("test\ninvalid", "test\x00invalid", "test-\N{SNOWMAN}", "test invalid"):
            credentials.set_vendor_key("OPENAI_API_KEY", key)
            self.assertEqual(self.job("image_generate", fake)["error"]["code"], "byok_key_invalid")
        self.assertEqual(fake.calls, [])

    def test_invalid_tool_inputs_are_refused_before_http(self):
        cases = [("image_generate", {}, {}), ("image_generate", {"prompt": " "}, {}),
                 ("image_generate", {"prompt": "a chair", "size": "tiny"}, {}),
                 ("image_generate", {"prompt": "a chair", "conversation": "private"}, {}),
                 ("sfx", {"prompt": "a bell", "duration_seconds": True}, {}),
                 ("sfx", {"prompt": "a bell", "duration_seconds": float("nan")}, {}),
                 ("sfx", {"prompt": "a bell", "duration_seconds": 10 ** 1000}, {}),
                 ("retopology", {"face_limit": 499}, {"model": self.model}),
                 ("retopology", {"face_limit": 15000, "quad": True}, {"model": self.model}),
                 ("image_edit", {"prompt": "green"}, {}), ("render", {}, {})]
        for tool, inputs, files in cases:
            fake = FakeTransport()
            with self.subTest(tool=tool, inputs=inputs):
                self.assertEqual(self.job(tool, fake, inputs, files)["error"]["code"], "invalid_input")
                self.assertEqual(fake.calls, [])

    def test_invalid_files_are_refused_before_http(self):
        text = self.root / "data.txt"
        text.write_text("test")
        empty = self.root / "empty.png"
        empty.touch()
        for files in ({"reference": text}, {"reference": self.root / "missing.png"}, {"reference": empty},
                      {"reference": self.reference, "conversation": self.reference}):
            fake = FakeTransport()
            self.assertEqual(self.job("image_edit", fake, files=files)["error"]["code"], "invalid_input")
            self.assertEqual(fake.calls, [])

    def test_oversize_input_is_refused_before_http(self):
        with patch.object(catalog, "_TOOLS", tuple([{**catalog._TOOLS[1], "file_inputs": [
                {"field": "reference", "accept": ["image/png"], "max_mb": 0, "required": True}]}])):
            fake = FakeTransport()
            self.assertEqual(self.job("image_edit", fake)["error"]["code"], "invalid_input")
            self.assertEqual(fake.calls, [])

    def test_local_output_write_failure_has_a_clear_error(self):
        blocked = self.root / "blocked"
        blocked.write_text("a file, not a directory")
        job = run("image_generate", {"prompt": "chair"}, {}, blocked, transport=FakeTransport(image_reply()),
                  no_generation=False, private_mode=False)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "byok_output_failed")
        self.assertEqual(job["outputs"], [])


class TransportTest(unittest.TestCase):
    def test_error_messages_never_echo_vendor_payloads(self):
        for status in (401, 403, 400, 422, 500):
            fake = FakeTransport(reply({"error": {"message": "secret-key and private input"}}, status))
            with self.assertRaises(ByokError) as caught:
                Client(fake).json("POST", "https://api.openai.com/v1/images/generations", payload={})
            self.assertNotIn("secret-key", str(caught.exception))
            self.assertNotIn("private input", json.dumps(caught.exception.error))

    def test_gemini_invalid_key_400_and_elevenlabs_detail_are_classified(self):
        cases = [{"error": {"details": [{"reason": "API_KEY_INVALID"}]}},
                 {"detail": {"status": "invalid_api_key"}}, {"error": {"code": "invalid_api_key"}}]
        for body in cases:
            with self.assertRaises(ByokError) as caught:
                Client(FakeTransport(reply(body, 400))).json("GET", "https://vendor.example/api")
            self.assertEqual(caught.exception.error["code"], "byok_key_invalid")

    def test_retry_after_case_insensitive_and_malformed(self):
        for value, expected in (("2.5", 3), ("-1", 0), ("NaN", None), ("bad", None),
                                ("Thu, 01 Jan 1970 00:00:00 GMT", 0)):
            with self.assertRaises(ByokError) as caught:
                Client(FakeTransport(reply({}, 429, {"retry-after": value}))).json("GET", "https://vendor.example/api")
            self.assertEqual(caught.exception.error.get("retry_after"), expected)

    def test_transport_connection_failures(self):
        for failure in (urllib.error.URLError("private"), ConnectionResetError("private"), TimeoutError("private"),
                        http.client.IncompleteRead(b"private")):
            with self.assertRaises(ByokError) as caught:
                Client(FakeTransport(failure)).json("GET", "https://vendor.example/api")
            self.assertEqual(caught.exception.error["code"], "vendor_failed")

    def test_malformed_json_and_response_size_limit(self):
        for response in (Response(200, {}, b"bad JSON"), reply([])):
            with self.assertRaises(ByokError) as caught:
                Client(FakeTransport(response)).json("GET", "https://vendor.example/api")
            self.assertEqual(caught.exception.error["code"], "vendor_failed")
        with self.assertRaises(ByokError):
            Client(FakeTransport(Response(200, {}, b"1234"))).request("GET", "https://vendor.example/file", max_bytes=3)

    def test_download_urls_refuse_local_hosts_and_non_https(self):
        for url in ("http://cdn.example/file", "file:///tmp/test", "https://localhost/file", "https://127.0.0.1/file",
                    "https://user:pass@cdn.example/file", "https://cdn.example:123/file", "https://[::1]/file"):
            fake = FakeTransport()
            with self.assertRaises(ByokError):
                Client(fake).download(url)
            self.assertFalse(fake.calls)

    def test_urllib_transport_passes_timeout_and_refuses_redirects(self):
        response = io.BytesIO(b"output")
        response.status, response.headers = 200, {"Content-Type": "audio/mpeg"}
        opener = type("Opener", (), {"open": lambda self, request, timeout: response})()
        with patch("urllib.request.build_opener", return_value=opener) as build:
            actual = urllib_transport("POST", "https://vendor.example/api", {"Accept": "audio/mpeg"}, b"{}", 7)
        self.assertEqual(actual.body, b"output")
        handler = build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://attacker.example"))

    def test_urllib_http_error_becomes_response(self):
        error = urllib.error.HTTPError("https://vendor.example/api", 429, "rate limit", {"Retry-After": "8"}, io.BytesIO(b"{}"))
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.side_effect = error
            response = urllib_transport("GET", "https://vendor.example/api", {}, None, 7)
        self.assertEqual(response.status, 429)
        self.assertEqual(response.headers["Retry-After"], "8")


class CatalogTest(unittest.TestCase):
    def test_contract_fields_enums_and_schemas(self):
        tools = descriptors(no_generation=False, private_mode=False, keys_set={k: True for k in credentials.VENDOR_KEY_NAMES})
        fields = {"name", "namespace", "version", "title", "description", "kind", "status", "available", "unavailable_reason",
                  "input_schema", "file_inputs", "output", "apply", "cost", "latency", "approval", "backends", "byok"}
        expected = {"image_generate": ("gen", "image", "add_image_reference", "A"),
                    "image_edit": ("gen", "image", "add_image_reference", "A"),
                    "image_to_3d": ("gen", "model_3d", "import_model", "A"),
                    "retopology": ("assist", "model_3d", "import_model", "B"),
                    "voice_line": ("gen", "audio", "add_audio_strip", "B"),
                    "sfx": ("gen", "audio", "add_audio_strip", "C")}
        self.assertEqual({tool["name"] for tool in tools}, set(expected))
        for tool in tools:
            self.assertEqual(set(tool), fields)
            self.assertEqual(tool["namespace"], "loopcut_cloud")
            self.assertEqual(tool["version"], 1)
            self.assertIn(tool["status"], ("live", "beta", "coming_soon"))
            self.assertIn(tool["latency"], ("seconds", "minutes", "long"))
            self.assertEqual(tool["backends"], ["managed", "byok"])
            self.assertEqual(set(tool["byok"]), {"provider", "key_name", "data_class"})
            self.assertIn(tool["byok"]["key_name"], credentials.VENDOR_KEY_NAMES)
            self.assertEqual((tool["kind"], tool["output"]["type"], tool["apply"], tool["byok"]["data_class"]), expected[tool["name"]])
            self.assertTrue(tool["available"])
            self.assertIsNone(tool["unavailable_reason"])
            self.assertEqual(tool["input_schema"]["type"], "object")
            self.assertTrue(set(tool["input_schema"]["required"]) <= set(tool["input_schema"]["properties"]))
            for file in tool["file_inputs"]:
                self.assertEqual(set(file), {"field", "accept", "max_mb", "required"})
                self.assertIsInstance(file["required"], bool)
                self.assertGreater(file["max_mb"], 0)
                self.assertTrue(file["accept"])
        json.dumps(tools, allow_nan=False)

    def test_key_availability_and_policies(self):
        for no_generation in (False, True):
            for private_mode in (False, True):
                for present in (False, True):
                    for tool in descriptors(no_generation=no_generation, private_mode=private_mode,
                                            keys_set={k: present for k in credentials.VENDOR_KEY_NAMES}):
                        blocked_generation = no_generation and tool["kind"] == "gen"
                        available = present and not blocked_generation and not (private_mode and tool["byok"]["data_class"] == "C")
                        self.assertEqual(tool["available"], available)
                        self.assertEqual(tool["unavailable_reason"], None if available else "no_generation" if blocked_generation
                                         else "private_mode" if private_mode and tool["byok"]["data_class"] in ("C", "D") else "disabled")

    def test_catalog_returns_independent_copies(self):
        first = descriptors(no_generation=False, private_mode=False, keys_set={})
        first[0]["input_schema"]["properties"]["prompt"]["type"] = "corrupt"
        self.assertEqual(descriptors(no_generation=False, private_mode=False, keys_set={})[0]["input_schema"]["properties"]["prompt"]["type"], "string")

    def test_backend_choice_table(self):
        descriptor = descriptors(no_generation=False, private_mode=False, keys_set={})[0]
        for signed in (False, True):
            for preferred in (None, "managed", "byok"):
                for key in (False, True):
                    with self.subTest(signed=signed, preferred=preferred, key=key):
                        result = choose_backend(descriptor, signed_in_with_credits=signed, preferred_backend=preferred, has_key=key)
                        expected = ("byok" if key and (not signed or preferred == "byok") else
                                    "managed" if signed and preferred != "byok" else None)
                        self.assertEqual(result["backend"], expected)
                        self.assertEqual(result["unavailable_reason"], None if expected else "disabled")

    def test_managed_only_never_chooses_byok(self):
        for tool in ("render", "asset_search"):
            descriptor = {"name": tool, "backends": ["managed"], "status": "live", "unavailable_reason": "sign_in"}
            for signed in (True, False):
                result = choose_backend(descriptor, signed_in_with_credits=signed, preferred_backend="byok", has_key=True)
                self.assertEqual(result, {"backend": "managed" if signed else None, "unavailable_reason": None if signed else "sign_in"})

    def test_generation_and_coming_soon_descriptor_never_choose_a_backend(self):
        descriptor = descriptors(no_generation=True, private_mode=False, keys_set={})[0]
        self.assertEqual(choose_backend(descriptor, signed_in_with_credits=True, preferred_backend="byok", has_key=True),
                         {"backend": None, "unavailable_reason": "no_generation"})
        descriptor.update(status="coming_soon", unavailable_reason=None)
        self.assertEqual(choose_backend(descriptor, signed_in_with_credits=True, preferred_backend="managed", has_key=True)["backend"], None)

    def test_default_catalog_and_runner_read_policy_snapshot(self):
        with patch.object(config, "service_policy", return_value={"no_generation": True, "private_mode": True}), \
                patch.object(credentials, "vendor_keys_set", return_value={k: True for k in credentials.VENDOR_KEY_NAMES}):
            tools = descriptors()
            self.assertTrue(next(t for t in tools if t["name"] == "retopology")["available"])
            self.assertTrue(all(not t["available"] for t in tools if t["kind"] == "gen"))
            fake = FakeTransport()
            job = run("image_generate", {"prompt": "chair"}, {}, "/unused", transport=fake)
            self.assertEqual(job["error"]["code"], "generation_disabled")
            self.assertEqual(fake.calls, [])


class CredentialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {"LOOPCUT_CONFIG_DIR": self.tmp.name,
                                                   **{k: "" for k in credentials.VENDOR_KEY_NAMES}})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.path = Path(self.tmp.name) / "credentials.json"

    def test_vendor_round_trip_preserves_existing_api_keys_in_both_directions(self):
        self.path.write_text(json.dumps({"api_keys": {"https://loopcut.org/v1": "test-loopcut"}}))
        for name in credentials.VENDOR_KEY_NAMES:
            credentials.set_vendor_key(name, "  test-vendor  ")
            self.assertEqual(credentials.vendor_key(name), "test-vendor")
        self.assertEqual(credentials.api_key("https://loopcut.org/v1/"), "test-loopcut")
        credentials.store("https://endpoint.example/v1/", "test-endpoint")
        self.assertEqual(credentials.vendor_keys_set(), {name: True for name in credentials.VENDOR_KEY_NAMES})
        for name in credentials.VENDOR_KEY_NAMES:
            self.assertEqual(credentials.vendor_key(name), "test-vendor")
            credentials.clear_vendor_key(name)
            self.assertEqual(credentials.vendor_key(name), "")
        self.assertEqual(credentials.api_key("https://loopcut.org/v1"), "test-loopcut")
        self.assertEqual(credentials.api_key("https://endpoint.example/v1"), "test-endpoint")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_environment_precedence_and_clear_does_not_remove_environment_key(self):
        credentials.set_vendor_key("OPENAI_API_KEY", "test-stored")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-environment"}):
            self.assertEqual(credentials.vendor_key("OPENAI_API_KEY"), "test-environment")
            credentials.clear_vendor_key("OPENAI_API_KEY")
            self.assertEqual(credentials.vendor_key("OPENAI_API_KEY"), "test-environment")
            self.assertTrue(credentials.vendor_keys_set()["OPENAI_API_KEY"])
        self.assertEqual(credentials.vendor_key("OPENAI_API_KEY"), "")

    def test_missing_damaged_and_legacy_files(self):
        self.assertEqual(credentials.load(), {})
        self.assertFalse(any(credentials.vendor_keys_set().values()))
        for raw in ("not json", "[]", '{"api_keys": null, "vendor_keys": ["bad"]}',
                    '{"api_keys": {"bad": 4}, "vendor_keys": {"OPENAI_API_KEY": null}}'):
            self.path.write_text(raw)
            self.assertEqual(credentials.load(), {})
            self.assertEqual(credentials.vendor_key("OPENAI_API_KEY"), "")
            with self.assertRaises(OSError):
                credentials.set_vendor_key("OPENAI_API_KEY", "test-key")
            self.assertEqual(self.path.read_text(), raw)

    def test_only_known_vendor_key_names_are_accepted(self):
        for call in (lambda: credentials.vendor_key("PATH"), lambda: credentials.set_vendor_key("UNKNOWN", "test"),
                     lambda: credentials.clear_vendor_key("UNKNOWN")):
            with self.assertRaises(ValueError):
                call()

    def test_failed_atomic_replace_preserves_original_file_and_cleans_temp(self):
        credentials.store("https://loopcut.org/v1", "test-account")
        before = self.path.read_bytes()
        with patch.object(credentials.os, "replace", side_effect=OSError("blocked")):
            with self.assertRaises(OSError):
                credentials.set_vendor_key("OPENAI_API_KEY", "test-key")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_api_key_removal_keeps_vendor_keys(self):
        credentials.store("https://loopcut.org/v1", "test-account")
        credentials.set_vendor_key("FAL_KEY", "test-fal")
        credentials.store("https://loopcut.org/v1", "")
        self.assertEqual(credentials.load(), {})
        self.assertEqual(credentials.vendor_key("FAL_KEY"), "test-fal")


class SettingsTest(unittest.TestCase):
    def test_settings_ui_import_registration_masked_fields_and_callbacks(self):
        code = r'''
import os, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, ADDONS)
sys.path.insert(0, str(Path(ADDONS).parents[1] / "loopcut/tests"))
from fake_preferences import AddonPreferences
bpy = types.ModuleType("bpy")
registered = []
bpy.types = types.SimpleNamespace(Operator=type("Operator", (), {}), Menu=type("Menu", (), {}),
                                  AddonPreferences=AddonPreferences)
bpy.props = types.SimpleNamespace(**{name: (lambda **kw: kw) for name in
                                    ("StringProperty", "EnumProperty", "BoolProperty", "IntProperty")})
bpy.context = types.SimpleNamespace(preferences=types.SimpleNamespace(addons={}))
bpy.app = types.SimpleNamespace(driver_namespace={}, background=True, timers=types.SimpleNamespace(
    is_registered=lambda function: False, register=lambda *args, **kwargs: None, unregister=lambda function: None))
utils = types.ModuleType("bpy.utils")
previews = types.ModuleType("bpy.utils.previews")
utils.previews = previews
utils.register_class = registered.append
utils.unregister_class = registered.remove
bpy.utils = utils
sys.modules.update({"bpy": bpy, "bpy.utils": utils, "bpy.utils.previews": previews})
from loopcut import config, credentials, settings
from loopcut.ui import __name__ as ui_name
settings.register()
assert settings.LoopcutPreferences in registered
assert settings.LOOPCUT_OT_clear_vendor_key in registered
assert len({getattr(c, "bl_idname", c.__name__) for c in registered}) == len(registered)
class Layout:
    def __init__(self): self.labels, self.props, self.ops = [], [], []
    def box(self): return self
    def column(self, **kw): return self
    def row(self, **kw): return self
    def label(self, **kw): self.labels.append(kw["text"])
    def prop(self, obj, name, **kw): self.props.append(name)
    def operator(self, name, **kw):
        op = types.SimpleNamespace()
        self.ops.append((name, op))
        return op
with tempfile.TemporaryDirectory() as folder:
    os.environ["LOOPCUT_CONFIG_DIR"] = folder
    for name in credentials.VENDOR_KEY_NAMES: os.environ.pop(name, None)
    for name, label, prop, note, first in settings.VENDOR_ROWS:
        assert settings.LoopcutPreferences.__annotations__[first]["default"] is True
        field = settings.LoopcutPreferences.__annotations__[prop]
        assert field["subtype"] == "PASSWORD" and field["options"] == {"SKIP_SAVE"}
        field["set"](None, "test-saved")
        assert field["get"](None) == "test-saved"
        operator = settings.LOOPCUT_OT_clear_vendor_key()
        operator.key_name = name
        assert operator.execute(None) == {"FINISHED"}
        assert field["get"](None) == ""
    os.environ["OPENAI_API_KEY"] = "test-env"
    layout = Layout()
    settings.draw_vendor_keys(layout, object())
    assert "Your own keys" in layout.labels
    assert all(prop in layout.props and note in layout.labels for _, _, prop, note, _ in settings.VENDOR_ROWS)
    # Only the environment's OpenAI key is set: only its "Use this key instead of credits" box shows.
    assert [first for *_, first in settings.VENDOR_ROWS if first in layout.props] == ["openai_key_first"]
    assert {op.key_name for name, op in layout.ops} == set(credentials.VENDOR_KEY_NAMES)
    assert any("Environment" in label for label in layout.labels)
    os.environ["LOOPCUT_NO_GENERATION"] = "true"
    os.environ["LOOPCUT_PRIVATE_MODE"] = "false"
    os.environ["LOOPCUT_ENV_FILE"] = str(Path(folder) / "empty.env")
    Path(os.environ["LOOPCUT_ENV_FILE"]).touch()
    assert config.service_policy() == {"no_generation": True, "private_mode": False}
settings.unregister()
assert registered == []
'''
        result = subprocess.run([sys.executable, "-c", "ADDONS = " + repr(str(ADDONS)) + "\n" + code],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])


if __name__ == "__main__":
    unittest.main()
