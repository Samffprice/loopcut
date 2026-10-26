"""Credit spending policy (C47): what runs on its own, what pauses on a card, workers, receipts and the panel.

No Blender and no network: the policy is pure, consent runs through agent._Consent with the wait patched, and the
server-refusal retry goes through cloud_account.submit and cloud_jobs.run against an in-memory transport.
"""

import io
import json
import math
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import (agent, cloud, cloud_account, cloud_http, cloud_jobs, config, conversations,  # noqa: E402
                     spending, state)
from loopcut.ui import layout, theme as T  # noqa: E402

S = spending.Settings(ask_above=25)  # A user who set their own 25, the default before 0.2.20.
TOKEN = "lcc_" + "0123456789abcdef" * 4  # The server's single-use confirm_token: lcc_ and 64 hex.
PROJECT = {"id": "local", "no_generation": False}


def managed(credits, server=100, quote=None):
    cost = {"credits_estimate": credits, "quote_required": quote is not None}
    descriptor = {"name": "image_generate", "version": 1, "backend": "managed", "cost": cost,
                  "approval": {"confirm_over_credits": server}, "file_inputs": [], "latency": "seconds",
                  "title": "Generate image"}
    return cloud_account.approval_details(descriptor, quote)


def own_key(provider="openai"):
    return cloud_account.approval_details({"backend": "byok", "byok": {"provider": provider}})


def measure(font, size, text):
    return len(text) * size * 0.6


class PolicyTest(unittest.TestCase):
    """spending.decide on its own: the owner's Balanced rules."""

    def decide(self, details, spend=None, settings=S, **turn):
        return spending.decide(details, spend or spending.new_ledger(), settings, **turn)

    def test_small_calls_run_and_big_calls_ask(self):
        for credits, kind in ((4, spending.RUN), (6, spending.RUN), (20, spending.RUN), (25, spending.RUN),
                              (26, spending.COST), (80, spending.COST)):
            with self.subTest(credits=credits):
                self.assertEqual(self.decide(managed(credits)), kind)

    def test_the_default_ask_above_is_100_so_a_90_credit_call_runs_without_a_card(self):
        """Owner, 2026-10-26 (0.2.20): "a good chunk of credits just auto approve"; picture→3D final (80) included."""
        self.assertEqual(spending.ASK_ABOVE, 100)
        self.assertEqual(spending.Settings().ask_above, 100)
        for credits, kind in ((6, spending.RUN), (80, spending.RUN), (90, spending.RUN), (100, spending.RUN),
                              (101, spending.COST), (640, spending.COST)):
            with self.subTest(credits=credits):
                self.assertEqual(self.decide(managed(credits), settings=spending.Settings()), kind)

    def test_the_lower_of_server_and_user_thresholds_decides(self):
        self.assertEqual(self.decide(managed(20, server=10)), spending.COST, "the server asks lower")
        loose = spending.Settings(ask_above=50)
        self.assertEqual(self.decide(managed(40, server=100), settings=loose), spending.RUN)
        self.assertEqual(self.decide(managed(60, server=100), settings=loose), spending.COST)
        self.assertEqual(self.decide(managed(40, server=30), settings=loose), spending.COST)

    def test_server_zero_asks_for_every_priced_call_and_free_never_asks(self):
        # What the server sends add-ons older than 0.2.4; free calls never ask or count.
        self.assertEqual(self.decide(managed(1, server=0)), spending.COST)
        self.assertEqual(self.decide(managed(0, server=0)), spending.RUN)
        self.assertEqual(self.decide(managed(0), calls=99), spending.RUN)

    def test_chat_budget_crossing_asks_once_and_allow_adds_another_budget(self):
        spend = {"credits": 146, "auto": 146, "allowed": 0}
        details = managed(6)
        self.assertEqual(self.decide(managed(4), spend), spending.RUN, "146 + 4 fits in 150")
        self.assertEqual(self.decide(details, spend), spending.BUDGET)
        lines = spending.ask_lines(spending.BUDGET, details, spend, S)
        self.assertIn("This chat has used 146 credits. Allow 150 more?", lines)
        turn = agent.Turn()
        spending.allow(spending.BUDGET, details, spend, S, turn, [])
        self.assertEqual(spending.budget(spend, S), 300)
        self.assertEqual(self.decide(details, spend), spending.RUN)

    def test_allow_counts_from_what_the_chat_already_used(self):
        spend = {"credits": 290, "auto": 290, "allowed": 0}  # Reopened at 290 with the budget back at 150.
        spending.allow(spending.BUDGET, managed(6), spend, S, agent.Turn(), [])
        self.assertEqual(spending.budget(spend, S), 440)

    def test_explicitly_approved_spend_does_not_use_the_budget(self):
        spend = {"credits": 400, "auto": 20, "allowed": 0}
        self.assertEqual(self.decide(managed(6), spend), spending.RUN)
        line = spending.ask_lines(spending.BUDGET, managed(6), {"credits": 400, "auto": 148, "allowed": 0}, S)
        self.assertIn("This chat has used 400 credits (148 without asking). Allow 150 more?", line)

    def test_burst_guard_asks_before_the_sixth_paid_call(self):
        self.assertEqual(self.decide(managed(6), calls=4), spending.RUN)
        self.assertEqual(self.decide(managed(6), calls=5), spending.BURST_ASK)
        turn = agent.Turn()
        turn.paid_calls = 5
        spending.allow(spending.BURST_ASK, managed(6), spending.new_ledger(), S, turn, [])
        self.assertEqual(turn.burst_limit, 10)
        self.assertEqual(self.decide(managed(6), calls=5, burst_limit=turn.burst_limit), spending.RUN)

    def test_ask_me_first_asks_for_every_paid_call_a_6_credit_picture_too(self):
        """Paid extras "Ask me first" (ASK): every paid call asks, whatever its price; free ones never do."""
        first = spending.Settings(extras=spending.ASK)
        self.assertEqual(self.decide(managed(6), settings=first), spending.COST)
        self.assertEqual(self.decide(own_key(), settings=first, vendors=["OpenAI"]), spending.COST)
        self.assertEqual(self.decide(managed(0), settings=first), spending.RUN)
        self.assertEqual(spending.ask_lines(spending.COST, managed(6), spending.new_ledger(), first)[0],
                         "You asked to approve every paid call.")
        self.assertIn("and they approve every paid call", spending.declined(spending.COST, managed(6), first))

    def test_only_when_i_ask_refuses_a_paid_call_not_asked_for_with_no_card(self):
        only = spending.Settings(extras=spending.ONLY_ASKED)
        self.assertEqual(self.decide(managed(6), settings=only, asked=False), spending.NOT_ASKED)
        self.assertEqual(self.decide(own_key(), settings=only, asked=False, vendors=["OpenAI"]), spending.NOT_ASKED)
        self.assertEqual(self.decide(managed(0), settings=only, asked=False), spending.RUN, "free never asks")
        self.assertEqual(self.decide(managed(6), settings=only, asked=True), spending.RUN)
        self.assertEqual(self.decide(managed(120), settings=only, asked=True), spending.COST, "Ask above still asks")
        self.assertEqual(self.decide(managed(6), asked=False), spending.RUN, "Use when it helps: as before")
        self.assertEqual(spending.declined(spending.NOT_ASKED, managed(6), only),
                         "Not run: the user wants paid tools only when they ask for one. Nothing ran and nothing was "
                         "charged. Go on without it; you may say in one line what it would add.")

    def test_a_call_is_asked_for_when_the_latest_message_names_its_kind_or_result(self):
        for tool, text, asked in (
                ("sfx", "add some rain ambience", True), ("sfx", "model a typewriter", False),
                ("sfx", "no sound please", False), ("sfx", "don't add music", False),
                ("sfx", "rain sounds, without music", True), ("image_generate", "make a picture of the axe first", True),
                ("image_to_3d", "turn the photo into a 3D model", True), ("image_to_3d", "turn it into 3D", True),
                ("image_to_3d", "make me a 3D lantern", False), ("retopology", "make it game-ready", True),
                ("motion_from_text", "animate him walking", True), ("render", "render it in the cloud", True),
                ("auto_rig", "rig my character", True), ("auto_rig", "a bright room", False),
                ("sfx", "use the paid tools if you like", True), ("voice_line", "add a voiceover", True),
                ("lip_sync", "model a typewriter", False), ("sfx", None, False), ("", "sound", False)):
            with self.subTest(tool=tool, text=text):
                self.assertEqual(spending.asked_for(tool, text), asked)

    def test_ask_every_time_asks_for_paid_and_own_key_calls(self):
        every = spending.Settings(ask_every=True)
        self.assertEqual(self.decide(managed(1), settings=every), spending.COST)
        self.assertEqual(self.decide(own_key(), settings=every, vendors=["OpenAI"]), spending.COST)
        self.assertEqual(self.decide(managed(0), settings=every), spending.RUN)

    def test_own_keys_ask_once_per_chat_per_vendor_within_the_burst_guard(self):
        vendors = []
        self.assertEqual(self.decide(own_key(), vendors=vendors), spending.VENDOR)
        spending.allow(spending.VENDOR, own_key(), spending.new_ledger(), S, agent.Turn(), vendors)
        self.assertEqual(vendors, ["OpenAI"])
        self.assertEqual(self.decide(own_key(), vendors=vendors), spending.RUN)
        self.assertEqual(self.decide(own_key("elevenlabs"), vendors=vendors), spending.VENDOR)
        self.assertEqual(self.decide(own_key(), vendors=vendors, calls=5), spending.BURST_ASK)
        full = {"credits": 150, "auto": 150, "allowed": 0}
        self.assertEqual(self.decide(own_key(), full, vendors=vendors), spending.RUN, "own keys spend no credits")
        self.assertEqual(spending.start(full, own_key(), True), 0)
        self.assertEqual(full["credits"], 150)

    def test_quoted_tools_go_through_the_same_rule(self):
        quote = {"quote_id": "qt_1", "credits": 20, "expires_at": "later"}
        self.assertEqual(self.decide(managed(100, quote=quote)), spending.RUN)
        self.assertEqual(self.decide(managed(100, quote={**quote, "credits": 80})), spending.COST)
        self.assertEqual(self.decide(managed(100, server=0, quote=quote)), spending.COST)
        self.assertEqual(self.decide(managed(100, quote=quote), {"credits": 140, "auto": 140, "allowed": 0}),
                         spending.BUDGET)

    def test_cost_card_names_the_limit_and_the_balance_before_and_after(self):
        lines = spending.ask_lines(spending.COST, managed(80), spending.new_ledger(), S, balance=1840)
        self.assertEqual(lines, ["Over 25 credits, so it asks first.", "Balance 1,840 → 1,760 credits"])
        short = spending.ask_lines(spending.COST, managed(80), spending.new_ledger(), S, balance=30)
        self.assertIn("You have 30 credits; this needs 80.", short)
        self.assertEqual(spending.ask_lines(spending.COST, managed(80, server=0), spending.new_ledger(), S)[0],
                         "Every paid call asks on this account.")
        vendor = spending.ask_lines(spending.VENDOR, own_key(), spending.new_ledger(), S, balance=500)
        self.assertEqual(vendor, ["Uses your own OpenAI key: your OpenAI account pays, not credits.",
                                  "Allow OpenAI calls in this chat?"])

    def test_declined_result_says_why_and_that_the_turn_goes_on(self):
        text = spending.declined(spending.COST, managed(80), S)
        self.assertIn("declined this paid call (it costs 80 credits, over the 25 credits", text)
        self.assertIn("Nothing ran and nothing was charged", text)
        self.assertIn("150-credit budget", spending.declined(spending.BUDGET, managed(6), S))
        self.assertIn("paid call 6 in this turn", spending.declined(spending.BURST_ASK, managed(6), S, calls=5))
        self.assertIn("daily credit limit", spending.declined(spending.DAILY, managed(6), S))


