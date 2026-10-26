"""A paid tool refused for credits opens the chat's out-of-credits pop-up (0.2.19, owner-approved 2026-10-26).

Only in the credits arm (the manifest's account.billing "credits"). The server's insufficient_credits on a quote or
a job goes through cloud.execute and a real agent._Consent, as a turn makes the call, and the layout draws what the
user sees. No Blender: bpy is a stand-in and the service an in-memory module and transport.
"""

import os
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.modules.setdefault("bpy", types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import (agent, chat_credits, cloud, cloud_account, cloud_http, config, conversations,  # noqa: E402
                     mainthread, state, tools)
from loopcut.ui import layout  # noqa: E402
from test_agent_loop import panel_input  # noqa: E402
from test_cloud import Reply, Transport, manifest, tool  # noqa: E402

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc).timestamp()
RESET = "2026-11-01T00:00:00Z"
REFILL = {"credits": 25, "at": "2026-10-30T09:12:00Z"}
SHORT = "insufficient_credits: not enough credits for this. Tell the user; the panel shows how to get more."
OFFER_COPY = ("Pro", "Get ", "plan", "come back", "$", "upgrade", "Upgrade", "Add credits", "pricing", "http")


def measure(font, size, text):
    return len(text) * size * 0.6


def texts(display, prefix):
    return [p["text"] for p in display["prims"] if p.get("text") and p["id"].startswith(prefix)]


def words(display, prefix):
    return " ".join(texts(display, prefix))


def hit(display, id):
    return next(h for h in display["hits"] if h["id"] == id)


def paid(name, title, display_title, credits):
    return tool(name, title=title, display={"title": display_title, "summary": title}, backends=["managed"], byok=None,
                cost={"credits_estimate": credits, "quote_required": False})


TOOLS = (paid("image_to_3d", "Image to 3D model", "Image to 3D", 80), paid("image_edit", "Edit image", "Image editing", 20))


def refused(balance, needed):
    return cloud_http.CloudError("You need more credits to start this job.", "insufficient_credits", 402,
                                 {"balance": balance, "needed": needed, "upgrade": "https://loopcut.org/pricing"})


