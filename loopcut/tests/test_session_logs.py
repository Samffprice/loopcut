"""Conversation logs and problem reports (session_logs.py, contract proposal 015): the transcript built from a
conversation (images left out, private markers dropped, chat cards in place, worker conversations referenced and
uploaded), redaction of planted secrets, the size caps, the gating, the sender's handling of each answer, the
report and its operator, the console tee, and where Report a Problem may be drawn. No Blender, no network.

With the private platform contract present (platform_contract.contract_dir(), or LOOPCUT_CONTRACT_DIR), every built
body is validated against conversation-log.schema.json and problem-report.schema.json, and the contract's own
fixtures check the small validator below first.

Key-shaped test values are joined at run time (k()), so the file itself holds nothing a secret scanner flags."""

import contextlib
import gzip
import importlib
import io
import json
import os
import re
import struct
import sys
import threading
import time
import types
import unittest
import urllib.error
import uuid
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

import test_cloud as fixtures
from platform_contract import contract_dir
from loopcut import cloud_http, context, credentials, session_logs, telemetry

INSTALL_ID = "01234567-89ab-4cde-8fab-0123456789ab"
PARENT = "0123456789ab4cde8fab0123456789ab"
WORKER = "fedcba9876544321a987fedcba987654"
HEADERS = {"x-loopcut-contract": "0.7", "x-loopcut-client": "loopcut-addon/0.2.5 blender/5.2.2 os/mac arch/arm64"}
SYSTEM = {"blender_version": "5.2.2", "os": "mac", "arch": "arm64"}
ADDONS = Path(__file__).resolve().parents[2] / "scripts" / "addons_core" / "loopcut"


def k(*parts: str) -> str:
    return "".join(parts)


