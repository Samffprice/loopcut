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

    def test_step_label_rounds_per_step_to_a_whole_credit_never_under_one(self):
        for per_step, label in ((0.84, "≈1 credit / step"), (0.2, "≈1 credit / step"), (2.6, "≈3 credits / step"),
                                (None, "≈1 credit / step"), ("1", "≈1 credit / step"), (-1, "≈1 credit / step")):
            with self.subTest(per_step=per_step):
                self.assertEqual(chat_credits.step_label({"chat_credits": {"per_step": per_step}}), label)
        self.assertEqual(chat_credits.step_label({"chat_credits": {"per_step": 0.84}}, short=True), "≈1 / step")

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

    def test_the_upgrade_button_and_variant_b_rows_follow_the_plan_and_times(self):
        self.assertEqual(chat_credits.upgrade_button(UPGRADE), "Get Pro · 2,200 credits a month · $20")
        self.assertEqual(chat_credits.upgrade_button({**UPGRADE, "plan": "max"}), "Get Max · 6,600 credits a month · $60")
        self.assertEqual(chat_credits.upgrade_button({"plan": "team", "label": "See Team"}), "See Team")
        item = {"reason": "week_limit", "plan": "free", "upgrade": {**UPGRADE, "times": 17}}
        self.assertEqual(chat_credits.offer_rows(item), [
            ("chat", "17× more chat every month"), ("model", "Loopcut Pro, the stronger model"),
            ("credits", "2,200 cloud credits a month"), ("render", "Cycles renders and top-ups")])
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
        self.assertEqual(turn.credits, {"before": 100, "header": None, "tools": 2})
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
        self.assertEqual(texts(display, "input.credits"), ["94 credits · chat 6"])
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
                             "These pause until then:", "Keep chatting for free"):
                    self.assertIn(line, " ".join(words))
                self.assertEqual(hit(display, "popup.upgrade")["action"], ("limit_upgrade", self.session["id"]))
                label = " ".join(texts(display, "popup.upgrade"))
                self.assertEqual(label, "Get Pro · 2,200 credits a month · $20" if width == 420 else
                                 "Get Pro · $20 a month")
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


if __name__ == "__main__":
    unittest.main()