class Fixture(unittest.TestCase):
    """A signed-in runtime on a manifest with the account below; each paid call is quoted as the server does, and
    self.quote_refusal or self.job_refusal is the server's answer instead."""
    PLAN, BALANCE, BILLING, TOOLS = "free", 15, "credits", TOOLS

    def account(self, plan=None, balance=None):
        plan, balance = plan or self.PLAN, self.BALANCE if balance is None else balance
        account = {"plan": plan, "settings": {"no_generation": False, "private_mode": True},
                   "credits": {"balance": balance, "monthly": 0 if plan == "free" else 2200, "resets_at": RESET},
                   "chat_credits": {"per_step": 0.2, **({"refill": REFILL} if plan == "free" else {})}}
        if self.BILLING:
            account["billing"] = self.BILLING
        return account

    def manifest(self, *tools, **account):
        return manifest(*(tools or self.TOOLS), account=self.account(**account),
                        flags={"services": True, "premium_skills": True, "billing": True})

    def serve(self, *tools, **account):
        """The service's next manifest, for the add-on's next refresh."""
        self.transport.replies.append(Reply(self.manifest(*tools, **account)))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.quote_refusal = self.job_refusal = None
        self.jobs = []
        module = types.ModuleType("loopcut.cloud_jobs")

        def run(descriptor, arguments, files, *, quote=False, **_):
            if quote and not arguments.get("quote_id"):
                if self.quote_refusal:
                    raise self.quote_refusal
                raise cloud_http.CloudError("Approve the quote", "quote_required", extra={
                    "quote_id": "qt_" + descriptor["name"], "credits": descriptor["cost"]["credits_estimate"],
                    "expires_at": "2099-01-01T00:00:00Z"})
            self.jobs.append(descriptor["name"])
            if self.job_refusal:
                raise self.job_refusal
            return {"id": "job_1", "status": "succeeded", "outputs": [], "error": None,
                    "credits": {"reserved": 1, "charged": 1, "refunded": 0}}
        module.resolve_files, module.run, module.apply_result = (lambda d, a, o: {}), run, (lambda d, j: "applied")
        self.transport = Transport(Reply(self.manifest()))
        client = cloud_http.Client(base_url="https://service.test", api_key="lc_demo", transport=self.transport)
        runtime = cloud.Runtime(client, temp.name, signed_in=True)
        data = tempfile.TemporaryDirectory()  # nudges.json: the locked card's caps.
        self.addCleanup(data.cleanup)
        for target in (patch.object(cloud, "_runtime", runtime), patch.dict(sys.modules, {"loopcut.cloud_jobs": module}),
                       patch.dict(os.environ, {"LOOPCUT_DATA_DIR": data.name}),
                       patch.object(cloud, "_apply_to_scene", lambda module, descriptor, job, scene, report=None:
                                    tools.ToolResult("Added the image.")),
                       patch.object(agent, "_redraw"), patch.object(conversations, "save"),
                       patch.object(agent, "_wait_for_approval", return_value=True),
                       patch.dict(state.ui, {"allowance_notice": None})):
            target.start()
            self.addCleanup(target.stop)
        state.ui.pop("allowance_notice")
        runtime.refresh()
        self.session = state.reset()
        self.turn = self.new_turn()

    def new_turn(self):
        turn = agent.Turn()
        self.session.update(turn=turn, busy=True)
        return turn

    def call(self, name="image_to_3d", *, session=None, turn=None, consent_turn=None):
        """One paid call as agent.run_loop makes it: its card, a _Consent bound to it, the tool's result."""
        session, turn = session or self.session, turn or self.turn
        card = state.item_tool(cloud.PREFIX + name, cloud.describe(cloud.PREFIX + name, {})[0], "")
        session["items"].append(card)
        consent = agent._Consent(session, consent_turn or turn, card, True)
        prepared = cloud.execute(cloud.PREFIX + name, {"prompt": "a wooden crate"})
        with cloud_account.approval_context(consent):
            result = prepared.then(lambda: False, lambda fn: fn()) if prepared.then else prepared
        card.update(status="done" if result.ok else "failed", output=result.text)
        return card, result

    def limits(self, session=None):
        return [i for i in (session or self.session)["items"] if i["kind"] == "limit"]

    def draw(self, width=420, height=900, **ui):
        runtime = cloud._runtime
        base = {"now": NOW, "billing": chat_credits.billing(runtime.manifest, True), "selection": [],
                "chat_credits": chat_credits.panel_data(runtime.manifest, True), "context_budget": 24000,
                "credit_balance": cloud_account.balance(runtime.manifest),
                "allowance_notice": state.ui.get("allowance_notice")}
        return layout.build(self.session, width, height, 1.0, measure, "fast", None, {**base, **ui})