class ConsentTest(unittest.TestCase):
    """agent._Consent: each limit pauses the turn on a card; Approve continues, Skip declines only that call."""

    def setUp(self):
        self.session = state.new_session()
        self.turn = agent.Turn()
        self.turn.spending = S
        for target in (patch.object(agent, "_redraw"), patch.object(conversations, "save")):
            target.start()
            self.addCleanup(target.stop)

    def consent(self, card=None, **options):
        card = card if card is not None else state.item_tool("loopcut_cloud__image_generate", "Generate image", "")
        return card, agent._Consent(self.session, self.turn, card, **options)

    def answer(self, approved):
        return patch.object(agent, "_wait_for_approval", return_value=approved)

    def test_ask_me_first_brings_the_card_back_for_a_6_credit_picture(self):
        self.turn.spending = spending.Settings(extras=spending.ASK)
        card, consent = self.consent()
        seen = {}
        with patch.object(agent, "_wait_for_approval", side_effect=lambda turn: seen.update(card) or True):
            self.assertTrue(consent({**managed(6), "balance": 20}))
        self.assertEqual((seen["status"], seen["ask"], seen["ask_label"]), ("awaiting", spending.COST, "Approve"))
        self.assertIn("You asked to approve every paid call.", seen["ask_lines"])
        self.assertEqual(self.session["spend"], {"credits": 6, "auto": 0, "allowed": 0}, "approved, not auto")

    def test_only_when_i_ask_refuses_an_extra_with_no_card_and_runs_what_the_user_asked_for(self):
        self.turn.spending = spending.Settings(extras=spending.ONLY_ASKED)
        self.session["items"].append(state.item_user("model a typewriter"))
        card, consent = self.consent(state.item_tool("loopcut_cloud__sfx", "Sound effect", ""))
        with self.answer(True) as wait, self.assertRaises(cloud_http.CloudError) as caught:
            consent(managed(4))
        wait.assert_not_called()
        self.assertEqual(caught.exception.code, "declined")
        self.assertEqual(cloud.error_text(caught.exception), "declined: Not run: the user wants paid tools only when "
                         "they ask for one. Nothing ran and nothing was charged. Go on without it; you may say in "
                         "one line what it would add.")
        self.assertTrue(card["declined"])
        self.assertEqual(self.session["spend"]["credits"], 0)
        self.session["items"].append(state.item_user("add the sound of keys clacking"))
        card, consent = self.consent(state.item_tool("loopcut_cloud__sfx", "Sound effect", ""))
        with self.answer(True) as wait:
            self.assertTrue(consent(managed(4)))
        wait.assert_not_called()
        self.assertEqual(self.session["spend"]["credits"], 4)
        card, consent = self.consent(state.item_tool("loopcut_cloud__image_to_3d", "Image to 3D model", ""),
                                     clicked=True, shown=80)
        self.assertTrue(consent(managed(80)), "the user's own click asked for it")

    def test_a_call_is_implied_when_the_user_neither_asked_for_it_nor_approved_its_card(self):
        """Undone means no counts only what the model added on its own."""
        self.session["items"].append(state.item_user("model a typewriter"))
        cases = ((managed(4), {}, True), (managed(120), {}, False),  # 120 asked on its card: approved.
                 (managed(80), {"clicked": True, "shown": 80}, False), (managed(4), {"chosen": True}, False))
        for details, options, implied in cases:
            with self.subTest(details=details["credits"], options=options), self.answer(True):
                _, consent = self.consent(state.item_tool("loopcut_cloud__sfx", "Sound effect", ""), **options)
                self.assertTrue(consent(details))
                self.assertIs(consent.implied, implied)
        self.session["items"].append(state.item_user("add typing sounds"))
        _, consent = self.consent(state.item_tool("loopcut_cloud__sfx", "Sound effect", ""))
        self.assertTrue(consent(managed(4)))
        self.assertFalse(consent.implied, "the user asked for it")
        self.assertEqual(spending.undone_text("sfx"), "Not run: the user removed the last sound you added; don't add "
                                                      "it again unless they ask. Nothing was charged.")
        self.assertEqual((spending.kind_noun("motion_from_text"), spending.kind_noun("motion_from_video")),
                         ("motion", "motion"), "one kind")

    def test_small_call_runs_unasked_and_is_counted(self):
        card, consent = self.consent()
        with self.answer(True) as wait:
            self.assertTrue(consent(managed(6)))
        wait.assert_not_called()
        self.assertEqual(card["cost"], "About 6 credits ($0.06)")
        self.assertEqual(self.session["spend"], {"credits": 6, "auto": 6, "allowed": 0})
        self.assertEqual(self.turn.paid_calls, 1)

    def test_over_ask_above_pauses_and_approve_runs_this_call(self):
        card, consent = self.consent()
        seen = {}

        def wait(turn):
            seen.update(card)
            return True
        with patch.object(agent, "_wait_for_approval", side_effect=wait):
            self.assertTrue(consent({**managed(80), "balance": 1840}))
        self.assertEqual(seen["status"], "awaiting")
        self.assertEqual(seen["ask"], spending.COST)
        self.assertIn("Balance 1,840 → 1,760 credits", seen["ask_lines"])
        self.assertTrue(seen["paid_approval"])
        self.assertEqual(card["status"], "running")
        self.assertNotIn("ask", card)
        self.assertEqual(self.session["spend"], {"credits": 80, "auto": 0, "allowed": 0}, "approved, not auto")

    def test_a_card_back_from_its_approval_shows_its_time_from_the_answer(self):
        """The running card's time (ui.layout._running_time) counted the wait for the user's answer as the step's."""
        for details in ({**managed(80), "balance": 1840}, {**managed(6), "file_access": True, "required": True}):
            with self.subTest(file_access="file_access" in details):
                card, consent = self.consent()
                card.update(status="running", started_at=1000.0)
                with self.answer(True), patch.object(agent.time, "time", return_value=1600.0):
                    self.assertTrue(consent(details))
                self.assertEqual((card["status"], card["started_at"]), ("running", 1600.0))

    def test_skip_declines_only_this_call_with_the_reason(self):
        card, consent = self.consent()
        with self.answer(False), self.assertRaises(cloud_http.CloudError) as caught:
            consent(managed(80))
        self.assertEqual(caught.exception.code, "declined")
        self.assertIn("The user declined this paid call", str(caught.exception))
        self.assertEqual((card["status"], card["declined"]), ("rejected", True))
        self.assertFalse(self.turn.cancel.is_set(), "the turn goes on")
        self.assertTrue(self.turn.declined, "the router's nudge leaves the next move to the user (router.nudge)")
        self.assertEqual(self.session["spend"]["credits"], 0)
        self.assertEqual(self.turn.paid_calls, 0)
        _, consent = self.consent()
        self.assertTrue(consent(managed(6)), "the next small call still runs")

    def test_a_declined_file_upload_marks_the_turn_declined(self):
        card, consent = self.consent(asks=True)
        with self.answer(False):
            self.assertFalse(consent({"required": True, "text": "Upload a.png", "credits": None, "file_access": True}))
        self.assertEqual(card["status"], "rejected")
        self.assertTrue(self.turn.declined)

    def test_a_declined_call_reaches_the_model_as_its_tool_result(self):
        # The whole turn is in test_agent_loop: Skip, the declined result, and the turn going on.
        error = cloud_http.CloudError(spending.declined(spending.COST, managed(80), S), "declined")
        self.assertTrue(cloud.error_text(error).startswith("declined: The user declined this paid call"))

    def test_chat_budget_pauses_and_allow_continues(self):
        self.session["spend"] = {"credits": 148, "auto": 148, "allowed": 0}
        card, consent = self.consent()
        seen = {}
        with patch.object(agent, "_wait_for_approval", side_effect=lambda turn: seen.update(card) or True):
            self.assertTrue(consent(managed(6)))
        self.assertEqual(seen["ask"], spending.BUDGET)
        self.assertEqual(seen["ask_label"], "Allow 150 more")
        self.assertIn("This chat has used 148 credits. Allow 150 more?", seen["ask_lines"])
        self.assertEqual(self.session["spend"], {"credits": 154, "auto": 154, "allowed": 150})
        _, consent = self.consent()
        with self.answer(False) as wait:
            self.assertTrue(consent(managed(6)))
        wait.assert_not_called()

    def test_chat_budget_skip_declines_and_keeps_the_budget(self):
        self.session["spend"] = {"credits": 148, "auto": 148, "allowed": 0}
        _, consent = self.consent()
        with self.answer(False), self.assertRaises(cloud_http.CloudError) as caught:
            consent(managed(6))
        self.assertIn("150-credit budget", str(caught.exception))
        self.assertEqual(self.session["spend"], {"credits": 148, "auto": 148, "allowed": 0})

    def test_burst_guard_pauses_the_sixth_paid_call(self):
        with self.answer(True) as wait:
            for _ in range(5):
                self.assertTrue(self.consent()[1](managed(4)))
            wait.assert_not_called()
            card, consent = self.consent()
            self.assertTrue(consent(managed(4)))
            wait.assert_called_once()
        self.assertEqual(self.turn.paid_calls, 6)
        self.assertEqual(self.turn.burst_limit, 10)

    def test_burst_then_budget_ask_in_turn_and_both_must_allow(self):
        self.turn.paid_calls = 5
        self.session["spend"] = {"credits": 148, "auto": 148, "allowed": 0}
        kinds = []
        card, consent = self.consent()
        with patch.object(agent, "_wait_for_approval", side_effect=lambda turn: kinds.append(card["ask"]) or True):
            self.assertTrue(consent(managed(6)))
        self.assertEqual(kinds, [spending.BURST_ASK, spending.BUDGET])

    def test_run_without_asking_never_skips_a_paid_approval(self):
        self.session["auto_run"] = True
        _, consent = self.consent(files_allowed=True)
        with self.answer(True) as wait:
            consent(managed(80))
        wait.assert_called_once()

    def test_own_key_asks_once_per_chat_and_vendor_approvals_are_not_saved(self):
        with self.answer(True) as wait:
            self.consent()[1](own_key())
            self.consent()[1](own_key())
            self.assertEqual(wait.call_count, 1)
            self.consent()[1](own_key("gemini"))
            self.assertEqual(wait.call_count, 2)
        self.assertEqual(self.session["spend"]["credits"], 0, "own keys spend no credits")
        self.assertEqual(self.turn.paid_calls, 3, "but count in the burst guard")
        self.assertNotIn("spend_vendors", conversations._PERSISTED)

    def test_a_worker_with_no_user_turn_cannot_approve(self):
        _, consent = self.consent(asks=False)
        with self.answer(True) as wait:
            self.assertFalse(consent(managed(80)))
        wait.assert_not_called()

    def test_receipt_replaces_the_hold_once(self):
        card, consent = self.consent()
        consent(managed(20))
        consent({"charged": 18, "text": "Used 18 credits ($0.18)"})
        consent({"charged": 18, "text": "Used 18 credits ($0.18)"})
        self.assertEqual(self.session["spend"], {"credits": 18, "auto": 18, "allowed": 0})
        self.assertEqual(card["cost"], "Used 18 credits ($0.18)")
        free_card, free = self.consent()
        free(managed(0))
        free({"charged": 0, "text": "No charge"})
        self.assertEqual(free_card["cost"], "Free")


