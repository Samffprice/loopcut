"""Conversion cards (nudges.py) and the server's billing switch, without Blender.

The spec is loopcut-web docs/platform/onboarding-value-and-conversion.md §4: cards at the moment the account
matters, frequency caps, nothing pitched that No generation or Private mode blocks, purchases only while the
manifest's flags.billing is on, and the model-source header only for signed-in installs.
"""

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import cloud, cloud_account, config, conversations, nudges, state, tools
from loopcut.ui import layout
from test_cloud import Reply, RuntimeFixture, manifest, tool


def measure(font, size, text):
    return len(text) * size * .6


def display(session, width=420, **ui):
    return layout.build(session, width, 900, 1, measure, "fast", None, ui)


class DataDir(unittest.TestCase):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        context = patch.dict(os.environ, {"LOOPCUT_DATA_DIR": folder.name})
        context.start()
        self.addCleanup(context.stop)
        with nudges._lock:
            nudges._pending.clear()


class CapsTest(DataDir):
    def test_soft_cards_once_per_key_one_a_day_and_snooze(self):
        now = time.time()
        self.assertTrue(nudges.may_show("trial", "a", now))
        nudges.mark_shown("trial", "a", now)
        self.assertFalse(nudges.may_show("trial", "a", now + 3 * nudges.DAY), "a seen trial card never returns")
        self.assertFalse(nudges.may_show("low_credits", "b", now + 60), "one soft card a day")
        self.assertTrue(nudges.may_show("low_credits", "b", now + nudges.DAY + 1))
        nudges.snooze("low_credits", now)
        self.assertFalse(nudges.may_show("low_credits", "b", now + nudges.DAY + 1))
        self.assertTrue(nudges.may_show("low_credits", "b", now + nudges.WEEK + 1))

    def test_tips_switch_silences_soft_and_offer_cards_but_not_blocking_ones(self):
        nudges.set_tips_off(True)
        self.assertTrue(nudges.tips_off())
        for kind in ("trial", "low_credits", "locked_tool"):
            self.assertFalse(nudges.may_show(kind, "k"))
        self.assertTrue(nudges.may_show("chatgpt"))
        nudges.set_tips_off(False)
        self.assertTrue(nudges.may_show("trial", "k"))

    def test_locked_tool_card_once_per_conversation_and_three_a_week(self):
        row = {"name": "sfx", "title": "Sound effects", "why": "sign_in", "credits": 4, "own_key": True}
        session = state.new_session()
        first = nudges.offer_locked(session, row)
        self.assertIn("card", first)
        again = nudges.offer_locked(session, row)
        self.assertTrue(again.startswith("Not shown again."))
        self.assertEqual([i["kind"] for i in session["items"]], ["offer"])
        for n in range(2):
            nudges.offer_locked(state.new_session(), row)
        late = state.new_session()
        self.assertTrue(nudges.offer_locked(late, row).startswith("Not shown again."), "weekly cap")
        self.assertEqual(late["items"], [])

    def test_unreadable_store_starts_fresh(self):
        Path(os.environ["LOOPCUT_DATA_DIR"], "nudges.json").write_text("{not json")
        self.assertTrue(nudges.may_show("trial", "x"))
        self.assertFalse(nudges.tips_off())