class FreePlanTest(Fixture):
    def test_a_refused_photo_to_3d_opens_one_pop_up_with_its_need_get_pro_and_the_refill(self):
        self.quote_refusal = refused(15, 80)
        self.call()
        self.assertEqual(self.jobs, [], "refused at its quote: no job")
        [item] = self.limits()
        self.assertIs(state.ui["allowance_notice"]["item"], item)
        self.assertEqual(state.ui["allowance_notice"]["conversation"], self.session["id"])
        n = self.session["items"].index(item)
        self.assertEqual(n, 1, "right under the refused call's card")
        for width, height in ((420, 900), (260, 700)):
            with self.subTest(width=width):
                display = self.draw(width, height)  # The turn still runs: the pop-up is open at once.
                self.assertTrue(self.session["busy"])
                self.assertEqual(words(display, "popup.title"), "Not enough credits: Image to 3D needs 80, you have 15")
                self.assertEqual(words(display, "popup.text"), "25 free credits come back Fri 30 Oct")
                self.assertEqual(words(display, "popup.offer"), "Pro: 2,200 credits a month ($22)")
                self.assertEqual(words(display, "popup.upgrade"), "Get Pro · $20 a month")
                self.assertEqual(hit(display, "popup.upgrade")["action"], ("limit_upgrade", n))
                self.assertEqual(words(display, "popup.fine"), "Cancel anytime · Asset search stays free")
                # Mid-turn, switching the chat's AI waits for the turn: no ChatGPT or key row yet.
                self.assertFalse(any(p["id"].startswith("popup.free") for p in display["prims"]))
                self.assertEqual(hit(display, "popup.close")["action"], ("limit_popup_close", None))
                # 15 credits still chat and run the cheaper tools: no "These pause until then" tiles.
                self.assertFalse(any(p["id"].startswith(("popup.pause", "popup.tile")) for p in display["prims"]))
                sheet = next(p for p in display["prims"] if p["id"] == "popup.sheet")
                self.assertLessEqual(sheet["y"] + sheet["h"], height)
                for p in display["prims"][display["prims"].index(sheet):]:
                    if p.get("text"):
                        self.assertLessEqual(p["x"] + measure("ui", p["size"], p["text"]), width, p["id"])
        # The same link and tracking as the chat's own 402 (its upgrade.url).
        self.assertEqual(item["upgrade"], {"url": config.pricing_url("insufficient_credits", "pro"), "plan": "pro",
                                           "label": "Get Pro"})
        self.assertTrue(item["upgrade"]["url"].endswith("/pricing?from=app&reason=insufficient_credits&plan=pro"))
        self.assertEqual((item["plan"], item["resets_at"], item["refill"]), ("free", RESET, REFILL))

    def test_closed_it_leaves_one_slim_card_under_the_call_whose_button_opens_it_again(self):
        self.quote_refusal = refused(15, 80)
        card, _ = self.call()
        item = self.limits()[0]
        item["popup_closed"] = True
        display = self.draw()
        self.assertFalse(any(p["id"].startswith("popup.") for p in display["prims"]))
        self.assertEqual(texts(display, "item1.limit.title"), ["Not enough credits for Image to 3D"])
        self.assertEqual(words(display, "item1.limit.text"), "Free credits come back Fri 30 Oct")
        self.assertEqual(hit(display, "item1.limit.open")["action"], ("limit_popup_open", 1))  # Mid-turn too.
        self.assertEqual(texts(display, "item1.limit.open"), ["See Pro"])
        self.assertFalse(any(p["id"].startswith("input.limit") for p in display["prims"]))
        # The refused call's own card keeps one readable line, collapsed: the model's text and its buttons gave way
        # to the pop-up's one offer.
        self.assertEqual(card["cloud_error"], {"text": SHORT,
                                               "message": "Not enough credits: this needs 80 ($0.80), you have 15.",
                                               "upgrade": ""})
        self.assertFalse(card["expanded"])
        self.assertEqual(texts(display, "item0.tool.reason"), ["Not enough credits: this needs 80 ($0.80), you have 15."])
        self.assertFalse(any(p["id"].startswith(("item0.tool.output", "item0.tool.account_error"))
                             for p in display["prims"]))
        self.assertFalse(any(h["id"].startswith(("item0.tool.upgrade", "item0.tool.plans")) for h in display["hits"]))

    def test_at_no_balance_a_refused_image_edit_opens_it_with_the_paused_tools(self):
        # The manifest still said 15 when the server refused the job at 0 (a second edit in the same reply).
        self.job_refusal = refused(0, 20)
        self.call("image_edit")
        self.assertEqual(self.jobs, ["image_edit"])
        [item] = self.limits()
        display = self.draw()
        self.assertEqual(words(display, "popup.title"), "Not enough credits: Image editing needs 20, you have 0")
        self.assertEqual(words(display, "popup.upgrade"), "Get Pro · $20 a month")
        self.assertIn("25 free credits come back Fri 30 Oct", words(display, "popup.text"))
        self.assertIn("These pause until then:", words(display, "popup.pause"))
        self.assertEqual(len([p for p in display["prims"] if p["id"].endswith(".lock.body")]), 6)

    def test_the_models_result_is_short_and_says_nothing_of_the_offer(self):
        self.quote_refusal = refused(15, 80)
        _, result = self.call()
        self.assertFalse(result.ok)
        first, *rest = result.text.split("\n")
        self.assertEqual(first, SHORT)
        self.assertLess(len(result.text), 200)
        for copy in OFFER_COPY:
            self.assertNotIn(copy, result.text)

    def test_a_retry_or_another_refused_tool_in_the_turn_keeps_one_pop_up_and_a_new_turn_can_show_it(self):
        self.quote_refusal = refused(15, 80)
        self.call()
        [first] = self.limits()
        first["popup_closed"] = True  # The user closed it; the model tries again, then another paid tool.
        self.call()
        self.quote_refusal = refused(15, 20)
        self.call("image_edit")
        self.assertEqual(self.limits(), [first])
        self.assertIs(state.ui["allowance_notice"]["item"], first)
        self.assertTrue(first["popup_closed"], "not opened again")
        self.assertFalse(any(p["id"].startswith("popup.") for p in self.draw()["prims"]))
        # The turn ends; the next one starts (agent._launch): the old card is history, and it can show again.
        self.session.update(busy=False, turn=None)
        turn = agent.Turn()
        with patch.object(agent, "_run"):
            agent._launch(self.session, turn)
        self.assertIsNone(state.ui.get("allowance_notice"))
        history = self.draw()
        self.assertEqual(texts(history, "item1.limit.title"), ["Not enough credits for Image to 3D"])
        self.assertFalse(any(h["id"].startswith("item1.limit.") for h in history["hits"]), "history: no buttons")
        self.turn = turn
        self.call("image_edit")
        self.assertEqual(len(self.limits()), 2)
        second = self.limits()[1]
        self.assertIs(state.ui["allowance_notice"]["item"], second)
        self.assertEqual(words(self.draw(), "popup.title"), "Not enough credits: Image editing needs 20, you have 15")

    def test_chat_replies_in_the_same_turn_leave_it_open(self):
        """A hosted reply's success clears the chat's own refusal (credits came back); a tool's still stands."""
        self.quote_refusal = refused(15, 80)
        self.call()
        cfg = types.SimpleNamespace(base_url=config.SERVICE_BASE_URL)
        with patch.object(mainthread, "run_on_main", side_effect=lambda fn: fn()), patch.object(agent, "_update_account"):
            agent._hosted_reply(self.turn, cfg, {"plan": "free", "billing": "credits", "credits_balance": 15},
                                success=True, session=self.session)
        item = self.limits()[0]
        self.assertIs(state.ui["allowance_notice"]["item"], item)
        self.assertFalse(item.get("resolved"))
        self.assertTrue(any(p["id"] == "popup.sheet" for p in self.draw()["prims"]))

    def test_a_card_that_waits_for_the_user_closes_it_and_esc_closes_it_not_the_turn(self):
        self.quote_refusal = refused(15, 80)
        self.call()
        item = self.limits()[0]
        display = self.draw()
        key, _ = panel_input(display, "ESC")
        with patch.object(agent, "stop") as stop:
            key(self.session)
        stop.assert_not_called()
        self.assertTrue(item["popup_closed"])
        item["popup_closed"] = False
        state.awaiting(state.item_tool("run_python", "Add a lid", "bpy.ops.mesh.primitive_cube_add()"))
        self.assertTrue(item["popup_closed"], "the question comes first; the slim card stays")
        self.assertFalse(any(p["id"].startswith("popup.") for p in self.draw()["prims"]))