class MadePictureTest(unittest.TestCase):
    """Owner, 2026-10-26 (0.2.20): image_to_3d on a picture Loopcut made asks as any paid call does, with no card of
    its own; Loopcut checks the picture itself (cloud_jobs.made_seen, test_cloud_e2e). The picture's own button
    approves the price it showed."""

    setUp, consent, answer = ConsentTest.setUp, ConsentTest.consent, ConsentTest.answer

    def made(self, credits=80, server=100, path="/tmp/made.png"):
        details = managed(credits, server, quote={"quote_id": "qt_1", "credits": credits,
                                                  "expires_at": "2099-01-01T00:00:00Z"})
        return {**details, "balance": 100, "made_picture": {"name": "Loopcut job_1 image", "path": path}}

    def test_a_made_pictures_3d_follows_the_usual_thresholds(self):
        default = spending.Settings()
        for credits, settings, kind in ((80, default, spending.RUN), (25, default, spending.RUN),
                                        (0, default, spending.RUN), (101, default, spending.COST),
                                        (80, S, spending.COST), (80, spending.Settings(ask_every=True), spending.COST)):
            with self.subTest(credits=credits, settings=settings):
                self.assertEqual(spending.decide(self.made(credits), spending.new_ledger(), settings), kind)
        self.assertFalse(hasattr(spending, "MADE"), "no always-ask card")
        self.assertEqual(spending.skip_label(spending.COST), "Skip this")
        self.assertEqual(spending.ask_title(spending.COST, self.made()), "")

    def test_at_the_default_the_models_80_credit_conversion_runs_without_a_card(self):
        self.turn.spending = spending.Settings()
        card, consent = self.consent(state.item_tool("loopcut_cloud__image_to_3d", "Image to 3D model", ""))
        with self.answer(True) as wait:
            self.assertTrue(consent(self.made()))
        wait.assert_not_called()
        self.assertEqual(card["status"], "running")
        self.assertEqual(self.session["spend"], {"credits": 80, "auto": 80, "allowed": 0})

    def test_the_pictures_button_approves_the_price_it_showed_and_no_card_asks(self):
        card, consent = self.consent(state.item_tool("loopcut_cloud__image_to_3d", "Image to 3D model", ""),
                                     clicked=True, shown=80)
        with self.answer(True) as wait, patch.object(agent, "_wait_for_card") as card_wait:
            self.assertTrue(consent(self.made()))
            self.assertTrue(consent(self.made(25)), "a lower quote too")
        wait.assert_not_called()
        card_wait.assert_not_called()
        self.assertEqual(card["status"], "running")
        self.assertEqual(self.session["spend"], {"credits": 0, "auto": 0, "allowed": 0},
                         "nothing held: the charge goes to its chat when the call ends (agent.turn_into_3d)")
        consent({"charged": 80, "text": "Used 80 credits ($0.80)"})
        self.assertEqual((consent.charged, card["cost"]), (80, "Used 80 credits ($0.80)"))
        self.assertEqual(self.session["spend"]["credits"], 0)

    def asks_on_its_card(self, consent, card, details, answer=True):
        """A click's ask, answered on its card as the panel does (agent.decide with the drawn card), while a later
        turn's own card waits too. Returns (what the consent returned or raised, the later turn)."""
        later = agent.Turn()
        self.session["turn"] = later
        waiting = state.awaiting(state.item_tool("run_python", "Something else", ""))
        self.session["items"][:] = [waiting, card]
        out = []
        def run():
            try:
                out.append(consent(details))
            except Exception as ex:
                out.append(ex)
        thread = threading.Thread(target=run)
        with patch.object(agent, "_POLL_SECONDS", .01), patch.object(state, "session", return_value=self.session):
            thread.start()
            deadline = time.monotonic() + 5
            while card.get("status") != "awaiting" and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(card["status"], "awaiting")
            self.assertTrue(agent.decide(answer, index=1, drawn=(id(card), card["ask_serial"])))
            thread.join(5)
        self.assertFalse(thread.is_alive(), "answered")
        return out[0], later

    def test_a_quote_over_the_price_the_button_showed_asks_on_its_own_card(self):
        """The button showed 25; the quote says 80: the click did not approve that, so its card asks, and its
        answer is its own, never the turn running meanwhile's."""
        card, consent = self.consent(state.item_tool("loopcut_cloud__image_to_3d", "Image to 3D model", ""),
                                     clicked=True, shown=25)
        result, later = self.asks_on_its_card(consent, card, self.made(80))
        self.assertIs(result, True)
        self.assertFalse(later.decided.is_set(), "the later turn's card was not answered by it")
        self.assertEqual(card["status"], "running")

    def test_a_daily_limit_approval_that_showed_the_price_is_the_conversions_approval(self):
        card, consent = self.consent(state.item_tool("loopcut_cloud__image_to_3d", "Image to 3D model", ""))
        kinds = []
        with patch.object(agent, "_wait_for_approval", side_effect=lambda turn: kinds.append(card["ask"]) or True):
            limit = {**self.made(), "limit": spending.DAILY, "required": True, "message": "Today's limit is reached."}
            self.assertTrue(consent(limit))
            self.assertTrue(consent(self.made()))
        self.assertEqual(kinds, [spending.DAILY])