BEARER = k("lc", "_live_SECRETBEARER123")
OWN_KEY = k("sk", "-proj-OWNKEYabc123def456ghi")
REPORT_KEY = k("sk", "-proj-REPORTKEY1234567890xyz")


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def jpeg_header(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def walk(value):
    yield value
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def conversation(images: Path):
    """Two segments: a turn with a note, a tool, a capture and a scene-change card; a steer note that ends in Stop."""
    capture = png(3, 2)
    name = "a" * 64 + ".png"
    images.mkdir(parents=True, exist_ok=True)
    (images / name).write_bytes(capture)
    messages = [
        {"role": "user", "content": "Scene: Cube.\n\nMake a chair"},
        {"role": "assistant", "content": "On it.", context.FOLDED: True,
         "tool_calls": [call("c1", "run_python", '{"code": "make_chair()"}')]},
        {"role": "tool", "tool_call_id": "c1", "content": "Chair added"},
        {"role": "user", context.CAPTURE: True, "content": [
            {"type": "text", "text": context.CAPTURE_LABEL + "the chair (after the step above)."},
            {"type": "image_url", "image_url": {"url": "loopcut-image:" + name}}]},
        {"role": "assistant", "content": "Done."},
        {"role": "user", context.STEER: True, "content": "<steer>make it red</steer>"},
        {"role": "assistant", "content": "", "tool_calls": [call("c2", "run_python", '{"code": "red()"}')]},
        {"role": "tool", "tool_call_id": "c2", "content": "Cancelled before this ran."},
    ]
    items = [
        {"kind": "user", "text": "Make a chair", "images": []},
        {"kind": "notice", "text": "Studied the reference: chair.png", "action_label": "", "checkpoint": "", "url": ""},
        {"kind": "assistant", "text": "On it."},
        {"kind": "tool", "name": "run_python", "summary": "", "code": "", "status": "done", "output": "Chair added"},
        {"kind": "assistant", "text": "Done."},
        {"kind": "changes", "text": "1 object added", "lines": ["+ Chair"], "checkpoint": "", "resolved": ""},
        {"kind": "user", "text": "make it red", "images": []},
        {"kind": "assistant", "text": ""},
        {"kind": "tool", "name": "run_python", "summary": "", "code": "", "status": "rejected", "output": ""},
        {"kind": "error", "text": "Stopped."},
    ]
    return messages, items, len(capture)


# --- A JSON Schema subset: what the 015 schemas use ----------------------------------------------------------

class Invalid(ValueError):
    pass


def _resolve(ref: str, root: dict, folder: Path):
    target, _, pointer = ref.partition("#")
    document = root if not target else json.loads((folder / target).read_text())
    node = document
    for part in [p for p in pointer.split("/") if p]:
        node = node[part]
    return node, document


def _is(value, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "null":
        return value is None
    return isinstance(value, {"string": str, "boolean": bool, "object": dict, "array": list}[name])


def validate(value, schema: dict, root: dict, folder: Path, where: str = "$") -> None:
    if "$ref" in schema:
        node, document = _resolve(schema["$ref"], root, folder)
        validate(value, node, document, folder, where)
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is(value, name) for name in names):
            raise Invalid(f"{where}: type")
    if "const" in schema and value != schema["const"]:
        raise Invalid(f"{where}: const")
    if "enum" in schema and value not in schema["enum"]:
        raise Invalid(f"{where}: enum")
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 1 << 40):
            raise Invalid(f"{where}: length")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise Invalid(f"{where}: pattern")
    if _is(value, "number") and not schema.get("minimum", float("-inf")) <= value <= schema.get("maximum", float("inf")):
        raise Invalid(f"{where}: range")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1 << 40):
            raise Invalid(f"{where}: items")
        for index, item in enumerate(value):
            if "items" in schema:
                validate(item, schema["items"], root, folder, f"{where}[{index}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise Invalid(f"{where}.{key}: required")
        for key, item in value.items():
            if key in schema.get("properties", {}):
                validate(item, schema["properties"][key], root, folder, f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                raise Invalid(f"{where}.{key}: not allowed")
    for part in schema.get("allOf", []):
        validate(value, part, root, folder, where)


def _schema(name: str):
    folder = contract_dir()
    path = folder / "schemas" / name if folder else None
    return (json.loads(path.read_text()), path.parent) if path and path.is_file() else (None, None)


# --- Shared set-up ------------------------------------------------------------------------------------------------

class Base(fixtures.RuntimeFixture):
    def setUp(self):
        super().setUp()
        bpy = sys.modules["bpy"]
        bpy.app.background = False
        bpy.app.version = (5, 2, 2)
        self.root = Path(self.directory.name)
        self.prefs = types.SimpleNamespace(share_conversations=True)
        for patcher in (patch.object(session_logs, "_preferences", return_value=self.prefs),
                        patch.object(session_logs, "_jobs_root", return_value=self.root / "workers"),
                        patch.dict(os.environ, {"LOOPCUT_TELEMETRY": "", "LOOPCUT_DATA_DIR": str(self.root / "data")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reset()
        self.addCleanup(self.reset)

    @staticmethod
    def reset():
        session_logs._stopped, session_logs._paused_until = False, 0.0
        session_logs._conflicts.clear()
        session_logs._pending.clear()
        session_logs._report.update(status="", code="", message="")

    def upload(self, transcript=None, *, key=None, log="", session_id=None):
        return {"session_id": session_id or session_logs.session_uuid(PARENT), "install_id": INSTALL_ID,
                "app_version": "0.2.6", "key": key, "headers": HEADERS, "secrets": [], "home": "/Users/someone",
                "system": dict(SYSTEM), "retried": False, "not_before": 0.0, "log": log,
                "meta": {"provider": "chatgpt", "model": "gpt-5.5", "started_at": "2026-10-20T18:14:30Z",
                         "turns": 1, "outcome": "ok"},
                "transcript": transcript if transcript is not None else [{"role": "user", "text": "hi"}]}


class Transport:
    """urlopen's signature; records each request with its gzip body decoded."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, request, *, timeout):
        headers = {k.lower(): v for k, v in request.header_items()}
        raw = request.data
        body = json.loads(gzip.decompress(raw) if headers.get("content-encoding") == "gzip" else raw)
        self.calls.append({"url": request.full_url, "headers": headers, "body": body, "wire": len(raw)})
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def reply(status, payload=None):
    return fixtures.Reply(payload, status) if status < 400 else fixtures.error("x", status)


# --- The transcript ---------------------------------------------------------------------------------------------

class TranscriptTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.images = Path(self.directory.name) / "images"

    def test_messages_in_order_with_cards_where_they_happened(self):
        messages, items, size = conversation(self.images)
        entries = session_logs.build_transcript(messages, items, images=self.images)
        shape = [(e["role"], e.get("text") if "text" in e else e.get("error")) for e in entries]
        self.assertEqual(shape, [
            ("user", "Scene: Cube.\n\nMake a chair"),
            ("event", "Studied the reference: chair.png"),  # Before any tool card: right after the message.
            ("assistant", "On it."),
            ("tool", "Chair added"),
            ("user", context.CAPTURE_LABEL + "the chair (after the step above)."),
            ("event", "image omitted"),
            ("assistant", "Done."),
            ("event", "1 object added"),                  # After the last tool card: the end of its segment.
            ("user", "<steer>make it red</steer>"),
            ("assistant", ""),
            ("tool", "Cancelled before this ran."),
            ("event", "Stopped."),
        ])
        self.assertEqual(entries[2]["tool_calls"], [{"id": "c1", "name": "run_python", "arguments": '{"code": "make_chair()"}'}])
        self.assertEqual(entries[2]["data"], {"folded": True})
        self.assertEqual((entries[3]["name"], entries[3]["call_id"]), ("run_python", "c1"))
        self.assertEqual(entries[4]["data"], {"kind": "capture"})
        self.assertEqual(entries[5]["data"], {"kind": "viewport", "width": 3, "height": 2, "bytes": size})
        self.assertEqual(entries[7]["data"], {"kind": "changes", "lines": ["+ Chair"]})
        self.assertEqual(entries[8]["data"], {"kind": "steer"})
        self.assertEqual(entries[-1], {"role": "event", "error": "Stopped.", "data": {"kind": "error"}})

    def test_private_markers_and_image_bytes_never_appear(self):
        messages, items, _ = conversation(self.images)
        inline = "data:image/png;base64," + "A" * 4000
        messages.append({"role": "user", context.ATTACHED: True, context.REFERENCE_CARD: True, "content": [
            {"type": "text", "text": "Like this " + inline}, {"type": "image_url", "image_url": {"url": inline}},
            {"type": "input_audio", "input_audio": {"data": "AAAA"}}]})
        entries = session_logs.build_transcript(messages, items, images=self.images)
        for value in walk(entries):
            if isinstance(value, str):
                self.assertFalse(value.startswith(context.PRIVATE_PREFIX), value)
                self.assertNotIn("base64,", value)
        attached = [e for e in entries if e.get("data", {}).get("kind") == "attachment"]
        self.assertEqual(attached, [{"role": "event", "text": "image omitted", "data": {"kind": "attachment", "bytes": 3000}}])
        self.assertIn({"role": "event", "text": "input_audio omitted"}, entries)
        self.assertIn("Like this [image data omitted]", [e.get("text") for e in entries])

    def test_image_size_reads_png_and_jpeg_headers(self):
        path = Path(self.directory.name) / "j.png"
        path.write_bytes(jpeg_header(1280, 720))
        self.assertEqual(session_logs.image_size(path), (1280, 720))
        path.write_bytes(png(17, 9))
        self.assertEqual(session_logs.image_size(path), (17, 9))
        path.write_bytes(b"not an image")
        self.assertIsNone(session_logs.image_size(path))
        missing = session_logs._image_event("loopcut-image:" + "b" * 64 + ".png", "viewport", self.images)
        self.assertEqual(missing["data"], {"kind": "viewport"})

    def test_worker_sessions_are_referenced_at_the_end_of_their_turn(self):
        messages = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "a"},
                    {"role": "user", "content": "two"}, {"role": "assistant", "content": "b"}]
        items = [{"kind": "user", "text": "one"}, {"kind": "user", "text": "two"}]
        worker = {"session_id": session_logs.session_uuid(WORKER), "worker": "w1", "job": "1-1", "wave": "w1",
                  "status": "done", "turn": 1}
        entries = session_logs.build_transcript(messages, items, workers=[worker])
        self.assertEqual([e.get("text") for e in entries], ["one", "a", "worker session", "two", "b"])
        self.assertEqual(entries[2]["data"], {"session_id": session_logs.session_uuid(WORKER), "worker": "w1",
                                              "job": "1-1", "wave": "w1", "status": "done"})

    def test_cards_without_matching_messages_still_come_through(self):
        entries = session_logs.build_transcript([], [{"kind": "user", "text": "x"}, {"kind": "error", "text": "No key"},
                                                     {"kind": "limit", "text": "Used", "reason": "week_limit",
                                                      "plan": "free", "resets_at": "Monday"}])
        self.assertEqual(entries, [{"role": "event", "error": "No key", "data": {"kind": "error"}},
                                   {"role": "event", "text": "Used", "data": {"kind": "limit", "reason": "week_limit",
                                                                              "plan": "free", "resets_at": "Monday"}}])

    def test_session_ids_become_canonical_uuid4(self):
        self.assertEqual(session_logs.session_uuid(PARENT), "01234567-89ab-4cde-8fab-0123456789ab")
        value = uuid.uuid4()
        self.assertEqual(session_logs.session_uuid(value.hex), str(value))
        self.assertIsNone(session_logs.session_uuid("0123456789ab1cde8fab0123456789ab"), "a version 1 id")
        self.assertIsNone(session_logs.session_uuid("not-an-id"))
        self.assertEqual(session_logs.rfc3339(0), "1970-01-01T00:00:00Z")


# --- Redaction and size -----------------------------------------------------------------------------------------

class RedactionTest(Base):
    def setUp(self):
        super().setUp()
        config = self.root / "config"
        config.mkdir()
        (config / "credentials.json").write_text(json.dumps({
            "api_keys": {"https://loopcut.org/v1": BEARER, "https://api.openai.com/v1": OWN_KEY,
                         "blenderkit": "bk-plain-secret-value"},
            "vendor_keys": {"FAL_KEY": "falvendorkeyvalue"}}))
        chatgpt = self.root / "chatgpt"
        chatgpt.mkdir()
        (chatgpt / "accounts.json").write_text(json.dumps({"active": "app", "accounts": {"app": {
            "access_token": "opaque-access-token-1", "refresh_token": "rt_opaqueRefreshToken",
            "id_token": "idtokenvalue99", "email": "me@example.com", "client_id": "app_123"}}}))
        for patcher in (patch.object(credentials, "folder", return_value=config),
                        patch.dict(os.environ, {"LOOPCUT_CHATGPT_DIR": str(chatgpt), "MY_SERVICE_TOKEN": "envsecretvalue1",
                                                "LOOPCUT_ENV_FILE": "", "HARMLESS_FLAG": "notasecretatall"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_known_secrets_cover_credentials_chatgpt_tokens_and_environment(self):
        secrets = session_logs.known_secrets()
        for value in (BEARER, OWN_KEY, "bk-plain-secret-value", "falvendorkeyvalue", "opaque-access-token-1",
                      "rt_opaqueRefreshToken", "idtokenvalue99", "envsecretvalue1"):
            self.assertIn(value, secrets)
        self.assertNotIn("me@example.com", secrets)
        self.assertNotIn("notasecretatall", secrets)

    def test_planted_secrets_leave_no_trace_in_the_body(self):
        secrets = session_logs.known_secrets()
        planted = [BEARER, OWN_KEY, "opaque-access-token-1", "rt_opaqueRefreshToken", "envsecretvalue1",
                   "bk-plain-secret-value"]
        transcript = [
            {"role": "user", "text": f"My key is {planted[0]} and {planted[1]}"},
            {"role": "assistant", "text": "", "tool_calls": [{"id": "c1", "name": "run_python",
                                                               "arguments": json.dumps({"code": f"key = '{planted[2]}'"})}]},
            {"role": "tool", "call_id": "c1", "name": "run_python", "text": f"{planted[3]} /Users/someone/Desktop/a.blend"},
        ]
        upload = {**self.upload(transcript, log=f"Loopcut: env {planted[4]}\nLoopcut: {planted[5]}"), "secrets": secrets}
        body = session_logs.log_body(upload)
        text = json.dumps(body)
        for secret in planted:
            self.assertNotIn(secret, text)
        self.assertNotIn("/Users/someone", text)
        self.assertIn("~/Desktop/a.blend", text)
        self.assertGreaterEqual(text.count("[redacted]"), len(planted))

    def test_key_shapes_are_caught_without_knowing_the_key(self):
        shapes = [k("eyJ", "hbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlLXZhbHVl"), k("Bear", "er abc.def-123"),
                  k("sk", "-ant-api03-abcdefghijklmnop"), k("sk", "-proj-1234567890abcdefghij"),
                  k("sk", "_live_51HxYzAbCdEfGh12"), k("lc", "_9f8e7d6c5b4a"), k("AI", "zaSyA1234567890abcdefghijklmnopqrs"),
                  k("re", "_123abc456def789ghi"), k("gh", "p_abcdefghijklmnopqrstuvwxyz0123456789"),
                  k("github", "_pat_11ABCDEFG0123456789_abcdefghijklmnop"), k("AK", "IAIOSFODNN7EXAMPLE"),
                  k("01234567-89ab-cdef-0123-456789abcdef", ":0123456789abcdef0123456789abcdef"),
                  k("hf", "_abcdefghijklmnopqrstuvwxyz1234567890")]
        for shape in shapes:
            with self.subTest(shape=shape[:6]):
                self.assertNotIn(shape, session_logs.redact_text(f"value: {shape} end"))
        url = "https://bucket.r2.dev/out.png?X-Amz-Signature=abc&X-Amz-Credential=def"
        self.assertEqual(session_logs.redact_text(url), "https://bucket.r2.dev/out.png?[redacted]")
        code = ("sk_basis = obj.shape_key_add(name='Basis'); task_name = 're_pattern_for_names'; calc_value = 1  "
                "# the bearer of bad news")
        self.assertEqual(session_logs.redact_text(code), code, "ordinary identifiers stay")

    def test_windows_home_in_every_spelling(self):
        home = "C:\\Users\\Ana"
        text = "C:\\Users\\Ana\\a.blend C:/Users/Ana/b.blend 'C:\\\\Users\\\\Ana\\\\c.blend'"
        self.assertEqual(session_logs.redact_text(text, home=home), "~\\a.blend ~/b.blend '~\\\\c.blend'")
        self.assertEqual(session_logs.redact_text("/ stays", home="/"), "/ stays")


class SizeTest(Base):
    def test_long_fields_keep_head_and_tail(self):
        text = "H" * 20000 + "M" * 50000 + "T" * 20000
        trimmed = session_logs.trim_text(text)
        self.assertTrue(trimmed.startswith("H" * session_logs.FIELD_SIDE))
        self.assertTrue(trimmed.endswith("T" * session_logs.FIELD_SIDE))
        self.assertIn(f"[… {len(text) - 2 * session_logs.FIELD_SIDE:,} characters omitted …]", trimmed)
        self.assertEqual(session_logs.trim_text("short"), "short")

    def test_at_most_2000_entries_newest_kept(self):
        entries = [{"role": "user", "text": str(n)} for n in range(2500)]
        capped = session_logs.cap_entries(entries)
        self.assertEqual(len(capped), 2000)
        self.assertEqual(capped[0], {"role": "event", "text": "501 earlier entries omitted",
                                     "data": {"kind": "omitted", "entries": 501}})
        self.assertEqual(capped[-1]["text"], "2499")
        again = session_logs.cap_entries(capped + [{"role": "user", "text": "x"}])
        self.assertEqual(again[0]["data"]["entries"], 502)

    def test_a_huge_conversation_is_trimmed_to_fit_older_tool_results_first(self):
        def noise(n):
            return os.urandom(n // 2 + 1).hex()[:n]  # Hex barely compresses (about 2:1).
        transcript = []
        for step in range(120):
            transcript += [{"role": "assistant", "text": f"step {step}", "tool_calls": [
                {"id": f"c{step}", "name": "run_python", "arguments": noise(30000)}]},
                {"role": "tool", "call_id": f"c{step}", "text": noise(60000)}]
        body = session_logs.log_body(self.upload(transcript, log="x\n" * 400000))
        raw = cloud_http.json_body(body)
        self.assertLessEqual(len(raw), session_logs.MAX_DECODED)
        self.assertLessEqual(len(cloud_http.gzip_body(raw)), session_logs.MAX_GZIP)
        self.assertLessEqual(len(body.get("log", "")), 262144)
        tools = [e for e in body["transcript"] if e["role"] == "tool"]
        self.assertTrue(tools and len(tools[0]["text"]) <= len(tools[-1]["text"]))
        self.assertIn("characters omitted", tools[0]["text"])

    def test_nothing_fits_is_too_large_and_never_sent(self):
        transport = Transport()
        with patch.object(session_logs, "MAX_GZIP", 50):
            self.assertRaises(session_logs.TooLarge, session_logs.log_body, self.upload())
            self.assertEqual(session_logs.deliver(self.upload(), transport), "too_large")
        self.assertEqual(transport.calls, [])


# --- Gating -------------------------------------------------------------------------------------------------------

class GatingTest(Base):
    def setUp(self):
        super().setUp()
        for patcher in (patch.object(session_logs, "_privacy_seen", return_value=True),
                        patch.object(credentials, "folder", return_value=self.root / "config"),
                        patch.object(credentials, "api_key", return_value=""),
                        patch.object(cloud_http, "contract_headers", return_value=dict(HEADERS)),
                        patch.object(session_logs, "known_secrets", return_value=[]),
                        patch.object(session_logs, "_ensure_sender")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_sharing_needs_the_preference_the_environment_and_an_interactive_blender(self):
        self.assertTrue(session_logs.sharing_on())
        self.prefs.share_conversations = False
        self.assertFalse(session_logs.sharing_on())
        self.prefs.share_conversations = True
        with patch.dict(os.environ, {"LOOPCUT_TELEMETRY": "off"}):
            self.assertFalse(session_logs.sharing_on())
        sys.modules["bpy"].app.background = True
        self.assertFalse(session_logs.sharing_on())
        sys.modules["bpy"].app.background = False
        session_logs._stopped = True
        self.assertFalse(session_logs.sharing_on())

    def test_nothing_is_shared_before_the_user_has_seen_what_is_shared(self):
        """The setup's privacy step (onboarding.privacy_seen) comes first, for every install, those updated from
        0.2.5 included: the preference defaults to on, so an update must not start uploading unannounced."""
        with patch.object(session_logs, "_privacy_seen", return_value=False):
            self.assertFalse(session_logs.sharing_on())
            self.assertEqual(session_logs.turn_start({"id": uuid.uuid4().hex}, types.SimpleNamespace(model="m")), {})
        self.assertTrue(session_logs.sharing_on())
        with patch.object(session_logs, "_privacy_seen", side_effect=RuntimeError("no add-on")):
            self.assertFalse(session_logs.sharing_on())  # Unknown is not seen.

    def test_turn_start_names_every_route_and_is_empty_when_off(self):
        cfg = types.SimpleNamespace(model="gpt-5.5")
        for source, provider, model, expected in (("chatgpt", "chatgpt", "gpt-5.5", "gpt-5.5"),
                                                  ("loopcut", "loopcut", "fast", "fast"),
                                                  ("own_key", "own_key", "claude-opus-5.5", "claude-opus-5.5"),
                                                  ("own_key", "own_key", "my-private-finetune", "other"),
                                                  ("local", "own_key", "llama3.3:70b", "llama3.3-70b")):
            with self.subTest(source=source, model=model), patch("loopcut.cloud.model_source", return_value=source):
                cfg.model = model
                session = {"id": uuid.uuid4().hex}
                self.assertEqual(session_logs.turn_start(session, cfg), {"provider": provider, "model": expected})
                self.prefs.share_conversations = False
                self.assertEqual(session_logs.turn_start(session, cfg), {})
                self.prefs.share_conversations = True

    def test_turn_end_queues_on_the_main_thread_with_the_install_id(self):
        messages, items, _ = conversation(self.root / "data" / "conversations" / PARENT / "images")
        session = {"id": PARENT, "messages": messages, "items": items, "created": 1760984070.0, "turn_number": 2,
                   "jobs": []}
        turn = types.SimpleNamespace(conversation_log={"provider": "chatgpt", "model": "gpt-5.5"},
                                     outcome="cancelled", error_code="")
        hops = []
        with patch("loopcut.mainthread.run_on_main", side_effect=hops.append), \
                patch.object(credentials, "interest_install_id", return_value=INSTALL_ID):
            session_logs.on_turn_end(session, turn)
            self.assertEqual(len(hops), 1, "nothing is queued off the main thread")
            self.assertEqual(session_logs._pending, {})
            hops[0]()
        upload = session_logs._pending[session_logs.session_uuid(PARENT)]
        self.assertEqual((upload["install_id"], upload["key"]), (INSTALL_ID, None))
        self.assertEqual(upload["meta"], {"provider": "chatgpt", "model": "gpt-5.5", "started_at": "2025-10-20T18:14:30Z",
                                          "turns": 2, "outcome": "cancelled"})
        self.assertEqual(upload["system"]["os"], telemetry.os_name())
        image = next(e for e in upload["transcript"] if e.get("text") == "image omitted")
        self.assertEqual(image["data"]["width"], 3, "the conversation's own images folder")

    def test_nothing_without_a_start_with_the_switch_off_or_without_an_install_id(self):
        session = {"id": PARENT, "messages": [], "items": [{"kind": "user", "text": "x"}], "created": 0, "jobs": []}
        hops = []
        start = {"provider": "loopcut", "model": "fast"}
        with patch("loopcut.mainthread.run_on_main", side_effect=lambda fn: hops.append(fn) or fn()), \
                patch.object(credentials, "interest_install_id", return_value=INSTALL_ID):
            session_logs.on_turn_end(session, types.SimpleNamespace(conversation_log={}, outcome="completed"))
            with patch.dict(os.environ, {"LOOPCUT_TELEMETRY": "off"}):
                session_logs.on_turn_end(session, types.SimpleNamespace(conversation_log=start, outcome="completed"))
            self.assertEqual(hops, [])
            self.prefs.share_conversations = False  # Switched off during the turn.
            session_logs.on_turn_end(session, types.SimpleNamespace(conversation_log=start, outcome="completed"))
            self.assertEqual(len(hops), 1)
            self.assertEqual(session_logs._pending, {})
        self.prefs.share_conversations = True
        with patch.object(credentials, "interest_install_id", return_value=None):
            session_logs.queue_uploads([self.upload()])
        self.assertEqual(session_logs._pending, {})

    def test_only_a_loopcut_bearer_goes_along(self):
        with patch.object(credentials, "interest_install_id", return_value=INSTALL_ID):
            for stored, expected in ((BEARER, BEARER), (OWN_KEY, None), ("", None)):
                with patch.object(credentials, "api_key", return_value=stored):
                    session_logs.queue_uploads([{key: value for key, value in self.upload().items()
                                                 if key in ("session_id", "meta", "transcript", "log")}])
                self.assertEqual(session_logs._pending[session_logs.session_uuid(PARENT)]["key"], expected)

    def test_a_turn_that_delegated_uploads_its_workers_too(self):
        job = self.root / "workers" / PARENT / "2-1"
        folder = job / "w1" / "w1"
        folder.mkdir(parents=True)
        (job / "job.json").write_text(json.dumps({"turn": 2, "waves": [{"n": 1, "workers": [
            {"id": "w1", "model": "fast", "outcome": "step_limit", "status": "partial"}]}]}))
        (folder / "meta.json").write_text(json.dumps({"id": WORKER}))
        (folder / "conversation.json").write_text(json.dumps({"id": WORKER, "created": 1760984100.0,
            "items": [{"kind": "user", "text": "Brief for Chair"}],
            "messages": [{"role": "user", "content": "Build the chair"}, {"role": "assistant", "content": "Built."}]}))
        (folder / "worker.log").write_text("Blender started\nSaved\n")
        earlier = uuid.UUID(int=99, version=4).hex
        old = self.root / "workers" / PARENT / "1-1" / "w1" / "w1"  # An earlier turn's worker: referenced only.
        old.mkdir(parents=True)
        (old / "meta.json").write_text(json.dumps({"id": earlier}))
        session = {"id": PARENT, "messages": [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}],
                   "items": [{"kind": "user", "text": "one"}, {"kind": "user", "text": "two"}], "created": 0,
                   "turn_number": 2}
        uploads = session_logs.snapshot(session, types.SimpleNamespace(outcome="completed", error_code=""),
                                        {"provider": "loopcut", "model": "pro"})
        self.assertEqual([u["session_id"] for u in uploads],
                         [session_logs.session_uuid(PARENT), session_logs.session_uuid(WORKER)])
        references = [e["data"]["session_id"] for e in uploads[0]["transcript"] if e.get("text") == "worker session"]
        self.assertEqual(references, [session_logs.session_uuid(earlier), session_logs.session_uuid(WORKER)])
        worker = uploads[1]
        self.assertEqual(worker["meta"], {"provider": "loopcut", "model": "fast", "started_at": "2025-10-20T18:15:00Z",
                                          "turns": 1, "outcome": "error"})
        self.assertEqual([e["text"] for e in worker["transcript"]], ["Build the chair", "Built."])
        self.assertEqual(worker["log"], "Blender started\nSaved\n")


# --- Sending ------------------------------------------------------------------------------------------------------

class SenderTest(Base):
    def setUp(self):
        super().setUp()
        patcher = patch.object(session_logs, "_ensure_sender")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_the_newest_snapshot_of_a_conversation_waits_and_the_queue_is_bounded(self):
        session_logs.enqueue(self.upload([{"role": "user", "text": "old"}]))
        session_logs.enqueue(self.upload([{"role": "user", "text": "new"}]))
        self.assertEqual(len(session_logs._pending), 1)
        self.assertEqual(next(iter(session_logs._pending.values()))["transcript"][0]["text"], "new")
        ids = [str(uuid.uuid4()) for _ in range(session_logs.QUEUE_SIZE + 2)]
        for session_id in ids:
            session_logs.enqueue(self.upload(session_id=session_id))
        self.assertEqual(list(session_logs._pending), ids[-session_logs.QUEUE_SIZE:])

    def test_anonymous_upload_is_gzip_json_without_authorization(self):
        transport = Transport(reply(204))
        self.assertEqual(session_logs.deliver(self.upload(), transport), "sent")
        sent = transport.calls[0]
        self.assertEqual(sent["url"], "https://loopcut.org/v1/logs")
        self.assertEqual(sent["headers"]["content-encoding"], "gzip")
        self.assertEqual(sent["headers"]["content-type"], "application/json")
        self.assertEqual(sent["headers"]["x-loopcut-contract"], "0.7")
        self.assertNotIn("authorization", sent["headers"])
        self.assertEqual(set(sent["body"]), {"session_id", "install_id", "app_version", "meta", "transcript"})
        self.assertEqual(sent["body"]["meta"], {**SYSTEM, "provider": "chatgpt", "model": "gpt-5.5",
                                                "started_at": "2026-10-20T18:14:30Z", "turns": 1, "outcome": "ok"})

    def test_signed_in_the_bearer_is_a_header_and_never_in_the_body(self):
        transport = Transport(reply(204))
        upload = {**self.upload([{"role": "user", "text": f"key {BEARER} here"}]), "key": BEARER, "secrets": [BEARER]}
        self.assertEqual(session_logs.deliver(upload, transport), "sent")
        self.assertEqual(transport.calls[0]["headers"]["authorization"], "Bearer " + BEARER)
        self.assertNotIn(BEARER, json.dumps(transport.calls[0]["body"]))

    def test_404_and_426_stop_until_the_next_launch(self):
        for status in (404, 426):
            self.reset()
            session_logs.enqueue(self.upload(session_id=str(uuid.uuid4())))
            self.assertEqual(session_logs.deliver(self.upload(), Transport(reply(status))), "stopped")
            self.assertTrue(session_logs._stopped)
            self.assertEqual(session_logs._pending, {})
            session_logs.enqueue(self.upload())
            self.assertEqual(session_logs._pending, {})
            self.assertEqual(session_logs.deliver(self.upload(), Transport()), "stopped")

    def test_429_pauses_ten_minutes_and_keeps_the_upload(self):
        before = time.monotonic()
        self.assertEqual(session_logs.deliver(self.upload(), Transport(reply(429))), "paused")
        self.assertGreaterEqual(session_logs._paused_until, before + session_logs.PAUSE_429)
        self.assertIs(session_logs._pending[session_logs.session_uuid(PARENT)]["retried"], False, "the retry is kept")
        self.assertIsNone(session_logs._take(time.monotonic())[0])
        self.assertIsNotNone(session_logs._take(session_logs._paused_until)[0])

    def test_409_drops_that_conversation_for_good(self):
        self.assertEqual(session_logs.deliver(self.upload(), Transport(reply(409))), "conflict")
        session_logs.enqueue(self.upload())
        self.assertEqual(session_logs._pending, {})

    def test_network_errors_and_5xx_retry_once_a_minute_later(self):
        for failure in (reply(503), urllib.error.URLError(ConnectionResetError())):
            with self.subTest(failure=type(failure).__name__):
                self.reset()
                before = time.monotonic()
                self.assertEqual(session_logs.deliver(self.upload(), Transport(failure)), "retry")
                waiting = session_logs._pending[session_logs.session_uuid(PARENT)]
                self.assertTrue(waiting["retried"])
                self.assertGreaterEqual(waiting["not_before"], before + session_logs.RETRY_AFTER)
                self.assertIsNone(session_logs._take(time.monotonic())[0])
                self.assertIs(session_logs._take(waiting["not_before"])[0]["retried"], True)
                self.assertEqual(session_logs.deliver(waiting, Transport(reply(503))), "dropped")
                self.assertEqual(session_logs._pending, {})

    def test_a_newer_snapshot_replaces_a_waiting_retry(self):
        session_logs.deliver(self.upload([{"role": "user", "text": "old"}]), Transport(reply(502)))
        session_logs.enqueue(self.upload([{"role": "user", "text": "new"}]))
        waiting = session_logs._pending[session_logs.session_uuid(PARENT)]
        self.assertEqual((waiting["retried"], waiting["transcript"][0]["text"]), (False, "new"))

    def test_other_4xx_are_dropped(self):
        for status in (400, 401, 413, 422):
            self.assertEqual(session_logs.deliver(self.upload(), Transport(reply(status))), "dropped")
        self.assertEqual(session_logs._pending, {})
        self.assertFalse(session_logs._stopped)

    def test_the_sender_thread_delivers_and_ends_when_idle(self):
        sent = threading.Event()
        with patch.object(session_logs, "deliver", side_effect=lambda upload: sent.set()), \
                patch.object(session_logs, "IDLE_SECONDS", 0.05):
            session_logs._pending[session_logs.session_uuid(PARENT)] = self.upload()
            thread = threading.Thread(target=session_logs._send_loop, daemon=True)
            thread.start()
            self.assertTrue(sent.wait(2))
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(session_logs._pending, {})


# --- Problem reports ----------------------------------------------------------------------------------------------

class ReportTest(Base):
    def setUp(self):
        super().setUp()
        self.conversations = self.root / "data" / "conversations"
        for n in range(4):
            conversation_id = uuid.UUID(int=n + 1, version=4).hex
            folder = self.conversations / conversation_id
            folder.mkdir(parents=True)
            (folder / "meta.json").write_text(json.dumps({"id": conversation_id, "projects": [], "title": f"chat {n}",
                                                          "created": 1760984070.0 + n, "updated": 1760990000.0 + n,
                                                          "turns": 1}))
            (folder / "conversation.json").write_text(json.dumps({"id": conversation_id, "created": 1760984070.0 + n,
                "items": [{"kind": "user", "text": f"ask {n}"}, {"kind": "error", "text": "HTTP 502"}],
                "messages": [{"role": "user", "content": f"ask {n} {REPORT_KEY}"}]}))
        self.context = {"install_id": INSTALL_ID, "app_version": "0.2.6", "key": None, "headers": HEADERS,
                        "secrets": [REPORT_KEY], "home": "/Users/someone", "system": dict(SYSTEM),
                        "root": self.conversations, "route": ("own_key", "gpt-5.5"),
                        "log": "Loopcut: started\nLoopcut: POST request failed in transport: OSError\n"}

    def test_the_three_newest_conversations_the_note_contact_and_log(self):
        body = session_logs.report_body(f"It froze, my key {REPORT_KEY}", " me@example.com ", True, self.context)
        self.assertEqual([s["transcript"][0]["text"].split()[1] for s in body["sessions"]], ["3", "2", "1"])
        self.assertEqual(body["sessions"][0]["meta"], {**SYSTEM, "provider": "own_key", "model": "gpt-5.5",
                                                       "started_at": "2025-10-20T18:14:33Z", "turns": 1, "outcome": "error"})
        self.assertEqual(body["note"], "It froze, my key [redacted]")
        self.assertEqual(body["contact_email"], "me@example.com")
        self.assertIn("transport: OSError", body["log"])
        self.assertNotIn(REPORT_KEY, json.dumps(body))
        self.assertEqual(set(body), {"install_id", "app_version", "note", "contact_email", "system", "sessions", "log"})
        for session in body["sessions"]:
            self.assertEqual(set(session), {"session_id", "meta", "transcript"})

    def test_no_conversations_when_unticked_and_a_bad_email_is_left_out(self):
        body = session_logs.report_body("", "not an email", False, self.context)
        self.assertEqual(body["sessions"], [])
        self.assertNotIn("contact_email", body)
        self.assertEqual(body["note"], "")

    def test_a_report_too_large_for_its_conversations_goes_without_them(self):
        seen = []

        def fit(body):
            seen.append((len(body["sessions"]), body["note"]))
            if len(seen) == 1:
                raise session_logs.TooLarge()
            return {"fitted": True}
        with patch.object(session_logs, "fit", side_effect=fit):
            self.assertEqual(session_logs.report_body("note", "", True, self.context), {"fitted": True})
        self.assertEqual(seen, [(3, "note"), (0, "note")])

    def test_the_code_comes_back_and_failures_point_to_email(self):
        body = session_logs.report_body("x", "", True, self.context)
        transport = Transport(reply(201, {"code": "LC-7K3QXM"}))
        self.assertEqual(session_logs.send_report(body, None, HEADERS, transport), "LC-7K3QXM")
        self.assertEqual(transport.calls[0]["url"], "https://loopcut.org/v1/reports")
        self.assertEqual(transport.calls[0]["headers"]["content-encoding"], "gzip")
        for failure, words in ((reply(404), "Reports aren't available right now"), (reply(429), "Too many reports"),
                               (urllib.error.URLError(OSError()), "Could not send the report"),
                               (reply(201, {"code": "nope"}), "no code came back")):
            with self.subTest(words=words), self.assertRaisesRegex(session_logs.ReportError, words) as caught:
                session_logs.send_report(body, None, HEADERS, Transport(failure))
            self.assertIn("hello@loopcut.org", str(caught.exception))

    def test_a_refused_bearer_is_retried_unsigned_and_413_without_conversations(self):
        body = session_logs.report_body("x", "", True, self.context)
        transport = Transport(reply(401), reply(413), reply(201, {"code": "LC-ABCDEF"}))
        self.assertEqual(session_logs.send_report(body, BEARER, HEADERS, transport), "LC-ABCDEF")
        self.assertEqual([c["headers"].get("authorization") for c in transport.calls], ["Bearer " + BEARER, None, None])
        self.assertEqual([len(c["body"]["sessions"]) for c in transport.calls], [3, 3, 0])

    def test_start_report_sends_off_the_main_thread_and_reports_back_on_it(self):
        done = threading.Event()
        results = []
        with patch.object(session_logs, "_sender_context", return_value=dict(self.context)), \
                patch.object(session_logs, "_current_route", return_value=("chatgpt", "gpt-5.5")), \
                patch.object(session_logs, "send_report", return_value="LC-7K3QXM") as send, \
                patch("loopcut.mainthread.run_on_main", side_effect=lambda fn: fn()):
            self.assertTrue(session_logs.start_report("note", "", True, on_done=lambda s: (results.append(s), done.set())))
            self.assertFalse(session_logs.start_report("again", "", True), "one report at a time")
            self.assertTrue(done.wait(5))
        self.assertEqual(results[0]["code"], "LC-7K3QXM")
        self.assertEqual(session_logs.report_state()["status"], "sent")
        self.assertIn("LC-7K3QXM", session_logs.report_line())
        self.assertEqual(send.call_args.args[1:], (None, HEADERS))
        sys.modules["bpy"].app.background = True
        self.assertFalse(session_logs.start_report("x", "", True))


class ReportOperatorTest(Base):
    """support.py's LOOPCUT_OT_report_problem under a small fake bpy."""

    def setUp(self):
        super().setUp()

        class Operator:
            def report(self, kind, text):
                self.reports.append((set(kind), text))
        self.manager = types.SimpleNamespace(clipboard="", windows=[object()], popup_menu=Mock(),
                                             invoke_props_dialog=Mock(return_value={"RUNNING_MODAL"}))
        fake = types.SimpleNamespace(
            types=types.SimpleNamespace(Operator=Operator), app=types.SimpleNamespace(background=False, version=(5, 2, 2)),
            props=types.SimpleNamespace(StringProperty=lambda **kw: kw, BoolProperty=lambda **kw: kw),
            context=types.SimpleNamespace(window=None, window_manager=self.manager,
                                          temp_override=lambda **kw: contextlib.nullcontext()),
            ops=Mock(), utils=types.SimpleNamespace(register_class=Mock(), unregister_class=Mock()))
        saved = sys.modules.pop("loopcut.support", None)
        modules = patch.dict(sys.modules, {"bpy": fake})
        modules.start()
        self.addCleanup(modules.stop)
        self.addCleanup(lambda: sys.modules.__setitem__("loopcut.support", saved) if saved
                        else sys.modules.pop("loopcut.support", None))
        self.support = importlib.import_module("loopcut.support")
        self.fake = fake
        self.op = self.support.LOOPCUT_OT_report_problem()
        self.op.reports, self.op.note, self.op.contact_email, self.op.include_conversations = [], "It froze", "", True

    def test_invoke_prefills_the_account_email_in_a_dialog(self):
        with patch.object(session_logs, "account_email", return_value="me@example.com"):
            self.op.invoke(types.SimpleNamespace(window_manager=self.manager), None)
        self.assertEqual(self.op.contact_email, "me@example.com")
        self.manager.invoke_props_dialog.assert_called_once()

    def test_execute_starts_the_report_and_the_result_lands_on_the_clipboard(self):
        with patch.object(session_logs, "start_report", return_value=True) as start:
            self.assertEqual(self.op.execute(None), {"FINISHED"})
        start.assert_called_once_with("It froze", "", True, on_done=self.support.report_finished)
        self.support.report_finished({"status": "sent", "code": "LC-7K3QXM", "message": "Report sent: LC-7K3QXM"})
        self.assertEqual(self.manager.clipboard, "LC-7K3QXM")
        self.manager.popup_menu.assert_called_once()
        self.manager.clipboard = ""
        self.support.report_finished({"status": "failed", "code": "", "message": "Please email hello@loopcut.org"})
        self.assertEqual(self.manager.clipboard, "")

    def test_background_or_a_refusal_cancels(self):
        with patch.object(session_logs, "start_report", return_value=False):
            self.assertEqual(self.op.execute(None), {"CANCELLED"})
        self.assertIn("hello@loopcut.org", self.op.reports[0][1])
        self.fake.app.background = True
        with patch.object(session_logs, "start_report") as start:
            self.assertEqual(self.op.execute(None), {"CANCELLED"})
        start.assert_not_called()
        self.assertFalse(self.support.LOOPCUT_OT_report_problem.poll(None))

    def test_both_operators_register(self):
        self.support.register()
        names = [c.args[0].bl_idname for c in self.fake.utils.register_class.call_args_list]
        self.assertEqual(names, ["loopcut.report_bug", "loopcut.report_problem"])


class PlacementTest(unittest.TestCase):
    def test_report_a_problem_is_never_drawn_in_the_chat_panel(self):
        """The owner's rule: Preferences › Add-ons › Loopcut only. Only settings (the preferences page) and
        support (the operator) may name it; the chat panel and sidebar (ui/) never do."""
        users = sorted(path.relative_to(ADDONS).as_posix() for path in ADDONS.rglob("*.py")
                       if "report_problem" in path.read_text(encoding="utf-8"))
        self.assertEqual(users, ["settings.py", "support.py"])


# --- The console tee ----------------------------------------------------------------------------------------------

class TeeTest(unittest.TestCase):
    def setUp(self):
        self.out, self.err = io.StringIO(), io.StringIO()
        streams = patch.multiple(sys, stdout=self.out, stderr=self.err)
        streams.start()
        self.addCleanup(streams.stop)
        self.addCleanup(session_logs.remove_tee)
        with session_logs._log_lock:
            session_logs._log.clear()
            session_logs._log_chars = 0
            session_logs._partial.update(out="", err="")

    def test_writes_pass_through_unchanged_and_lines_are_kept(self):
        session_logs.install_tee()
        session_logs.install_tee()  # Idempotent.
        print("Loopcut: hello")
        sys.stdout.write("part one, ")
        sys.stdout.write("part two\n")
        sys.stdout.writelines(["listed\n"])
        print("Traceback (most recent call last):", file=sys.stderr)
        self.assertEqual(self.out.getvalue(), "Loopcut: hello\npart one, part two\nlisted\n")
        self.assertEqual(self.err.getvalue(), "Traceback (most recent call last):\n")
        lines = [line.split(" ", 1)[1] for line in session_logs.recent_log().splitlines()]
        self.assertEqual(lines, ["Loopcut: hello", "part one, part two", "listed", "! Traceback (most recent call last):"])
        self.assertEqual(sys.stdout.getvalue(), self.out.getvalue(), "other attributes are the stream's")
        session_logs.remove_tee()
        self.assertIs(sys.stdout, self.out)
        self.assertIs(sys.stderr, self.err)

    def test_the_buffer_is_bounded_and_filtered_by_time(self):
        session_logs.install_tee()
        with patch.object(session_logs, "LOG_LINES", 5):
            for n in range(12):
                print(f"line {n}")
        self.assertEqual([line.split(" ", 1)[1] for line in session_logs.recent_log().splitlines()],
                         [f"line {n}" for n in range(7, 12)])
        self.assertEqual(session_logs.recent_log(since=time.time() + 60), "")
        self.assertLessEqual(len(session_logs.recent_log(limit=20).encode()), 20)

    def test_a_tee_left_by_a_reload_is_replaced_and_a_wrapped_tee_goes_quiet(self):
        session_logs.install_tee()
        first = sys.stdout
        session_logs._tees.clear()  # As if the module were reloaded.
        session_logs.install_tee()
        self.assertFalse(first.active)
        self.assertIs(sys.stdout._stream, self.out)
        tee = sys.stdout
        sys.stdout = io.StringIO()  # Something else wraps it now.
        session_logs.remove_tee()
        self.assertFalse(tee.active)

    def test_register_skips_background_blender(self):
        with patch.dict(sys.modules, {"bpy": types.SimpleNamespace(app=types.SimpleNamespace(background=True))}):
            session_logs.register()
        self.assertIs(sys.stdout, self.out)


# --- cloud_http's gzip option -------------------------------------------------------------------------------------

class CompressTest(unittest.TestCase):
    def test_compress_sends_gzip_json_and_the_header_cannot_be_forged(self):
        transport = Transport(reply(204))
        client = cloud_http.Client(base_url="https://service.test", transport=transport, client_headers=HEADERS)
        client.request_response("POST", "/v1/logs", {"a": "é"}, auth=False, compress=True)
        self.assertEqual(transport.calls[0]["body"], {"a": "é"})
        self.assertEqual(transport.calls[0]["headers"]["content-encoding"], "gzip")
        self.assertEqual(cloud_http.gzip_body(b"same"), cloud_http.gzip_body(b"same"))
        with self.assertRaisesRegex(ValueError, "cannot be overridden"):
            client.request_response("POST", "/v1/logs", {}, auth=False, headers={"Content-Encoding": "br"})


# --- The contract ---------------------------------------------------------------------------------------------------

class ContractSchemaTest(Base):
    def setUp(self):
        super().setUp()
        self.log_schema, self.folder = _schema("conversation-log.schema.json")
        self.report_schema, _ = _schema("problem-report.schema.json")
        if self.log_schema is None or self.report_schema is None:
            self.skipTest("needs the platform contract with proposal 015's schemas")

    def check(self, body, schema):
        validate(body, schema, schema, self.folder)

    def test_the_validator_agrees_with_the_contract_fixtures(self):
        fixtures_dir = self.folder.parent / "fixtures"
        for name, schema in (("conversation-log", self.log_schema), ("problem-report", self.report_schema)):
            valid = sorted((fixtures_dir / name).glob("*.json"))
            invalid = sorted((fixtures_dir / "invalid" / name).glob("*.json"))
            self.assertTrue(valid and invalid)
            for path in valid:
                self.check(json.loads(path.read_text()), schema)
            for path in invalid:
                with self.subTest(path=path.name), self.assertRaises(Invalid):
                    self.check(json.loads(path.read_text()), schema)

    def test_every_built_log_validates(self):
        messages, items, _ = conversation(self.root / "images")
        items.append({"kind": "wave", "wave": "w1", "number": 1, "text": "Wave 1 · 1 worker",
                      "rows": [{"id": "w1", "deliverable": "Chair", "role": "builder", "status": "done"}]})
        items.append({"kind": "offer", "nudge": "trial", "title": "Try Pro", "text": "…", "actions": []})
        workers = [{"session_id": session_logs.session_uuid(WORKER), "worker": "w1", "job": "1-1", "wave": "w1",
                    "status": "done", "turn": 1}]
        transcript = session_logs.build_transcript(messages, items, images=self.root / "images", workers=workers)
        for provider, model, outcome in (("chatgpt", "gpt-5.5", "ok"), ("loopcut", "fast", "error"),
                                         ("own_key", "other", "cancelled"), ("own_key", "claude-opus-5.5", "empty")):
            upload = self.upload(transcript, log="Loopcut: a line\n")
            upload["meta"].update(provider=provider, model=model, outcome=outcome)
            self.check(session_logs.log_body(upload), self.log_schema)
        self.check(session_logs.log_body(self.upload([])), self.log_schema)
        crowded = session_logs.build_transcript(
            [{"role": "assistant", "tool_calls": [call("c" * 500, "n" * 500, "{}")]}]
            + [{"role": "tool", "tool_call_id": "c" * 500, "content": "x"}] * 2500, [])
        self.check(session_logs.log_body(self.upload(crowded)), self.log_schema)

    def test_every_built_report_validates(self):
        report = ReportTest("test_the_three_newest_conversations_the_note_contact_and_log")
        report.setUp()
        self.addCleanup(report.doCleanups)
        for note, email, include in (("It froze", "me@example.com", True), ("", "", False), ("x" * 5000, "bad", True)):
            self.check(session_logs.report_body(note, email, include, report.context), self.report_schema)
        self.check({"code": "LC-7K3QXM"}, self.report_schema["$defs"]["receipt"])
        self.assertTrue(session_logs._CODE.match("LC-7K3QXM"))


if __name__ == "__main__":
    unittest.main()