class WorkerTest(Fixture):
    def test_a_workers_refused_call_shows_in_the_users_chat_once_per_the_users_turn(self):
        self.quote_refusal = refused(15, 80)
        worker = agent.Turn()
        worker.chat, worker.worker = (self.session, self.turn), "worker crate (props)"
        own = state.new_session()
        for _ in range(2):
            card, result = self.call(session=own, turn=worker)
            self.assertEqual(result.text.split("\n")[0], SHORT)
        self.assertEqual(self.limits(own), [])
        [item] = self.limits()
        self.assertIs(state.ui["allowance_notice"]["item"], item)
        self.assertTrue(self.turn.tool_paywall)
        self.assertEqual(words(self.draw(), "popup.title"), "Not enough credits: Image to 3D needs 80, you have 15")
        self.call()  # The user's own call in the same turn adds none.
        self.assertEqual(len(self.limits()), 1)


class ProPlanTest(Fixture):
    PLAN = "pro"

    def test_pro_gets_add_credits_not_an_upgrade(self):
        self.quote_refusal = refused(15, 80)
        self.call()
        [item] = self.limits()
        self.assertEqual(item["upgrade"], {"url": config.credits_url("insufficient_credits"), "label": "Add credits"})
        display = self.draw()
        self.assertEqual(words(display, "popup.title"), "Not enough credits: Image to 3D needs 80, you have 15")
        self.assertEqual(words(display, "popup.upgrade"), "Add credits")
        self.assertEqual(hit(display, "popup.upgrade")["action"], ("limit_upgrade", 1))
        self.assertEqual(words(display, "popup.text"), "2,200 credits come back Sun 1 Nov")
        self.assertEqual(words(display, "popup.fine"), "Asset search stays free")  # A top-up is no subscription.
        self.assertFalse(any(p["id"].startswith("popup.offer") for p in display["prims"]))
        self.assertNotIn("Get Max", words(display, "popup."))
        item["popup_closed"] = True
        self.assertEqual(texts(self.draw(), "item1.limit.open"), ["Add credits"])