class WorkerTest(unittest.TestCase):
    """A worker spends from the user's chat; its asks show in the user's panel, named, and it waits."""

    def setUp(self):
        self.main = state.new_session()
        self.main_turn = agent.Turn()
        self.main_turn.spending = S
        self.worker_turn = agent.Turn()
        self.worker_turn.chat = (self.main, self.main_turn)
        self.worker_turn.worker = "worker chair (props)"
        self.worker_session = state.new_session()
        for target in (patch.object(agent, "_redraw"), patch.object(conversations, "save"),
                       patch.object(agent, "_POLL_SECONDS", 0.01), patch.object(state, "session", return_value=self.main)):
            target.start()
            self.addCleanup(target.stop)

    def consent(self):
        card = state.item_tool("loopcut_cloud__image_to_3d", "Image to 3D", "")
        self.worker_session["items"].append(card)
        return card, agent._Consent(self.worker_session, self.worker_turn, card, asks=False)

    def test_worker_spend_counts_against_the_chat_budget(self):
        self.main["spend"] = {"credits": 140, "auto": 140, "allowed": 0}
        _, consent = self.consent()
        self.assertTrue(consent(managed(6)))
        self.assertEqual(self.main["spend"]["auto"], 146)
        self.assertEqual(self.worker_session["spend"]["credits"], 0, "never a budget of its own")
        self.assertEqual(self.main_turn.paid_calls, 1)

    def ask_in_thread(self, details):
        _, consent = self.consent()
        outcome = {}

        def run():
            try:
                outcome["result"] = consent(details)
            except Exception as ex:  # noqa: BLE001 - reported to the test
                outcome["error"] = ex
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not any(i.get("status") == "awaiting" for i in self.main["items"]):
            self.assertLess(time.monotonic(), deadline, "the worker's ask never reached the user's panel")
            time.sleep(0.01)
        return thread, outcome

    def test_worker_ask_shows_in_the_main_panel_named_and_the_worker_waits(self):
        self.main["spend"] = {"credits": 148, "auto": 148, "allowed": 0}
        thread, outcome = self.ask_in_thread(managed(6))
        index, card = next((n, i) for n, i in enumerate(self.main["items"]) if i.get("status") == "awaiting")
        self.assertEqual(card["ask"], spending.BUDGET)
        self.assertTrue(card["summary"].startswith("worker chair (props): Image to 3D"))
        self.assertEqual(card["ask_lines"][0], "Asked by worker chair (props), working in the background.")
        time.sleep(0.05)
        self.assertTrue(thread.is_alive(), "the worker waits for the answer")
        self.assertTrue(agent.decide(True, index=index))
        thread.join(5)
        self.assertEqual(outcome, {"result": True})
        self.assertEqual(card["status"], "done")
        self.assertEqual(self.main["spend"]["auto"], 154)

    def test_worker_skip_declines_its_call_without_stopping_the_turn(self):
        thread, outcome = self.ask_in_thread(managed(80))
        self.assertTrue(agent.decide(False))
        thread.join(5)
        self.assertEqual(outcome["error"].code, "declined")
        self.assertFalse(self.main_turn.cancel.is_set())
        self.assertEqual(self.main["items"][-1]["status"], "rejected")

    def test_a_main_card_and_a_worker_card_each_keep_their_own_answer(self):
        main_card = state.item_tool("loopcut_cloud__render", "Render", "")
        main_card["status"] = "awaiting"
        self.main["items"].append(main_card)
        self.main["turn"] = self.main_turn
        thread, outcome = self.ask_in_thread(managed(80))
        worker_index = len(self.main["items"]) - 1
        self.assertTrue(agent.decide(True, index=worker_index))
        thread.join(5)
        self.assertEqual(outcome, {"result": True})
        self.assertFalse(self.main_turn.decided.is_set(), "the main card is still waiting")

    def test_stop_ends_a_waiting_worker_ask(self):
        thread, outcome = self.ask_in_thread(managed(80))
        self.main_turn.cancel.set()
        thread.join(5)
        self.assertIsInstance(outcome["error"], agent.llm.Cancelled)
        self.assertEqual(self.main["items"][-1]["status"], "rejected")

    def test_waves_give_worker_turns_the_users_chat(self):
        from loopcut import waves
        wave = types.SimpleNamespace(session=self.main, turn=types.SimpleNamespace(
            roles=types.SimpleNamespace(worker=config.Config(api_key="k", base_url="https://x.test", model="m",
                                                             reasoning_effort="low", auto_run=False, auto_look=False,
                                                             max_steps=0, context_budget=24000))))
        worker = types.SimpleNamespace(wave=wave, id="chair", brief={"role": "props"})
        turn = waves.Worker._new_turn(worker, threading.Event())
        self.assertIs(turn.chat[0], self.main)
        self.assertEqual(turn.worker, "worker chair (props)")


