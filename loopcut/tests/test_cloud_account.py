"""Credit consent and account/project policy at the UI and real mock HTTP boundaries.

The contract server runs in memory through MockTransport: no Blender and no network.
"""

import json
import hashlib
import sys
import tempfile
import threading
import time
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import agent, blobs, cloud, cloud_account as account, cloud_http, cloud_jobs, config, skills, state, tools
from loopcut.ui import layout
from mock_service import MockTransport


def descriptor(**changes):
    return {"backend": "managed", "cost": {"credits_estimate": 51, "quote_required": False},
            "approval": {"confirm_over_credits": 50}, "byok": {"provider": "openai", "data_class": "A"}, **changes}


def display(session=None, **ui):
    return layout.build(session or state.new_session(), 420, 600, 1, lambda font, size, text: len(text) * size * .6,
                        "fast", None, ui)


class LogicTest(unittest.TestCase):
    def test_credit_balance_monthly_and_reset_only_when_signed_in(self):
        manifest = {"account": {"credits": {"balance": 1840, "monthly": 2000, "resets_at": "2026-11-01T00:00:00Z"}}}
        self.assertEqual(account.credits_label(manifest, True), "1,840 credits ($18.40) · renews 2026-11-01")
        self.assertEqual(account.credits_label(manifest, False), "")
        self.assertEqual(account.credits_label({"account": {}}, True), "")
        self.assertEqual(account.credits_label({"account": None}, True), "")
        manifest["account"]["credits"]["balance"] = 0
        self.assertTrue(account.credits_label(manifest, True).startswith("0 credits"))

    def test_malformed_optional_credit_metadata_is_not_displayed(self):
        for balance in (None, True, -1, float("nan"), "42"):
            self.assertEqual(account.credits_label({"account": {"credits": {"balance": balance}}}, True), "")
        self.assertEqual(account.credits_label({"account": {"credits": {"balance": 5, "resets_at": "bad"}}}, True), "5 credits ($0.05)")

    def test_panel_data_keeps_the_empty_shape_without_a_known_balance(self):
        empty = {"credits": "", "today": "", "message": ""}
        self.assertEqual(account.panel_data(None), empty)
        for manifest in ({}, {"account": None}, {"account": {}},
                         *({"account": {"credits": {"balance": value}}}
                           for value in (None, True, -1, float("nan"), "42"))):
            for billing in (False, True):
                with self.subTest(manifest=manifest, billing=billing), \
                        patch.object(config, "billing_open", return_value=billing):
                    runtime = types.SimpleNamespace(manifest=manifest, signed_in=True, lock=threading.Lock())
                    self.assertEqual(account.panel_data(runtime), empty)

    def test_panel_data_retains_a_known_zero_balance_and_hides_it_after_sign_out(self):
        runtime = types.SimpleNamespace(manifest={"account": {"credits": {"balance": 0}}},
                                        signed_in=True, lock=threading.Lock())
        for billing in (False, True):
            with self.subTest(billing=billing), patch.object(config, "billing_open", return_value=billing):
                runtime.signed_in = True
                self.assertEqual(account.panel_data(runtime)["credit_balance"], 0)
                runtime.signed_in = False
                self.assertEqual(account.panel_data(runtime),
                                 {"credits": "", "credit_balance": None, "today": "", "message": ""})

    def test_managed_threshold_is_strict_and_card_names_the_estimate(self):
        for credits, required in ((0, False), (49, False), (50, False), (51, True)):
            detail = account.approval_details(descriptor(cost={"credits_estimate": credits}))
            text = f"About {credits} credits (${credits / 100:.2f})" if credits else "Free"
            self.assertEqual(detail, {"required": required, "text": text, "credits": credits, "threshold": 50})

    def test_server_threshold_zero_confirms_every_priced_call(self):
        # What the server sends clients that hide the balance: every charge asks first; free calls never do.
        for credits, required in ((0, False), (1, True), (6, True)):
            tool = descriptor(cost={"credits_estimate": credits}, approval={"confirm_over_credits": 0})
            self.assertEqual(account.approval_details(tool)["required"], required)

    def test_receipt_names_what_a_finished_job_used(self):
        self.assertEqual(account.receipt({"credits": {"reserved": 6, "charged": 6, "refunded": 0}}),
                         "Used 6 credits ($0.06)")
        for job in ({"credits": {"charged": 0}}, {"credits": {"charged": -1}}, {}, None):
            self.assertEqual(account.receipt(job), "")

    def test_quote_card_uses_actual_cost_and_the_same_threshold(self):
        # C47: a quoted tool is decided on its quote like an estimate, instead of always asking.
        tool = descriptor(cost={"credits_estimate": 100, "quote_required": True})
        detail = account.approval_details(tool, {"quote_id": "qt_1", "credits": 3, "expires_at": "later"})
        self.assertEqual(detail, {"required": False, "text": "3 credits ($0.03), quoted", "credits": 3, "threshold": 50})
        self.assertTrue(account.approval_details(tool, {"quote_id": "qt_1", "credits": 80, "expires_at": "later"})["required"])
        self.assertTrue(account.approval_details(tool)["required"], "no quote yet: the card waits for one")
        self.assertNotIn("100", account.approval_details(tool)["text"])
        with self.assertRaises(cloud_http.CloudError):
            account.approval_details(tool, {"quote_id": "qt_1", "credits": -3, "expires_at": "later"})

    def test_byok_names_each_vendor(self):
        for provider, vendor in (("openai", "OpenAI"), ("fal", "fal"), ("gemini", "Google Gemini"), ("elevenlabs", "ElevenLabs")):
            detail = account.approval_details(descriptor(backend="byok", byok={"provider": provider}))
            self.assertEqual(detail["text"], f"Uses your own {vendor} key")
            self.assertTrue(detail["required"])
            self.assertIsNone(detail["credits"])

    def test_account_restriction_wins_over_project_off(self):
        for default in (False, True):
            for override in account.OVERRIDES:
                self.assertEqual(account.effective_no_generation(default, override), default or override == "on")
        with self.assertRaises(ValueError):
            account.effective_no_generation(False, "maybe")

    def test_signed_in_uses_manifest_signed_out_uses_local(self):
        manifest = {"account": {"settings": {"no_generation": False, "private_mode": False}}}
        local = {"no_generation": True, "private_mode": True}
        self.assertEqual(account.policy_for(manifest, signed_in=True, local=local), manifest["account"]["settings"])
        self.assertEqual(account.policy_for(manifest, signed_in=False, local=local), local)
        self.assertTrue(account.policy_for(manifest, signed_in=True, override="on")["no_generation"])
        self.assertFalse(account.policy_for(manifest, signed_in=False, local=local, override="off")["no_generation"])

    def test_scene_property_is_not_a_global_preference(self):
        first, second = {}, {}
        account.set_project_override(first, "on")
        self.assertEqual(account.project_override(first), "on")
        self.assertEqual(account.project_override(second), "inherit")
        self.assertEqual(account.project_override({account.PROJECT_PROPERTY: "bad"}), "on")

    def test_privacy_labels_and_refusals(self):
        for value, label in (("A", "Private (A)"), ("B", "Longer retention (B)"), ("C", "May train (C)"), ("D", "May train (D)")):
            tool = descriptor(byok={"data_class": value})
            self.assertEqual(account.data_label(tool), label)
            self.assertEqual(bool(account.refusal(tool, {"private_mode": True})), value in ("C", "D"))
        self.assertIn("Private mode", account.refusal(descriptor(unavailable_reason="private_mode"), {}))

    def test_the_managed_data_class_comes_first_and_the_own_key_class_is_the_fallback(self):
        # Contract 0.6: a top-level data_class is the class of the route Loopcut runs. Render, lip sync and the
        # asset library have no own-key route (byok null), so they read "Data class unknown" before.
        for managed, own_key, label in (("A", None, "Private (A)"), ("B", None, "Longer retention (B)"),
                                        ("B", {"data_class": "A"}, "Longer retention (B)"),
                                        (None, {"data_class": "C"}, "May train (C)"),  # 0.6 null: unknown, fall back
                                        ("unknown", {"data_class": "B"}, "Longer retention (B)"),
                                        (None, None, "Data class unknown")):
            with self.subTest(managed=managed, own_key=own_key):
                self.assertEqual(account.data_label(descriptor(data_class=managed, byok=own_key)), label)
        older = descriptor(byok={"provider": "openai", "data_class": "D"})
        self.assertNotIn("data_class", older)
        self.assertEqual(account.data_label(older), "May train (D)", "a 0.5 descriptor has no top-level class")

    def test_private_mode_refuses_an_own_key_route_by_its_own_class_only(self):
        private = {"private_mode": True}
        trains_own_key = descriptor(data_class="A", byok={"provider": "elevenlabs", "data_class": "C"})
        self.assertEqual(account.refusal(trains_own_key, private), "Private mode blocks this vendor: May train (C).")
        managed_trains = descriptor(data_class="C", byok={"provider": "openai", "data_class": "A"})
        self.assertEqual(account.refusal(managed_trains, private), "", "the managed class never refuses an own-key route")
        refused = descriptor(data_class="C", byok=None, unavailable_reason="private_mode")
        self.assertEqual(account.refusal(refused, {}), "Private mode blocks this vendor: May train (C).")

    @patch.object(config, "billing_open", lambda: True)
    def test_cost_and_upgrade_are_visible_on_card(self):
        session = state.new_session()
        card = state.item_tool("loopcut_cloud__render", "Render", "")
        card.update(status="awaiting", cost="73 credits (quoted)", paid_approval=True)
        session["items"].append(card)
        drawn = display(session)
        self.assertTrue(any(p.get("text") == "73 credits (quoted)" for p in drawn["prims"]))
        self.assertFalse(any(h["action"][0] == "approve_always" for h in drawn["hits"]))
        card.update(status="failed", expanded=True, cloud_error={"upgrade": "https://loopcut.org/pricing"})
        self.assertTrue(any(h["action"] == ("cloud_upgrade", 0) for h in display(session)["hits"]))

    def test_credits_account_link_and_panel_settings(self):
        drawn = display(credits="1,840 credits · 2,000/month · resets 2026-11-01", service_settings=True,
                        project_override="on", service_policy={"no_generation": False, "private_mode": True})
        hit = next(h for h in drawn["hits"] if h["id"] == "input.credits")
        self.assertEqual(hit["action"], ("open_account", None))
        self.assertEqual(hit["tip"], "", "account details must not float over the composer")
        self.assertEqual({h["action"][1] for h in drawn["hits"] if h["action"][0] == "project_generation"}, set(account.OVERRIDES))
        self.assertFalse(any(p["id"] == "input.credits" for p in display()["prims"]))

    def test_missing_consent_cannot_run_a_paid_or_byok_call(self):
        with self.assertRaises(cloud_http.CloudError):
            account.confirm(account.approval_details(descriptor()))
        with self.assertRaises(cloud_http.CloudError):
            account.confirm(account.approval_details(descriptor(backend="byok")))
        account.confirm(account.approval_details(descriptor(cost={"credits_estimate": 1})))

    @patch.object(config, "billing_open", lambda: True)
    def test_model_error_is_short_and_link_is_safe(self):
        error = cloud_http.CloudError("Refused", "insufficient_credits", 402,
                                      {"balance": 1, "needed": 20, "upgrade": "https://loopcut.org/pricing"})
        detail = account.error_details(error)
        self.assertIn("balance 1, needed 20", detail["text"])
        self.assertNotIn("https:", detail["text"])
        self.assertEqual(detail["upgrade"], "https://loopcut.org/pricing")
        error.extra["upgrade"] = "javascript:alert(1)"
        self.assertEqual(account.error_details(error)["upgrade"], "")

    def test_consent_is_per_call_and_workers_cannot_approve(self):
        turn = agent.Turn()
        card = state.item_tool("loopcut_cloud__image_generate", "Generate", "")
        detail = account.approval_details(descriptor())
        with patch.object(agent, "_redraw"), patch.object(agent, "_wait_for_approval", return_value=True) as wait:
            self.assertFalse(agent._cloud_consent(turn, card, False, detail))
            wait.assert_not_called()
            self.assertTrue(agent._cloud_consent(turn, card, True, detail))
            self.assertTrue(card["paid_approval"])
            self.assertEqual(card["cost"], "About 51 credits ($0.51)")
            wait.assert_called_once_with(turn)


class MockAccountTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mock = MockTransport(live=True)
        self.client = cloud_http.Client(base_url="http://127.0.0.1:3000", api_key="lc_demo", transport=self.mock,
            client_headers={"x-loopcut-contract": "0.2", "x-loopcut-client": "loopcut-addon/test blender/test"})
        self.runtime = cloud.Runtime(self.client, self.directory.name, signed_in=True)
        self.assertTrue(self.runtime.refresh())
        self.scene = {}
        fake = types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={}), context=types.SimpleNamespace(scene=self.scene))
        for context in (patch.dict(sys.modules, {"bpy": fake}), patch.object(cloud, "_runtime", self.runtime),
                        patch.object(cloud_jobs, "_client", side_effect=AssertionError("use the captured client")),
                        patch.object(cloud_jobs, "_registry", {}), patch.object(cloud, "_jobs", {}),
                        patch.object(cloud_jobs, "_download", side_effect=lambda client, job, cancel: job)):
            context.start()
            self.addCleanup(context.stop)
        self.cancel = threading.Event()

    def tool(self, name="image_generate"):
        tool = deepcopy(next(d for d in self.runtime.manifest["tools"] if d["name"] == name))
        tool.update(backend="managed", latency="seconds")
        return tool

    def render_arguments(self):
        path = Path(self.directory.name) / "bundle.json"
        path.write_text('{"schema":"loopcut.bundle/0.1"}')
        blobs.upload_missing([{"path": path}], client=self.client)
        return {"bundle": blobs.sha256(path), "frame_start": 1, "frame_end": 2}

    def run_approved(self, tool, arguments, consent=lambda details: True, project=None):
        project = project or {"id": "test", "no_generation": False}
        with account.approval_context(consent):
            args = account.authorize(cloud_jobs, tool, arguments, {}, project=project, cancel=self.cancel,
                                     runtime=self.runtime, client=self.client)
            return account.submit(cloud_jobs, tool, args, {}, project=project, on_progress=None, cancel=self.cancel,
                                  runtime=self.runtime, client=self.client)

    def posts(self, endpoint):
        return [c["body"] for c in self.mock.calls if c["path"] == endpoint and c["method"] == "POST"]

    @patch.object(config, "billing_open", lambda: True)
    def test_manifest_credits_refresh_and_signed_out_hide(self):
        self.assertIn("1,840 credits", account.panel_data(self.runtime)["credits"])
        self.assertEqual(account.panel_data(self.runtime)["credit_balance"], 1840)
        self.client.request("POST", "/mock/control", {"balance": 1200})
        self.assertTrue(self.runtime.refresh())
        self.assertIn("1,200 credits", account.panel_data(self.runtime)["credits"])
        self.assertEqual(account.panel_data(self.runtime)["credit_balance"], 1200)
        self.runtime.signed_in = False
        self.assertEqual(account.panel_data(self.runtime)["credits"], "")
        self.assertIsNone(account.panel_data(self.runtime)["credit_balance"])

    def test_patch_both_settings_round_trip_and_updates_availability(self):
        settings = account.patch_settings(self.runtime, {"no_generation": True, "private_mode": False})
        self.assertEqual(settings, {"no_generation": True, "private_mode": False})
        self.assertEqual(self.client.request("GET", "/v1/manifest")["account"]["settings"], settings)
        self.assertTrue(account.current_policy({})["no_generation"])
        self.assertFalse(any(d["kind"] == "gen" for d in self.runtime.descriptors()))
        self.assertTrue(cloud.skill_options()["no_generation"])
        self.assertFalse(account.setting_value(self.runtime, "private_mode", True))
        self.assertTrue(account.patch_settings(self.runtime, {"no_generation": False})["private_mode"] is False)
        self.assertTrue(any(d["kind"] == "gen" for d in self.runtime.descriptors()))

    def test_signed_out_patch_is_never_sent(self):
        self.runtime.signed_in = False
        before = len(self.mock.calls)
        with self.assertRaises(cloud_http.CloudError):
            account.patch_settings(self.runtime, {"no_generation": True})
        self.assertEqual(len(self.mock.calls), before)
        self.assertTrue(account.setting_value(self.runtime, "no_generation", True))

    def wait_for_settings(self):
        deadline = time.monotonic() + 3
        while getattr(self.runtime, "account_saving", False) and time.monotonic() < deadline:
            threading.Event().wait(.005)
        self.assertFalse(self.runtime.account_saving, "settings worker did not finish")

    def test_rapid_preference_changes_are_serialized_and_round_trip(self):
        entered, release = threading.Event(), threading.Event()
        def pause_first(call):
            if call["method"] == "PATCH" and not entered.is_set():
                entered.set()
                release.wait(3)
        self.mock.before_request = pause_first
        account.change_setting(self.runtime, "no_generation", True)
        self.assertTrue(entered.wait(2))
        try:
            self.assertTrue(account.setting_value(self.runtime, "no_generation", False))
            self.assertFalse(self.runtime.manifest["account"]["settings"]["no_generation"])
            account.change_setting(self.runtime, "no_generation", False)
            self.assertFalse(account.setting_value(self.runtime, "no_generation", True))
        finally:
            release.set()
        self.wait_for_settings()
        self.assertEqual([c["body"] for c in self.mock.calls if c["method"] == "PATCH"],
                         [{"no_generation": True}, {"no_generation": False}])
        self.assertFalse(self.mock.state.settings["no_generation"])
        self.assertEqual(account.panel_data(self.runtime)["message"], "")

    def test_failed_patch_restores_manifest_value_and_explains_failure(self):
        def offline(call):
            if call["method"] == "PATCH":
                raise cloud_http.CloudError("Could not reach the Loopcut service", "connection_failed")
        self.mock.before_request = offline
        account.change_setting(self.runtime, "no_generation", True)
        self.wait_for_settings()
        self.assertFalse(account.setting_value(self.runtime, "no_generation", True))
        self.assertIn("Settings not saved", account.panel_data(self.runtime)["message"])
        self.assertFalse(self.mock.state.settings["no_generation"])

    def test_invalid_settings_are_rejected_before_http(self):
        for changes in ({}, {"private_mode": 1}, {"other": True}):
            with self.assertRaises(ValueError):
                account.patch_settings(self.runtime, changes)

    def test_project_on_reaches_request_and_filters_gen_skills_and_tools(self):
        account.set_project_override(self.scene, "on")
        self.runtime.policy = account.current_policy({})
        self.assertFalse(any(d["kind"] == "gen" for d in self.runtime.descriptors()))
        options = cloud.skill_options()
        generated = {"front_matter": {"id": "test/gen", "version": 1, "title": "Gen", "when": "anything",
                     "roles": ["single"], "requires_tools": [], "tier": "paid", "kind": "gen", "words": "anything"},
                     "body": "GENERATION TEST MARKER"}
        generated["sha256"] = hashlib.sha256(cloud._skill_bytes(generated)).hexdigest()
        self.assertIn("GENERATION TEST MARKER", skills.single_notes("anything", server_skills=[generated]))
        self.assertNotIn("GENERATION TEST MARKER", skills.single_notes("anything", **{**options, "server_skills": [generated]}))
        project = {"id": "scene", "no_generation": self.runtime.policy["no_generation"]}
        self.run_approved(self.tool("asset_search"), {"query": "chair"}, project=project)
        self.assertTrue(self.posts("/v1/jobs")[-1]["project"]["no_generation"])

    def test_generation_disabled_error_identifies_both_scopes(self):
        for scope in ("account", "project"):
            self.client.request("POST", "/mock/control", {"settings": {"no_generation": scope == "account"}})
            with self.assertRaises(cloud_http.CloudError) as caught:
                self.run_approved(self.tool(), {"prompt": "prop"}, project={"id": "scene", "no_generation": scope == "project"})
            self.assertEqual((caught.exception.status, caught.exception.code), (403, "generation_disabled"))
            self.assertIn("this " + scope, account.error_details(caught.exception)["text"])

    def test_private_mode_filters_tools_and_refuses_jobs_and_quotes(self):
        tool = self.tool("sfx")
        self.assertFalse(any(d["name"] == "sfx" for d in self.runtime.descriptors()))
        self.assertIn("Private mode", next(r["reason"] for r in account.tool_rows(self.runtime) if r["title"] == tool["title"]))
        for endpoint in ("/v1/jobs", "/v1/quotes"):
            payload = {"tool": "sfx", "input": {"prompt": "a bell"}, "files": {}}
            if endpoint.endswith("jobs"):
                payload.update(tool_version=1, project={"id": "scene", "no_generation": False}, idempotency_key="private-test")
            with self.assertRaises(cloud_http.CloudError) as caught:
                self.client.request("POST", endpoint, payload)
            self.assertEqual((caught.exception.status, caught.exception.code), (403, "private_mode"))
            self.assertIn("data class C", account.error_details(caught.exception)["text"])

    @patch.object(config, "billing_open", lambda: True)
    def test_402_gives_user_link_and_short_model_result(self):
        self.client.request("POST", "/mock/control", {"balance": 0})
        notices = []
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_approved(self.tool(), {"prompt": "prop"})
        with account.approval_context(lambda d: notices.append(d)):
            text = cloud.error_text(caught.exception)
        self.assertEqual(caught.exception.status, 402)
        self.assertIn("balance 0, needed", text)
        self.assertEqual(notices[0]["error"]["upgrade"], "https://loopcut.org/pricing")
        self.assertEqual(len(self.mock.state.jobs), 0)

    def test_quote_then_card_then_job_contains_only_approved_quote_id(self):
        seen = []
        def consent(detail):
            self.assertEqual(len(self.posts("/v1/quotes")), 1)
            self.assertEqual(self.posts("/v1/jobs"), [])
            seen.append(detail)
            return True
        result = self.run_approved(self.tool("render"), self.render_arguments(), consent)
        self.assertEqual(result["status"], "succeeded")
        quoted = next(iter(self.mock.state.quotes.values()))["quote"]
        self.assertEqual(seen[0]["text"], f"{quoted['credits']} credits (${quoted['credits'] / 100:.2f}), quoted")
        submitted = self.posts("/v1/jobs")[0]
        self.assertEqual(submitted["quote_id"], quoted["quote_id"])
        self.assertNotIn("quote_id", submitted["input"])
        self.assertEqual(self.runtime.quotes[quoted["quote_id"]], quoted)

    def test_unknown_model_quote_is_replaced_and_still_requires_consent(self):
        arguments = {**self.render_arguments(), "quote_id": "qt_model_supplied"}
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_approved(self.tool("render"), arguments, lambda details: False)
        self.assertEqual(caught.exception.code, "approval_required")
        self.assertEqual(len(self.runtime.quotes), 1)
        self.assertNotIn("qt_model_supplied", self.runtime.quotes)
        self.assertEqual(self.posts("/v1/jobs"), [])

    def test_reject_quote_submits_no_job(self):
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_approved(self.tool("render"), self.render_arguments(), lambda d: False)
        self.assertEqual(caught.exception.code, "approval_required")
        self.assertEqual(len(self.posts("/v1/quotes")), 1)
        self.assertEqual(self.posts("/v1/jobs"), [])

    def test_quote_expired_requotes_once_and_uses_replacement(self):
        def expire_first(call):
            if call["path"] == "/v1/jobs" and len(self.posts("/v1/jobs")) == 1:
                self.mock.state.quotes[call["body"]["quote_id"]]["expiry"] = 0
        self.mock.before_request = expire_first
        approvals = []
        result = self.run_approved(self.tool("render"), self.render_arguments(), lambda d: approvals.append(d) or True)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(len(self.posts("/v1/quotes")), 2)
        first, retry = self.posts("/v1/jobs")
        self.assertNotEqual(first["quote_id"], retry["quote_id"])
        self.assertEqual(len(self.mock.state.jobs), 1)
        self.assertEqual(set(self.runtime.quotes), {first["quote_id"], retry["quote_id"]})
        self.assertNotEqual(first["idempotency_key"], retry["idempotency_key"])

    def test_second_expiry_does_not_loop(self):
        def expire(call):
            if call["path"] == "/v1/jobs":
                self.mock.state.quotes[call["body"]["quote_id"]]["expiry"] = 0
        self.mock.before_request = expire
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_approved(self.tool("render"), self.render_arguments())
        self.assertEqual(caught.exception.code, "quote_expired")
        self.assertEqual(len(self.posts("/v1/quotes")), 2)
        self.assertEqual(len(self.posts("/v1/jobs")), 2)

    def test_higher_replacement_quote_cannot_spend_more_than_approved(self):
        def increase(call):
            if call["path"] == "/v1/jobs":
                self.mock.state.quotes[call["body"]["quote_id"]]["expiry"] = 0
                self.mock.state.catalog["render"]["cost"]["credits_estimate"] += 10
        self.mock.before_request = increase
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_approved(self.tool("render"), self.render_arguments())
        self.assertEqual(caught.exception.code, "quote_changed")
        replacement = caught.exception.extra
        self.assertEqual(self.runtime.quotes[replacement["quote_id"]], replacement)
        self.assertEqual(len(self.posts("/v1/jobs")), 1)
        self.assertEqual(len(self.mock.state.jobs), 0)

    @patch.object(config, "billing_open", lambda: True)
    def test_cloud_dispatch_uses_consent_before_running_and_surfaces_402(self):
        notices = []
        def consent(detail):
            notices.append(detail)
            if "error" in detail:
                return True
            self.assertEqual(self.posts("/v1/jobs"), [])
            self.client.request("POST", "/mock/control", {"balance": 0})
            return True
        with patch.object(cloud_jobs, "resolve_files", return_value={}), account.approval_context(consent):
            pending = cloud.execute("loopcut_cloud__image_generate", {"prompt": "prop"})
            result = pending.then(lambda: False, lambda fn: fn())
        self.assertFalse(result.ok)
        self.assertIn("balance 0, needed", result.text)
        self.assertEqual(notices[-1]["error"]["upgrade"], "https://loopcut.org/pricing")

    def test_project_policy_changed_during_consent_refuses_before_submission(self):
        def consent(details):
            if "error" not in details:
                self.runtime.policy = {"no_generation": True, "private_mode": True}
            return True
        with patch.object(cloud_jobs, "resolve_files", return_value={}), account.approval_context(consent):
            pending = cloud.execute("loopcut_cloud__image_generate", {"prompt": "prop"})
            result = pending.then(lambda: False, lambda fn: fn())
        self.assertFalse(result.ok)
        self.assertIn("this project", result.text)
        self.assertEqual(self.posts("/v1/jobs"), [])

    def test_cancel_after_quote_consent_does_not_submit(self):
        def consent(details):
            self.cancel.set()
            return True
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_approved(self.tool("render"), self.render_arguments(), consent)
        self.assertEqual(caught.exception.code, "canceled")
        self.assertEqual(self.posts("/v1/jobs"), [])

    def test_cancel_after_expiry_does_not_requote_or_retry(self):
        def expire_and_cancel(call):
            if call["path"] == "/v1/jobs":
                self.mock.state.quotes[call["body"]["quote_id"]]["expiry"] = 0
                self.cancel.set()
        self.mock.before_request = expire_and_cancel
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.run_approved(self.tool("render"), self.render_arguments())
        self.assertEqual(caught.exception.code, "canceled")
        self.assertEqual(len(self.posts("/v1/quotes")), 1)
        self.assertEqual(len(self.posts("/v1/jobs")), 1)
        self.assertEqual(len(self.mock.state.jobs), 0)

    def test_account_and_private_policy_rechecked_after_consent(self):
        for change in ("account", "private_mode"):
            with self.subTest(change=change):
                self.client.request("PATCH", "/v1/settings", {"private_mode": False})
                self.runtime.refresh()
                self.runtime.policy["private_mode"] = False
                def consent(details):
                    if "error" not in details:
                        if change == "account":
                            cloud._runtime = None
                        else:
                            self.runtime.manifest["account"]["settings"]["private_mode"] = True
                    return True
                with patch.object(cloud, "_runtime", self.runtime), account.approval_context(consent):
                    result = cloud.execute(cloud.PREFIX + "sfx", {"prompt": "bell"}).then(lambda: False, lambda fn: fn())
                self.assertFalse(result.ok)
                self.assertIn("account_changed" if change == "account" else "private_mode", result.text)
                self.assertEqual(self.posts("/v1/jobs"), [])


if __name__ == "__main__":
    unittest.main()
