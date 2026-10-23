"""Usage events (telemetry.py): the payload builders against the contract's allow-list, the gating (preference,
environment switch, background), the client-side caps and fire-and-forget delivery. No Blender, no network.

The contract's usage-event.schema.json (proposal 014) lives in the private platform repository: with it present
(platform_contract.contract_dir(), or LOOPCUT_CONTRACT_DIR), every built payload is validated against it and the
contract's own fixtures check the small validator below. Without it, the allow-list tests still run."""

import json
import os
import re
import sys
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import test_cloud as fixtures
from platform_contract import contract_dir
from loopcut import cloud_http, credentials, telemetry

INSTALL_ID = "01234567-89ab-4cde-8fab-0123456789ab"


# --- A JSON Schema subset: what usage-event.schema.json uses --------------------------------------------------

class Invalid(ValueError):
    pass


def _resolve(ref: str, root: dict, folder: Path) -> tuple[dict, dict]:
    target, _, pointer = ref.partition("#")
    document = root if not target else json.loads((folder / target).read_text())
    node = document
    for part in [p for p in pointer.split("/") if p]:
        node = node[part]
    return node, document


_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}


def _is_type(value, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def validate(value, schema: dict, root: dict, folder: Path, where: str = "$") -> None:
    if "$ref" in schema:
        node, document = _resolve(schema["$ref"], root, folder)
        validate(value, node, document, folder, where)
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, name) for name in names):
            raise Invalid(f"{where}: type")
    if "const" in schema and value != schema["const"]:
        raise Invalid(f"{where}: const")
    if "enum" in schema and value not in schema["enum"]:
        raise Invalid(f"{where}: enum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1 << 30):
            raise Invalid(f"{where}: length")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise Invalid(f"{where}: pattern")
    if _is_type(value, "number"):
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")):
            raise Invalid(f"{where}: range")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise Invalid(f"{where}.{key}: required")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], root, folder, f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                raise Invalid(f"{where}.{key}: not allowed")
    for part in schema.get("allOf", []):
        validate(value, part, root, folder, where)
    if "if" in schema:
        try:
            validate(value, schema["if"], root, folder, where)
            matched = True
        except Invalid:
            matched = False
        if matched and "then" in schema:
            validate(value, schema["then"], root, folder, where)


def _schema():
    folder = contract_dir()
    path = folder / "schemas" / "usage-event.schema.json" if folder else None
    return (json.loads(path.read_text()), path.parent) if path and path.is_file() else (None, None)


# --- Payloads ---------------------------------------------------------------------------------------------------

def payloads() -> list[dict]:
    """One of each shape the add-on can build, edge values included."""
    start = [telemetry.app_start_props(first, (5, 2, 2), cycle, os_value, machine)
             for first, cycle, os_value, machine in ((True, "release", "darwin", "arm64"), (False, "alpha", "win32", "AMD64"),
                                                     (False, "", "linux", "aarch64"), (True, "beta", "freebsd14", "riscv64"))]
    steps = [telemetry.setup_step_props(step, choice, ok, code)
             for step in telemetry.STEPS for choice in telemetry.CHOICES
             for ok, code in ((True, None), (False, "access_denied"), (False, 502), (False, "Could not reach x: y"))]
    turns = [telemetry.turn_props(provider, model, outcome, duration, tools, code)
             for provider, model in (("loopcut", "fast"), ("chatgpt", "gpt-6.1-sol"), ("own_key", "anthropic/claude-opus-5.5"),
                                     ("own_key", "My Client's Secret Finetune v2"), ("own_key", "llama3.1:8b"))
             for outcome, code in (("ok", None), ("error", "rate_limited"), ("cancelled", None), ("empty", "empty_reply"))
             for duration, tools in ((0, 0), (12_345.6, 7), (10 ** 9, 10 ** 6))]
    return ([telemetry.envelope("app_start", p, INSTALL_ID, "0.2.5") for p in start]
            + [telemetry.envelope("setup_step", p, INSTALL_ID, "0.2.4") for p in steps]
            + [telemetry.envelope("turn", p, INSTALL_ID, "0.2.5-beta.1") for p in turns])


