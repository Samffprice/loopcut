"""The display list for the header, the input footer and the popups, with a stand-in for blf."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import state  # noqa: E402
from loopcut.ui import layout, theme as T  # noqa: E402


def measure(font, size, text):
    return len(text) * size * 0.6


def build(session=None, **ui):
    session = session or state.new_session()
    base = {"selection": [], "context_budget": 24_000, "account": None}
    return layout.build(session, 420, 600, 1.0, measure, "fast", None, {**base, **ui})


def ids(display, kind):
    return [p["id"] for p in display[kind]]


def hit(display, id):
    return next(h for h in display["hits"] if h["id"] == id)


class HeaderTest(unittest.TestCase):
    def test_jobs_view_survives_a_new_conversation_and_exposes_controls(self):
        row = {"id": "job1", "state": "running", "frames": [1, 2, 3], "format": "MP4",
               "camera": "Camera", "width": 640, "height": 480, "samples": 32, "budget_seconds": 60,
               "scene_revision": "abc", "progress": {"completed": 1, "files": [{"file": "f.png"}]}}
        display = build(state.new_session(), view="jobs", jobs=[row])
        self.assertEqual(hit(display, "job.job1.job_cancel")["action"], ("job_cancel", "job1"))
        self.assertEqual(hit(display, "job.job1.job_preview")["action"], ("job_preview", "job1"))
        self.assertEqual(hit(display, "header.jobs")["action"], ("history_close", None))
        row["state"] = "cancelled"
        display = build(state.new_session(), view="jobs", jobs=[row])
        self.assertEqual(hit(display, "job.job1.job_resume")["action"], ("job_resume", "job1"))

    def test_icon_buttons_with_tooltips_sit_at_the_right(self):
        display = build()
        buttons = [hit(display, id) for id, _, _ in layout.HEADER_BUTTONS]
        self.assertEqual([b["action"][0] for b in buttons], ["close_panel", "service_settings", "history_open", "new_chat", "jobs_open"])
        self.assertEqual([b["tip"] for b in buttons], ["Close panel", "Settings", "History", "New chat", "Background jobs"])
        xs = [b["x"] for b in buttons]
        self.assertEqual(xs, sorted(xs, reverse=True), "close is rightmost, then settings, history, new")
        self.assertLess(xs[-1] + buttons[-1]["w"], 420)

    def test_history_view_shows_a_crumb_and_a_way_back(self):
        display = build(view="history")
        self.assertIn("header.crumb", ids(display, "prims"))
        self.assertEqual(hit(display, "header.history")["action"], ("history_close", None))
        self.assertIn("header.history.bg", ids(display, "prims"), "the open view's button is lit")

    def test_hovered_button_is_highlighted(self):
        self.assertNotIn("header.settings.bg", ids(build(), "prims"))
        self.assertIn("header.settings.bg", ids(build(hover="header.settings"), "prims"))


class FooterTest(unittest.TestCase):
    def test_model_button_opens_the_menu_not_the_settings(self):
        display = build()
        self.assertEqual(hit(display, "input.model")["action"], ("model_menu", None))
        label = next(p for p in display["prims"] if p["id"] == "input.model.label")
        self.assertEqual(label["text"], "Fast")

    def test_model_menu_lists_options_with_the_current_one_ticked(self):
        options = [("fast", "Fast", "", True), ("pro", "Pro", "0.5x usage", False),
                   ("opus", "Opus", "24x usage  ·  Max plan", False)]
        display = build(model_menu=True, model_options=options)
        names = [p["text"] for p in display["prims"] if p["id"].startswith("models.row") and p["id"].endswith(".name")]
        self.assertEqual(names, ["✓  Fast", "     Pro", "     Opus", "Settings…"], "no name cut short by its note")
        self.assertEqual(hit(display, "models.row1")["action"], ("model_pick", "pro"))
        self.assertEqual(hit(display, "models.row3")["action"], ("open_settings", None))

    def test_context_ring_fills_with_the_request_and_the_session_ring_drains(self):
        session = state.new_session()
        session["usage"] = {"input": 30_000, "output": 2_000, "context": 6_000}
        display = build(session, account={"session": {"used": 0.25}, "week": {"used": 0.5}})
        rings = {p["id"]: p for p in display["prims"] if p["t"] == "ring"}
        self.assertAlmostEqual(rings["input.ring.context"]["share"], 0.25)
        self.assertAlmostEqual(rings["input.ring.session"]["share"], 0.75)
        self.assertLess(rings["input.ring.context"]["x"], rings["input.ring.session"]["x"])
        tip = hit(display, "input.rings")["tip"].split("\n")
        self.assertEqual(tip, ["Context: 6.0k of 24.0k tokens (25%) before compaction",
                               "This conversation: 30.0k in, 2.0k out",
                               "Session: 25% used, 75% left", "Week: 50% used, 50% left"])

    def test_without_an_account_there_is_one_ring(self):
        rings = [p for p in build()["prims"] if p["t"] == "ring"]
        self.assertEqual([r["id"] for r in rings], ["input.ring.context"])
        self.assertEqual(rings[0]["share"], 0.0)

    def test_ring_colors_warn_as_they_run_out(self):
        self.assertEqual(layout.ring_shares({"context": 20_000}, 24_000, None)[0][2], T.WARN)
        self.assertEqual(layout.ring_shares({"context": 30_000}, 24_000, None)[0][2], T.ERROR)
        self.assertEqual(layout.ring_shares({}, 24_000, {"session": {"used": 0.95}})[1][2], T.ERROR)

    def test_hovering_the_rings_draws_the_tooltip_above_them(self):
        display = build(hover="input.rings")
        self.assertIn("tooltip.bg", ids(display, "prims"))
        tooltip = next(p for p in display["prims"] if p["id"] == "tooltip.bg")
        self.assertLess(tooltip["y"] + tooltip["h"], hit(display, "input.rings")["y"])
        self.assertNotIn("tooltip.bg", ids(build(hover="input.card"), "prims"), "hits without a tip get none")

    def test_busy_hint_and_placeholder_say_notes_are_taken(self):
        session = state.new_session()
        session["busy"] = True
        display = build(session)
        texts = {p["id"]: p["text"] for p in display["prims"] if p["t"] == "text"}
        self.assertEqual(texts["input.hint"], "⏎  note   esc  stop")
        self.assertEqual(texts["input.placeholder"], "Add a note; it goes in at the next step")


class PickerTest(unittest.TestCase):
    def test_plus_chip_opens_the_picker(self):
        display = build()
        plus = hit(display, "input.add")
        self.assertEqual(plus["action"], ("picker_open", None))
        self.assertTrue(plus["tip"].startswith("Add context"))

    def test_picker_lists_rows_under_a_search_field(self):
        picker = {"query": "cu", "rows": [("Cube", "object"), ("Cube.001", "object")], "active": 1}
        display = build(picker=picker)
        texts = {p["id"]: p["text"] for p in display["prims"] if p["t"] == "text"}
        self.assertEqual(texts["picker.title"], "Add context")
        self.assertEqual(texts["picker.query"], "cu")
        self.assertEqual(texts["picker.row1.name"], "Cube.001")
        self.assertIn("picker.row1.bg", ids(display, "prims"), "the active row is lit")
        self.assertEqual(hit(display, "picker.row0")["action"], ("picker_pick", 0))
        card = next(p for p in display["prims"] if p["id"] == "picker.bg")
        self.assertLess(card["y"] + card["h"], hit(display, "input.card")["y"], "it floats above the input")

    def test_empty_picker_shows_the_placeholder_and_no_matches(self):
        texts = {p["id"]: p["text"] for p in build(picker={"query": "", "rows": [], "active": 0})["prims"] if p["t"] == "text"}
        self.assertEqual(texts["picker.query"], "Search objects, materials, add-ons…")
        self.assertEqual(texts["picker.empty"], "No matches")


class QueuedTest(unittest.TestCase):
    def test_a_queued_message_carries_a_tag_until_it_is_folded_in(self):
        session = state.new_session()
        item = state.item_user("also make it red")
        item["queued"] = True
        session["items"].append(item)
        self.assertIn("item0.queued", ids(build(session), "prims"))
        del item["queued"]
        self.assertNotIn("item0.queued", ids(build(session), "prims"))


class GuardNoticeTest(unittest.TestCase):
    def test_a_switched_off_modifier_can_be_shown_again_once(self):
        session = state.new_session()
        session["items"].append(state.item_guard(
            "Subsurf on Chair_Seat, est. 12M faces: viewport display off; lower the viewport level",
            [["Chair_Seat", "Subsurf"]]))
        display = build(session)
        self.assertEqual(hit(display, "item0.notice.action")["action"], ("guard_show", 0))
        session["items"][0]["action_label"] = ""  # What clicking it does, besides the showing.
        self.assertNotIn("item0.notice.action", ids(build(session), "hits"))
        session["items"].append(state.item_guard("Render refused by the cost guard: ...", []))
        self.assertNotIn("item1.notice.action", ids(build(session), "hits"))


class UpdateBannerTest(unittest.TestCase):
    def test_no_banner_leaves_the_chat_under_the_header(self):
        display = build()
        self.assertNotIn("update.bg", ids(display, "prims"))
        self.assertFalse([h for h in display["hits"] if h["id"].startswith("update.")])

    def test_banner_sits_under_the_header_and_pushes_the_chat_down(self):
        banner = {"text": "Loopcut 0.1.4 is ready.", "button": ("Restart to update", "update_restart"),
                  "dismiss": True, "progress": None}
        session = state.new_session()
        session["items"].append(state.item_user("hi"))
        plain = build(session)
        with_banner = build(session, update=banner)
        bg = next(p for p in with_banner["prims"] if p["id"] == "update.bg")
        self.assertEqual(bg["y"], T.HEADER_HEIGHT)
        self.assertEqual(bg["h"], T.BANNER_HEIGHT)
        card = lambda d: next(p for p in d["prims"] if p["id"] == "item0.user.card")  # noqa: E731
        self.assertEqual(card(with_banner)["y"] - card(plain)["y"], T.BANNER_HEIGHT)
        self.assertEqual(hit(with_banner, "update.button")["action"], ("update_restart", None))
        self.assertEqual(hit(with_banner, "update.dismiss")["action"], ("update_dismiss", None))
        text = next(p for p in with_banner["prims"] if p["id"] == "update.text")
        self.assertEqual(text["text"], banner["text"])
        button = next(p for p in with_banner["prims"] if p["id"] == "update.button")
        self.assertLess(text["x"] + measure("ui", T.FONT_SIZE_SMALL, text["text"]), button["x"])

    def test_download_progress_has_a_bar_and_no_buttons(self):
        banner = {"text": "Downloading Loopcut 0.1.4… 43%", "button": None, "dismiss": False, "progress": 0.43}
        display = build(update=banner)
        bar = next(p for p in display["prims"] if p["id"] == "update.progress")
        self.assertEqual(bar["w"], round(420 * 0.43))
        self.assertFalse([h for h in display["hits"] if h["id"].startswith("update.")])

    def test_long_text_is_cut_short_of_the_button(self):
        banner = {"text": "Update failed: " + "x" * 200, "button": ("Try again", "update_download"), "dismiss": True,
                  "progress": None}
        display = build(update=banner)
        text = next(p for p in display["prims"] if p["id"] == "update.text")
        button = next(p for p in display["prims"] if p["id"] == "update.button")
        self.assertTrue(text["text"].endswith("…"))
        self.assertLessEqual(text["x"] + measure("ui", T.FONT_SIZE_SMALL, text["text"]), button["x"])


def texts(display):
    return {p["id"]: p["text"] for p in display["prims"] if p["t"] == "text"}


def prim(display, id):
    return next(p for p in display["prims"] if p["id"] == id)


def idea(name, title=None, level=None, badge=None, voted=False, summary="", state="ready"):
    """A row as cloud.coming_soon() makes it for the vote strip and view."""
    return {"name": name, "title": title or name.title(), "summary": summary, "state": state, "category": "",
            "voted": voted, "level": level, "badge": badge}


def right(p):
    return p["x"] + measure("ui", p["size"], p["text"])


def vote_view(width=420, height=800, **ui):
    return layout.build(state.new_session(), width, height, 1, measure, ui={"view": "vote", **ui})


class FirstRunTest(unittest.TestCase):
    def test_signed_out_has_equal_sign_in_routes_and_a_quiet_key_link(self):
        display = build(needs_setup=True)
        loopcut, chatgpt = hit(display, "chat.signin.loopcut"), hit(display, "chat.signin.chatgpt")
        self.assertEqual(loopcut["action"], ("sign_in", "loopcut"))
        self.assertEqual(chatgpt["action"], ("sign_in", "chatgpt"))
        self.assertEqual((loopcut["w"], loopcut["h"]), (chatgpt["w"], chatgpt["h"]))
        self.assertEqual(loopcut["y"], chatgpt["y"])
        self.assertEqual(texts(display)["chat.setup.label"], "or use your own key")
        self.assertEqual(hit(display, "chat.setup")["action"], ("own_key", None))

    def test_narrow_signed_out_stacks_equal_routes_without_clipping(self):
        display = layout.build(state.new_session(), 240, 800, 1, measure, ui={"needs_setup": True})
        a, b = hit(display, "chat.signin.loopcut"), hit(display, "chat.signin.chatgpt")
        self.assertGreater(b["y"], a["y"])
        for button in (a, b):
            self.assertLessEqual(button["x"] + button["w"], 240 - T.PAD)

    def test_examples_fill_the_input_and_scope_explains_the_boundary(self):
        display = build()
        for n in range(3):
            self.assertEqual(hit(display, f"chat.hint{n}")["action"], ("example_request", n))
            self.assertEqual(texts(display)[f"chat.hint{n}.label.l0"], layout.EXAMPLE_REQUESTS[n])
        scope = " ".join(value for key, value in texts(display).items() if key.startswith("chat.hints.scope"))
        self.assertIn("inspect and edit", scope)
        self.assertIn("can't see other apps", scope)
        self.assertIn("approval", scope)

    def test_dismissed_hints_do_not_reappear_in_a_new_chat(self):
        display = build(hints_dismissed=True)
        self.assertFalse([p for p in display["prims"] if p["id"].startswith("chat.hint")])
        self.assertIn("chat.empty0.l0", ids(display, "prims"))
        session = state.new_session()
        session["items"].append(state.item_user("Make it red"))
        self.assertFalse([h for h in build(session)["hits"] if h["action"][0] == "example_request"])

    def test_failed_first_message_still_has_routes_forward(self):
        session = state.new_session()
        session["items"].append(state.item_error("No connection. Sign in or choose a model provider."))
        display = build(session, needs_setup=True, sign_in={"chatgpt": {"status": "error", "error": "Try sign-in again."}})
        self.assertIn("chat.signin.loopcut", ids(display, "hits"))
        self.assertIn("chat.signin.chatgpt", ids(display, "hits"))
        self.assertIn("chat.setup", ids(display, "hits"))
        self.assertIn("Try sign-in again.", texts(display).values())

    def test_both_browser_routes_have_copy_cancel_and_a_key_exit(self):
        for route in ("loopcut", "chatgpt"):
            with self.subTest(route=route):
                display = build(needs_setup=True, sign_in={route: {"status": "waiting", "url": "https://sign-in.test",
                                                                  "user_code": "TEST"}})
                self.assertEqual(hit(display, "chat.signin.copy")["action"], ("sign_in_copy", route))
                self.assertEqual(hit(display, "chat.signin.cancel")["action"], ("sign_in_cancel", route))
                self.assertEqual(hit(display, "chat.setup")["action"], ("own_key", None))
                self.assertIn("chat.signin.browser.l0", ids(display, "prims"))

    def test_starting_loopcut_sign_in_is_cancelable(self):
        display = build(needs_setup=True, sign_in={"loopcut": {"status": "starting"}})
        self.assertEqual(hit(display, "chat.signin.cancel")["action"], ("sign_in_cancel", "loopcut"))


class CloudPanelTest(unittest.TestCase):
    def test_signed_out_panel_settings_keep_account_reminder_without_a_dialog(self):
        display = build(service_settings=True, service_signed_in=False)
        self.assertIn('Sign in to Loopcut', texts(display).values())
        self.assertIn(('sign_in', 'loopcut'), [h['action'] for h in display['hits']])
        signed_in = build(service_settings=True, service_signed_in=True)
        self.assertNotIn('Sign in to Loopcut', texts(signed_in).values())

    def test_signed_out_vote_view_has_a_sign_in_note_that_wraps_short_of_the_button(self):
        rows = [idea("future", "Future tool", 0.5)]
        for width in (240, 420, 900):
            with self.subTest(width=width):
                display = vote_view(width, coming_soon=rows, service_signed_in=False)
                sign_in, row = hit(display, "vote.signin"), hit(display, "vote.future")
                self.assertEqual(sign_in["action"], ("sign_in", "loopcut"))
                self.assertGreaterEqual(sign_in["y"], row["y"] + row["h"], "below the ideas")
                self.assertLessEqual(sign_in["x"] + sign_in["w"], width - T.PAD)
                notes = [p for p in display["prims"] if p["id"].startswith("vote.signin.note.")]
                self.assertEqual(" ".join(p["text"] for p in notes), "Sign in to hear when your picks launch.")
                for p in notes:
                    self.assertLessEqual(right(p), sign_in["x"])
                if width == 240:
                    self.assertGreater(len(notes), 1)
                self.assertNotIn("error", " ".join(texts(display).values()).lower())
        signed_in = vote_view(coming_soon=rows, service_signed_in=True)
        self.assertNotIn("vote.signin", ids(signed_in, "hits"))
        self.assertFalse([p for p in signed_in["prims"] if p["id"].startswith("vote.signin")])

    def test_many_ideas_stay_reachable_between_the_header_and_the_footer(self):
        rows = [idea(str(n), "Future tool " + str(n), (17 - n) / 17) for n in range(17)]
        for width in (240, 420, 900):
            with self.subTest(width=width):
                seen, scroll = set(), 0
                while True:
                    display = vote_view(width, 600, coming_soon=rows, service_signed_in=False, vote_scroll=scroll)
                    footer = prim(display, "vote.footer.rule")["y"]
                    for h in display["hits"]:
                        if h["action"][0] == "vote":
                            self.assertGreater(h["y"] + h["h"], T.HEADER_HEIGHT, "culled once above the view")
                            self.assertLess(h["y"], footer, "culled once below the view")
                            if h["y"] >= T.HEADER_HEIGHT and h["y"] + h["h"] <= footer:
                                seen.add(h["action"][1])
                    if scroll >= display["max_scroll"]:
                        break
                    scroll += 100
                self.assertGreater(display["max_scroll"], 0)
                self.assertEqual(seen, {r["name"] for r in rows})
                sign_in = hit(display, "vote.signin")
                self.assertLessEqual(sign_in["y"] + sign_in["h"], footer, "the end of the list clears the footer")
                self.assertEqual(footer, 600 - layout.VOTE_FOOTER_HEIGHT)
    def test_screenshot_fixtures_build_without_blender(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
        from c11_shots import fixtures, STATES
        canned = fixtures(state)
        self.assertEqual(set(canned), set(STATES))
        for name, fixture in canned.items():
            with self.subTest(name=name):
                session = state.new_session()
                session.update({k: v for k, v in fixture.items() if k != "ui"})
                display = layout.build(session, 240 if name == "narrow_job" else 420, 800, 1, measure,
                                       ui=fixture["ui"])
                self.assertIn("input.card", ids(display, "hits"))
                if name == "narrow_job":
                    self.assertIn("cloud.job_fixture.cancel", ids(display, "hits"))

    def test_strip_opens_the_vote_and_names_the_leader(self):
        rows = [idea("sfx", "Sound effects", 0.4), idea("image_generate", "Image generation", 1.0, "most_wanted")]
        display = build(coming_soon=rows)
        strip = hit(display, "vote.strip")
        self.assertEqual(strip["action"], ("votes_open", None))
        self.assertEqual(texts(display)["vote.strip.label"], layout.VOTE_STRIP)
        tail = prim(display, "vote.strip.tail")
        self.assertEqual(tail["text"], "Image generation leads  ›")
        self.assertLessEqual(right(prim(display, "vote.strip.label")), tail["x"])
        self.assertLessEqual(right(tail), strip["x"] + strip["w"])
        for leader in ({"badge": "popular"}, {"badge": None}):  # Only a most-wanted first row is named.
            self.assertEqual(texts(build(coming_soon=[rows[0], {**rows[1], **leader}]))["vote.strip.tail"], "›")
        # The order the vote view froze decides who is first.
        self.assertEqual(texts(build(coming_soon=rows, vote_order=["sfx", "image_generate"]))["vote.strip.tail"], "›")

    def test_narrow_strip_drops_the_leader_but_keeps_the_way_in(self):
        rows = [idea("image_generate", "Image generation", 1.0, "most_wanted")]
        display = layout.build(state.new_session(), 240, 600, 1, measure, ui={"coming_soon": rows})
        strip, tail = hit(display, "vote.strip"), prim(display, "vote.strip.tail")
        self.assertEqual(strip["action"], ("votes_open", None))
        self.assertEqual(tail["text"], "›")
        self.assertLessEqual(right(prim(display, "vote.strip.label")), tail["x"])
        self.assertLessEqual(strip["x"] + strip["w"], 240 - T.PAD)

    def test_strip_is_hidden_without_ideas_and_in_the_vote_view(self):
        rows = [idea("future", "Future tool", 0.5)]
        without, chat_view = build(), build(coming_soon=rows)
        self.assertNotIn("vote.strip", ids(without, "hits"))
        self.assertEqual(prim(without, "input.card")["h"], prim(chat_view, "input.card")["h"])
        self.assertEqual(prim(without, "input.zone")["y"] - prim(chat_view, "input.zone")["y"], 28, "the strip's line")
        voting = build(coming_soon=rows, view="vote")
        self.assertNotIn("vote.strip", ids(voting, "hits"))
        self.assertNotIn("input.card", ids(voting, "hits"), "the footer stands in for the composer")

    def test_vote_states_do_not_move_messages_or_input_during_a_turn(self):
        session = chat(state.item_user("Add a floor"), busy=True)
        changes = ({}, {"state": "sending"}, {"voted": True}, {"level": 1.0, "badge": "most_wanted"},
                   {"level": 0.2, "badge": "popular", "voted": True})
        for width in (240, 420):
            with self.subTest(width=width):
                displays = [layout.build(session, width, 800, 1, measure, ui={
                    "coming_soon": [{**idea(str(n), "Future tool " + str(n)), **change} for n in range(4)],
                    "vote_notice": "All 3 votes used. Take one back first."}) for change in changes]
                for display in displays[1:]:
                    self.assertEqual(prim(display, "input.card"), prim(displays[0], "input.card"))
                    self.assertEqual(prim(display, "item0.user.card"), prim(displays[0], "item0.user.card"))
                    self.assertEqual(hit(display, "vote.strip"), hit(displays[0], "vote.strip"))
                    self.assertNotIn("vote.footer.status.l0", ids(display, "prims"), "the notice is the vote view's")
    def test_cloud_message_is_dismissible_and_keeps_busy_geometry_when_dismissed(self):
        session = chat(state.item_user("Add lighting"), busy=True)
        message = "Update Loopcut to use cloud tools"
        display = build(session, cloud_message=message, cloud_message_reserved=True)
        self.assertEqual(hit(display, "cloud.dismiss")["action"], ("cloud_dismiss", message))
        dismissed = build(session, cloud_message_reserved=True)
        self.assertNotIn("cloud.message", ids(dismissed, "prims"))
        self.assertEqual(prim(display, "input.card"), prim(dismissed, "input.card"))
        self.assertEqual(prim(display, "item0.user.card"), prim(dismissed, "item0.user.card"))

    def test_narrow_job_keeps_percent_and_cancel_separate_from_truncated_title(self):
        row = {"id": "job_test", "title": "A long cloud job title " * 8, "pct": 43,
               "message": "Preparing the scene for a long job", "cancelable": True}
        for view in ("chat", "jobs"):
            display = layout.build(state.new_session(), 240, 600, 1, measure,
                                   ui={"view": view, "cloud_jobs": [row]})
            title, pct = prim(display, "cloud.job_test.title"), prim(display, "cloud.job_test.pct")
            cancel = hit(display, "cloud.job_test.cancel")
            self.assertTrue(title["text"].endswith("…"))
            self.assertEqual(pct["text"], "43%")
            self.assertLessEqual(title["x"] + measure("ui", title["size"], title["text"]), pct["x"])
            self.assertLessEqual(pct["x"] + measure("ui", pct["size"], pct["text"]), cancel["x"])
            self.assertLessEqual(cancel["x"] + cancel["w"], 240 - T.PAD)
            self.assertEqual(cancel["action"], ("cloud_job_cancel", "job_test"))


def status(display):
    return " ".join(p["text"] for p in display["prims"] if p["id"].startswith("vote.footer.status."))


class VoteViewTest(unittest.TestCase):
    def test_rows_rank_by_level_vote_on_click_and_say_what_a_click_does(self):
        rows = [idea("sfx", "Sound effects", 0.4, summary="Sound effects for a shot."),
                idea("render", "Cloud rendering", 1.0, "most_wanted", voted=True, summary="Render on our machines."),
                idea("rig", "Auto-rig")]
        display = vote_view(coming_soon=rows)
        votes = [h for h in display["hits"] if h["action"][0] == "vote"]
        self.assertEqual([h["id"] for h in votes], ["vote.render", "vote.sfx", "vote.rig"], "no level ranks last")
        self.assertEqual([h["action"] for h in votes], [("vote", "render"), ("vote", "sfx"), ("vote", "rig")])
        self.assertEqual([h["y"] for h in votes], sorted(h["y"] for h in votes))
        self.assertEqual(hit(display, "vote.render")["tip"], "Render on our machines.\nClick to take your vote back")
        self.assertEqual(hit(display, "vote.sfx")["tip"], "Sound effects for a shot.\nClick to vote")
        self.assertEqual(hit(display, "vote.rig")["tip"], "Click to vote", "no summary: only what a click does")
        self.assertEqual(texts(display)["vote.render.toggle.glyph"], "✓")
        self.assertEqual(texts(display)["vote.sfx.toggle.glyph"], "▲")
        self.assertEqual(texts(display)["header.crumb"], "›  What's next")
        sending = vote_view(coming_soon=[{**rows[1], "state": "sending"}])
        self.assertEqual(texts(sending)["vote.render.toggle.glyph"], "…")
        # A frozen order wins over the levels; ideas it does not name follow, by level.
        frozen = vote_view(coming_soon=rows, vote_order=["sfx", "render"])
        self.assertEqual([h["id"] for h in frozen["hits"] if h["action"][0] == "vote"],
                         ["vote.sfx", "vote.render", "vote.rig"])

    def test_with_every_vote_placed_the_other_rows_say_to_take_one_back(self):
        rows = [idea(str(n), "Idea " + str(n), 0.5, voted=n < 3) for n in range(4)]
        display = vote_view(coming_soon=rows)
        self.assertEqual(hit(display, "vote.3")["tip"], "All 3 votes are placed. Take one back to move it.")
        self.assertEqual(hit(display, "vote.3")["action"], ("vote", "3"), "the click still reaches the limit notice")
        for n in range(3):
            self.assertEqual(hit(display, "vote." + str(n))["tip"], "Click to take your vote back")

    def test_fill_follows_the_level_and_ideas_without_one_have_none(self):
        rows = [idea("a", "A", 1.0, voted=True), idea("b", "B", 0.25), idea("c", "C", 0.0), idea("d", "D", None),
                idea("e", "E", 1.7)]
        display = vote_view(coming_soon=rows)
        row = prim(display, "vote.a.row")
        fills = {name: prim(display, "vote." + name + ".fill") for name in "abce"}
        self.assertEqual(fills["a"]["w"], row["w"])
        self.assertEqual(fills["b"]["w"], round(row["w"] * 0.25))
        self.assertEqual(fills["c"]["w"], 2 * T.RADIUS, "a sliver, never nothing")
        self.assertEqual(fills["e"]["w"], row["w"], "clamped to the row")
        self.assertNotIn("vote.d.fill", ids(display, "prims"))
        self.assertEqual((fills["a"]["x"], fills["a"]["y"], fills["a"]["h"]), (row["x"], row["y"], row["h"]))
        self.assertNotEqual(fills["a"]["color"], fills["b"]["color"], "your own picks are stronger")
        self.assertNotEqual(row["border_color"], prim(display, "vote.b.row")["border_color"])

    def test_narrow_rows_drop_the_badge_and_keep_the_whole_title(self):
        rows = [idea("image_generate", "Image generation", 1.0, "most_wanted"),
                idea("sfx", "Sound effects", 0.8, "popular"), idea("odd", "Odd", 0.5, "legendary")]
        wide = vote_view(900, coming_soon=rows)
        self.assertEqual(texts(wide)["vote.image_generate.badge.label"], "Most wanted")
        self.assertEqual(texts(wide)["vote.sfx.badge.label"], "Popular")
        self.assertNotIn("vote.odd.badge", ids(wide, "prims"), "only the badges Loopcut knows")
        title, badge = prim(wide, "vote.image_generate.title"), prim(wide, "vote.image_generate.badge")
        self.assertLessEqual(right(title), badge["x"])
        self.assertLessEqual(badge["x"] + badge["w"], prim(wide, "vote.image_generate.toggle")["x"])
        narrow = vote_view(240, coming_soon=rows)
        self.assertFalse([p for p in narrow["prims"] if p["id"].endswith(".badge")])
        self.assertEqual(texts(narrow)["vote.image_generate.title"], "Image generation")
        self.assertEqual(texts(narrow)["vote.sfx.title"], "Sound effects")
        long = vote_view(240, coming_soon=[idea("long", "A very long idea title " * 3, 1.0, "most_wanted")])
        title = prim(long, "vote.long.title")
        self.assertTrue(title["text"].endswith("…"))
        self.assertLessEqual(right(title), prim(long, "vote.long.toggle")["x"])

    def test_footer_counts_votes_left_and_a_notice_wins(self):
        for voted, expected in ((0, "3 votes left"), (1, "2 votes left"), (2, "1 vote left"),
                                (3, "All votes placed. Thank you!")):
            with self.subTest(voted=voted):
                display = vote_view(coming_soon=[idea(str(n), "Idea " + str(n), 0.5, voted=n < voted) for n in range(4)])
                self.assertEqual(status(display), expected)
                tokens = [prim(display, "vote.token." + str(n)) for n in range(3)]
                self.assertEqual([t["color"] == T.ACCENT for t in tokens], [n < voted for n in range(3)])
        rows = [idea("a", "A", 0.5, voted=True)]
        notice = "All 3 votes used. Take one back first."
        display = vote_view(coming_soon=rows, vote_notice=notice)
        self.assertEqual(status(display), notice)
        self.assertEqual(prim(display, "vote.footer.status.l0")["color"], T.WARN)
        failed = "Couldn't reach Loopcut; that vote wasn't saved. Try again."
        for width in (240, 420, 900):
            with self.subTest(width=width):
                display = vote_view(width, coming_soon=rows, vote_notice=failed)
                back = hit(display, "vote.back")
                lines = [p for p in display["prims"] if p["id"].startswith("vote.footer.status.")]
                self.assertLessEqual(len(lines), 2)
                if width > 240:
                    self.assertEqual(status(display), failed)
                else:
                    self.assertTrue(lines[-1]["text"].endswith("…"), "two lines, the second cut short")
                for p in lines:
                    self.assertLessEqual(right(p), back["x"])
                    self.assertGreaterEqual(p["x"], prim(display, "vote.token.2")["x"] + prim(display, "vote.token.2")["w"])

    def test_back_button_leaves_and_nothing_overlaps_the_footer(self):
        rows = [idea(str(n), "Idea " + str(n), 0.5) for n in range(3)]
        for width in (240, 420):
            with self.subTest(width=width):
                display = vote_view(width, 600, coming_soon=rows, service_signed_in=False)
                back = hit(display, "vote.back")
                self.assertEqual(back["action"], ("history_close", None))
                self.assertLessEqual(back["x"] + back["w"], width - T.PAD)
                top = prim(display, "vote.footer.rule")["y"]
                self.assertEqual(top, 600 - layout.VOTE_FOOTER_HEIGHT)
                self.assertGreaterEqual(back["y"], top)
                self.assertLessEqual(back["y"] + back["h"], 600)
                for p in display["prims"]:
                    if p["id"].startswith(("vote.footer.", "vote.token.", "vote.back")):
                        self.assertGreaterEqual(p["y"], top, p["id"])
                    elif p["id"].startswith("vote.") and not p["id"].startswith("vote.strip"):
                        self.assertLessEqual(p["y"] + p["h"], top, p["id"])

    def test_footnote_says_what_the_bars_mean_or_where_votes_go_without_them(self):
        def footnote(rows):
            display = vote_view(900, coming_soon=rows)
            return " ".join(p["text"] for p in display["prims"] if p["id"].startswith("vote.footnote."))
        self.assertEqual(footnote([idea("a", "A", 1.0), idea("b", "B")]), layout.VOTE_FOOTNOTE)
        self.assertEqual(footnote([idea("a", "A"), idea("b", "B")]), layout.VOTE_FOOTNOTE_OFFLINE)

    def test_the_list_reads_from_the_top_and_its_scroll_is_clamped(self):
        rows = [idea(str(n), "Future tool " + str(n), 0.5) for n in range(17)]
        first = vote_view(420, 600, coming_soon=rows)
        self.assertEqual(prim(first, "vote.title")["y"], T.HEADER_HEIGHT + T.PAD, "top-anchored, unlike the chat")
        top_row = hit(first, "vote.0")
        limit = first["max_scroll"]
        self.assertGreater(limit, 100)
        scrolled = vote_view(420, 600, coming_soon=rows, vote_scroll=40)
        self.assertEqual(hit(scrolled, "vote.0")["y"], top_row["y"] - 40)
        self.assertEqual(vote_view(420, 600, coming_soon=rows, vote_scroll=-50)["prims"], first["prims"])
        self.assertEqual(vote_view(420, 600, coming_soon=rows, vote_scroll=limit + 500)["prims"],
                         vote_view(420, 600, coming_soon=rows, vote_scroll=limit)["prims"])
        self.assertEqual(vote_view(420, 600, coming_soon=rows[:2])["max_scroll"], 0.0)


def worker(id, status, **extra):
    """A row made by state.wave_row, as waves.py makes and updates it."""
    row = state.wave_row(id, extra.pop("deliverable", id.title()), extra.pop("role", "modeling"))
    row.update(status=status, **extra)
    return row


def wave(*rows):
    return state.item_wave("w1", 1, list(rows))


def merge(**extra):
    """A card made by state.item_merge, its lines as waves.card_lines writes them."""
    item = state.item_merge("w1", "Merge: 8 created, 1 replaced, 1 conflicts.",
                            ["+ Armchair", "~ Rug_Fabric", "~ Armchair_Seat (renamed)", "! Armchair_Test (kept aside)",
                             "! scene.camera (conflict)"], "cp1", ["Armchair_Test"])
    item.update(extra)
    return item


def chat(item, busy=False):
    session = state.new_session()
    session["items"].append(item)
    session["busy"] = busy
    return session


class WaveCardTest(unittest.TestCase):
    def test_a_row_per_worker_with_deliverable_status_steps_checks_and_note(self):
        item = wave(worker("armchair", "done", steps=14, checks="5/5"),
                    worker("floor_lamp", "working", steps=3, role="lighting", note="Rolled back an edit to Rug"))
        display = build(chat(item, busy=True))
        t = texts(display)
        self.assertEqual(t["item0.wave.title"], "Wave 1 · 2 workers")
        self.assertEqual([t["item0.wave.row0.name"], t["item0.wave.row1.name"]], ["Armchair", "Floor_Lamp"])
        self.assertEqual([t["item0.wave.row0.status"], t["item0.wave.row1.status"]], ["done", "working"])
        self.assertEqual(t["item0.wave.row0.detail"], "modeling · 14 steps · checks 5/5")
        self.assertEqual(t["item0.wave.row1.detail"], "lighting · 3 steps")
        self.assertEqual(t["item0.wave.row1.note.l0"], "Rolled back an edit to Rug")
        self.assertEqual(prim(display, "item0.wave.row0.glyph")["color"], T.OK)
        self.assertEqual(prim(display, "item0.wave.row1.glyph")["color"], T.ACCENT)
        self.assertLess(prim(display, "item0.wave.row0.name")["y"], prim(display, "item0.wave.row1.name")["y"])
        card = prim(display, "item0.wave.card")
        self.assertGreaterEqual(card["y"] + card["h"], prim(display, "item0.wave.row1.note.l0")["y"] + 16)

    def test_old_saved_rows_display_only_the_steps_taken(self):
        display = build(chat(wave(worker("old", "partial", steps=30, cap=30))))
        self.assertEqual(texts(display)["item0.wave.row0.detail"], "modeling · 30 steps")

    def test_running_rows_can_be_cancelled_one_at_a_time(self):
        item = wave(worker("armchair", "done"), worker("lamp", "working"), worker("rug", "queued"),
                    worker("table", "merging"), worker("shelf", "checking"))
        display = build(chat(item, busy=True))
        self.assertEqual(hit(display, "item0.wave.row1.cancel")["action"], ("worker_cancel", ("w1", "lamp")))
        self.assertEqual(hit(display, "item0.wave.row2.cancel")["action"], ("worker_cancel", ("w1", "rug")))
        self.assertEqual(hit(display, "item0.wave.row4.cancel")["action"], ("worker_cancel", ("w1", "shelf")))
        self.assertTrue(hit(display, "item0.wave.row1.cancel")["tip"])
        cancels = [id for id in ids(display, "hits") if id.endswith(".cancel")]
        self.assertNotIn("item0.wave.row0.cancel", cancels, "a finished worker has nothing to cancel")
        self.assertNotIn("item0.wave.row3.cancel", cancels, "a merge completes or rolls back; it is not cancelled")
        idle = build(chat(item, busy=False))
        self.assertFalse([id for id in ids(idle, "hits") if id.endswith(".cancel")], "no turn, no Cancel")

    def test_queued_rows_have_no_steps_yet_and_every_status_draws(self):
        statuses = list(layout.WORKER_STATUS) + ["something_new"]
        session = chat(wave(*(worker(f"w{k}", s) for k, s in enumerate(statuses))), busy=True)
        base = {"selection": [], "context_budget": 24_000, "account": None}
        t = texts(layout.build(session, 420, 2000, 1.0, measure, "fast", None, base))  # Tall: nothing scrolled away.
        self.assertEqual(t["item0.wave.row0.detail"], "modeling", "queued: no 0/20 steps")
        stopped = statuses.index("stopped")
        self.assertEqual(t[f"item0.wave.row{stopped}.glyph"], "–", "a row saved mid-run reads stopped")
        self.assertEqual(t[f"item0.wave.row{len(statuses) - 1}.status"], "something_new")

    def test_long_names_stop_short_of_the_status_and_details_short_of_cancel(self):
        item = wave(worker("x", "working", deliverable="A very long deliverable name " * 4, role="materials " * 8,
                           steps=12, checks="1/3"))
        display = build(chat(item, busy=True))
        name, status = prim(display, "item0.wave.row0.name"), prim(display, "item0.wave.row0.status")
        self.assertTrue(name["text"].endswith("…"))
        self.assertLessEqual(name["x"] + measure("ui", T.FONT_SIZE, name["text"]), status["x"])
        detail, cancel = prim(display, "item0.wave.row0.detail"), prim(display, "item0.wave.row0.cancel")
        self.assertTrue(detail["text"].endswith("…"))
        self.assertLessEqual(detail["x"] + measure("ui", T.FONT_SIZE_SMALL, detail["text"]), cancel["x"])


class MergeCardTest(unittest.TestCase):
    def test_what_came_in_with_accept_and_restore(self):
        display = build(chat(merge()))
        t = texts(display)
        self.assertEqual(t["item0.merge.title.l0"], "Merge: 8 created, 1 replaced, 1 conflicts.")
        self.assertEqual(t["item0.merge.l0.l0"], "Armchair")
        self.assertEqual([t[f"item0.merge.sign{k}"] for k in range(5)], ["+", "~", "~", "!", "!"])
        self.assertEqual(prim(display, "item0.merge.sign0")["color"], T.OK)
        self.assertEqual(prim(display, "item0.merge.sign4")["color"], T.WARN, "conflicts stand out")
        self.assertEqual(hit(display, "item0.merge.accept")["action"], ("accept_quarantine", 0))
        self.assertEqual(t["item0.merge.accept.label"], "Accept 1 held back")
        self.assertEqual(hit(display, "item0.merge.restore")["action"], ("restore", "cp1"))
        self.assertEqual(prim(display, "item0.merge.card")["border_color"], T.WARN, "something waits on the user")

    def test_accepted_busy_or_expired_takes_the_buttons_away(self):
        accepted = build(chat(merge(resolved="accepted")))
        self.assertNotIn("item0.merge.accept", ids(accepted, "hits"))
        self.assertIn("item0.merge.accepted.l0", ids(accepted, "prims"))
        self.assertIn("item0.merge.restore", ids(accepted, "hits"))
        busy = build(chat(merge(), busy=True))
        self.assertFalse([id for id in ids(busy, "hits") if id.startswith("item0.merge.")])
        base = {"selection": [], "context_budget": 24_000, "account": None}
        expired = layout.build(chat(merge(quarantined=[])), 420, 600, 1.0, measure, "fast", {"cp1": "expired"}, base)
        self.assertFalse([id for id in ids(expired, "hits") if id.startswith("item0.merge.")])

    def test_plain_lines_draw_without_a_sign_and_long_lists_fold(self):
        display = build(chat(merge(lines=["nothing to merge from lamp (cancelled)"], quarantined=[])))
        t = texts(display)
        self.assertNotIn("item0.merge.sign0", t)
        self.assertEqual(t["item0.merge.l0.l0"], "nothing to merge from lamp (cancelled)")
        many = build(chat(merge(lines=[f"+ Box{k}" for k in range(10)])))
        self.assertEqual(texts(many)["item0.merge.more"], "Show 5 more")
        self.assertEqual(hit(many, "item0.merge.more")["action"], ("toggle", 0))


if __name__ == "__main__":
    unittest.main()
