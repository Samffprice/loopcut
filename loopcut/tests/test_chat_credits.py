"""The credits arm (chat_credits.py, contract v1 2026-10-23): what the manifest and the gateway say, what a turn
cost, the panel's out-of-credits pop-up and variant B, and the proof that without account.billing nothing changed
from 0.2.13."""

import json
import sys
import threading
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import flag_off_snapshot  # noqa: E402
from loopcut import chat_credits, llm, state  # noqa: E402
from loopcut.ui import layout  # noqa: E402

NOW = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc).timestamp()
RESET = "2026-11-01T00:00:00.000Z"  # The live server's shape (2026-10-23): the 1st of next month, midnight UTC.
UPGRADE = {"url": "https://loopcut.org/pricing?from=app&reason=insufficient_credits&plan=pro", "plan": "pro",
           "label": "Get Pro", "note": "Pro: 2,200 credits a month, $20."}
ARM = {"billing": "credits", "plan": "free", "monthly": 100, "resets_at": RESET, "step": "≈1 credit / step"}
VENDORS = ("Claude", "Opus", "MiMo", "Xiaomi", "RunPod", "fal", "OpenRouter", "GLM")


def measure(font, size, text):
    return len(text) * size * 0.6


def texts(display, prefix=""):
    return [p["text"] for p in display["prims"] if p.get("text") and p["id"].startswith(prefix)]


def hit(display, id):
    return next(h for h in display["hits"] if h["id"] == id)


class FlagOffTest(unittest.TestCase):
    def test_without_account_billing_the_panel_and_setup_draw_exactly_as_0_2_13(self):
        """fixtures/flag_off_0.2.13.json is the same snapshot taken from the 0.2.13 add-on (git a3c75c53133): with
        no account.billing (older servers, other accounts) every panel scene and setup screen is unchanged."""
        expected = json.loads(flag_off_snapshot.FIXTURE.read_text())
        now = json.loads(json.dumps(flag_off_snapshot.snapshot(), sort_keys=True))
        for part in ("panel", "splash"):
            self.assertEqual(sorted(now[part]), sorted(expected[part]))
            for name in expected[part]:
                with self.subTest(part=part, scene=name):
                    self.assertEqual(now[part][name], expected[part][name])

    def test_header_info_is_unchanged_without_the_credits_headers(self):
        headers = {"x-loopcut-plan": "free", "x-loopcut-session-used": "0.5"}
        self.assertEqual(sorted(llm.account_from_headers(headers)), ["plan", "session", "week"])
        self.assertIsNone(llm.account_from_headers({"x-loopcut-billing": "credits"}))  # Not the gateway.