EDIT_OPEN = paid("image_edit", "Edit image", "Image editing", 20)
EDIT_LOCKED = {**EDIT_OPEN, "available": False, "unavailable_reason": "credits"}


class LockedTest(Fixture):
    """At no balance the manifest locks the paid tools for credits, so a call the model makes anyway is refused here,
    before any quote: in the credits arm it is the same refusal, and opens the same pop-up. The manifest is refreshed
    first: it may be from before a purchase."""
    BALANCE = 0
    TOOLS = (EDIT_LOCKED,)

    def test_a_locked_tool_called_anyway_opens_the_pop_up_and_tells_the_model_the_same(self):
        self.serve()  # Still locked when asked again.
        card, result = self.call("image_edit")
        self.assertEqual(self.jobs, [])
        self.assertEqual(self.transport.replies, [], "refreshed before refusing")
        self.assertFalse(result.ok)
        self.assertEqual(result.text.split("\n")[0], SHORT)
        self.assertEqual(card["cloud_error"]["message"], "Not enough credits: this needs 20 ($0.20), you have 0.")
        [item] = self.limits()
        display = self.draw()
        self.assertEqual(words(display, "popup.title"), "Not enough credits: Image editing needs 20, you have 0")
        self.assertEqual(words(display, "popup.upgrade"), "Get Pro · $20 a month")
        self.assertIn("These pause until then:", words(display, "popup.pause"))

    def test_credits_bought_since_the_last_refresh_run_the_call_without_the_offer(self):
        self.serve(EDIT_OPEN, plan="pro", balance=2200)  # Upgraded on the site minutes ago.
        card, result = self.call("image_edit")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.jobs, ["image_edit"])
        self.assertEqual(self.limits(), [])
        self.assertIsNone(state.ui.get("allowance_notice"))
        self.assertNotIn("cloud_error", card)

    def test_outside_the_credits_arm_it_stays_the_short_refusal(self):
        cloud._runtime.manifest["account"].pop("billing")
        card, result = self.call("image_edit")
        self.assertEqual(result.text, "Not enough credits.")
        self.assertEqual(self.limits(), [])
        self.assertIsNone(state.ui.get("allowance_notice"))