class ServerRefusalTest(unittest.TestCase):
    """daily_limit_reached and rate_limited with a confirm_token pause on a card; Approve retries with it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.posts, self.quotes, self.quote_refusals = [], [], []
        self.refusals = ["daily_limit_reached"]
        self.runtime = types.SimpleNamespace(lock=threading.RLock(), cache_dir=Path(self.temp.name), quotes={},
                                             quote_requests={}, handle_error=lambda error: None,
                                             manifest={"account": {"credits": {"balance": 500}}})
        self.client = cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=self.transport,
                                        client_headers={"x-loopcut-contract": "0.1", "x-loopcut-client": "test"})
        self.session, self.turn = state.new_session(), agent.Turn()
        self.card = state.item_tool("loopcut_cloud__image_generate", "Generate image", "")
        for target in (patch.object(agent, "_redraw"), patch.object(cloud_jobs, "_redraw"),
                       patch.object(conversations, "save"), patch.object(cloud_jobs, "_registry", {})):
            target.start()
            self.addCleanup(target.stop)

    def transport(self, request, *, timeout):
        headers = {k.lower(): v for k, v in request.header_items()}
        if request.full_url.endswith("/blobs/check"):
            return Reply({"missing": []})
        body = json.loads(request.data)
        if request.full_url.endswith("/v1/quotes"):
            self.quotes.append(headers)
            if self.quote_refusals:
                self.quote_refusals.pop(0)
                return Reply({"error": {"code": "daily_limit_reached", "type": "limit", "confirm_token": TOKEN,
                                        "message": "Today's cloud spending limit of 100 credits is reached."}}, 402)
            return Reply({"quote_id": "qt_1", "credits": 20, "expires_at": "2026-10-16T01:00:00Z"})
        assert request.full_url.endswith("/v1/jobs"), request.full_url
        self.posts.append((headers, body))
        if self.refusals:
            code = self.refusals.pop(0)
            message = ("Too many paid jobs: more than 30 in 10 minutes; retry after 42 seconds." if code == "rate_limited"
                       else "Today's cloud spending limit of 100 credits is reached. Change it at https://loopcut.org/account")
            return Reply({"error": {"code": code, "type": "limit", "message": message, "confirm_token": TOKEN}},
                         status=429 if code == "rate_limited" else 402)
        return Reply({"id": "job_ok", "tool": "image_generate", "status": "succeeded",
                      "progress": {"pct": 100, "message": "Done"},
                      "credits": {"reserved": 6, "charged": 6, "refunded": 0}, "outputs": [], "error": None,
                      "provenance": {"job_id": "job_ok", "tool": "image_generate", "vendor": "test", "model": "m",
                                     "kind": "gen", "created_at": "2026-10-16T00:00:00Z"}})

    def call(self, approved, quoted=False):
        descriptor = {"name": "image_generate", "version": 1, "backend": "managed", "kind": "gen", "latency": "seconds",
                      "cost": {"credits_estimate": 6, "quote_required": quoted}, "file_inputs": [],
                      "approval": {"confirm_over_credits": 100}, "title": "Generate image"}
        seen = {}
        consent = agent._Consent(self.session, self.turn, self.card)
        wait = patch.object(agent, "_wait_for_approval", side_effect=lambda turn: seen.update(self.card) or approved)
        with wait as waited, cloud_account.approval_context(consent):
            arguments = cloud_account.authorize(cloud_jobs, descriptor, {"prompt": "a lamp"}, {}, project=PROJECT,
                                                cancel=threading.Event(), runtime=self.runtime, client=self.client)
            try:
                return cloud_account.submit(cloud_jobs, descriptor, arguments, {}, project=PROJECT, on_progress=None,
                                            cancel=threading.Event(), runtime=self.runtime, client=self.client), \
                    seen, waited
            except cloud_http.CloudError as ex:
                return ex, seen, waited

    def test_daily_limit_pauses_and_approve_retries_with_the_token(self):
        job, seen, waited = self.call(True)
        self.assertEqual(job["status"], "succeeded")
        waited.assert_called_once()
        self.assertEqual(seen["ask"], spending.DAILY)
        self.assertEqual(seen["ask_link"], "https://loopcut.org/account")
        self.assertIn("Today's cloud spending limit of 100 credits is reached.", seen["ask_lines"])
        self.assertIn("Balance 500 → 480 credits", seen["ask_lines"])  # The quote's 20, not the 6 estimated.
        self.assertEqual(len(self.posts), 2)
        self.assertNotIn("x-loopcut-confirm", self.posts[0][0])
        self.assertEqual(self.posts[1][0]["x-loopcut-confirm"], TOKEN)
        self.assertEqual(self.posts[0][1], self.posts[1][1], "the same request, same idempotency key")
        self.assertEqual(self.card["status"], "running")
        self.assertEqual(cloud_account._pending(self.runtime), {})

    def test_a_refused_quote_asks_once_and_its_token_admits_the_job(self):
        # Quotes pass the same limits; the token is spent only when a job is admitted, so the job carries it.
        self.refusals, self.quote_refusals = [], ["daily_limit_reached"]
        job, seen, waited = self.call(True, quoted=True)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(waited.call_count, 1, "one card, the limit's: a 20-credit quote runs without asking")
        self.assertEqual([q.get("x-loopcut-confirm") for q in self.quotes], [None, TOKEN])
        self.assertEqual(self.posts[0][0]["x-loopcut-confirm"], TOKEN)
        self.assertEqual(self.posts[0][1]["quote_id"], "qt_1")

    def test_rate_limited_pauses_too(self):
        self.refusals = ["rate_limited"]
        job, seen, _ = self.call(True)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(seen["ask"], spending.RATE)
        self.assertEqual(seen["ask_lines"][:2], ["Many paid jobs in a short time.",
                                                 "Too many paid jobs: more than 30 in 10 minutes; retry after 42 seconds."])
        self.assertEqual(self.posts[1][0]["x-loopcut-confirm"], TOKEN)

    def test_skip_declines_without_a_retry_and_releases_the_retry_key(self):
        error, seen, _ = self.call(False)
        self.assertEqual(error.code, "declined")
        self.assertIn("daily credit limit", str(error))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(cloud_account._pending(self.runtime), {})
        self.assertFalse(self.turn.cancel.is_set())

    def test_one_retry_only(self):
        self.refusals = ["daily_limit_reached", "daily_limit_reached"]
        error, _, waited = self.call(True)
        self.assertEqual(error.code, "daily_limit_reached")
        self.assertEqual((len(self.posts), waited.call_count), (2, 1))

    def test_without_a_token_the_refusal_is_a_readable_card(self):
        error = cloud_http.CloudError("Today's cloud spending limit of 1000 credits is reached. Change it at "
                                      "https://loopcut.org/account", "daily_limit_reached", 402)
        details = cloud_account.error_details(error)
        self.assertEqual(details["message"], "Today's cloud spending limit of 1000 credits is reached.")
        self.assertEqual((details["link"], details["link_label"]), ("https://loopcut.org/account", "Change daily limit ↗"))
        self.assertIn("Do not retry paid tools today", details["text"])
        bare = cloud_http.CloudError("", "daily_limit_reached", 402, {"daily_limit": 1000, "spent_today": 1000,
                                                                       "url": "javascript:alert(1)"})
        self.assertEqual(cloud_account.error_details(bare)["message"],
                         "Today's credit limit is reached (1,000 of 1,000 credits today).")
        self.assertTrue(cloud_account.error_details(bare)["link"].startswith(config.SERVICE_URL + "/account"))
        self.assertNotIn(TOKEN, cloud.error_text(cloud_http.CloudError("x", "daily_limit_reached", 402,
                                                                        {"confirm_token": TOKEN})))

    def test_server_burst_guard_card_and_a_vendor_429_stay_apart(self):
        server = cloud_account.error_details(cloud_http.CloudError("Slow down", "rate_limited", 429, {"retry_after": 7}))
        self.assertTrue(server["message"].startswith("Too many paid jobs in a short time."))
        self.assertTrue(server["stop"])
        self.assertIn("retry after 7 seconds", server["text"])
        vendor = cloud_http.CloudError("Vendor busy", "rate_limited", None, {"retry_after": 13})
        self.assertIsNone(cloud_account.error_details(vendor))


class Reply(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status, self.headers = status, {"Content-Type": "application/json"}


class ChargeTest(unittest.TestCase):
    """Running jobs: the hold shows, Cancel returns it, and a finished job says it can't be canceled."""

    def setUp(self):
        self.session, self.turn = state.new_session(), agent.Turn()
        for target in (patch.object(agent, "_redraw"), patch.object(cloud_jobs, "_redraw"),
                       patch.object(conversations, "save"), patch.object(cloud_jobs, "_registry", {})):
            target.start()
            self.addCleanup(target.stop)

    @staticmethod
    def job(status, reserved=80, charged=0):
        return {"id": "job_r", "tool": "render", "status": status, "progress": {"pct": 40, "message": "Rendering"},
                "credits": {"reserved": reserved, "charged": charged, "refunded": reserved - charged}, "outputs": []}

    def test_rows_show_the_hold_and_whether_cancel_still_works(self):
        running = cloud_jobs.progress_rows({"job_r": {"job": self.job("running"), "title": "Render"}})
        self.assertEqual((running[0]["note"], running[0]["cancelable"]), ("80 credits held. Cancel returns them.", True))
        downloading = cloud_jobs.progress_rows({"job_r": {"job": self.job("succeeded", charged=80), "title": "Render",
                                                          "ready": False}})
        self.assertEqual((downloading[0]["note"], downloading[0]["cancelable"]),
                         ("Finished and charged; it can't be canceled now.", False))

    def test_cancel_on_a_running_job_returns_its_hold_to_the_chat(self):
        card = state.item_tool("loopcut_cloud__render", "Render", "")
        consent = agent._Consent(self.session, self.turn, card)
        self.turn.spending = spending.Settings(ask_above=100)
        consent(managed(80, server=100))
        self.assertEqual(self.session["spend"]["credits"], 80)
        cancel = threading.Event()
        cloud_jobs._registry["job_r"] = {"title": "Render", "job": self.job("running"), "cancel": cancel, "ready": False}
        with cloud_account.approval_context(consent):
            report = cloud_account.charge_reporter()
        self.assertTrue(cloud_jobs.watch("job_r", lambda job, error: cloud._settle_final(report, job, error)))
        self.assertTrue(cloud_jobs.cancel_job("job_r"))
        self.assertTrue(cancel.is_set())
        canceled = self.job("canceled", charged=0)
        error = cloud_http.CloudError("Cloud job canceled; unspent credits were refunded", "canceled",
                                      extra={"job": canceled, "refunded": 80})
        cloud_jobs._registry["job_r"].update(job=canceled, error=error)
        cloud_jobs._final("job_r", canceled, error)
        self.assertEqual(self.session["spend"], {"credits": 0, "auto": 0, "allowed": 0})
        self.assertEqual(card["cost"], "Canceled: its credits were returned")
        self.assertFalse(cloud_jobs.cancel_job("job_r"), "an ended job can't be canceled, and the panel says so")

    def test_watching_a_job_that_already_ended_settles_at_once(self):
        calls = []
        cloud_jobs._registry["job_r"] = {"title": "Render", "job": self.job("succeeded", charged=72), "ready": True,
                                         "cancel": threading.Event()}
        cloud_jobs.watch("job_r", lambda job, error: calls.append((job["credits"]["charged"], error)))
        self.assertEqual(calls, [(72, None)])

    def test_failures_release_a_hold_only_when_nothing_was_charged(self):
        reports = []
        report = lambda charged, text="": reports.append(charged)  # noqa: E731
        cloud._settle_error(report, cloud_http.CloudError("declined", "declined"))
        cloud._settle_error(report, cloud_http.CloudError("Refused", "insufficient_credits", 402))
        cloud._settle_error(report, cloud_http.CloudError("Lost", "connection_failed"))
        cloud._settle_error(report, cloud_http.CloudError("Server", "http_error", 503))
        running = {"status": "running", "credits": {"reserved": 6, "charged": 0, "refunded": 0}}
        cloud._settle_error(report, cloud_http.CloudError("Unsure", "job_unconfirmed", 503, {"job": running}))
        self.assertEqual(reports, [0, 0], "a job 5xx answers left unconfirmed may still be charged: its hold stays")

    def test_a_job_kept_with_its_chat_titles_its_card_with_the_charge_its_record_settles(self):
        """The card's title (agent._priced_title) and the turn's line (chat_credits.turn_line) agree on the settled
        charge: rel019-cloud2's render, 29 quoted and held, 5 charged."""
        from loopcut import cloud_restore
        card = state.item_tool("loopcut_cloud__render", "Render scene" + agent.QUOTING, "")
        consent = agent._Consent(self.session, self.turn, card)
        self.turn.spending = spending.Settings(ask_above=100)
        consent(managed(29, server=100))
        self.assertEqual((card["summary"], self.session["spend"]["credits"]), ("Render scene · 29 credits", 29))
        with cloud_account.approval_context(consent):
            report = cloud_account.charge_reporter()
        job = {"id": "job_r", "status": "succeeded", "credits": {"reserved": 29, "charged": 5, "refunded": 24}}
        with patch.object(cloud_restore, "settle") as settle:
            cloud._settle_saved(report, {"id": "job_r"}, job)
        settle.assert_called_once()
        self.assertEqual((card["summary"], card["cost"]), ("Render scene · 5 credits", "Used 5 credits ($0.05)"))
        self.assertEqual(self.session["spend"]["credits"], 29, "its record settles the chat's spend, once")

    def test_a_models_cancel_asks_on_its_card_and_keep_it_running_cancels_nothing(self):
        card = state.item_tool("loopcut_cloud__job_status", "Cloud job job_r", "")
        consent, shown = agent._Consent(self.session, self.turn, card), []
        question = {"title": "Cloud rendering", "held": 29, "pct": 93}
        with patch.object(consent, "_pause", lambda card, **fields: shown.append(fields) or False):
            self.assertFalse(consent({"cancel_job": question}))
        [fields] = shown
        self.assertEqual((fields["ask"], fields["ask_title"], fields["ask_label"], fields["ask_skip"]),
                         (spending.CANCEL, "Cancel this cloud job?", "Cancel it", "Keep it running"))
        self.assertEqual(fields["ask_lines"], ["Cloud rendering, 93% done. Loopcut's AI asked to cancel it.",
                                               "29 credits held; canceling returns them."])
        self.assertEqual((card["status"], card["declined"], self.turn.declined), ("rejected", True, True))
        self.assertIn("kept this cloud job running", spending.declined(spending.CANCEL, question, spending.Settings()))
        downloading = spending.ask_lines(spending.CANCEL, {**question, "pct": None, "charged": 29}, {}, S)
        self.assertEqual(downloading[-1], "It finished and was charged 29 credits; canceling stops its download.")
        with patch.object(consent, "_pause", lambda card, **fields: True):
            self.assertTrue(consent({"cancel_job": question}))
        self.assertEqual(card["status"], "running")
        self.assertEqual(spending.ledger(self.session)["credits"], 0, "asking holds nothing")

    def test_only_a_plain_request_to_stop_skips_the_cancel_card(self):
        for text in ("stop the render", "Cancel it please", "please cancel the cloud render, I'll do it here", "STOP"):
            self.assertTrue(spending.asked_to_stop(text), text)
        for text in ("why did it stop?", "don't cancel it", "don’t stop", "do not stop", "keep going", "stopped?",
                     "it looks wrong, but no need to stop", "render it in the cloud", "", None):
            self.assertFalse(spending.asked_to_stop(text), text)