class CardsTest(DataDir):
    def test_locked_tool_cards_by_reason_never_sell_in_blender(self):
        row = {"name": "image_to_3d", "title": "Image to 3D", "credits": 80, "own_key": False}
        signed_out = nudges.locked_tool_card({**row, "why": "sign_in"})
        self.assertEqual(signed_out["title"], "Image to 3D needs a free Loopcut account")
        self.assertIn("about 80 credits ($0.80)", signed_out["text"])
        self.assertEqual([a["do"] for a in signed_out["actions"]], ["sign_in_loopcut", "dismiss"])
        broke = nudges.locked_tool_card({**row, "why": "credits", "own_key": True}, balance=12)
        self.assertIn("You have 12.", broke["text"])
        self.assertEqual([a["do"] for a in broke["actions"]], ["web", "web", "vendor_keys", "dismiss"])
        self.assertTrue(broke["actions"][0]["arg"].endswith("/account?from=app&reason=locked_tool"))
        from loopcut import cloud_http
        for action in broke["actions"]:
            if action["do"] == "web":
                cloud_http.validate_url(action["arg"])  # The panel refuses anything else.
        self.assertIn("reason=locked_tool&plan=pro", broke["actions"][1]["arg"])
        plan = nudges.locked_tool_card({**row, "why": "plan"})
        self.assertEqual(plan["title"], "Image to 3D needs a paid plan")

    def test_chatgpt_stops_offer_loopcut_models_by_sign_in_state(self):
        card = nudges.chatgpt_card("subscription_sharing_usage_limit_exceeded")
        self.assertEqual(card["nudge"], "chatgpt")
        self.assertIn("goes through Loopcut's server", card["text"])
        whens = {a["do"]: a["when"] for a in card["actions"]}
        self.assertEqual(whens["continue_loopcut"], "loopcut_in")
        self.assertEqual(whens["sign_in_loopcut"], "loopcut_out")
        self.assertIn("manage_chatgpt", whens)
        self.assertIn("own_key", [a["do"] for a in nudges.chatgpt_card("subscription_sharing_user_not_eligible")["actions"]])
        self.assertIsNone(nudges.chatgpt_card("rate_limited"))

    def test_trial_chips_only_for_live_tools_and_low_credit_copy(self):
        card = nudges.trial_card(100, ["sfx", "asset_search", "image_to_3d"])
        self.assertEqual(card["title"], "100 trial credits added ($1.00)")
        chips = [a for a in card["actions"] if a["do"] == "prompt"]
        self.assertEqual([c["label"] for c in chips], ["Sound effect", "Picture to 3D"])
        self.assertEqual(nudges.trial_card(100, [])["text"], "They pay for Loopcut's cloud tools as they open.")
        monthly = nudges.low_credits_card(440, 2200, "2026-11-01")
        self.assertEqual(monthly["title"], "440 credits left ($4.40)")
        self.assertIn("renew 2026-11-01", monthly["text"])
        self.assertIn("2,200 credits every month", nudges.low_credits_card(20, 0, "")["text"])

    def test_observe_queues_only_while_billing_is_open_and_flush_waits_for_a_reply(self):
        credits = {"balance": 20, "trial": {"amount": 100, "granted_at": "2026-10-16T10:00:00Z"}}
        data = {"account": {"credits": credits}}
        nudges.observe(data, True, ["sfx"])
        self.assertEqual(nudges._pending, [], "billing closed: nothing to sell")
        with patch.object(config, "billing_open", lambda: True):
            nudges.observe(data, False, ["sfx"])
            self.assertEqual(nudges._pending, [], "signed out")
            nudges.observe(data, True, ["sfx"])
            nudges.observe(data, True, ["sfx"])
        self.assertEqual([k for _, k, _ in nudges._pending], ["trial", "low_credits"], "deduplicated")
        session = state.new_session()
        session["items"].append(state.item_user("hi"))
        nudges.flush(session)
        self.assertEqual(len(session["items"]), 1, "never before the first reply")
        session["items"].append({**state.item_assistant(), "streaming": False})
        with patch.object(config, "billing_open", lambda: True):
            nudges.observe(data, True, ["sfx"])
        nudges.flush(session)
        offers = [i for i in session["items"] if i["kind"] == "offer"]
        self.assertEqual([o["nudge"] for o in offers], ["trial"], "one soft card per turn end")
        with patch.object(config, "billing_open", lambda: True):
            nudges.observe(data, True, ["sfx"])
        nudges.flush(session)
        self.assertEqual(len([i for i in session["items"] if i["kind"] == "offer"]), 1, "one a day")

    def test_low_credit_thresholds(self):
        with patch.object(config, "billing_open", lambda: True):
            for credits, low in (({"balance": 441, "monthly": 2200}, False), ({"balance": 439, "monthly": 2200}, True),
                                 ({"balance": 25}, False), ({"balance": 24}, True), ({"balance": 0}, False)):
                with nudges._lock:
                    nudges._pending.clear()
                nudges.observe({"account": {"credits": credits}}, True, [])
                self.assertEqual(bool(nudges._pending), low, credits)

    def test_offer_cards_are_never_saved_with_the_conversation(self):
        session = state.new_session()
        session["items"] = [state.item_user("hi"), nudges.chatgpt_card("subscription_sharing_usage_limit_exceeded")]
        conversations.save(session)
        saved = json.loads((conversations.root() / session["id"] / "conversation.json").read_text())
        self.assertEqual([i["kind"] for i in saved["items"]], ["user"])
        lonely = state.new_session()
        lonely["items"] = [nudges.trial_card(100, [])]
        conversations.save(lonely)
        self.assertFalse((conversations.root() / lonely["id"]).exists())