class ParsingTest(unittest.TestCase):
    def test_billing_is_read_only_when_signed_in_and_only_its_two_values(self):
        manifest = {"account": {"plan": "free", "billing": "credits", "chat_credits": {"per_step": 0.84}}}
        self.assertEqual(chat_credits.billing(manifest, True), "credits")
        self.assertEqual(chat_credits.billing(manifest, False), "")
        self.assertEqual(chat_credits.billing({"account": {"billing": "allowance"}}, True), "allowance")
        for bad in ({}, {"account": None}, {"account": {"billing": "free"}}, {"account": {"billing": 1}}, None,
                    {"account": "credits"}):
            self.assertEqual(chat_credits.billing(bad, True), "")

    def test_step_label_shows_a_tenth_under_one_credit_and_whole_credits_above(self):
        for per_step, label in ((0.84, "≈0.8 credits / step"), (0.2, "≈0.2 credits / step"), (0.04, "≈0.1 credits / step"),
                                (1.2, "≈1 credit / step"), (2.6, "≈3 credits / step"), (None, "≈1 credit / step"),
                                ("1", "≈1 credit / step"), (-1, "≈1 credit / step")):
            with self.subTest(per_step=per_step):
                self.assertEqual(chat_credits.step_label({"chat_credits": {"per_step": per_step}}), label)
        self.assertEqual(chat_credits.step_label({"chat_credits": {"per_step": 0.84}}, short=True), "≈0.8 / step")
        self.assertEqual(chat_credits.step_label({"chat_credits": {"per_step": 0.2}}, short=True), "≈0.2 / step")

    def test_monthly_names_the_plans_grant_even_in_the_sign_up_month(self):
        self.assertEqual(chat_credits.monthly({"plan": "free", "credits": {"balance": 100, "monthly": 0}}), 100)
        self.assertEqual(chat_credits.monthly({"plan": "pro", "credits": {"monthly": 2200}}), 2200)
        self.assertEqual(chat_credits.monthly({"plan": "pro"}), 2200)
        self.assertEqual(chat_credits.monthly({}), 100)
        data = chat_credits.panel_data({"account": {"plan": "free", "billing": "credits",
                                                    "credits": {"balance": 3, "monthly": 0, "resets_at": RESET}}}, True)
        self.assertEqual(data, {"billing": "credits", "plan": "free", "monthly": 100, "resets_at": RESET,
                                "step": "≈1 credit / step"})
        self.assertEqual(chat_credits.panel_data({"account": {"credits": {"balance": 3}}}, True)["billing"], "")
        for bad in (1.5, "x", True, [1]):  # A malformed chat_credits never raises (the panel draws from it).
            self.assertEqual(chat_credits.panel_data({"account": {"billing": "credits", "chat_credits": bad}}, True)
                             ["step"], "≈1 credit / step")
        self.assertEqual(chat_credits.day_label("9999-12-31T23:00:00+05:00", NOW), "Fri 31 Dec")
        self.assertEqual(chat_credits.short_reset("not a date", NOW), "")

    def test_reset_dates_read_as_the_day_credits_come_back(self):
        self.assertEqual(chat_credits.day_label(RESET, NOW), "Sun 1 Nov")  # Midnight UTC keeps its calendar date.
        self.assertEqual(chat_credits.day_label(RESET, datetime(2026, 11, 2, tzinfo=timezone.utc).timestamp()), "")
        self.assertEqual(chat_credits.day_label("soon", NOW), "")
        self.assertEqual(chat_credits.short_reset("2026-10-23T23:00:00Z", NOW), "in about 3 h")
        self.assertEqual(chat_credits.short_reset(RESET, NOW), "Sun 1 Nov")
        self.assertEqual(chat_credits.come_back(ARM, RESET, NOW), "100 free credits come back Sun 1 Nov")
        self.assertEqual(chat_credits.come_back({**ARM, "plan": "pro", "monthly": 2200}, RESET, NOW),
                         "2,200 credits come back Sun 1 Nov")
        self.assertEqual(chat_credits.come_back({}, "", NOW), "100 free credits come back next month")
        self.assertTrue(chat_credits.expired(RESET, datetime(2026, 11, 1, 0, 0, 1, tzinfo=timezone.utc).timestamp()))
        self.assertFalse(chat_credits.expired("", NOW))

    def test_the_weekly_top_up_is_named_when_it_comes_before_the_monthly_grant(self):
        refill = {"credits": 25, "at": "2026-10-30T09:12:00Z"}
        self.assertEqual(chat_credits.refill_of(refill), refill)
        for bad in (None, [], {"credits": 0, "at": refill["at"]}, {"credits": "25", "at": refill["at"]},
                    {"credits": 25}, {"credits": 25, "at": "soon"}, {"credits": float("nan"), "at": refill["at"]}):
            self.assertIsNone(chat_credits.refill_of(bad), bad)
        manifest = {"account": {"plan": "free", "billing": "credits", "chat_credits": {"per_step": 0.84,
                                                                                      "refill": refill}}}
        self.assertEqual(chat_credits.panel_data(manifest, True)["refill"], refill)
        self.assertNotIn("refill", chat_credits.panel_data({"account": {"billing": "credits"}}, True))
        item = {"resets_at": RESET, "refill": refill}
        self.assertEqual(chat_credits.next_refill(item, ARM, NOW), refill)
        self.assertEqual(chat_credits.next_refill({"resets_at": RESET}, {**ARM, "refill": refill}, NOW), refill)
        self.assertIsNone(chat_credits.next_refill({"resets_at": "2026-10-29T00:00:00Z", "refill": refill}, ARM, NOW))
        self.assertIsNone(chat_credits.next_refill(item, ARM, datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()))
        self.assertIsNone(chat_credits.next_refill({"resets_at": RESET}, ARM, NOW))
        self.assertEqual(chat_credits.come_back(ARM, RESET, NOW, refill=refill), "25 free credits come back Fri 30 Oct")
        soon = {"credits": 25, "at": "2026-10-23T23:00:00Z"}
        self.assertEqual(chat_credits.come_back(ARM, RESET, NOW, refill=soon), "25 free credits come back in about 3 h")
        self.assertEqual(chat_credits.come_back(ARM, RESET, NOW, short=True, refill=soon),
                         "Free credits come back in about 3 h")
        self.assertFalse(chat_credits.refused_until_passed(item, NOW))
        self.assertTrue(chat_credits.refused_until_passed(item, datetime(2026, 10, 30, 10, tzinfo=timezone.utc)
                                                          .timestamp()))
        self.assertFalse(chat_credits.refused_until_passed({"resets_at": RESET}, datetime(2026, 10, 30, 10,
                                                           tzinfo=timezone.utc).timestamp()))

    def test_a_402_keeps_its_refill_only_for_insufficient_credits(self):
        from loopcut import agent
        refill = {"credits": 25, "at": "2026-10-30T09:12:00Z"}
        error = llm.LLMError("Out of credits.", 402, "insufficient_credits",
                             {"message": "Out of credits.", "plan": "free", "resets_at": RESET, "upgrade": UPGRADE,
                              "refill": refill})
        self.assertEqual(agent._limit_item(error)["refill"], refill)
        error.details.pop("refill")
        self.assertNotIn("refill", agent._limit_item(error))
        week = llm.LLMError("Used.", 402, "week_limit", {"message": "Used.", "refill": refill})
        self.assertNotIn("refill", agent._limit_item(week))

    def test_the_upgrade_button_and_variant_b_rows_follow_the_plan_and_times(self):
        self.assertEqual(chat_credits.upgrade_button(UPGRADE), "Get Pro · 2,200 credits a month · $20")
        self.assertEqual(chat_credits.upgrade_button({**UPGRADE, "plan": "max"}), "Get Max · 6,600 credits a month · $60")
        self.assertEqual(chat_credits.upgrade_button({"plan": "team", "label": "See Team"}), "See Team")
        item = {"reason": "week_limit", "plan": "free", "upgrade": {**UPGRADE, "times": 17}}
        self.assertEqual(chat_credits.offer_rows(item), [
            ("chat", "17× more chat every month"), ("model", "Loopcut Pro, the stronger model"),
            ("credits", "2,200 cloud credits a month ($22)"), ("render", "Cycles renders and top-ups")])
        self.assertEqual(chat_credits.offer_rows({"upgrade": UPGRADE})[0][1], "Much more chat every month")
        self.assertNotIn("model", [key for key, _ in chat_credits.offer_rows({"upgrade": {**UPGRADE, "plan": "max"}})])
        self.assertEqual(chat_credits.offer_used(item), "You've used this week's free chat.")
        self.assertEqual(chat_credits.offer_plan(item), ("Loopcut Pro", "$20 / month"))
        for text in [line for _, line in chat_credits.offer_rows(item)] + [chat_credits.upgrade_button(UPGRADE)]:
            self.assertFalse(any(vendor in text for vendor in VENDORS), text)

    def test_reply_headers_carry_the_billing_and_the_balance_before_the_charge(self):
        info = llm.account_from_headers({"x-loopcut-plan": "free", "x-loopcut-billing": "credits",
                                         "x-loopcut-credits-balance": "100"})
        self.assertEqual((info["billing"], info["credits_balance"]), ("credits", 100))
        for weird in ("-3", "\xb2", "1.5", ""):
            info = llm.account_from_headers({"x-loopcut-plan": "free", "x-loopcut-billing": "weird",
                                             "x-loopcut-credits-balance": weird})
            self.assertNotIn("billing", info)
            self.assertNotIn("credits_balance", info)


class Runtime:
    def __init__(self, account, signed_in=True):
        self.manifest = {"account": account}
        self.signed_in = signed_in
        self.lock = threading.RLock()
        self.refreshes = []

    def refresh(self):
        self.refreshes.append(True)
        return len(self.refreshes) > 1  # The first try finds another refresh running.