class KeptHereTest(unittest.TestCase):
    """cloud_jobs.kept_here, what undone means no reads: the user still has a job's outputs in the open file."""

    class Block(dict):
        def __init__(self, job="", users=1):
            super().__init__()
            self.users = users
            if job:
                self["loopcut_provenance"] = json.dumps({"job_id": job, "tool": "sfx"})

    def kept(self, job="job_1", **data):
        scenes = data.pop("strips", None)
        fake = types.SimpleNamespace(data=types.SimpleNamespace(
            scenes=[types.SimpleNamespace(sequence_editor=types.SimpleNamespace(strips_all=scenes))] if scenes else [],
            **data))
        with patch.dict(sys.modules, {"bpy": fake}):
            return cloud_jobs.kept_here(job)

    def test_deleted_or_undone_outputs_are_gone_and_what_a_delete_leaves_does_not_count(self):
        B = self.Block
        self.assertTrue(self.kept(objects=[B("job_1")], meshes=[B("job_1")]), "its model is in the scene")
        self.assertFalse(self.kept(objects=[], meshes=[B("job_1", users=0)], materials=[B("job_1")]),
                         "its objects were deleted: the mesh and material left behind don't count")
        self.assertTrue(self.kept(strips=[B("job_1")], sounds=[B("job_1")]), "its sound strip is in the editor")
        self.assertFalse(self.kept(strips=[B()], sounds=[B("job_1", users=0)]), "its strip was deleted")
        self.assertTrue(self.kept(actions=[B("job_1", users=1)]), "its motion is on the character")
        self.assertFalse(self.kept(actions=[B("job_1", users=0)]))
        self.assertFalse(self.kept(objects=[B("job_2")], images=[B("job_2")]), "another job's")
        self.assertFalse(self.kept(), "Undo took everything")


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"LOOPCUT_DATA_DIR": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)

    def saved(self):
        session = state.new_session()
        session["items"].append(state.item_user("make a lamp"))
        session["messages"].append({"role": "user", "content": "make a lamp"})
        return session

    def test_chat_spend_survives_a_reload_and_vendor_consent_does_not(self):
        session = self.saved()
        session["spend"] = {"credits": 46, "auto": 40, "allowed": 150}
        session["spend_vendors"] = ["OpenAI"]
        conversations.save(session)
        loaded = conversations.load(session["id"])
        self.assertEqual(loaded["spend"], {"credits": 46, "auto": 40, "allowed": 150})
        self.assertEqual(loaded["spend_vendors"], [])

    def test_what_the_model_added_unasked_survives_a_reload_and_damaged_rows_go(self):
        """Undone means no holds for the chat, reopened too; only for this chat (a new one starts empty)."""
        session = self.saved()
        spending.note_implied(session, "sfx", "job_1")
        spending.note_implied(session, "sfx", "job_1")  # A replay of the same job.
        session["implied"].append({"tool": "image_generate", "job": "job_2", "removed": True})
        session["implied"] += [{"tool": 3, "job": "job_3"}, "junk", {"tool": "sfx", "job": ""}]
        conversations.save(session)
        loaded = conversations.load(session["id"])
        self.assertEqual(loaded["implied"], [{"tool": "sfx", "job": "job_1", "removed": False},
                                             {"tool": "image_generate", "job": "job_2", "removed": True}])
        self.assertEqual(state.new_session()["implied"], [])
        self.assertEqual(spending.implied_rows("lots"), [])
        for n in range(40):
            spending.note_implied(session, "sfx", f"job_{n + 10}")
        self.assertEqual(len(session["implied"]), spending.IMPLIED_KEEP)
        self.assertEqual(session["implied"][-1]["job"], "job_49", "the newest kept")

    def test_older_and_damaged_files_start_from_zero(self):
        session = self.saved()
        conversations.save(session)
        path = Path(self.temp.name) / "conversations" / session["id"] / "conversation.json"
        if not path.exists():
            path = next(Path(self.temp.name).rglob("conversation.json"))
        for spend in (None, {"credits": -5}, {"credits": "46"}, "lots"):
            with self.subTest(spend=spend):
                body = json.loads(path.read_text())
                body.pop("spend", None)
                if spend is not None:
                    body["spend"] = spend
                path.write_text(json.dumps(body))
                self.assertEqual(conversations.load(session["id"])["spend"], spending.new_ledger())