class OtherArmTest(Fixture):
    """An account outside the credits arm (the allowance windows, or a server before account.billing): today's
    behaviour, exactly."""
    BILLING = "allowance"

    def test_no_pop_up_and_the_card_and_result_are_as_before(self):
        for billing in ("allowance", None):
            with self.subTest(billing=billing):
                cloud._runtime.manifest["account"].pop("billing", None)
                if billing:
                    cloud._runtime.manifest["account"]["billing"] = billing
                self.session["items"].clear()
                self.quote_refusal = refused(15, 80)
                card, result = self.call()
                self.assertEqual(self.limits(), [])
                self.assertIsNone(state.ui.get("allowance_notice"))
                self.assertFalse(getattr(self.turn, "tool_paywall", False))
                self.assertTrue(result.text.startswith("insufficient_credits: balance 15, needed 80. Add credits or "
                                                       "use your own vendor key."))
                self.assertEqual(card["cloud_error"], {
                    "text": "insufficient_credits: balance 15, needed 80. Add credits or use your own vendor key.",
                    "message": "Not enough credits: this needs 80 ($0.80), you have 15.",
                    "upgrade": "https://loopcut.org/pricing"})
                display = self.draw(billing=billing or "")
                self.assertFalse(any(p["id"].startswith("popup.") for p in display["prims"]))
                self.assertEqual(hit(display, "item0.tool.upgrade")["action"], ("cloud_upgrade", 0))


if __name__ == "__main__":
    unittest.main()


class LockedCardTest(Fixture):
    """The model's own way to a locked tool (offer_tool, nudges.offer_locked) at no balance: on the free plan of the
    credits arm, the same pop-up (Get Pro, when free credits come back) in place of the card whose Get credits led
    to a page the free plan cannot buy on. Its caps stay: once per tool per chat, three a week."""
    BALANCE = 0
    TOOLS = (EDIT_LOCKED, {**TOOLS[0], "available": False, "unavailable_reason": "credits"})

    def offer(self, name="image_edit"):
        return cloud.execute(cloud.PREFIX + cloud.OFFER, {"tool": name}).text

    def test_the_free_plans_locked_card_is_the_pop_up(self):
        result = self.offer()
        self.assertTrue(result.startswith("The user now sees a card with the way to unlock it."), result)
        self.assertFalse([i for i in self.session["items"] if i["kind"] == "offer"], "no Get credits card")
        [item] = self.limits()
        self.assertIs(state.ui["allowance_notice"]["item"], item)
        self.assertEqual(item["upgrade"], {"url": config.pricing_url("locked_tool", "pro"), "plan": "pro",
                                           "label": "Get Pro"})
        display = self.draw()
        self.assertEqual(words(display, "popup.title"), "Not enough credits: Image editing needs 20, you have 0")
        self.assertEqual(words(display, "popup.upgrade"), "Get Pro · $20 a month")
        self.assertEqual(words(display, "popup.text"), "25 free credits come back Fri 30 Oct")
        self.assertNotIn("Get credits", words(display, ""))

    def test_its_caps_stay_once_per_tool_per_chat_and_three_a_week(self):
        self.offer()
        self.session.update(busy=False, turn=None)
        with patch.object(agent, "_run"):
            agent._launch(self.session, agent.Turn())
        self.assertTrue(self.offer().startswith("Not shown again."), "once per tool in this chat")
        self.assertEqual(len(self.limits()), 1)
        for _ in range(2):
            self.session = state.reset()
            self.new_turn()
            self.offer()
        self.session = state.reset()
        self.new_turn()
        self.assertTrue(self.offer().startswith("Not shown again."), "three a week")
        self.assertEqual(self.limits(), [])

    def test_it_and_a_refusals_pop_up_never_both_show_for_a_tool_in_one_turn(self):
        self.offer()
        self.serve()
        self.call("image_edit")  # The model calls the locked tool anyway: refused, no second pop-up.
        self.assertEqual(len(self.limits()), 1)
        self.session = state.reset()  # The other order, in another chat.
        self.turn = self.new_turn()
        self.serve()
        self.call("image_edit")
        self.assertTrue(self.offer().startswith("Not shown again."))
        self.assertEqual(len(self.limits()), 1)
        self.assertFalse([i for i in self.session["items"] if i["kind"] == "offer"])

    def test_pro_keeps_its_card_and_never_alongside_a_refusals_pop_up(self):
        cloud._runtime.manifest["account"]["plan"] = "pro"
        self.offer()
        [card] = [i for i in self.session["items"] if i["kind"] == "offer"]
        self.assertEqual(card["title"], "Not enough credits for Image editing")
        self.assertEqual(self.limits(), [])
        self.serve(*self.TOOLS, plan="pro")
        self.call("image_edit")  # Refused in the same turn: the card already offered credits for it.
        self.assertEqual(self.limits(), [])
        self.assertTrue(self.offer("image_to_3d").startswith("The user now sees"))  # Another tool: its own card.

    def test_other_arms_keep_the_card(self):
        cloud._runtime.manifest["account"].pop("billing")
        self.offer()
        self.assertEqual([i["kind"] for i in self.session["items"]], ["offer"])
        self.assertIsNone(state.ui.get("allowance_notice"))