class TurnCreditsTest(unittest.TestCase):
    def setUp(self):
        from loopcut import cloud
        self.account = {"plan": "free", "billing": "credits", "chat_credits": {"per_step": 0.84},
                        "credits": {"balance": 100, "monthly": 0, "resets_at": RESET}}
        self.runtime = Runtime(self.account)
        for target, name, value in ((cloud, "_runtime", self.runtime),):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(state.ui, {"account": None})
        p.start()
        self.addCleanup(p.stop)
        self.session = state.reset()
        self.session["spend"] = {"credits": 2, "auto": 2, "allowed": 0}

    def test_mode_follows_the_manifest_then_the_replies_and_stays_off_otherwise(self):
        self.assertTrue(chat_credits.active())
        self.runtime.manifest["account"] = {"plan": "free"}
        self.assertEqual(chat_credits.mode(), "")
        state.ui["account"] = {"plan": "free", "billing": "credits"}
        self.assertTrue(chat_credits.active())
        self.runtime.signed_in = False
        state.ui["account"] = None
        self.assertFalse(chat_credits.active())

    def test_a_turn_records_the_balance_difference_and_its_chat_part(self):
        turn = types.SimpleNamespace(first_item=0, outcome="completed", returned_submission=False)
        chat_credits.turn_start(self.session, turn)
        self.assertEqual(turn.credits, {"before": 100, "header": None, "tools": 2, "jobs": []})
        self.session["turn"] = turn
        chat_credits.note_balance({"credits_balance": 100})
        chat_credits.note_balance({"credits_balance": 99})  # A later reply in the same turn: not its start.
        self.assertEqual(turn.credits["header"], 100)
        reply = state.item_assistant()
        reply.update(text="Added brass key caps.", streaming=False)
        self.session["items"] = [state.item_user("details"), reply]
        self.session["spend"]["credits"] = 4  # A 2-credit tool ran in this turn.
        with patch("loopcut.conversations.save") as save:
            chat_credits.record(self.session, reply, turn.credits, False, after=94)
        save.assert_called_once_with(self.session)
        self.assertEqual((reply["turn_credits"], reply["chat_credits"]), (6, 4))
        self.assertEqual(chat_credits.turn_line(reply), "Done · 6 credits")
        self.assertEqual(chat_credits.chat_total(self.session), 8)  # Tools 4, AI chat 4.
        reply.update(turn_credits=0.3, turn_stopped=True)
        self.assertEqual(chat_credits.turn_line(reply), "Stopped · <1 credit")
        self.assertEqual(chat_credits.turn_line({"text": "old"}), "")

    def test_a_running_jobs_hold_shows_as_held_until_it_settles_to_its_charge(self):
        """rel019-cloud2 (2026-10-26): "Done · 29 credits" under the reply while the render's card said 5 charged;
        the balance read 71 until the job settled, then 95."""
        from loopcut import cloud_restore, spending
        self.session.update(id="c" * 32, spend=spending.new_ledger())
        turn = types.SimpleNamespace(first_item=0, outcome="completed", returned_submission=False)
        chat_credits.turn_start(self.session, turn)
        self.session["turn"] = turn
        chat_credits.note_balance({"credits_balance": 100})
        reply = state.item_assistant()
        reply.update(text="The cloud render is running; you can close your laptop.", streaming=False)
        self.session["items"] = [state.item_user("render a 5 second video in the cloud"), reply]
        with spending.lock:  # The render's quote, held as its call ran (agent._Consent).
            spending.start(spending.ledger(self.session), {"credits": 29}, False)
        row = {"id": "job_render", "conversation": self.session["id"], "held": 29, "auto": False, "settled": False,
               "status": "running", "fingerprint": "", "key": ""}
        self.session["cloud_jobs"] = [row]
        with patch.object(chat_credits.threading, "Thread"):
            chat_credits.turn_end(self.session, turn)
        with patch("loopcut.conversations.save"):
            chat_credits.record(self.session, reply, turn.credits, False, after=71)
        self.assertEqual(chat_credits.turn_line(reply, self.session), "Done · 29 held")
        self.assertEqual(chat_credits.chat_line(self.session), "This chat: 29 held")
        self.assertEqual(chat_credits.chat_total(self.session), 0)
        # The job settles: 5 charged, 24 returned.
        job = {"id": "job_render", "status": "succeeded", "credits": {"reserved": 29, "charged": 5, "refunded": 24},
               "outputs": []}
        with patch("loopcut.conversations.save"):
            cloud_restore.settle(dict(row), job)
        self.assertEqual(chat_credits.turn_line(reply, self.session), "Done · 5 credits")
        self.assertEqual(chat_credits.chat_line(self.session), "This chat: 5 credits")
        self.assertEqual(chat_credits.chat_total(self.session), 5)
        self.assertEqual(reply["chat_credits"], 0, "the AI part of the turn")
        # A caller without the chat's job records: its hold, as the balance had it.
        self.assertEqual(chat_credits.turn_line(reply), "Done · 29 credits")

    def test_the_start_prefers_the_manifests_fraction_when_the_header_agrees(self):
        self.assertEqual(chat_credits._start({"header": 100, "before": 99.5}), 99.5)
        self.assertEqual(chat_credits._start({"header": 100, "before": 90}), 100)
        self.assertEqual(chat_credits._start({"header": None, "before": 90}), 90)
        self.assertIsNone(chat_credits._start({"header": None, "before": None}))

    def test_no_line_outside_the_arm_for_refused_sends_or_replyless_turns(self):
        self.runtime.manifest["account"] = {"plan": "free"}
        turn = types.SimpleNamespace(first_item=0, outcome="completed", returned_submission=False)
        chat_credits.turn_start(self.session, turn)
        self.assertFalse(hasattr(turn, "credits"))
        with patch.object(chat_credits.threading, "Thread") as thread:
            chat_credits.turn_end(self.session, turn)
            refused = types.SimpleNamespace(first_item=0, outcome="error", returned_submission=True,
                                            credits={"before": 1, "header": None, "tools": 0})
            chat_credits.turn_end(self.session, refused)
            tools_only = types.SimpleNamespace(first_item=0, outcome="completed", returned_submission=False,
                                               credits={"before": 1, "header": None, "tools": 0})
            self.session["items"] = [state.item_user("hi")]
            chat_credits.turn_end(self.session, tools_only)
        thread.assert_not_called()

    def test_settling_retries_a_busy_refresh_then_records_on_the_main_thread(self):
        reply = state.item_assistant()
        self.session["items"] = [reply]
        data = {"before": 100, "header": None, "tools": 2}
        self.account["credits"]["balance"] = 97
        main = types.SimpleNamespace(run_on_main=lambda fn: fn(), request_redraw=lambda: None)  # No bpy here.
        with patch.dict(sys.modules, {"loopcut.mainthread": main}), patch("loopcut.mainthread", main, create=True), \
                patch.object(chat_credits.time, "sleep"), patch("loopcut.conversations.save"):
            chat_credits._settle(self.session, reply, data, True, delay=0)
        self.assertEqual(len(self.runtime.refreshes), 2)
        self.assertEqual((reply["turn_credits"], reply["chat_credits"], reply["turn_stopped"]), (3, 3, True))
        # No fresh balance at all (offline): no line, never a wrong "0 credits".
        other = state.item_assistant()
        self.session["items"] = [other]
        self.runtime.refresh = lambda: False
        with patch.dict(sys.modules, {"loopcut.mainthread": main}), patch("loopcut.mainthread", main, create=True), \
                patch.object(chat_credits.time, "sleep"), patch("loopcut.conversations.save"):
            chat_credits._settle(self.session, other, dict(data), False, delay=0)
        self.assertNotIn("turn_credits", other)

    def test_a_turn_that_ends_into_the_next_counts_each_credit_once(self):
        """A queued note starts the next turn at once: this turn ends where the next one's first reply began."""
        reply = state.item_assistant()
        self.session["items"] = [reply]
        first = types.SimpleNamespace()
        data = {"before": 100, "header": None, "tools": 2, "tools_after": 2, "turn": first}
        nxt = types.SimpleNamespace(credits={"before": 100, "header": 96, "tools": 2})
        self.session["turn"] = nxt
        with patch("loopcut.conversations.save"):
            chat_credits.record(self.session, reply, data, False, after=93)  # 93 already has the next turn's 3.
        self.assertEqual(reply["turn_credits"], 4)
        reply.pop("turn_credits")
        nxt.credits.update(header=None, before=100)  # No reply yet (or no gateway): it starts from here.
        with patch("loopcut.conversations.save"):
            chat_credits.record(self.session, reply, data, False, after=96)
        self.assertEqual((reply["turn_credits"], nxt.credits["before"]), (4, 96))


class PanelTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(layout.config, "billing_open", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.session = state.new_session()
        self.session["id"] = "c" * 32
        self.card = state.item_limit("Out of credits. 100 free credits come back in 8 days.", "insufficient_credits",
                                     "free", RESET, UPGRADE)

    def build(self, width=420, height=900, **ui):
        base = {"selection": [], "context_budget": 24000, "now": NOW, "billing": "credits", "chat_credits": ARM,
                "credit_balance": 0, "account": {"plan": "free", "session": {"used": 0.9}, "week": {"used": 0.5}}}
        return layout.build(self.session, width, height, 1.0, measure, "fast", None, {**base, **ui})

    def notice(self):
        return {"item": self.card, "conversation": self.session["id"]}

    def test_credits_accounts_show_no_windows_and_their_footer_counts_chat(self):
        reply = state.item_assistant()
        reply.update(text="Added brass key caps.", streaming=False, turn_credits=6, chat_credits=4)
        self.session["items"] = [state.item_user("details"), reply]
        self.session["spend"] = {"credits": 2, "auto": 2, "allowed": 0}
        display = self.build(credit_balance=94.6, credits="94 credits ($0.94)", today="Today: 0 credits")
        self.assertFalse(any(p["id"].startswith("input.usage") for p in display["prims"]))
        self.assertEqual([p["id"] for p in display["prims"] if p["t"] == "ring"], ["input.ring.context"])
        self.assertNotIn("allowance", hit(display, "input.rings")["tip"])
        self.assertEqual(texts(display, "input.credits"), ["94 credits ($0.94) · This chat: 6 credits"])
        narrow = self.build(240, credit_balance=94.6)  # Too long for one line: one each.
        self.assertEqual(texts(narrow, "input.credits"), ["94 credits ($0.94)", "This chat: 6 credits"])
        self.assertEqual(hit(display, "input.credits")["action"], ("open_account", None))
        self.assertIn("AI 4 credits, tools 2 credits", hit(display, "input.credits")["tip"])
        self.assertEqual(texts(display, "item1.credits"), ["✓", "Done · 6 credits"])
        reply["streaming"] = True
        self.assertFalse(texts(self.build(), "item1.credits"))
        # The same items for an account outside the arm keep 0.2.13's footer and rings.
        reply["streaming"] = False
        other = self.build(billing="", chat_credits=None, credits="94 credits ($0.94)", credit_balance=94,
                           today="Today: 0 credits")
        self.assertEqual(texts(other, "input.credits")[0], "94 credits ($0.94)")
        self.assertIn("Today: 0 credits", " ".join(texts(other, "input.credits")))  # Allowance accounts keep it.
        self.assertEqual(len([p for p in other["prims"] if p["t"] == "ring"]), 2)

    def test_out_of_credits_opens_the_pop_up_with_pro_first_and_the_free_way_on(self):
        for width, height in ((420, 900), (260, 700)):
            with self.subTest(width=width):
                display = self.build(width, height, allowance_notice=self.notice())
                words = texts(display, "popup.")
                for line in ("You're out of credits", "100 free credits come back Sun 1 Nov",
                             "These pause until then:", "Have ChatGPT Plus or Pro? Chat on it at no cost.",
                             "Pro: 2,200 credits a month ($22)"):
                    self.assertIn(line, " ".join(words))
                self.assertNotIn("Keep chatting for free", " ".join(words))  # False to anyone without Plus.
                self.assertEqual(hit(display, "popup.upgrade")["action"], ("limit_upgrade", self.session["id"]))
                label = " ".join(texts(display, "popup.upgrade"))
                self.assertEqual(label, "Get Pro · $20 a month")  # The line above it says the credits.
                self.assertEqual(hit(display, "popup.free.chatgpt")["action"], ("sign_in", "chatgpt"))
                self.assertEqual(hit(display, "popup.free.key")["action"], ("own_key", None))
                self.assertEqual(hit(display, "popup.close")["action"], ("limit_popup_close", None))
                self.assertIn("Cancel anytime · Asset search stays free", " ".join(texts(display, "popup.fine")))
                images = [p for p in display["prims"] if p["t"] == "image"]
                self.assertEqual(len(images), 4)
                self.assertTrue(all(Path(p["path"]).is_file() for p in images))
                self.assertEqual(len([p for p in display["prims"] if p["id"].endswith(".lock.body")]), 6)
                sheet = next(p for p in display["prims"] if p["id"] == "popup.sheet")
                self.assertLessEqual(sheet["y"] + sheet["h"], height)
                for p in display["prims"][display["prims"].index(sheet):]:
                    if p.get("text"):
                        self.assertLessEqual(p["x"] + measure("ui", p["size"], p["text"]), width, p["id"])
                self.assertEqual(layout.hit_test(display, width / 2, 40), ("limit_popup_close", None))
                self.assertEqual(layout.hit_test(display, sheet["x"] + 4, sheet["y"] + 4), ("noop", None))
                self.assertFalse(any(p["id"].startswith("input.limit") for p in display["prims"]))
                self.assertFalse(any(v in " ".join(words) for v in VENDORS))
        display = self.build(allowance_notice=self.notice(), chatgpt_plan=True)
        self.assertEqual(hit(display, "popup.free.chatgpt")["action"], ("limit_chatgpt", self.session["id"]))

    def test_closing_leaves_one_slim_card_whose_button_opens_it_again(self):
        self.card["popup_closed"] = True
        display = self.build(allowance_notice=self.notice())
        self.assertFalse(any(p["id"].startswith("popup.") for p in display["prims"]))
        self.assertEqual(texts(display, "input.limit.title"), ["You're out of credits"])
        self.assertEqual(" ".join(texts(display, "input.limit.text")), "Free credits come back Sun 1 Nov")
        self.assertEqual(hit(display, "input.limit.open")["action"], ("limit_popup_open", self.session["id"]))
        self.assertEqual(texts(display, "input.limit.open"), ["See Pro"])
        with patch.object(layout.config, "billing_open", return_value=False):
            closed = self.build(allowance_notice=self.notice())
            self.assertEqual(texts(closed, "input.limit.open"), ["Options"])
            self.card["popup_closed"] = False
            reopened = self.build(allowance_notice=self.notice())
            self.assertFalse(any(h["id"] == "popup.upgrade" for h in reopened["hits"]))  # No sales: no Pro.
            self.assertIn("Asset search stays free", texts(reopened, "popup.fine"))
        self.card.update(status="waiting", popup_closed=True)  # Get Pro was pressed: the browser has it.
        display = self.build(allowance_notice=self.notice())
        self.assertFalse(any(p["id"].startswith("popup.") for p in display["prims"]))
        self.assertEqual(texts(display, "input.limit.title"), ["Waiting for your upgrade"])
        self.assertEqual([h["id"] for h in display["hits"] if h["id"].startswith("input.limit.")], ["input.limit.open"])

    def test_narrow_and_short_panels_keep_every_button_in_view(self):
        self.card["popup_closed"] = True
        for width in (200, 240, 260):
            with self.subTest(width=width):
                display = self.build(width, 700, allowance_notice=self.notice())
                title = [p for p in display["prims"] if p["id"].startswith("input.limit.title")]
                button = hit(display, "input.limit.open")
                self.assertGreaterEqual(button["y"], max(p["y"] + p["h"] for p in title))  # Under the text.
                self.assertLessEqual(button["x"] + button["w"], width)
        self.card["popup_closed"] = False
        for height in (420, 450, 600):
            with self.subTest(height=height):
                display = self.build(420, height, allowance_notice=self.notice())
                for id in ("popup.upgrade", "popup.free.chatgpt", "popup.free.key", "popup.close"):
                    box = hit(display, id)
                    self.assertLessEqual(box["y"] + box["h"], height, id)
                pictures = [p for p in display["prims"] if p["t"] == "image"]
                self.assertEqual(len(pictures), 4 if height == 600 else 0)  # Compact when short: no pictures.
        self.assertEqual(len([p for p in self.build(420, 900, allowance_notice=self.notice())["prims"]
                              if p["t"] == "image"]), 4)

    def test_an_abandoned_checkout_still_offers_the_free_ways_and_an_upgrade_elsewhere_ends_it(self):
        self.card.update(status="waiting", popup_closed=True)
        display = self.build(allowance_notice=self.notice())
        self.assertEqual(texts(display, "input.limit.title"), ["Waiting for your upgrade"])
        self.assertEqual(texts(display, "input.limit.open"), ["Options"])
        self.card["popup_closed"] = False  # Options pressed.
        display = self.build(allowance_notice=self.notice())
        self.assertEqual(hit(display, "popup.free.key")["action"], ("own_key", None))
        self.card.update(status="", popup_closed=False)
        display = self.build(allowance_notice=self.notice(), chat_credits={**ARM, "plan": "pro", "monthly": 2200})
        self.assertFalse(any(p["id"].startswith(("popup.", "input.limit")) for p in display["prims"]))

    def test_nothing_shows_while_busy_or_after_the_credits_come_back(self):
        self.session["busy"] = True
        display = self.build(allowance_notice=self.notice())
        self.assertFalse(any(p["id"].startswith(("popup.", "input.limit")) for p in display["prims"]))
        self.session["busy"] = False
        later = datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc).timestamp()
        display = self.build(allowance_notice=self.notice(), now=later)
        self.assertFalse(any(p["id"].startswith(("popup.", "input.limit")) for p in display["prims"]))

    def test_a_refusal_mid_turn_shows_its_slim_card_in_the_chat_only(self):
        self.card["popup_closed"] = True
        self.session["items"] = [state.item_user("details"), self.card]
        display = self.build(allowance_notice=self.notice())
        self.assertEqual(texts(display, "item1.limit.title"), ["You're out of credits"])
        self.assertEqual(hit(display, "item1.limit.open")["action"], ("limit_popup_open", 1))
        self.assertFalse(any(p["id"].startswith("input.limit") for p in display["prims"]))
        reloaded = dict(self.card)  # The same card read back from disk: history, no buttons.
        self.session["items"] = [state.item_user("details"), reloaded]
        display = self.build(allowance_notice=None)
        self.assertEqual(texts(display, "item1.limit.title"), ["You're out of credits"])
        self.assertFalse(any(h["id"].startswith("item1.limit.") for h in display["hits"]))

    def test_the_pop_up_and_its_slim_card_name_the_sooner_weekly_top_up(self):
        refill = {"credits": 25, "at": "2026-10-30T09:12:00Z"}
        self.card["refill"] = refill
        display = self.build(allowance_notice=self.notice())
        self.assertIn("25 free credits come back Fri 30 Oct", " ".join(texts(display, "popup.text")))
        self.assertNotIn("100 free credits", " ".join(texts(display, "popup.")))
        self.card["popup_closed"] = True
        display = self.build(allowance_notice=self.notice())
        self.assertEqual(" ".join(texts(display, "input.limit.text")), "Free credits come back Fri 30 Oct")
        after = datetime(2026, 10, 30, 9, 13, tzinfo=timezone.utc).timestamp()  # Topped up: the card is over.
        display = self.build(allowance_notice=self.notice(), now=after)
        self.assertFalse(any(p["id"].startswith(("popup.", "input.limit")) for p in display["prims"]))
        # No refill on the refusal: the manifest's (account.chat_credits.refill) names it.
        self.card.pop("refill")
        self.card["popup_closed"] = False
        display = self.build(allowance_notice=self.notice(), chat_credits={**ARM, "refill": refill})
        self.assertIn("25 free credits come back Fri 30 Oct", " ".join(texts(display, "popup.text")))

    def test_variant_b_replaces_the_allowance_card_with_a_new_server_only(self):
        card = state.item_limit("Chat allowance used.", "week_limit", "free", "2026-10-25T18:00:00Z",
                                {**UPGRADE, "label": "Upgrade to Pro", "times": 17})
        notice = {"item": card, "conversation": self.session["id"]}
        display = self.build(billing="allowance", chat_credits=None, allowance_notice=notice)
        words = " ".join(texts(display, "popup."))
        for line in ("Loopcut Pro", "$20 / month", "You've used this week's free chat.", "Upgrade and you get:",
                     "17× more chat every month", "Loopcut Pro, the stronger model", "2,200 cloud credits a month",
                     "Cycles renders and top-ups", "Upgrade to Pro", "Cancel anytime · Free chat refills Sun 25 Oct",
                     "Use ChatGPT Plus/Pro", "Add an API key"):
            self.assertIn(line, words)
        self.assertFalse(any(v in words for v in VENDORS))
        self.assertEqual(hit(display, "popup.upgrade")["action"], ("limit_upgrade", self.session["id"]))
        self.assertEqual(len([p for p in display["prims"] if p["t"] == "image" and Path(p["path"]).is_file()]), 4)
        card["popup_closed"] = True
        display = self.build(billing="allowance", chat_credits=None, allowance_notice=notice)
        self.assertEqual(texts(display, "input.limit.title"), ["Free chat used"])
        self.assertEqual(texts(display, "input.limit.text"), ["Refills Sun 25 Oct"])
        self.assertFalse(texts(display, "input.usage"))  # The card says what ran out; no usage line above it.
        card["popup_closed"] = False
        # A new server's refusal for an account without account.billing still says upgrade.times: variant B.
        times_only = layout.build(self.session, 420, 900, 1.0, measure, "fast", None,
                                  {"selection": [], "context_budget": 24000, "now": NOW, "allowance_notice": notice,
                                   "account": {"plan": "free"}})
        self.assertIn("17× more chat every month", texts(times_only, "popup."))
        card["upgrade"] = {key: value for key, value in card["upgrade"].items() if key != "times"}
        old = layout.build(self.session, 420, 900, 1.0, measure, "fast", None,
                           {"selection": [], "context_budget": 24000, "now": NOW, "allowance_notice": notice,
                            "account": {"plan": "free"}})
        self.assertFalse(any(p["id"].startswith("popup.") for p in old["prims"]))  # An older server: 0.2.13's card.
        self.assertEqual(hit(old, "input.limit.upgrade")["action"], ("limit_upgrade", self.session["id"]))
        # A Pro account offered Max: the slim card names Max, and no row repeats what Pro has.
        card.update(plan="pro", upgrade={**UPGRADE, "plan": "max", "times": 3}, popup_closed=True)
        notice = {"item": card, "conversation": self.session["id"]}
        pro = {"plan": "pro", "week": {"used": 1.0, "resets_at": "2026-10-25T18:00:00Z"}}
        display = self.build(billing="allowance", chat_credits=None, allowance_notice=notice, account=pro)
        self.assertEqual(texts(display, "input.limit.open"), ["See Max"])
        self.assertEqual(texts(display, "input.limit.title"), ["Chat allowance used"])
        card["popup_closed"] = False
        words = " ".join(texts(self.build(billing="allowance", chat_credits=None, allowance_notice=notice,
                                          account=pro), "popup."))
        self.assertIn("6,600 cloud credits a month", words)
        self.assertNotIn("Cycles renders and top-ups", words)
        # No upgrade to offer (the top plan): the old card too.
        card["upgrade"] = None
        display = self.build(billing="allowance", chat_credits=None, allowance_notice=notice)
        self.assertFalse(any(p["id"].startswith("popup.") for p in display["prims"]))


    # --- 0.2.15 (persona tests 2026-10-23/24, owner-approved backlog items 1-7) --------------------------------

    def offer_card(self, **upgrade):
        card = state.item_limit("Chat allowance used.", "week_limit", "free", "2026-10-25T18:00:00Z",
                                {**UPGRADE, "label": "Upgrade to Pro", "times": 17, **upgrade})
        return card, {"item": card, "conversation": self.session["id"]}

    def test_concrete_units_replace_n_times_on_both_pop_ups(self):
        self.card["upgrade"] = {**UPGRADE, "builds": 110}
        words = " ".join(texts(self.build(allowance_notice=self.notice()), "popup."))
        self.assertIn("Pro: 2,200 credits a month ($22), about 110 builds", words)
        card, notice = self.offer_card(builds=110)
        words = " ".join(texts(self.build(billing="", chat_credits=None, allowance_notice=notice), "popup."))
        self.assertIn("About 110 builds a month", words)
        self.assertIn("2,200 cloud credits a month ($22)", words)
        self.assertNotIn("× more", words)
        card["upgrade"].pop("builds")  # A refusal without builds keeps the times line.
        words = " ".join(texts(self.build(billing="", chat_credits=None, allowance_notice=notice), "popup."))
        self.assertIn("17× more chat every month", words)
        for bad in (0, -3, "110", 1.5, None):
            self.assertNotIn("builds", chat_credits.offer_line({**UPGRADE, "builds": bad}))

    def test_variant_b_has_the_green_box_and_no_balance_while_it_stands(self):
        card, notice = self.offer_card(builds=110)
        ui = dict(billing="", chat_credits=None, allowance_notice=notice, credits="100 credits ($1.00)",
                  credit_balance=100)
        display = self.build(**ui)
        self.assertEqual(" ".join(texts(display, "popup.free.title")), "Have ChatGPT Plus or Pro? Chat on it at no cost.")
        self.assertEqual(hit(display, "popup.free.chatgpt")["action"], ("sign_in", "chatgpt"))
        self.assertEqual(hit(display, "popup.free.key")["action"], ("own_key", None))
        self.assertEqual(hit(display, "popup.close")["action"], ("limit_popup_close", None))
        self.assertNotIn("100 credits ($1.00)", " ".join(texts(display)))
        self.assertFalse(any(h["id"] == "input.credits" for h in display["hits"]))
        sheet = next(p for p in display["prims"] if p["id"] == "popup.sheet")
        for p in display["prims"][display["prims"].index(sheet):]:
            if p.get("text"):
                self.assertLessEqual(p["x"] + measure("ui", p["size"], p["text"]), 420, p["id"])
        card["popup_closed"] = True  # Its slim card still stands: no balance yet.
        self.assertNotIn("100 credits ($1.00)", " ".join(texts(self.build(**ui))))
        later = datetime(2026, 10, 25, 18, 1, tzinfo=timezone.utc).timestamp()  # Refilled: the balance is back.
        ui["account"] = {"plan": "free", "week": {"used": 0.0, "resets_at": "2026-11-01T18:00:00Z"}}
        self.assertEqual(texts(self.build(now=later, **ui), "input.credits")[0], "100 credits ($1.00)")
        for height in (420, 520):  # Short panels keep every button of the taller sheet in view.
            card["popup_closed"] = False
            display = self.build(420, height, **{**ui, "account": {"plan": "free", "week": {"used": 1.0}}})
            for id in ("popup.upgrade", "popup.free.chatgpt", "popup.free.key", "popup.close"):
                box = hit(display, id)
                self.assertLessEqual(box["y"] + box["h"], height, (height, id))

    def test_the_pop_up_says_what_became_of_the_request_only_where_true(self):
        self.session["input"] = "make the keys brass"  # A new message refused before anything ran: back in the box.
        words = " ".join(texts(self.build(allowance_notice=self.notice()), "popup."))
        self.assertIn("Your message is saved in the box below.", words)
        self.session["input"] = ""
        words = " ".join(texts(self.build(allowance_notice=self.notice()), "popup."))
        self.assertNotIn("saved", words)
        # Stopped mid-turn: the turn is kept, and its slim card's Continue (or an upgrade) goes on with it.
        self.session["items"] = [state.item_user("details"), self.card]
        self.session["messages"] = [{"role": "user", "content": "details"}, {"role": "tool", "content": "Done."}]
        words = " ".join(texts(self.build(allowance_notice=self.notice()), "popup."))
        self.assertIn("Your request is saved; it picks up when you continue.", words)
        self.card["popup_closed"] = True
        display = self.build(allowance_notice=self.notice())
        self.assertEqual(hit(display, "item1.limit.retry")["action"], ("limit_retry", 1))
        self.assertEqual(texts(display, "item1.limit.retry"), ["Continue"])
        self.session["messages"].append({"role": "assistant", "content": "Finished."})  # Nothing left to resume.
        self.card["popup_closed"] = False
        self.assertNotIn("saved", " ".join(texts(self.build(allowance_notice=self.notice()), "popup.")))
        self.assertFalse(any(h["id"] == "item1.limit.retry" for h in self.build(allowance_notice=self.notice())["hits"]))

    def test_once_the_credits_are_back_a_stopped_turn_keeps_its_continue(self):
        self.card.update(refill={"credits": 25, "at": "2026-10-30T09:12:00Z"}, popup_closed=True)
        self.session["items"] = [state.item_user("details"), self.card]
        self.session["messages"] = [{"role": "user", "content": "details"}, {"role": "tool", "content": "Done."}]
        after = datetime(2026, 10, 30, 9, 13, tzinfo=timezone.utc).timestamp()
        display = self.build(allowance_notice=self.notice(), now=after)
        self.assertEqual(texts(display, "item1.limit.title"), ["Credits are back"])
        self.assertEqual(texts(display, "item1.limit.text"), ["Continue where it stopped."])
        self.assertEqual([h["action"] for h in display["hits"] if h["id"].startswith("item1.limit.")],
                         [("limit_retry", 1)])
        self.session["messages"].append({"role": "assistant", "content": "Finished."})  # Nothing to go on with.
        display = self.build(allowance_notice=self.notice(), now=after)
        self.assertEqual(texts(display, "item1.limit.text"), ["Free credits came back."])
        self.assertFalse(any(h["id"].startswith("item1.limit.") for h in display["hits"]))

    def test_the_locked_grid_shows_released_tools_only(self):
        words = texts(self.build(allowance_notice=self.notice()), "popup.tile")
        self.assertNotIn("Auto-rig", words)
        self.assertEqual(words, ["AI agent", "Photo → 3D", "Motion", "Renders", "Sound and voice", "Retopology"])

    def test_own_ai_users_see_tool_credits_and_no_padlock_on_the_agent(self):
        self.card["upgrade"] = {**UPGRADE, "builds": 110}
        for provider in ("CHATGPT", "OPENAI"):
            with self.subTest(provider=provider):
                display = self.build(allowance_notice=self.notice(), provider=provider, chatgpt_plan=True)
                words = " ".join(texts(display, "popup."))
                self.assertIn("Pro: 2,200 credits a month ($22) for renders, motion and 3D models", words)
                self.assertNotIn("builds", words)
                self.assertNotIn("Have ChatGPT Plus", words)  # Already on their own AI.
                locks = [p["id"] for p in display["prims"] if p["id"].endswith(".lock.body")]
                self.assertEqual(len(locks), 5)
                self.assertNotIn("popup.tile0.lock.body", locks)  # Tile 0 is the AI agent.
                self.assertEqual(hit(display, "popup.close")["action"], ("limit_popup_close", None))

    def test_a_reopened_chat_draws_its_new_cards_as_history(self):
        """Import path: a conversation saved with 0.2.15's refill and heads-up loads back and draws with no pop-up and
        no buttons on the old refusal (only the live refusal has them)."""
        import tempfile
        from loopcut import conversations
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.object(conversations, "root", return_value=Path(folder.name)):
            self.card.update(refill={"credits": 25, "at": "2026-10-30T09:12:00Z"}, popup_closed=True)
            heads_up = state.item_notice("18 credits left ($0.18)\n25 free credits come back Fri 30 Oct",
                                         action_label="Get Pro · $20 a month",
                                         url="https://loopcut.org/pricing?from=app&reason=low_credits&plan=pro")
            self.session["items"] = [state.item_user("details"), heads_up, self.card]
            conversations.save(self.session)
            body = conversations._validate(json.loads((Path(folder.name) / self.session["id"] / "conversation.json")
                                                      .read_text()))  # What load() checks (it needs bpy for the rest).
        self.session = {**state.new_session(), "id": body["id"], "items": body["items"]}
        display = self.build(allowance_notice=None)
        self.assertFalse(any(p["id"].startswith("popup.") for p in display["prims"]))
        self.assertEqual(texts(display, "item2.limit.title"), ["You're out of credits"])
        self.assertFalse(any(h["id"].startswith("item2.limit.") for h in display["hits"]))
        self.assertEqual(hit(display, "item1.notice.action")["action"], ("open_url", 1))

    def test_a_pop_up_that_cannot_draw_closes_and_the_chat_stays_usable(self):
        with patch.object(layout, "_credits_sheet", side_effect=RuntimeError("bad data")):
            display = self.build(allowance_notice=self.notice())
        self.assertFalse(any(p["id"].startswith("popup.") for p in display["prims"]))
        self.assertTrue(self.card["popup_closed"])
        self.assertTrue(any(h["id"] == "input.text" or h["id"].startswith("input") for h in display["hits"]))
        with patch.object(layout, "_draw_slim_card", side_effect=KeyError("plan")):
            display = self.build(allowance_notice=self.notice())
        self.assertFalse(any(p["id"].startswith("input.limit") for p in display["prims"]))
        self.assertTrue(any(h["id"].startswith("input") for h in display["hits"]))