class PanelTest(unittest.TestCase):
    """The credits block and the ask cards at the panel's narrow width."""

    def build(self, session, width=260, **ui):
        return layout.build(session, width, 700, 1.0, measure, "fast", None,
                            {"selection": [], "context_budget": 24_000, "account": None, **ui})

    @staticmethod
    def texts(display):
        return {p["id"]: p.get("text") for p in display["prims"] if p.get("t") == "text"}

    def test_balance_chat_and_today_lines(self):
        session = state.new_session()
        session["spend"]["credits"] = 46
        narrow = self.texts(self.build(session, credits="1,840 credits ($18.40)", today="Today: 120 of 1,000 credits"))
        self.assertEqual(narrow["input.credits"], "1,840 credits ($18.40)")
        self.assertEqual(narrow["input.credits1"], "This chat: 46 credits")
        self.assertEqual(narrow["input.credits2"], "Today: 120 of 1,000 credits")
        wide = self.texts(self.build(session, 480, credits="1,840 credits ($18.40)", today="Today: 120 credits"))
        self.assertEqual(wide["input.credits1"], "This chat: 46 credits · Today: 120 credits")
        self.assertNotIn("input.credits2", wide)

    def test_this_chat_shows_without_a_balance_once_it_spent(self):
        session = state.new_session()
        self.assertNotIn("input.credits", self.texts(self.build(session)))
        session["spend"]["credits"] = 6
        self.assertEqual(self.texts(self.build(session))["input.credits"], "This chat: 6 credits")

    def test_today_line_with_and_without_the_server_fields(self):
        def manifest(spend):
            return {"account": {"spend": spend}}
        self.assertEqual(cloud_account.today_label(manifest({"daily_limit": 1000, "spent_today": 120,
                                                            "resets_at": "2026-10-17T00:00:00Z"}), True),
                         "Today: 120 of 1,000 credits")
        self.assertEqual(cloud_account.today_label(manifest({"daily_limit": None, "spent_today": 120}), True),
                         "Today: 120 credits")
        for absent in ({"account": {}}, {"account": None}, {}, manifest(None), manifest({"spent_today": "x"})):
            self.assertEqual(cloud_account.today_label(absent, True), "")
        self.assertEqual(cloud_account.today_label(manifest({"spent_today": 1}), False), "")

    def ask_card(self, kind, **changes):
        card = state.item_tool("loopcut_cloud__image_to_3d", "Image to 3D · About 80 credits ($0.80)", "")
        details = managed(80)
        card.update(status="awaiting", cost=details["text"], paid_approval=True, ask=kind,
                    ask_label=spending.approve_label(kind, S),
                    ask_lines=spending.ask_lines(kind, details, {"credits": 150, "auto": 150, "allowed": 0}, S,
                                                 calls=5, balance=1840), **changes)
        return card

    def test_ask_cards_fit_260_px_with_approve_and_skip(self):
        for kind in (spending.COST, spending.BUDGET, spending.BURST_ASK, spending.VENDOR, spending.DAILY):
            with self.subTest(kind=kind):
                session = state.new_session()
                session["items"].append(self.ask_card(kind, ask_link="https://loopcut.org/account")
                                        if kind == spending.DAILY else self.ask_card(kind))
                display = self.build(session)
                hits = {h["id"]: h for h in display["hits"]}
                self.assertEqual(hits["item0.tool.run"]["action"], ("approve", 0))
                self.assertEqual(hits["item0.tool.reject"]["action"], ("reject", 0))
                self.assertNotIn("item0.tool.always", hits)
                for hit in display["hits"]:
                    if hit["id"].startswith("item0"):
                        self.assertLessEqual(hit["x"] + hit["w"], 260 - T.PAD, hit["id"])
                texts = self.texts(display)
                self.assertTrue(any(t and t.startswith("Skip this") for t in texts.values()))
                if kind == spending.BUDGET:
                    self.assertTrue(any(t and t.startswith("Allow 150 more") for t in texts.values()))
                if kind == spending.DAILY:
                    self.assertEqual(hits["item0.tool.link"]["action"], ("open_web", "https://loopcut.org/account"))

    def test_a_models_cancel_card_fits_260_px_and_says_what_it_cancels(self):
        question = {"title": "Cloud rendering", "held": 29, "pct": 93}
        card = state.item_tool("loopcut_cloud__job_status", "Cloud job job_r", "")
        card.update(status="awaiting", paid_approval=True, ask=spending.CANCEL,
                    ask_label=spending.approve_label(spending.CANCEL), ask_skip=spending.skip_label(spending.CANCEL),
                    ask_title=spending.ask_title(spending.CANCEL, question),
                    ask_lines=spending.ask_lines(spending.CANCEL, question, spending.new_ledger(), S))
        session = state.new_session()
        session["items"].append(card)
        display = self.build(session)
        hits = {h["id"]: h for h in display["hits"]}
        self.assertEqual((hits["item0.tool.run"]["action"], hits["item0.tool.reject"]["action"]),
                         (("approve", 0), ("reject", 0)))
        for hit in display["hits"]:
            if hit["id"].startswith("item0"):
                self.assertLessEqual(hit["x"] + hit["w"], 260 - T.PAD, hit["id"])
        shown = " ".join(t for t in self.texts(display).values() if t)
        for said in ("Cancel this cloud job?", "Cloud rendering, 93% done", "29 credits held", "Cancel it",
                     "Keep it running"):
            self.assertIn(said, shown)

    def test_worker_ask_card_names_the_worker(self):
        session = state.new_session()
        card = self.ask_card(spending.COST, worker="worker chair (props)", summary="worker chair (props): Image to 3D")
        card["ask_lines"] = ["Asked by worker chair (props), working in the background."] + card["ask_lines"]
        session["items"].append(card)
        texts = " ".join(t for t in self.texts(self.build(session)).values() if t)
        self.assertIn("worker chair (props)", texts)
        self.assertIn("Balance 1,840", texts)

    def test_limit_error_cards_offer_the_account_link_and_stop(self):
        session = state.new_session()
        session["busy"] = True
        card = state.item_tool("loopcut_cloud__render", "Render", "")
        card.update(status="failed", expanded=True, cloud_error={"message": "Today's credit limit is reached.",
                    "link": "https://loopcut.org/account", "link_label": "Change daily limit ↗"})
        burst = state.item_tool("loopcut_cloud__render", "Render", "")
        burst.update(status="failed", expanded=True, cloud_error={"message": "Too many paid jobs.", "stop": True})
        session["items"] += [card, burst]
        hits = {h["id"]: h["action"] for h in self.build(session)["hits"]}
        self.assertEqual(hits["item0.tool.link"], ("open_web", "https://loopcut.org/account"))
        self.assertEqual(hits["item1.tool.stop"], ("stop_turn", 1))
        session["busy"] = False
        self.assertNotIn("item1.tool.stop", {h["id"] for h in self.build(session)["hits"]})

    def test_running_job_row_shows_its_hold(self):
        rows = [{"id": "job_r", "title": "Render", "pct": 40, "message": "Rendering", "cancelable": True,
                 "note": "80 credits held. Cancel returns them."}]
        display = self.build(state.new_session(), cloud_jobs=rows)
        note = " ".join(t.strip() for i, t in self.texts(display).items() if i.startswith("cloud.job_r.note"))
        self.assertEqual(note, "80 credits held. Cancel returns them.")


