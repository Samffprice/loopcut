"""Gateway role gating and request headers, without Blender or external HTTP."""

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/addons_core"))

from loopcut import config, llm, router  # noqa: E402

BYOK = "https://openrouter.ai/api/v1"
LOCAL = "http://127.0.0.1:11434/v1"


class ServiceTest(unittest.TestCase):
    def test_gateway_and_unset_or_empty_stand_ins(self):
        for env in ({}, {"LOOPCUT_SERVICE_URLS": ""}, {"LOOPCUT_SERVICE_URLS": " , / , "}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertTrue(config.on_service(config.SERVICE_BASE_URL))
                self.assertTrue(config.on_service(config.SERVICE_BASE_URL + "///"))
                self.assertFalse(config.on_service(BYOK))
                self.assertFalse(config.on_service(LOCAL))

    def test_exact_glob_whitespace_and_trailing_slashes(self):
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS":
                " https://proxy.example/v1/// , http://127.0.0.1:*/v1/ , "}, clear=True):
            for url in ("https://proxy.example/v1", " https://proxy.example/v1/// ",
                        "http://127.0.0.1:8765/v1", "http://127.0.0.1:54321/v1/"):
                with self.subTest(url=url):
                    self.assertTrue(config.on_service(url))
            for url in (BYOK, "https://PROXY.example/v1", "https://proxy.example/v1/extra",
                        "http://127.0.0.2:8765/v1", "http://127.0.0.1:8765/v2"):
                with self.subTest(url=url):
                    self.assertFalse(config.on_service(url))

    def test_environment_is_read_per_call_without_preferences(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(config, "_preference_values") as prefs:
            self.assertFalse(config.on_service(LOCAL))
            os.environ["LOOPCUT_SERVICE_URLS"] = LOCAL
            self.assertTrue(config.on_service(LOCAL))
            os.environ["LOOPCUT_SERVICE_URLS"] = ""
            self.assertFalse(config.on_service(LOCAL))
            prefs.assert_not_called()


class RolesTest(unittest.TestCase):
    def roles(self, base_url, *, preferences=None, **overrides):
        env = {"LOOPCUT_API_KEY": "test", "LOOPCUT_BASE_URL": base_url, "LOOPCUT_MODEL": "pro", **overrides}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(config, "_find_env_file", return_value=None), \
                mock.patch.object(config, "_preference_values", return_value=preferences or {}):
            return config.load_roles()

    def test_byok_cannot_enable_workers_or_router_from_env_or_preferences(self):
        prefs = {"LOOPCUT_ORCHESTRATE": "true", "LOOPCUT_ROUTER": "on"}
        for url in (BYOK, LOCAL):
            for env in ({}, prefs):
                with self.subTest(url=url, env=env):
                    roles = self.roles(url, preferences=prefs, **env)
                    self.assertIs(roles.orchestrate, False)
                    self.assertIsNone(roles.router)

    def test_both_worker_and_orchestrator_must_be_on_service(self):
        for overrides in ({"LOOPCUT_WORKER_BASE_URL": BYOK}, {"LOOPCUT_ORCH_BASE_URL": BYOK}):
            with self.subTest(overrides=overrides):
                roles = self.roles(config.SERVICE_BASE_URL, LOOPCUT_ORCHESTRATE="true", **overrides)
                self.assertIs(roles.orchestrate, False)

    def test_router_uses_its_own_url(self):
        roles = self.roles(config.SERVICE_BASE_URL, LOOPCUT_ROUTER_BASE_URL=BYOK)
        self.assertIsNone(roles.router)
        roles = self.roles(BYOK, LOOPCUT_ROUTER_BASE_URL=config.SERVICE_BASE_URL)
        self.assertEqual(roles.router.base_url, config.SERVICE_BASE_URL)
        self.assertIs(roles.orchestrate, False)

    def test_service_obeys_defaults_preferences_and_explicit_switches(self):
        for url, stand_ins in ((config.SERVICE_BASE_URL, ""), (LOCAL, "http://127.0.0.1:*/v1")):
            for enabled in (True, False):
                with self.subTest(url=url, enabled=enabled):
                    roles = self.roles(url, LOOPCUT_SERVICE_URLS=stand_ins,
                                       LOOPCUT_ORCHESTRATE=str(enabled).lower(), LOOPCUT_ROUTER=str(enabled).lower())
                    self.assertIs(roles.orchestrate, enabled)
                    self.assertEqual(roles.router is not None, enabled)
            roles = self.roles(url, LOOPCUT_SERVICE_URLS=stand_ins)
            self.assertIs(roles.orchestrate, True)
            self.assertIsNotNone(roles.router)
        roles = self.roles(config.SERVICE_BASE_URL, preferences={"LOOPCUT_ORCHESTRATE": "false", "LOOPCUT_ROUTER": "off"})
        self.assertIs(roles.orchestrate, False)
        self.assertIsNone(roles.router)

    def test_chatgpt_stays_single_even_if_listed_as_a_stand_in(self):
        roles = self.roles(config.CHATGPT_PLAN_BASE_URL, LOOPCUT_SERVICE_URLS=config.CHATGPT_PLAN_BASE_URL,
                           LOOPCUT_ORCHESTRATE="true", LOOPCUT_ROUTER="on")
        self.assertIs(roles.orchestrate, False)
        self.assertIsNone(roles.router)


class HeadersTest(unittest.TestCase):
    def setUp(self):
        from prompt_index_fixture import index_patch
        patcher = index_patch()
        patcher.start()
        self.addCleanup(patcher.stop)

    def response(self, text="ok"):
        data = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
        response = io.BytesIO(b"data: " + json.dumps(data).encode() + b"\n\ndata: [DONE]\n\n")
        response.headers = {}
        return response

    def request(self, base_url, **kwargs):
        with mock.patch.object(llm.urllib.request, "urlopen", return_value=self.response()) as opened:
            llm.stream_chat(base_url=base_url, api_key="test", model="fast", messages=[], tools=[],
                            reasoning_effort="low", on_text=lambda _: None, is_cancelled=lambda: False, **kwargs)
        return {name.lower(): value for name, value in opened.call_args.args[0].header_items()}

    def test_byok_strips_every_loopcut_header_case_insensitively(self):
        supplied = {**llm.correlation_headers("conv", "worker"), "X-LOOPCUT-Prompt": "worker",
                    "x-LoopCut-future": "metadata", "X-Other": "keep"}
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": ""}):
            for url in (BYOK, LOCAL):
                with self.subTest(url=url):
                    headers = self.request(url, headers=supplied, prompt_id="worker")
                    self.assertFalse([name for name in headers if name.startswith("x-loopcut-")])
                    self.assertEqual(headers["x-other"], "keep")
                    self.assertEqual(headers["authorization"], "Bearer test")
        self.assertIn("X-LOOPCUT-Prompt", supplied, "the caller's dictionary is unchanged")

    def test_service_receives_correlation_and_each_prompt_id(self):
        for prompt in ("single", "orchestrator", "worker", "router"):
            with self.subTest(prompt=prompt):
                headers = self.request(config.SERVICE_BASE_URL, headers=llm.correlation_headers("conv", prompt),
                                       prompt_id=prompt)
                self.assertEqual(headers["x-loopcut-conversation"], "conv")
                self.assertEqual(headers["x-loopcut-role"], prompt)
                self.assertEqual(headers["x-loopcut-prompt"], prompt)

    def test_service_has_no_prompt_header_when_prompt_id_is_absent(self):
        for supplied in (llm.correlation_headers("conv", "single"), {"X-Loopcut-Prompt": "single"}):
            with self.subTest(headers=supplied):
                headers = self.request(config.SERVICE_BASE_URL, headers=supplied)
                self.assertNotIn("x-loopcut-prompt", headers)

    def test_harness_stand_in_receives_gateway_headers(self):
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": "http://127.0.0.1:*/v1"}):
            headers = self.request(LOCAL, headers=llm.correlation_headers("conv", "worker"), prompt_id="worker")
        self.assertEqual(headers["x-loopcut-conversation"], "conv")
        self.assertEqual(headers["x-loopcut-role"], "worker")
        self.assertEqual(headers["x-loopcut-prompt"], "worker")

    def test_unknown_prompt_is_rejected_before_any_request(self):
        for url in (BYOK, config.SERVICE_BASE_URL, config.CHATGPT_PLAN_BASE_URL):
            with self.subTest(url=url), mock.patch.object(llm.urllib.request, "urlopen") as opened:
                with self.assertRaises(ValueError):
                    llm.stream_chat(base_url=url, api_key="test", model="fast", messages=[], tools=[],
                                    reasoning_effort="low", on_text=lambda _: None,
                                    is_cancelled=lambda: False, prompt_id="summary")
                opened.assert_not_called()

    def test_router_real_request_to_byok_has_no_loopcut_headers(self):
        cfg = config.Config("test", BYOK, "fast", "low", True, False, 25, 40_000)
        reply = '{"level": 1, "deliverables": [], "why": "simple"}'
        with mock.patch.dict(os.environ, {"LOOPCUT_SERVICE_URLS": ""}), \
                mock.patch.object(llm.urllib.request, "urlopen", return_value=self.response(reply)) as opened:
            decision, _ = router.ask(cfg, {"role": "user", "content": "hello"},
                                     {"id": "conv", "messages": []}, lambda: False)
        self.assertIsNone(decision)
        opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