class OfferCardDrawTest(DataDir):
    def card(self):
        return nudges.chatgpt_card("subscription_sharing_usage_limit_exceeded")

    def actions(self, drawn):
        return [h["action"] for h in drawn["hits"] if h["action"][0] == "offer"]

    def test_buttons_follow_the_sign_in_and_wait_for_a_running_turn(self):
        session = state.new_session()
        session["items"] = [self.card()]
        labels = lambda drawn: [p["text"] for p in drawn["prims"] if p["id"].endswith(".label") and ".offer." in p["id"]]
        self.assertIn("Sign in to Loopcut (free)", labels(display(session)))
        self.assertNotIn("Continue on Loopcut Fast", labels(display(session)))
        signed = display(session, loopcut_signed_in=True)
        self.assertEqual(labels(signed)[0], "Continue on Loopcut Fast")
        session["busy"] = True
        self.assertNotIn("Continue on Loopcut Fast", labels(display(session, loopcut_signed_in=True)))
        session["busy"] = False
        session["items"][0]["resolved"] = "Carrying on with Loopcut Fast."
        drawn = display(session, loopcut_signed_in=True)
        self.assertEqual(self.actions(drawn), [])
        self.assertIn("Carrying on with Loopcut Fast.", json.dumps(drawn["prims"]))

    def test_buttons_wrap_inside_a_narrow_panel(self):
        session = state.new_session()
        session["items"] = [nudges.locked_tool_card({"name": "sfx", "title": "Sound effects", "why": "credits",
                                                     "credits": 4, "own_key": True}, balance=0)]
        for width in (260, 420):
            drawn = display(session, width=width)
            buttons = [h for h in drawn["hits"] if h["action"][0] == "offer"]
            self.assertEqual(len(buttons), 4)
            for hit in buttons:
                self.assertLessEqual(hit["x"] + hit["w"], width, (width, hit))
            if width == 260:
                self.assertGreater(len({h["y"] for h in buttons}), 1, "wrapped onto more rows")

    def test_purchase_buttons_on_tool_and_limit_cards_follow_billing(self):
        session = state.new_session()
        card = state.item_tool("loopcut_cloud__sfx", "Sound effects", "")
        card.update(status="failed", expanded=True, cloud_error={"message": "Not enough credits",
                                                                   "upgrade": "https://loopcut.org/account"})
        session["items"] = [card]
        self.assertFalse([h for h in display(session)["hits"] if h["action"][0] in ("cloud_upgrade", "open_web")])
        with patch.object(config, "billing_open", lambda: True):
            kinds = [h["action"][0] for h in display(session)["hits"] if h["action"][0] in ("cloud_upgrade", "open_web")]
            self.assertEqual(kinds, ["cloud_upgrade", "open_web"])

    def test_free_daily_limit_card_points_at_the_chatgpt_plan(self):
        session = state.new_session()
        session["items"] = [state.item_limit("Free Fast is shared and used up.", "free_daily_limit", "free", "", None)]
        drawn = display(session)
        self.assertIn("Free Fast is used up for today", json.dumps(drawn["prims"]))
        self.assertIn(("sign_in", "chatgpt"), [h["action"] for h in drawn["hits"]])
        self.assertNotIn("It can run Loopcut now.", json.dumps(drawn["prims"]), "the server's text already says it")
        weekly = state.new_session()
        weekly["items"] = [state.item_limit("This week's allowance is used.", "week_limit", "free", "", None)]
        self.assertIn("Have ChatGPT Plus or Pro? It can run Loopcut now.", json.dumps(display(weekly)["prims"]))
        switched = display(session, chatgpt_plan=True)
        self.assertIn("limit_chatgpt", [h["action"][0] for h in switched["hits"]])
        self.assertIn("Your ChatGPT plan can carry on now.", json.dumps(switched["prims"]))

    def test_tips_row_shows_only_while_billing_is_open(self):
        session = state.new_session()
        self.assertNotIn("tips_toggle", [h["action"][0] for h in display(session, service_settings=True)["hits"]])
        with patch.object(config, "billing_open", lambda: True):
            hits = display(session, service_settings=True, tips_off=True)["hits"]
        self.assertIn(("tips_toggle", None), [h["action"] for h in hits])