class SettingsTest(unittest.TestCase):
    def test_spending_settings_from_preferences_or_environment(self):
        with patch.object(config, "_preference_values", return_value={}), patch.dict(os.environ, {}, clear=False):
            for key in ("LOOPCUT_ASK_ABOVE_CREDITS", "LOOPCUT_CHAT_BUDGET_CREDITS", "LOOPCUT_ASK_EVERY_PAID_CALL",
                        "LOOPCUT_EXTRAS"):
                os.environ.pop(key, None)
            with patch.object(config, "_find_env_file", return_value=None):
                self.assertEqual(config.spending(), spending.Settings(100, 150, False))
            values = {"LOOPCUT_ASK_ABOVE_CREDITS": "0", "LOOPCUT_CHAT_BUDGET_CREDITS": "300",
                      "LOOPCUT_ASK_EVERY_PAID_CALL": "true"}
            with patch.object(config, "_preference_values", return_value=values), \
                    patch.object(config, "_find_env_file", return_value=None):
                self.assertEqual(config.spending(), spending.Settings(0, 300, True))
            for extras in spending.EXTRAS:
                with patch.object(config, "_preference_values", return_value={"LOOPCUT_EXTRAS": extras}), \
                        patch.object(config, "_find_env_file", return_value=None):
                    self.assertEqual(config.spending(), spending.Settings(extras=extras))
            with patch.object(config, "_preference_values", return_value={"LOOPCUT_EXTRAS": "SOMETIMES"}), \
                    patch.object(config, "_find_env_file", return_value=None), self.assertRaises(config.ConfigError):
                config.spending()
            with patch.object(config, "_preference_values", return_value={"LOOPCUT_CHAT_BUDGET_CREDITS": "0"}), \
                    patch.object(config, "_find_env_file", return_value=None), self.assertRaises(config.ConfigError):
                config.spending()


# The server's price for each per-unit tool (loopcut-web src/lib/pricing/price-card.ts, 2026-10-21), and the
# estimate its descriptor carries: the price of the tool's default arguments.
SERVER_PRICE = {
    "motion_from_text": (10, lambda a: 2 * max(1, math.ceil(a["duration_s"] - 1e-9))),  # 2/s, 5 s default.
    "image_to_3d": (80, lambda a: 25 if a.get("quality") == "draft" else 80),
    "image_generate": (6, lambda a: 20 if a.get("quality") == "high" else 6),
    "sfx": (4, lambda a: 4 + math.ceil(max(0, a["duration_seconds"] - 6) / 2 - 1e-9)),
}


class QuotedPriceTest(unittest.TestCase):
    """Every paid call's card shows the server's price for its own arguments, not the descriptor's estimate for
    the defaults (0.2.6 QA: "About 25 credits" on a 3-second motion charged 15), and the job is charged that quote."""

    def setUp(self):
        self.posts, self.quoted = [], []
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = types.SimpleNamespace(lock=threading.RLock(), cache_dir=Path(temp.name), quotes={},
                                             quote_requests={}, handle_error=lambda error: None, manifest={})
        self.client = cloud_http.Client(base_url="https://service.test", api_key="lc_test", transport=self.server,
                                        client_headers={"x-loopcut-contract": "0.1", "x-loopcut-client": "test"})
        for target in (patch.object(agent, "_redraw"), patch.object(cloud_jobs, "_redraw"),
                       patch.object(conversations, "save"), patch.object(cloud_jobs, "_registry", {})):
            target.start()
            self.addCleanup(target.stop)

    def server(self, request, *, timeout):
        if request.full_url.endswith("/blobs/check"):
            return Reply({"missing": []})
        body = json.loads(request.data)
        price = SERVER_PRICE[body["tool"]][1](body["input"])
        if request.full_url.endswith("/v1/quotes"):
            self.quoted.append(body)
            return Reply({"quote_id": f"qt_{len(self.quoted)}", "credits": price, "expires_at": "2099-01-01T00:00:00Z"})
        self.posts.append(body)
        job_id = f"job_ok{len(self.posts)}"
        return Reply({"id": job_id, "tool": body["tool"], "status": "succeeded", "progress": {"pct": 100, "message": "Done"},
                      "credits": {"reserved": price, "charged": price, "refunded": 0}, "outputs": [], "error": None,
                      "provenance": {"job_id": job_id, "tool": body["tool"], "vendor": "test", "model": "m",
                                     "kind": "gen", "created_at": "2026-10-21T00:00:00Z"}})

    def card_for(self, name, arguments, estimate=None):
        estimate = SERVER_PRICE[name][0] if estimate is None else estimate
        descriptor = {"name": name, "version": 1, "backend": "managed", "kind": "gen", "latency": "seconds",
                      "cost": {"credits_estimate": estimate, "quote_required": False}, "file_inputs": [],
                      "approval": {"confirm_over_credits": 100}, "title": name}
        card = state.item_tool("loopcut_cloud__" + name, name, "")
        consent = agent._Consent(state.new_session(), agent.Turn(), card)
        with cloud_account.approval_context(consent):
            approved = cloud_account.authorize(cloud_jobs, descriptor, arguments, {}, project=PROJECT,
                                               cancel=threading.Event(), runtime=self.runtime, client=self.client)
            job = cloud_account.submit(cloud_jobs, descriptor, approved, {}, project=PROJECT, on_progress=None,
                                       cancel=threading.Event(), runtime=self.runtime, client=self.client)
        return card, job

    def test_each_per_unit_tool_shows_and_charges_the_price_of_its_own_arguments(self):
        from loopcut.nudges import dollars
        cases = [("motion_from_text", {"prompt": "jump", "duration_s": 3}),   # 6, not the 5 s default's 10.
                 ("motion_from_text", {"prompt": "walk", "duration_s": 9.5}),  # 20: rounded up to whole seconds.
                 ("image_to_3d", {"prompt": "a lamp", "quality": "draft"}),    # 25, not the final's 80.
                 ("image_generate", {"prompt": "a lamp", "quality": "high"}),  # 20, not standard's 6.
                 ("sfx", {"prompt": "rain", "duration_seconds": 11})]          # 7, not one take's 4.
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                price = SERVER_PRICE[name][1](arguments)
                self.assertNotEqual(price, SERVER_PRICE[name][0])  # Non-default arguments: the estimate is wrong.
                card, job = self.card_for(name, dict(arguments))
                self.assertEqual(card["cost"], f"{price:g} credits ({dollars(price)}), quoted")
                self.assertEqual(self.posts[-1]["quote_id"], f"qt_{len(self.quoted)}")  # The job is charged it.
                self.assertEqual(job["credits"]["charged"], price)

    def test_a_free_tool_is_not_quoted(self):
        SERVER_PRICE["asset_search"] = (0, lambda a: 0)
        self.addCleanup(SERVER_PRICE.pop, "asset_search")
        card, _ = self.card_for("asset_search", {"query": "chair"})
        self.assertEqual(self.quoted, [])
        self.assertNotIn("quote_id", self.posts[-1])
        self.assertEqual(card["cost"], "Free")

    def test_the_quick_check_before_a_call_still_reads_the_estimate(self):
        descriptor = {"backend": "managed", "cost": {"credits_estimate": 80, "quote_required": False},
                      "approval": {"confirm_over_credits": 50}}
        self.assertEqual(cloud_account.approval_details(descriptor)["text"], "About 80 credits ($0.80)")
        self.assertTrue(cloud_account.quoted(descriptor))
        self.assertFalse(cloud_account.quoted({"cost": {"credits_estimate": 0, "quote_required": False}}))


if __name__ == "__main__":
    unittest.main()
