"""The display list for the header, the input footer and the popups, with a stand-in for blf."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import config, state  # noqa: E402
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

    def test_signed_out_reminder_is_below_button_and_wraps_at_narrow_widths(self):
        for width in (240, 420, 900):
            for response in ('sending', 'thanks', 'notify', 'sign_in', 'retry'):
                with self.subTest(width=width, response=response):
                    rows = [{'name': 'future', 'title': 'Future tool', 'state': response, 'notify_sign_in': True}]
                    display = layout.build(state.new_session(), width, 800, 1, measure, ui={'coming_soon': rows})
                    button, sign_in = hit(display, 'cloud.future'), hit(display, 'cloud.future.signin')
                    self.assertEqual(sign_in['action'], ('sign_in', 'loopcut'))
                    self.assertGreaterEqual(sign_in['y'], button['y'] + button['h'])
                    notes = [p for p in display['prims'] if p['id'].startswith('cloud.future.note.')]
                    self.assertEqual(' '.join(p['text'] for p in notes), 'Sign in to Loopcut to hear when it launches')
                    for p in notes:
                        self.assertLessEqual(p['x'] + measure('ui', p['size'], p['text']), sign_in['x'])
                    if response in ('notify', 'sign_in', 'retry'):
                        self.assertEqual(button['action'], ('cloud_interest', 'future'))
                    self.assertNotIn('error', ' '.join(texts(display).values()).lower())

    def test_many_reminders_keep_every_extra_reachable_and_input_below_header(self):
        rows = [{'name': str(n), 'title': 'Future tool ' + str(n), 'state': 'notify',
                 'notify_sign_in': True, 'category': 'Images' if n < 4 else ''} for n in range(17)]
        for width in (240, 420, 900):
            seen, starts, start = set(), [], 0
            while True:
                self.assertNotIn(start, starts)
                starts.append(start)
                display = layout.build(state.new_session(), width, 600, 1, measure,
                                       ui={'coming_soon': rows, 'coming_soon_start': start})
                self.assertGreaterEqual(prim(display, 'input.zone')['y'], T.HEADER_HEIGHT)
                self.assertGreaterEqual(hit(display, 'cloud.toggle')['y'], T.HEADER_HEIGHT)
                seen.update(h['action'][1] for h in display['hits'] if h['action'][0] == 'cloud_interest')
                categories = [p['text'] for p in display['prims'] if p['id'].startswith('cloud.category.')]
                if all(int(h['action'][1]) >= 4 for h in display['hits'] if h['action'][0] == 'cloud_interest'):
                    self.assertEqual(categories, [])
                if 'cloud.next' not in ids(display, 'hits'):
                    break
                start = hit(display, 'cloud.next')['action'][1]
            self.assertEqual(seen, {r['name'] for r in rows})
            self.assertLess(hit(display, 'cloud.previous')['action'][1], start)

    def test_collapsed_box_keeps_heading_and_toggle_only(self):
        rows = [{'name': 'future', 'title': 'Future tool', 'state': 'ready', 'category': 'Images'}]
        expanded = build(coming_soon=rows)
        collapsed = build(coming_soon=rows, coming_soon_expanded=False)
        self.assertEqual(hit(collapsed, 'cloud.toggle')['action'], ('cloud_toggle', None))
        self.assertIn('Optional extras · Coming soon', texts(collapsed).values())
        self.assertNotIn('cloud.future', ids(collapsed, 'hits'))
        self.assertFalse([p for p in collapsed['prims'] if p['id'].startswith('cloud.category.')])
        self.assertLess(prim(collapsed, 'cloud.box')['h'], prim(expanded, 'cloud.box')['h'])

    @mock.patch.object(config, 'BETA', False)  # The beta's tips: test_beta_gates.py.
    def test_privacy_labels_and_refusal_tips_keep_response_geometry_stable(self):
        session = chat(state.item_user('Add a floor'), busy=True)
        rows = [{'name': str(n), 'title': 'Future ' + str(n), 'state': 'ready',
                 'data_label': 'May train (C)', 'reason': 'Private mode blocks this vendor.'} for n in range(4)]
        suffixes = {'ready': '', 'sending': '…', 'thanks': ' · Thank you',
                    'notify': '', 'retry': ' · Retry'}
        for width in (240, 420, 900):
            first = None
            for response, suffix in suffixes.items():
                for row in rows:
                    row['state'] = response
                display = layout.build(session, width, 800, 1, measure, ui={'coming_soon': rows})
                first = first or display
                self.assertEqual(prim(display, 'input.card'), prim(first, 'input.card'))
                self.assertEqual(prim(display, 'item0.user.card'), prim(first, 'item0.user.card'))
                button, label = hit(display, 'cloud.0'), prim(display, 'cloud.0.label')
                self.assertEqual([button[k] for k in ('x', 'y', 'w', 'h')],
                                 [hit(first, 'cloud.0')[k] for k in ('x', 'y', 'w', 'h')])
                self.assertLessEqual(measure('ui', label['size'], label['text']), button['w'] - 16)
                self.assertTrue(label['text'].endswith(suffix))
                if width == 900:
                    self.assertIn(rows[0]['data_label'], label['text'])
                self.assertIn(rows[0]['data_label'], button['tip'])
                self.assertIn(rows[0]['reason'], button['tip'])
                expected = {'thanks': 'Thank you', 'sending': 'Sending', 'notify': 'Tell us',
                            'retry': 'try again', 'ready': 'Tell us'}[response]
                self.assertIn(expected, button['tip'])

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

    def test_optional_extras_use_manifest_titles_and_thank_you(self):
        rows = [{"name": "future", "title": "Manifest title", "state": "ready"}]
        display = build(coming_soon=rows)
        self.assertIn("Optional extras · Coming soon", texts(display).values())
        self.assertEqual(texts(display)["cloud.future.label"], "Manifest title")
        self.assertEqual(hit(display, "cloud.future")["action"], ("cloud_interest", "future"))
        rows[0]["state"] = "thanks"
        clicked = build(coming_soon=rows)
        self.assertEqual(texts(clicked)["cloud.future.label"], "Manifest title · Thank you")
        self.assertEqual(hit(clicked, "cloud.future")["action"], ("noop", None))
        self.assertEqual(prim(display, "input.card"), prim(clicked, "input.card"))

    def test_response_states_do_not_move_messages_or_input_during_a_turn(self):
        session = chat(state.item_user("Add a floor"), busy=True)
        rows = [{"name": str(n), "title": "Future tool " + str(n), "state": "ready"} for n in range(4)]
        for width in (240, 420):
            displays = []
            for response in ("ready", "sending", "thanks", "notify", "retry"):
                for row in rows:
                    row["state"] = response
                displays.append(layout.build(session, width, 800, 1, measure, ui={"coming_soon": rows}))
            for display in displays[1:]:
                self.assertEqual(prim(display, "input.card"), prim(displays[0], "input.card"))
                self.assertEqual(prim(display, "item0.user.card"), prim(displays[0], "item0.user.card"))
        display = build(coming_soon=[{"name": "x", "title": "Tool", "state": "sign_in"}])
        self.assertEqual(hit(display, "cloud.x")["action"], ("cloud_interest", "x"))
        self.assertEqual(hit(display, "cloud.x.signin")["action"], ("sign_in", "loopcut"))

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