class CloudOfferTest(RuntimeFixture):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        context = patch.dict(os.environ, {"LOOPCUT_DATA_DIR": folder.name})
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(state, "_session", state.new_session())
        context.start()
        self.addCleanup(context.stop)

    def locked(self, *tools_, signed_in=True, balance=0, **changes):
        data = manifest(*tools_, **changes)
        data["account"]["credits"] = {"balance": balance}
        data["flags"]["billing"] = True
        runtime = self.runtime(Reply(data), signed_in=signed_in, keys_set={})
        runtime.refresh()
        return runtime

    def test_billing_flag_comes_from_the_manifest(self):
        self.assertFalse(config.billing_open(), "no runtime")
        runtime = self.locked(tool("sfx", kind="gen"))
        self.assertTrue(config.billing_open())
        runtime.manifest["flags"]["billing"] = "yes"
        self.assertFalse(config.billing_open(), "only a real true opens sales")
        del runtime.manifest["flags"]["billing"]
        self.assertFalse(config.billing_open(), "servers without the flag keep sales hidden")

    def test_locked_tools_by_reason_and_never_what_policy_blocks(self):
        runtime = self.locked(tool("sfx", kind="gen"), tool("retopology", kind="assist"),
                              tool("voice_line", kind="gen", unavailable_reason="plan"),
                              tool("render", kind="assist", unavailable_reason="region"),
                              tool("image_edit", kind="gen", status="coming_soon"))
        rows = {r["name"]: r["why"] for r in cloud.locked_tools()}
        self.assertEqual(rows, {"sfx": "credits", "retopology": "credits", "voice_line": "plan"})
        runtime.policy = {"no_generation": True}
        self.assertEqual([r["name"] for r in cloud.locked_tools()], ["retopology"], "No generation hides gen tools")
        runtime.policy = {}
        runtime.manifest["tools"][0]["data_class"] = "C"
        self.assertNotIn("sfx", [r["name"] for r in cloud.locked_tools()], "Private mode hides class C routes")
        runtime.manifest["flags"]["billing"] = False
        self.assertEqual(cloud.locked_tools(), [])

    def test_signed_out_tools_are_offered_with_an_account(self):
        self.locked(tool("sfx", kind="gen"), signed_in=False)
        self.assertEqual([(r["name"], r["why"]) for r in cloud.locked_tools()], [("sfx", "sign_in")])

    def test_offer_tool_schema_execution_and_bookkeeping(self):
        self.locked(tool("sfx", kind="gen", title="Sound effects"), balance=0)
        schemas = {s["function"]["name"]: s for s in cloud.tool_schemas()}
        offer = schemas[cloud.PREFIX + cloud.OFFER]["function"]
        self.assertEqual(offer["parameters"]["properties"]["tool"]["enum"], ["sfx"])
        self.assertIn("Never call it unprompted", offer["description"])
        self.assertNotIn(cloud.NAMESPACE + "." + cloud.OFFER, cloud.skill_options()["available_tools"])
        self.assertFalse(cloud.changes_scene(cloud.PREFIX + cloud.OFFER), "no checkpoint for a card")
        self.assertFalse(cloud.approval(cloud.PREFIX + cloud.OFFER, {"tool": "sfx"}))
        result = cloud.execute(cloud.PREFIX + cloud.OFFER, {"tool": "sfx"})
        self.assertTrue(result.ok)
        self.assertIn("needs Loopcut credits", result.text)
        card = state.session()["items"][-1]
        self.assertEqual((card["kind"], card["nudge"]), ("offer", "locked_tool"))
        self.assertIn("You have 0.", card["text"])
        self.assertFalse(cloud.execute(cloud.PREFIX + cloud.OFFER, {"tool": "image_generate"}).ok)
        self.assertEqual(cloud.describe(cloud.PREFIX + cloud.OFFER, {"tool": "sfx"})[0], "How to unlock Sound effects")
        self.assertEqual(self.transport.replies, [], "the offer itself calls no service")

    def test_model_source_header_only_when_signed_in(self):
        for signed_in in (True, False):
            runtime = self.runtime(Reply(manifest()), signed_in=signed_in)
            runtime.model_source = "chatgpt"
            runtime.refresh()
            headers = self.transport.calls[-1]["headers"]
            self.assertEqual(headers.get(cloud.MODEL_SOURCE_HEADER), "chatgpt" if signed_in else None)
        runtime = self.runtime(Reply(manifest()))
        runtime.model_source = "someone@example.test"
        runtime.refresh()
        self.assertNotIn(cloud.MODEL_SOURCE_HEADER, self.transport.calls[-1]["headers"], "only the four kinds")

    def test_finished_managed_job_carries_its_receipt(self):
        self.locked(tool("sfx", kind="gen"), balance=50)
        module = types.SimpleNamespace(apply_result=lambda descriptor, job: "Added the sound.")
        from loopcut import scene_diff
        job = {"id": "job_1", "status": "succeeded", "credits": {"reserved": 4, "charged": 4, "refunded": 0}}
        with patch.object(scene_diff, "snapshot", return_value={}), patch.object(cloud._runtime, "refresh_async") as again:
            result = cloud._apply(module, {"backend": "managed"}, job)
            self.assertEqual(result.receipt, "Used 4 credits ($0.04)")
            again.assert_called_once_with()
            self.assertEqual(cloud._apply(module, {"backend": "byok"}, job).receipt, "")