class LowCreditsTest(unittest.TestCase):
    """0.2.15 item 5: one quiet heads-up at about 20 credits, from the replies' balance, once a refill period; on Pro
    and Max only since 0.2.18 (owner: free users meet the out-of-credits offer unwarned)."""

    def setUp(self):
        from loopcut import agent, cloud, nudges
        self.agent, self.nudges = agent, nudges
        self.account = {"plan": "free", "billing": "credits", "chat_credits": {
            "per_step": 0.84, "refill": {"credits": 25, "at": "2026-10-30T09:12:00Z"}},
            "credits": {"balance": 18, "monthly": 0, "resets_at": RESET}}
        self.seen = []
        for target, name, value in ((cloud, "_runtime", Runtime(self.account)),
                                    (nudges, "first_time", lambda kind, key: (kind, key) not in self.seen
                                     and not self.seen.append((kind, key)))):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def warn(self, items, balance, billing="credits"):
        info = {"plan": "free", "billing": billing, "credits_balance": balance}
        with patch.object(self.agent.config, "billing_open", return_value=True), \
                patch.object(chat_credits.time, "time", return_value=NOW):
            self.agent._warn_low_credits(items, info, None)

    def test_never_on_the_free_plan(self):
        items = []
        for balance in (18, 9, 1):
            self.warn(items, balance)
        self.assertEqual(items, [])
        self.assertEqual(self.seen, [])

    def test_once_per_refill_period_with_add_credits_on_pro(self):
        self.account["plan"] = "pro"
        items = []
        self.warn(items, 21)
        self.assertEqual(items, [])
        self.warn(items, 18)
        self.assertEqual(len(items), 1)
        notice = items[0]
        self.assertEqual(notice["kind"], "notice")
        self.assertTrue(notice["text"].startswith("18 credits left ($0.18)\n"), notice["text"])
        self.assertEqual(notice["action_label"], "Add credits")
        self.assertIn("/account?from=app&reason=low_credits", notice["url"])
        self.assertEqual(self.seen, [("low_chat_credits", "2026-10-30T09:12:00Z")])
        self.warn(items, 9)
        self.assertEqual(len(items), 1)  # Once in this refill period.
        self.account["chat_credits"]["refill"]["at"] = "2026-11-06T09:12:00Z"  # The next week.
        self.warn(items, 12)
        self.assertEqual(len(items), 2)
        self.assertFalse(any(v in " ".join(i["text"] for i in items) for v in VENDORS))

    def test_a_reply_in_the_live_chat_brings_it_and_no_window_warning(self):
        self.account["plan"] = "pro"
        session = state.reset()
        info = {"plan": "free", "billing": "credits", "credits_balance": 18,
                "session": {"used": 0.95, "resets_at": "2026-10-24T01:00:00Z"}, "week": {"used": 0.97}}
        with patch.object(self.agent.config, "billing_open", return_value=True), \
                patch.object(chat_credits.time, "time", return_value=NOW):
            self.agent._warn_low_allowance(session, session["items"], info)
            self.agent._warn_low_allowance(session, session["items"], {**info, "credits_balance": None})
        self.assertEqual([i["text"] for i in session["items"]],
                         ["18 credits left ($0.18)\n25 free credits come back Fri 30 Oct"])

    def test_never_outside_the_arm_or_at_zero(self):
        items = []
        self.warn(items, 0)
        self.warn(items, 15, billing="allowance")
        self.warn(items, None)
        self.assertEqual(items, [])

    def test_the_arm_skips_the_older_low_credit_card_and_allowance_accounts_keep_it(self):
        nudges = self.nudges
        with patch.object(nudges.config, "billing_open", return_value=True):
            for billing, kinds in (("credits", []), ("allowance", ["low_credits"]), (None, ["low_credits"])):
                with nudges._lock:
                    nudges._pending.clear()
                nudges.observe({"account": {"billing": billing, "credits": {"balance": 15, "monthly": 100}}}, True, [])
                with nudges._lock:
                    self.assertEqual([k for _, k, _ in nudges._pending], kinds, billing)
                    nudges._pending.clear()

    def test_turns_on_a_users_own_ai_carry_no_credits_line(self):
        from loopcut import config
        turn = types.SimpleNamespace(config=types.SimpleNamespace(base_url="https://chatgpt.com/backend-api/codex"))
        chat_credits.turn_start(state.new_session(), turn)
        self.assertFalse(hasattr(turn, "credits"))
        hosted = types.SimpleNamespace(config=types.SimpleNamespace(base_url=config.SERVICE_URL + "/v1"))
        with patch.object(config, "on_service", return_value=True):
            chat_credits.turn_start(state.new_session(), hosted)
        self.assertEqual(hosted.credits["before"], 18)


class FlagOff0214Test(unittest.TestCase):
    def test_flag_off_matches_0_2_14(self):
        """fixtures/flag_off_0.2.14.json is flag_off_0_2_14's snapshot taken from the 0.2.14 add-on's credits package
        (75f6845c1ca): with no account.billing, chat_credits, refill, upgrade.times or builds, the panel, the setup,
        the chat footers, the conversion cards and the refusal cards are exactly 0.2.14's."""
        import flag_off_0_2_14
        expected = json.loads(flag_off_0_2_14.FIXTURE.read_text())
        now = json.loads(json.dumps(flag_off_0_2_14.snapshot(), sort_keys=True))
        self.assertEqual(sorted(now), sorted(expected))
        for part in expected:
            for name in expected[part]:
                with self.subTest(part=part, scene=name):
                    self.assertEqual(now[part][name], expected[part][name])

if __name__ == "__main__":
    unittest.main()
