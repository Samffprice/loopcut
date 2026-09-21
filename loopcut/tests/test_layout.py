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
    def test_icon_buttons_with_tooltips_sit_at_the_right(self):
        display = build()
        buttons = [hit(display, id) for id, _, _ in layout.HEADER_BUTTONS]
        self.assertEqual([b["action"][0] for b in buttons], ["close_panel", "open_settings", "history_open", "new_chat"])
        self.assertEqual([b["tip"] for b in buttons], ["Close panel", "Settings", "History", "New chat"])
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
        options = [("fast", "Fast", "Quick answers", True), ("pro", "Pro", "Thinks longer", False)]
        display = build(model_menu=True, model_options=options)
        names = [p["text"] for p in display["prims"] if p["id"].startswith("models.row") and p["id"].endswith(".name")]
        self.assertEqual(names, ["✓  Fast", "     Pro", "Settings…"])
        self.assertEqual(hit(display, "models.row1")["action"], ("model_pick", "pro"))
        self.assertEqual(hit(display, "models.row2")["action"], ("open_settings", None))

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


if __name__ == "__main__":
    unittest.main()