class UnavailableLineTest(CloudOfferTest):
    """F0276/F0278: a cloud tool the user can't call was invisible to the model, so "generate an image" ended in
    "this integration has no image tool". The prompt's end now names each one with its reason."""

    def runtime_for(self, data, signed_in=True):
        runtime = self.runtime(Reply(data), signed_in=signed_in, keys_set={})
        runtime.refresh()
        return runtime

    def test_production_beta_says_coming_soon_and_offers_no_card(self):
        import coming_soon_fixture
        self.runtime_for(coming_soon_fixture.manifest(), signed_in=False)
        line = cloud.unavailable_line()
        self.assertTrue(line.startswith(cloud.UNAVAILABLE_NOTE + ": "), line)
        self.assertIn("Image generation*", line)
        self.assertIn(": coming soon", line)
        self.assertIn(cloud.OWN_KEY_NOTE, line)
        self.assertNotIn("Sound effects*", line, "Private mode (on while signed out) blocks its own-key vendor")
        self.assertNotIn(cloud.PREFIX + cloud.OFFER, [s["function"]["name"] for s in cloud.tool_schemas()])
        self.assertLess(len(line), 600, "a short line: it is sent with every request")

    def test_reasons_while_sales_are_closed(self):
        self.runtime_for(manifest(tool("sfx", kind="gen", title="Sound effects", data_class="A"),
                                  tool("render", kind="assist", title="Cloud rendering", unavailable_reason="region")),
                         signed_in=False)
        line = cloud.unavailable_line()
        self.assertIn("Sound effects: needs a free Loopcut sign-in", line, "no *: Private mode blocks ElevenLabs (C)")
        self.assertIn("Cloud rendering: not in the user's region", line)
        data = manifest(tool("image_generate", kind="gen", title="Image generation", data_class="A"))
        data["account"]["credits"] = {"balance": 0}
        self.runtime_for(data)
        self.assertIn("Image generation*: coming soon", cloud.unavailable_line(), "no credits to point at while closed")

    def test_what_the_offer_card_covers_is_left_to_it(self):
        self.locked(tool("sfx", kind="gen", title="Sound effects", data_class="A"),
                    tool("voice_line", kind="gen", title="Voice lines", status="coming_soon", available=False))
        self.assertEqual([r["name"] for r in cloud.locked_tools()], ["sfx"])
        line = cloud.unavailable_line()
        self.assertNotIn("Sound effects", line)
        self.assertIn("Voice lines*: coming soon", line)

    def test_no_generation_and_private_mode_explain_and_offer_nothing(self):
        runtime = self.runtime_for(manifest(tool("sfx", kind="gen", title="Sound effects", data_class="C"),
                                            tool("render", kind="assist", title="Cloud rendering", data_class="C")),
                                   signed_in=False)
        line = cloud.unavailable_line()
        self.assertIn("Sound effects, Cloud rendering: off: Private mode blocks this vendor", line)
        self.assertNotIn("*", line)
        runtime.policy = {"no_generation": True, "private_mode": True}
        line = cloud.unavailable_line()
        self.assertIn("Sound effects: off: No generation is on", line)
        self.assertNotIn(cloud.OWN_KEY_NOTE, line)

    def test_nothing_missing_is_no_line(self):
        self.runtime_for(manifest(tool("sfx", kind="gen", data_class="A")))
        self.assertEqual(cloud.unavailable_line(), "")
        cloud._runtime = None
        self.assertEqual(cloud.unavailable_line(), "")

    def test_the_line_ends_the_system_prompt_before_the_add_ons(self):
        from loopcut import agent
        with patch.object(cloud, "unavailable_line", return_value="Loopcut cloud tools you can't call now: X: coming soon."):
            line = agent._setup_line(None)
        self.assertEqual(line, "Loopcut cloud tools you can't call now: X: coming soon.")
        prompt = agent._system_prompt({"addons_line": line + "\nInstalled add-ons: none"})
        self.assertTrue(prompt.endswith("X: coming soon.\nInstalled add-ons: none"))