class PayloadTest(unittest.TestCase):
    def test_every_payload_is_exactly_the_allow_list(self):
        for body in payloads():
            self.assertEqual(set(body), {"type", "event", "install_id", "app_version", "props"})
            self.assertEqual(body["type"], "usage")
            required, optional = telemetry.PROPS[body["event"]]
            self.assertTrue(required <= set(body["props"]) <= required | optional, body)
            for key, value in body["props"].items():
                self.assertIsInstance(value, (str, bool, int, type(None)), key)

    def test_enums_and_bounds(self):
        for body in payloads():
            props = body["props"]
            if body["event"] == "app_start":
                self.assertIn(props["os"], telemetry.OS_NAMES)
                self.assertIn(props["arch"], telemetry.ARCHES)
                self.assertRegex(props["blender_version"], r"\A[0-9]+(\.[0-9]+){0,3}(-[0-9A-Za-z]+)?\Z")
            elif body["event"] == "setup_step":
                self.assertIn(props["step"], telemetry.STEPS)
                self.assertIn(props["choice"], telemetry.CHOICES)
            else:
                self.assertIn(props["provider"], telemetry.PROVIDERS)
                self.assertIn(props["outcome"], telemetry.OUTCOMES)
                self.assertRegex(props["model"], r"\A[a-z0-9._-]{1,32}\Z")
                self.assertTrue(0 <= props["duration_ms"] <= 86_400_000)
                self.assertTrue(0 <= props["tool_calls"] <= 10_000)
            if "error_code" in props:
                self.assertRegex(props["error_code"], r"\A[a-z0-9_]{1,64}\Z")

    def test_platform_values(self):
        self.assertEqual([telemetry.os_name(v) for v in ("darwin", "win32", "linux", "cygwin")],
                         ["mac", "windows", "linux", "windows"])
        self.assertEqual([telemetry.arch_name(v) for v in ("arm64", "AMD64", "x86_64", "aarch64", "i386")],
                         ["arm64", "x86_64", "x86_64", "arm64", "other"])
        self.assertEqual(telemetry.blender_version((5, 2, 2), "release"), "5.2.2")
        self.assertEqual(telemetry.blender_version((5, 3, 0), "alpha"), "5.3.0-alpha")

    def test_no_free_text_gets_through(self):
        self.assertEqual(telemetry.model_id("own_key", "My Client's Secret Finetune v2"), "other")
        self.assertEqual(telemetry.model_id("own_key", "/Users/someone/models/x.gguf"), "other")
        self.assertEqual(telemetry.model_id("own_key", "anthropic/claude-opus-5.5"), "claude-opus-5.5")
        self.assertEqual(telemetry.model_id("own_key", "llama3.1:8b"), "llama3.1-8b")
        self.assertEqual(telemetry.model_id("chatgpt", "gpt-6.1-sol"), "gpt-6.1-sol")
        self.assertEqual(telemetry.model_id("loopcut", "fast"), "fast")
        self.assertEqual(telemetry.error_code("The model said: no"), "other")
        self.assertEqual(telemetry.error_code("rate-limited"), "rate_limited")
        self.assertEqual(telemetry.error_code(502), "http_502")
        self.assertIsNone(telemetry.error_code(""))
        # A turn's code only accompanies a failure; a setup step's also names the action taken.
        self.assertEqual(telemetry.setup_step_props("done", "chatgpt", True, "card_motion")["error_code"], "card_motion")
        self.assertNotIn("error_code", telemetry.setup_step_props("chatgpt", "chatgpt", True, None))
        self.assertNotIn("error_code", telemetry.turn_props("loopcut", "fast", "ok", 1, 0, "x"))

    def test_the_envelope_refuses_what_the_server_would(self):
        good = telemetry.app_start_props(True, (5, 2, 2), "", "darwin", "arm64")
        for event, props, install, version in (
                ("crash", good, INSTALL_ID, "0.2.5"),
                ("app_start", {**good, "scene": "x"}, INSTALL_ID, "0.2.5"),
                ("app_start", {k: v for k, v in good.items() if k != "os"}, INSTALL_ID, "0.2.5"),
                ("app_start", good, INSTALL_ID.upper(), "0.2.5"),
                ("app_start", good, str(uuid.uuid1()), "0.2.5"),
                ("app_start", good, INSTALL_ID, "0.2.5+build"),
                ("app_start", good, INSTALL_ID, "0.2.5\n")):
            with self.assertRaises(ValueError):
                telemetry.envelope(event, props, install, version)
        for step, choice in (("signup", None), ("chatgpt", "openai")):
            with self.assertRaises(ValueError):
                telemetry.setup_step_props(step, choice, True)
        with self.assertRaises(ValueError):
            telemetry.turn_props("openai", "gpt-5", "ok", 1, 0)