class ReviewFixesTest(Fixture):
    """The independent review's findings (2026-10-26), each pinned."""

    def test_after_buying_the_manifest_follows_the_new_plan_and_the_added_credits(self):
        runtime = cloud._runtime
        before = len(self.transport.calls)
        self.serve(balance=15)
        self.serve(balance=15)
        self.serve(balance=2215)
        headers = {"x-loopcut-contract": cloud_http.CONTRACT, "x-loopcut-client": "test"}  # Read on Blender's thread.
        with patch.object(cloud, "BALANCE_WATCH", (0.01, 5.0)), patch.object(cloud_http, "contract_headers",
                                                                             return_value=headers):
            cloud.watch_balance()
            deadline = time.monotonic() + 5
            while cloud_account.balance(runtime.manifest) != 2215 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(cloud_account.balance(runtime.manifest), 2215)
        time.sleep(0.05)
        self.assertEqual(len(self.transport.calls) - before, 3, "it stops once the balance rises")

    def test_a_waiting_card_keeps_the_pop_up_and_its_reopen_button_away(self):
        self.quote_refusal = refused(15, 80)
        self.call()
        item = self.limits()[0]
        card = state.item_tool("run_python", "Add a crate", "print(1)")
        card["status"] = "awaiting"  # A worker's ask can wait before state.awaiting's close reaches it.
        self.session["items"].append(card)
        display = self.draw()
        self.assertFalse(item.get("popup_closed"))
        self.assertFalse(any(h["id"] == "popup.close" for h in display["hits"]), "never over a waiting card")
        self.assertFalse(any(h["id"] == "item1.limit.open" for h in display["hits"]))
        self.assertEqual([c["card"] for c in display["cards"] if c["shown"]], [(id(card), card.get("ask_serial"))])
        card["status"] = "done"
        self.assertTrue(any(h["id"] == "popup.close" for h in self.draw()["hits"]))

    def test_keep_going_still_works_after_a_tool_refusal(self):
        from loopcut import llm
        request = {"role": "user", "content": "Make the crates."}
        self.session["messages"] = [request]
        self.session["items"].insert(0, state.item_user("Make the crates."))
        self.quote_refusal = refused(15, 80)
        self.call()
        self.turn.outcome, self.turn.executed_tools = "running", 1
        spec = agent.LoopSpec(role="single", system=lambda _: "", schemas=lambda: [], save=lambda s: None)
        cfg = types.SimpleNamespace(max_steps=0)
        completion = llm.Completion(text="Next I'll model the crate.")
        self.assertTrue(agent._continue_announced(self.session, self.turn, spec, request, completion, cfg, 1, True))

    def test_the_rig_hears_the_short_credits_result_too(self):
        from loopcut import rig_text
        error = refused(0, 20)
        self.assertEqual(cloud.error_text(error, rig=True), rig_text.CREDITS_OFFERED)
        self.assertNotIn("Add credits", rig_text.CREDITS_OFFERED)
        cloud._runtime.manifest["account"].pop("billing")
        self.assertEqual(cloud.error_text(error, rig=True), "Add credits, then retry.")

    def test_another_chat_shows_none_of_it(self):
        self.quote_refusal = refused(15, 80)
        self.call()
        self.session = state.reset()  # The user opens another conversation.
        display = self.draw()
        self.assertFalse(any(p["id"].startswith(("popup.", "input.limit")) for p in display["prims"]))

    def test_a_detached_jobs_refusal_counts_once_in_the_turn_that_runs(self):
        old = self.turn
        old_card = state.item_tool(cloud.PREFIX + "image_to_3d", "Image to 3D model", "")
        self.session["items"].append(old_card)
        self.session.update(busy=False, turn=None)
        with patch.object(agent, "_run"):
            agent._launch(self.session, agent.Turn())
        current = self.session["turn"]
        status_card = state.item_tool(cloud.PREFIX + "job_status", "Cloud job", "")
        self.session["items"].append(status_card)
        details = cloud_account.error_details(refused(15, 80))
        agent._Consent(self.session, old, old_card)({"error": details})  # The job's own card (error_reporter).
        agent._Consent(self.session, current, status_card)({"error": details})  # job_status's card.
        self.assertEqual(len(self.limits()), 1)
        self.assertTrue(current.tool_paywall)
        self.assertFalse(old.tool_paywall)

    def test_a_turn_that_ended_well_is_logged_ok_with_a_tool_refusal_in_it(self):
        from loopcut import session_logs
        self.quote_refusal = refused(15, 80)
        self.session["items"].insert(0, state.item_user("Make the crates."))
        self.call()
        reply = state.item_assistant()
        reply.update(text="Not enough credits for the 3D model; I built the crate by hand.", streaming=False)
        self.session["items"].append(reply)
        self.assertEqual(session_logs._items_outcome(self.session["items"]), "ok")

    def test_the_tiles_need_a_balance_below_a_chat_step(self):
        account = self.account(balance=0.4)
        lots = chat_credits.tool_item("Image to 3D", 80, 0.4, account)  # 0.4 still pays two Fast steps.
        self.assertEqual(chat_credits.tool_title(lots), "Not enough credits: Image to 3D needs 80, you have 0")
        self.assertFalse(chat_credits.pauses(lots))
        self.assertTrue(chat_credits.pauses(chat_credits.tool_item("Image to 3D", 80, 0.1, account)))
        self.assertTrue(chat_credits.pauses(chat_credits.tool_item("Image to 3D", 80, 0, {"plan": "free"})))
        self.assertFalse(chat_credits.pauses(chat_credits.tool_item("Image to 3D", 80, 0.4, {"plan": "free"})))

    def test_no_saved_request_line_or_continue_for_a_tool_refusal(self):
        self.quote_refusal = refused(15, 80)
        self.call()
        self.session["messages"] = [{"role": "user", "content": "Make the crates."},
                                    {"role": "tool", "content": "Refused."}]  # Stopped right after the refusal.
        self.session.update(busy=False, turn=None)
        self.assertTrue(state.can_resume(self.session))
        display = self.draw()
        self.assertTrue(any(h["id"] == "popup.close" for h in display["hits"]))
        self.assertFalse(any(p["id"].startswith("popup.saved") for p in display["prims"]))
        self.assertIn("Have ChatGPT Plus or Pro? Chat on it at no cost.", words(display, "popup.free"))
        self.limits()[0]["popup_closed"] = True
        self.assertFalse(any(h["id"] == "item1.limit.retry" for h in self.draw()["hits"]))