class ModelLineTest(unittest.TestCase):
    """F0288: asked which model it was, the assistant sent the user to ChatGPT's own model picker."""

    def line(self, provider, source, model="gpt-6.1-sol"):
        import loopcut
        from loopcut import agent
        # account.py and settings.py need Blender to import: stand-ins with what _model_line reads.
        settings = types.SimpleNamespace(preferences=lambda: types.SimpleNamespace(provider=provider),
                                         provider_label={"OPENAI": "OpenAI", "OLLAMA": "Ollama"}.get)
        account = types.SimpleNamespace(model_label=lambda model_id: {"fast": "Loopcut Fast"}.get(model_id, model_id))
        with patch.dict(sys.modules, {"loopcut.settings": settings, "loopcut.account": account}), \
                patch.object(loopcut, "settings", settings, create=True), \
                patch.object(loopcut, "account", account, create=True), \
                patch.object(cloud, "model_source", return_value=source):
            return agent._model_line(types.SimpleNamespace(model=model))

    def test_each_route_names_the_model_and_where_it_is_picked(self):
        self.assertEqual(self.line("CHATGPT", "chatgpt"),
                         "You are gpt-6.1-sol, reached through the user's ChatGPT plan (Sign in with ChatGPT); "
                         "the user picks the model in Loopcut's model menu, in this panel.")
        self.assertTrue(self.line("LOOPCUT", "loopcut", "fast").startswith(
            "You are Loopcut Fast, reached through the user's Loopcut account;"))
        self.assertTrue(self.line("OPENAI", "own_key", "gpt-6").startswith(
            "You are gpt-6, reached through the user's own OpenAI key;"))
        self.assertTrue(self.line("OLLAMA", "local", "qwen").startswith("You are qwen, reached through a model server"))
        self.assertLess(len(self.line("CHATGPT", "chatgpt")), 200, "one short line: the prompt cache matters")

    def test_it_leads_the_prompt_tail_and_a_failure_costs_only_itself(self):
        from loopcut import agent
        with patch.object(agent, "_model_line", return_value="You are m."), \
                patch.object(cloud, "unavailable_line", return_value="Cloud: X: coming soon."):
            self.assertEqual(agent._setup_line(None), "You are m.\nCloud: X: coming soon.")
        with patch.object(agent, "_model_line", side_effect=RuntimeError("no prefs")), \
                patch.object(cloud, "unavailable_line", return_value="Cloud: X: coming soon."):
            self.assertEqual(agent._setup_line(None), "Cloud: X: coming soon.")

if __name__ == "__main__":
    unittest.main()