@unittest.skipIf(_schema()[0] is None, "needs the platform contract's usage-event.schema.json (proposal 014)")
class ContractSchemaTest(unittest.TestCase):
    def setUp(self):
        self.schema, self.folder = _schema()

    def test_the_validator_agrees_with_the_contract_fixtures(self):
        fixtures_dir = self.folder.parent / "fixtures" / "usage-event"
        valid = sorted(fixtures_dir.glob("*.json"))
        self.assertTrue(valid)
        for path in valid:
            validate(json.loads(path.read_text()), self.schema, self.schema, self.folder)
        body = json.loads(valid[0].read_text())
        for broken in ({**body, "extra": 1}, {**body, "props": {**body["props"], "prompt": "hi"}},
                       {**body, "install_id": INSTALL_ID.upper()}, {**body, "type": "interest"}):
            with self.assertRaises(Invalid):
                validate(broken, self.schema, self.schema, self.folder)

    def test_every_built_payload_validates(self):
        for body in payloads():
            validate(body, self.schema, self.schema, self.folder)


# --- Gating, caps and delivery ----------------------------------------------------------------------------------

class SendingTest(fixtures.RuntimeFixture):
    def setUp(self):
        super().setUp()
        sys.modules["bpy"].app.background = False
        sys.modules["bpy"].app.version = (5, 2, 2)
        self.config_dir = Path(self.directory.name) / "config"
        for patcher in (patch.object(credentials, "folder", return_value=self.config_dir),
                        patch.object(credentials, "api_key", return_value=""),
                        patch.object(cloud_http, "contract_headers",
                                     return_value={"x-loopcut-contract": "0.6", "x-loopcut-client": "loopcut-addon/0.2.4 blender/5.2.2 os/mac arch/arm64"}),
                        patch.object(telemetry, "_ensure_sender"),
                        patch.dict(os.environ, {"LOOPCUT_TELEMETRY": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(telemetry._APP_START_FLAG, None)
        self.prefs = types.SimpleNamespace(share_usage_stats=True)
        prefs = patch.object(telemetry, "_preferences", return_value=self.prefs)
        prefs.start()
        self.addCleanup(prefs.stop)
        telemetry._stopped, telemetry._paused_until = False, 0.0
        telemetry._sent_times.clear()
        telemetry._seen_steps.clear()
        while not telemetry._queue.empty():
            telemetry._queue.get_nowait()

    def tearDown(self):
        os.environ.pop(telemetry._APP_START_FLAG, None)
        telemetry._stopped, telemetry._paused_until = False, 0.0
        super().tearDown()

    def queued(self) -> list:
        items = []
        while not telemetry._queue.empty():
            items.append(telemetry._queue.get_nowait())
        return items

    def test_an_event_is_queued_with_the_install_id_and_no_bearer_when_signed_out(self):
        telemetry.setup_step("chatgpt", "chatgpt", False, "access_denied")
        [(body, key, headers)] = self.queued()
        self.assertEqual(body["install_id"], credentials.interest_install_id())
        self.assertEqual(body["props"], {"step": "chatgpt", "choice": "chatgpt", "ok": False, "error_code": "access_denied"})
        self.assertIsNone(key)
        self.assertIn("os/mac", headers["x-loopcut-client"])

    def test_signed_in_events_carry_the_bearer(self):
        with patch.object(credentials, "api_key", return_value="lc_test"):
            telemetry.setup_step("done", "loopcut")
        [(body, key, _)] = self.queued()
        self.assertEqual(key, "lc_test")

    def test_both_paths_send_the_install_id_and_app_version(self):
        """The deployed usage-event schema requires both whether or not a bearer goes along."""
        from loopcut import config
        schema, folder = _schema()
        for api_key in ("", "lc_test"):
            with patch.object(credentials, "api_key", return_value=api_key):
                telemetry._seen_steps.clear()
                telemetry.setup_step("done", "loopcut")
            [(body, _, _)] = self.queued()
            self.assertEqual(body["install_id"], credentials.interest_install_id())
            self.assertEqual(body["app_version"], config.app_version())
            if schema is not None:
                validate(body, schema, schema, folder)

    def test_nothing_is_sent_when_switched_off_or_headless(self):
        self.prefs.share_usage_stats = False
        telemetry.setup_step("welcome", None)
        self.prefs.share_usage_stats = True
        with patch.dict(os.environ, {"LOOPCUT_TELEMETRY": "off"}):
            telemetry.setup_step("welcome", None)
        sys.modules["bpy"].app.background = True
        telemetry.setup_step("welcome", None)
        self.assertEqual(self.queued(), [])
        self.assertFalse((self.config_dir / "install.json").exists(), "no install id made for nothing")

    def test_first_use_milestones_are_persistent_private_and_contract_valid(self):
        schema, folder = _schema()
        for action in ("panel_opened", "prompt_typed", "prompt_sent", "result_error", "result_ok"):
            telemetry.first_use(action)
            telemetry.first_use(action)
        queued = self.queued()
        self.assertEqual([body["props"]["error_code"] for body, _, _ in queued],
                         ["first_panel_opened", "first_prompt_typed", "first_prompt_sent", "first_result_error"])
        for body, _, _ in queued:
            self.assertEqual(body["event"], "setup_step")
            self.assertEqual(set(body["props"]), {"step", "choice", "ok", "error_code"})
            if schema:
                validate(body, schema, schema, folder)
        saved = json.loads((self.config_dir / "first-use.json").read_text())
        self.assertEqual(saved, {"panel_opened": True, "prompt_typed": True, "prompt_sent": True, "result": True})
        telemetry._seen_steps.clear()  # A later launch still does not send milestones again.
        telemetry.first_use("prompt_sent")
        self.assertEqual(self.queued(), [])

    def test_first_use_storage_handles_spaces_and_non_ascii_paths(self):
        folder = self.config_dir / "Sam Price" / "模型"
        with patch.object(credentials, "folder", return_value=folder):
            telemetry.first_use("prompt_sent")
        self.assertTrue((folder / "first-use.json").is_file())
        self.assertEqual(len(self.queued()), 1)

    def test_first_use_opt_out_writes_no_flags_and_sends_nothing(self):
        self.prefs.share_usage_stats = False
        for action in telemetry._FIRST_ACTIONS:
            telemetry.first_use(action)
        self.assertFalse((self.config_dir / "first-use.json").exists())
        self.assertEqual(self.queued(), [])

    def test_first_use_ignores_existing_users_and_unrelated_results(self):
        self.prefs.first_message_sent = True
        for action in telemetry._FIRST_ACTIONS:
            telemetry.first_use(action)
        self.assertEqual(self.queued(), [])
        self.assertFalse((self.config_dir / "first-use.json").exists())

    def test_first_result_uses_the_actual_turn_outcome(self):
        telemetry.first_use("prompt_sent")
        self.queued()
        self.prefs.first_message_sent = True
        turn = types.SimpleNamespace(outcome="cancelled", error_code="", started=time.monotonic(), first_item=0,
                                     telemetry={"provider": "chatgpt", "model": "gpt-6.1-sol"})
        telemetry.turn_end(turn, [])
        bodies = [body for body, _, _ in self.queued()]
        self.assertEqual([b["event"] for b in bodies], ["turn", "setup_step"])
        self.assertEqual(bodies[1]["props"]["error_code"], "first_result_cancelled")
        self.assertFalse(bodies[1]["props"]["ok"])

    def test_caps(self):
        telemetry.app_start()
        telemetry.app_start()
        telemetry.setup_step("welcome", None)
        telemetry.setup_step("welcome", None)
        self.assertEqual([b["event"] for b, _, _ in self.queued()], ["app_start", "setup_step"])
        sent = []
        for n in range(telemetry.MAX_EVENTS + 5):
            telemetry.record("turn", telemetry.turn_props("loopcut", "fast", "ok", n, 0))
            sent += self.queued()  # The sender thread's part.
        self.assertEqual(len(sent), telemetry.MAX_EVENTS - 2, "the rolling window caps everything")

    def test_first_run_is_whether_the_install_record_existed_at_register(self):
        telemetry._first_run = None
        telemetry.note_first_run()
        self.assertTrue(telemetry._first_run)
        telemetry.app_start()
        [(body, _, _)] = self.queued()
        self.assertIs(body["props"]["first_run"], True)
        telemetry._first_run = None
        telemetry.note_first_run()
        self.assertFalse(telemetry._first_run, "the record exists now")

    def send(self, *replies, key=None):
        transport = fixtures.Transport(*replies)
        body = telemetry.envelope("turn", telemetry.turn_props("loopcut", "fast", "ok", 1, 0), INSTALL_ID, "0.2.5")
        telemetry._send(body, key, {"x-loopcut-contract": "0.6", "x-loopcut-client": "loopcut-addon/0.2.5 blender/5.2.2"},
                        transport=transport)
        return transport.calls

    def test_delivery_is_one_post_and_errors_are_dropped(self):
        [call] = self.send(fixtures.Reply(None, 204))
        self.assertEqual((call["method"], call["url"].rsplit("/", 2)[-2:]), ("POST", ["v1", "events"]))
        self.assertNotIn("authorization", call["headers"])
        [call] = self.send(fixtures.Reply(None, 204), key="lc_test")
        self.assertEqual(call["headers"]["authorization"], "Bearer lc_test")
        self.assertEqual(len(self.send(fixtures.Reply({"error": {"code": "upstream_error"}}, 500))), 1, "no retry")
        self.assertEqual(len(self.send(OSError("down"))), 1)
        self.assertFalse(telemetry._stopped)

    def test_an_old_server_or_a_switched_off_one_stops_sending_until_the_next_launch(self):
        self.send(fixtures.Reply({"error": {"code": "invalid_input", "field": "type", "message": "x"}}, 422))
        self.assertTrue(telemetry._stopped)
        self.assertEqual(self.send(), [], "nothing more goes out")
        telemetry._stopped = False
        self.send(fixtures.Reply({"error": {"code": "not_found", "message": "x"}}, 404))
        self.assertTrue(telemetry._stopped)
        telemetry._stopped = False
        self.send(fixtures.Reply({"error": {"code": "invalid_input", "field": "props.model", "message": "x"}}, 422))
        self.assertFalse(telemetry._stopped, "one bad event is dropped, not the rest")
        self.send(fixtures.Reply({"error": {"code": "rate_limited", "message": "x"}}, 429))
        self.assertGreater(telemetry._paused_until, time.monotonic())

    def test_turn_end_maps_outcomes(self):
        def turn(outcome, code="", items=()):
            value = types.SimpleNamespace(outcome=outcome, error_code=code, started=time.monotonic() - 2,
                                          first_item=1, telemetry={"provider": "loopcut", "model": "fast"})
            telemetry.turn_end(value, [{"kind": "user"}, *items])
            [(body, _, _)] = self.queued()
            telemetry._sent_times.clear()
            return body["props"]
        props = turn("completed", items=({"kind": "tool"}, {"kind": "assistant"}, {"kind": "tool"}))
        self.assertEqual((props["outcome"], props["tool_calls"]), ("ok", 2))
        self.assertGreaterEqual(props["duration_ms"], 2000)
        self.assertEqual(turn("error", "empty_reply")["outcome"], "empty")
        self.assertEqual(turn("error", "week_limit")["error_code"], "week_limit")
        self.assertEqual(turn("step_limit")["error_code"], "step_limit")
        self.assertEqual(turn("cancelled")["outcome"], "cancelled")
        telemetry.turn_end(types.SimpleNamespace(outcome="completed", started=None, telemetry={}), [])
        self.assertEqual(self.queued(), [], "a turn that never launched (tests, workers) sends nothing")


if __name__ == "__main__":
    unittest.main()
