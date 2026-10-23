"""The display list for the header, the input footer and the popups, with a stand-in for blf."""

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

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


class LimitRecoveryTest(unittest.TestCase):
    def test_expired_account_windows_disappear_from_footer_ring_and_tooltip(self):
        from datetime import datetime
        reset = "2099-10-21T18:00:00Z"
        now = datetime.fromisoformat(reset).timestamp()
        for window in ("session", "week"):
            for offset in (0, 1):
                with self.subTest(window=window, offset=offset):
                    account = {window: {"used": 0.84, "resets_at": reset}}
                    before = build(account=account, now=now - 1, credits="100 credits")
                    self.assertIn("84%", hit(before, "input.credits")["tip"])
                    after = build(account=account, now=now + offset, credits="100 credits")
                    self.assertNotIn("Chat:", hit(after, "input.credits")["tip"])
                    self.assertNotIn("chat allowance", hit(after, "input.rings")["tip"])
                    self.assertEqual([p["id"] for p in after["prims"] if p["t"] == "ring"], ["input.ring.context"])
                    account[window]["resets_at"] = "2099-10-22T18:00:00Z"
                    fresh = build(account=account, now=now + offset, credits="100 credits")
                    self.assertIn("84%", hit(fresh, "input.credits")["tip"])

    def test_refused_window_ring_and_tooltip_are_exhausted_without_mutating_usage(self):
        account = {"plan": "free", "session": {"used": 0.82, "used_with_reservations": 0.92},
                   "week": {"used": 0.48, "used_with_reservations": 0.58}}
        before = deepcopy(account)
        for window in ("session", "week"):
            for reported in (account, None):
                with self.subTest(window=window, reported=reported):
                    card = self.card(window + "_limit")
                    display = build(account=reported, allowance_notice={"item": card, "conversation": "other"})
                    rings = [p for p in display["prims"] if p["t"] == "ring"]
                    self.assertEqual(len(rings), 2)
                    self.assertEqual((rings[1]["id"], rings[1]["share"]), ("input.ring." + window, 0.0))
                    self.assertIn(f"{window.capitalize()} chat allowance: 100% used, 0% left", hit(display, "input.rings")["tip"])
        self.assertEqual(account, before)

    def test_refusal_override_expires_at_reset_even_without_an_account_refresh(self):
        from datetime import datetime, timezone
        reset = datetime(2099, 10, 21, 18, tzinfo=timezone.utc).timestamp()
        for window in ("session", "week"):
            with self.subTest(window=window):
                card = self.card(window + "_limit")
                account = {"plan": "free", "session": {"used": 0.82}, "week": {"used": 0.42}}
                notice = {"item": card}
                before = build(account=account, allowance_notice=notice, now=reset - 1)
                self.assertIn(f"{window.capitalize()} chat allowance: 100% used, 0% left", hit(before, "input.rings")["tip"])
                after = build(account=account, allowance_notice=notice, now=reset)
                self.assertEqual(hit(after, "input.rings")["tip"], hit(build(account=account), "input.rings")["tip"])
                self.assertFalse(any(p["id"].startswith("input.limit") for p in after["prims"]))

    def test_unknown_reset_keeps_override_and_resolved_or_other_limits_do_not_override(self):
        account = {"session": {"used": 0.82}}
        for reset in ("", "bad date"):
            card = self.card()
            card["resets_at"] = reset
            notice = {"item": card}
            self.assertEqual(layout.ring_shares({}, 1, account, notice)[1][1], 0)
            card["resolved"] = True
            self.assertAlmostEqual(layout.ring_shares({}, 1, account, notice)[1][1], 0.18)
        for reason in ("model_not_in_plan", "billing"):
            self.assertAlmostEqual(layout.ring_shares({}, 1, account, {"item": self.card(reason)})[1][1], 0.18)

    def test_old_cached_reset_does_not_cancel_a_fresh_refusal(self):
        account = {"plan": "free", "session": {"used": 0.82, "resets_at": "2099-10-20T18:00:00Z"}}
        self.assertEqual(layout.ring_shares({}, 1, account, {"item": self.card()})[1][1], 0)

    def card(self, reason="session_limit"):
        with patch.object(layout.config, "billing_open", return_value=True):
            return state.item_limit("Chat allowance used.", reason, "free", "2099-10-21T18:00:00Z",
                                    {"label": "Upgrade to Pro", "url": "https://loopcut.org/pricing"})

    @patch.object(layout.config, "billing_open", lambda: True)
    def test_newest_unresolved_limit_keeps_actions_after_scene_changes_and_notices(self):
        session = state.new_session()
        session["items"] = [self.card(), self.card(), state.item_changes("49 objects added", [], ""),
                            state.item_notice("Scene saved.")]
        drawn = build(session, chatgpt_plan=True)
        self.assertFalse(any(h["id"].startswith("item0.limit.") for h in drawn["hits"]))
        self.assertEqual(hit(drawn, "item1.limit.upgrade")["action"], ("limit_upgrade", 1))
        self.assertEqual(hit(drawn, "item1.limit.chatgpt")["action"], ("limit_chatgpt", 1))
        session["items"][1]["resolved"] = True
        self.assertFalse(any(".limit.upgrade" in h["id"] for h in build(session)["hits"]),
                         "resolving the latest card never revives older limits")
        session["items"][1].update(resolved=False, status="done")
        self.assertFalse(any(".limit.upgrade" in h["id"] for h in build(session)["hits"]))

    @patch.object(layout.config, "billing_open", lambda: True)
    def test_model_limit_keeps_fast_action_after_scene_changes(self):
        session = state.new_session()
        session["items"] = [self.card("model_not_in_plan"), state.item_changes("Objects added", [], "")]
        self.assertEqual(hit(build(session), "item0.limit.fast")["action"], ("limit_fast", 0))

    def test_credit_explanation_only_with_a_positive_balance(self):
        session = state.new_session()
        session["items"] = [self.card()]
        sentence = "Your credits pay for cloud tools and do not add chat time."
        for balance in (None, 0, 100):
            drawn = build(session, credit_balance=balance)
            text = " ".join(p.get("text", "") for p in drawn["prims"])
            self.assertEqual(sentence in text, balance == 100)

    def test_input_recovery_actions_stay_visible_and_match_card_at_narrow_width(self):
        session = state.new_session()
        card = self.card()
        session["items"] = [card, state.item_changes("49 objects added", [], "")]
        session["input"] = "make more details"
        session["cursor"] = len(session["input"])
        for width in (260, 420):
            for billing in (False, True):
                with self.subTest(width=width, billing=billing), \
                        patch.object(layout.config, "billing_open", return_value=billing):
                    drawn = layout.build(session, width, 900, 1, measure, ui={"allowance_notice": {"item": card, "conversation": session["id"]}, "chatgpt_plan": True})
                    inputs = [h for h in drawn["hits"] if h["id"].startswith("input.limit.")]
                    cards = [h for h in drawn["hits"] if h["id"].startswith("item0.limit.")]
                    self.assertEqual([h["action"] for h in inputs], [h["action"] for h in cards])
                    self.assertEqual(any(h["action"][0] == "limit_upgrade" for h in inputs), billing)
                    text = " ".join(p.get("text", "") for p in drawn["prims"] if p["id"].startswith("input.limit.text"))
                    self.assertIn("Resets " + state.reset_label(card["resets_at"]), text)
                    for h in inputs:
                        self.assertGreaterEqual(h["y"], 0)
                        self.assertLess(h["y"] + h["h"], drawn["input"]["y"])
                        self.assertLessEqual(h["x"] + h["w"], width)
        # The account notice still offers recovery in a brand new conversation.
        session["items"] = []
        with patch.object(layout.config, "billing_open", return_value=True):
            self.assertEqual(hit(build(session, allowance_notice={"item": card, "conversation": "another"}), "input.limit.upgrade")["action"],
                             ("limit_upgrade", None))

    def test_retry_requires_a_stopped_turn_in_this_conversation(self):
        session = state.new_session()
        card = self.card()
        session["items"] = [card]
        self.assertFalse(any(h["action"][0] == "limit_retry" for h in build(session)["hits"]))
        session["messages"] = [{"role": "tool", "content": "Stopped after a tool."}]
        self.assertEqual(hit(build(session), "item0.limit.retry")["action"], ("limit_retry", 0))
        session["items"] = []
        foreign = {"item": card, "conversation": "another"}
        self.assertFalse(any(h["action"][0] == "limit_retry" for h in build(session, allowance_notice=foreign)["hits"]))
        local = {"item": card, "conversation": session["id"]}
        self.assertEqual(hit(build(session, allowance_notice=local), "input.limit.retry")["action"],
                         ("limit_retry", session["id"]))
        session["messages"].append({"role": "assistant", "content": "Done."})
        self.assertFalse(any(h["action"][0] == "limit_retry" for h in build(session, allowance_notice=local)["hits"]))

    def test_notice_names_concrete_options_and_explains_credits(self):
        session = state.new_session()
        card = self.card()
        card["resets_at"] = ""
        drawn = build(session, allowance_notice={"item": card, "conversation": session["id"]}, credit_balance=100)
        text = " ".join(p.get("text", "") for p in drawn["prims"])
        self.assertIn("Use my own API key", text)
        self.assertIn("Sign in with ChatGPT", text)
        self.assertNotIn("Try another route", text)
        self.assertIn("Your credits pay for cloud tools and do not add chat time.", text)

    def test_limit_card_formats_its_reset_in_local_time_too(self):
        session = state.new_session()
        card = self.card()
        session["items"] = [card]
        text = " ".join(p.get("text", "") for p in build(session)["prims"])
        self.assertIn(state.reset_label(card["resets_at"]), text)
        self.assertNotIn(card["resets_at"], text)

    def test_reset_labels_use_explicit_local_date_or_short_relative_wait(self):
        from datetime import datetime
        reset = datetime(2026, 10, 22, 13, 0).astimezone()
        stamp = reset.isoformat()
        self.assertEqual(state.reset_label(stamp, now=reset.timestamp() - 2 * 86400),
                         "Thu 22 Oct, 13:00 (your time)")
        self.assertEqual(state.reset_label(stamp, now=reset.timestamp() - 4 * 3600), "in about 4 h")
        self.assertEqual(state.reset_label(stamp, now=reset.timestamp() - 1200), "in under an hour")
        self.assertEqual(state.reset_label("bad date"), "bad date")
        self.assertEqual(state.reset_label(""), "")

    def test_date_only_reset_keeps_its_calendar_date(self):
        from datetime import datetime, timezone
        midnight = datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp()
        for now in (midnight - 1200, midnight + 2 * 86400):
            with self.subTest(now=now):
                self.assertEqual(state.reset_label("2026-10-12", now=now), "2026-10-12")


class InputPointTest(unittest.TestCase):
    def display(self, text, width=420, scale=1):
        session = state.new_session()
        session.update(input=text, cursor=len(text), focused=True)
        return layout.build(session, width, 600, scale, measure)

    def test_click_tracks_rewrapped_lines_at_two_widths(self):
        text = "one two three four five six seven eight nine ten"
        for width, second_start in ((180, 14), (240, 24)):
            with self.subTest(width=width):
                display = self.display(text, width)
                line = prim(display, "input.l1")
                self.assertTrue(text[second_start:].startswith(line["text"]))
                x, y = line["x"] + measure("ui", line["size"], "ab"), line["y"] + line["h"] / 2
                self.assertEqual(layout.input_index(display, x, y, measure), second_start + 2)

    def test_click_uses_glyph_midpoints_and_scaled_font(self):
        def proportional(font, size, text):
            self.assertEqual(font, "ui")
            return sum(0.8 if ch == "W" else 0.2 for ch in text) * size

        session = state.new_session()
        session.update(input="Wi", cursor=2)
        display = layout.build(session, 420, 600, 1.5, proportional)
        line = prim(display, "input.l0")
        self.assertEqual(line["size"], round(T.FONT_SIZE * 1.5))
        for dx, expected in ((0.39, 0), (0.41, 1), (0.89, 1), (0.91, 2)):
            with self.subTest(dx=dx):
                self.assertEqual(layout.input_index(display, line["x"] + dx * line["size"],
                                                    line["y"], proportional), expected)

    def test_line_ends_and_points_outside_visible_text_are_clamped(self):
        display = self.display("one two three four five six seven eight nine ten", 180)
        first, last = prim(display, "input.l0"), prim(display, "input.l3")
        self.assertEqual(layout.input_index(display, 1000, first["y"], measure), 13,
                         "past a wrapped line stops before its hidden trailing space")
        self.assertEqual(layout.input_index(display, -100, -100, measure), 0)
        self.assertEqual(layout.input_index(display, last["x"], 1000, measure), 45)
        self.assertEqual(layout.input_index(display, 1000, 1000, measure), 48)
        display = self.display("ab  \ncd")
        self.assertEqual(layout.input_index(display, 1000, prim(display, "input.l0")["y"], measure), 4,
                         "explicit line endings retain their spaces")
        self.assertEqual(layout.input_index(self.display(""), 1000, 1000, measure), 0)

    def test_hard_wrap_right_edge_keeps_caret_on_clicked_row(self):
        text = "abcdefghijklmnopqrstuvwxyz"
        for width in (180, 240):
            display = self.display(text, width)
            first, second = prim(display, "input.l0"), prim(display, "input.l1")
            edge = first["x"] + measure("ui", first["size"], first["text"])
            for x in (edge, edge + 100):
                with self.subTest(width=width, x=x):
                    index = layout.input_index(display, x, first["y"] + first["h"] / 2, measure)
                    self.assertEqual(index, len(first["text"]) - 1)
                    session = state.new_session()
                    session.update(input=text, cursor=index, focused=True)
                    caret = prim(layout.build(session, width, 600, 1, measure), "input.caret")
                    self.assertLessEqual(first["y"], caret["y"])
                    self.assertLess(caret["y"], first["y"] + first["h"])
            self.assertEqual(layout.input_index(display, second["x"], second["y"], measure), len(first["text"]))
            self.assertEqual(layout.input_index(display, 1000, second["y"], measure), len(text))

    def test_scrolled_lines_and_selection_share_drawn_positions(self):
        text = "\n".join("abc" for _ in range(T.INPUT_MAX_LINES + 3))
        display = self.display(text)
        first = prim(display, "input.l0")
        self.assertEqual(layout.input_index(display, first["x"], -100, measure), 12)
        session = state.new_session()
        session.update(input=text, cursor=18, anchor=13, focused=True)
        display = layout.build(session, 420, 600, 1, measure)
        selection = prim(display, "input.selection0")
        self.assertEqual(selection["color"], T.SELECTION)
        self.assertEqual(layout.input_index(display, selection["x"], selection["y"], measure), 13)
        self.assertEqual(layout.input_index(display, selection["x"] + selection["w"],
                                            selection["y"], measure), 15)
        caret = prim(display, "input.caret")
        self.assertEqual(layout.input_index(display, caret["x"], caret["y"], measure), 18)
        self.assertLess(ids(display, "prims").index("input.selection0"), ids(display, "prims").index("input.l0"))


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


class JobsTest(unittest.TestCase):
    def test_live_process_is_visible_without_durable_or_cloud_jobs(self):
        row = {"id": "process1", "label": "Python script", "elapsed": 125.9,
               "log": "/workspace/logs/process.log"}
        display = build(view="jobs", jobs=[], cloud_jobs=[], local_processes=[row])
        text = " ".join(p["text"] for p in display["prims"] if p["t"] == "text")
        self.assertIn("Running · Python script", text)
        self.assertIn("2m 05s", text)
        self.assertIn("process.log", text)
        self.assertNotIn("/workspace", text)
        self.assertNotIn("No background jobs yet", text)
        log = next(p for p in display["prims"] if p.get("text") == "process.log")
        self.assertEqual(log["color"], T.TEXT_MUTED)

    def test_empty_jobs_still_show_empty_message(self):
        display = build(view="jobs", jobs=[], cloud_jobs=[], local_processes=[])
        text = " ".join(p["text"] for p in display["prims"] if p["t"] == "text")
        self.assertIn("No background jobs yet", text)


class FailureReasonTest(unittest.TestCase):
    def test_last_exception_skips_warnings_and_trailing_noise(self):
        output = ("Warning: unavailable device\nValueError: first failure\n"
                  "Traceback (most recent call last):\n  File \"<string>\", line 1\n"
                  "AssertionError: invalid result\n<string>:2: UserWarning: cleanup warning\n"
                  "Time: 00:00.10\nCleanup complete\n\n")
        self.assertEqual(layout.failure_reason(output), "AssertionError: invalid result")

    def test_error_and_shell_lines_survive_trailing_noise(self):
        for reason in ("Error: operation failed", "AssertionError", "custom.FailureException: stopped",
                       "zsh:1: read-only variable: status", "bash: line 1: missing: command not found",
                       "/bin/sh: missing: not found", "utility: cannot open file"):
            with self.subTest(reason=reason):
                self.assertEqual(layout.failure_reason(reason + "\nCleanup complete\n"), reason)

    def test_error_message_can_mention_a_warning(self):
        reason = "ValueError: warning limit exceeded"
        self.assertEqual(layout.failure_reason(reason + "\nCleanup complete"), reason)

    def test_unrecognized_output_uses_last_nonempty_line(self):
        self.assertEqual(layout.failure_reason("Starting\n  Process stopped unexpectedly  \n\n"),
                         "Process stopped unexpectedly")

    def test_empty_placeholder_and_warning_only_output_use_fallback(self):
        for output in ("", " \n\t", "(no output)", "\n (no output) \n", "Warning: unavailable device"):
            with self.subTest(output=output):
                self.assertEqual(layout.failure_reason(output), "Failed with no error output.")

    def test_fallback_includes_stored_exit_code(self):
        for code in (1, 2, -9):
            with self.subTest(code=code):
                self.assertEqual(layout.failure_reason("(no output)", code),
                                 f"Failed with no error output (exit {code}).")

    def test_exit_header_is_metadata_not_error_output(self):
        self.assertEqual(layout.failure_reason("exit 2 in 0.1 s\n(no output)"),
                         "Failed with no error output (exit 2).")
        self.assertEqual(layout.failure_reason("exit None in 0.1 s\n(no output)"),
                         "Failed with no error output.")
        self.assertEqual(layout.failure_reason("exit 1 in 0.1 s\nError: invalid input"),
                         "Error: invalid input")


class ToolCardLabelTest(unittest.TestCase):
    def display(self, summary, expanded=False):
        session = state.new_session()
        item = state.item_tool("get_scene_info", summary, "")
        item.update(status="done", expanded=expanded)
        session["items"].append(item)
        original = dict(item)
        display = build(session)
        self.assertEqual(item, original, "layout must preserve the stored identifier and summary")
        return display

    def test_duplicate_identifier_is_drawn_once(self):
        for summary in ("", "get_scene_info"):
            for expanded in (False, True):
                with self.subTest(summary=summary, expanded=expanded):
                    display = self.display(summary, expanded)
                    labels = [p["text"] for p in display["prims"]
                              if p["t"] == "text" and p["id"].startswith("item0.tool.")]
                    self.assertEqual(labels.count("get_scene_info"), 1)
                    self.assertNotIn("item0.tool.name", ids(display, "prims"))

    def test_collapsed_card_gives_the_title_the_available_width(self):
        title = "Read the current scene information"
        display = self.display(title)
        self.assertEqual(prim(display, "item0.tool.summary")["text"], title)
        self.assertNotIn("item0.tool.name", ids(display, "prims"))

    def test_expanded_card_retains_the_technical_identifier(self):
        display = self.display("Scene information", expanded=True)
        self.assertEqual(prim(display, "item0.tool.summary")["text"], "Scene information")
        self.assertEqual(prim(display, "item0.tool.name")["text"], "get_scene_info")
        self.assertEqual(hit(display, "item0.tool.card")["action"], ("toggle", 0))

    def test_awaiting_summary_is_complete_at_panel_sizes(self):
        summary = "The change is ready for review. Please check the details before continuing."
        code = "\n".join(["pass"] * (T.CODE_PREVIEW_LINES + 2))
        for width, scale in ((240, 1), (420, 1), (438, 1), (640, 1.5)):
            for expanded in (False, True):
                with self.subTest(width=width, scale=scale, expanded=expanded):
                    item = state.item_tool("run_python", summary, code)
                    item.update(status="awaiting", expanded=expanded)
                    original = deepcopy(item)
                    frame = layout.Frame(width, 600, scale, measure)
                    x, y = frame.px(T.PAD), frame.px(9)
                    height = layout._item_tool(frame, 0, item, x, y, width - 2 * x)
                    display = {"prims": frame.prims, "hits": frame.hits}
                    card = prim(display, "item0.tool.card")
                    rows = [p for p in frame.prims if p["id"].startswith("item0.tool.summary")]
                    self.assertEqual(" ".join(p["text"] for p in rows), summary)
                    glyph = prim(display, "item0.tool.glyph")
                    self.assertEqual(rows[0]["y"], glyph["y"])
                    for row in rows:
                        self.assertEqual(row["x"], glyph["x"] + frame.px(18))
                    for previous, current in zip(rows, rows[1:]):
                        self.assertEqual(current["y"], previous["y"] + previous["h"])

                    names = [p for p in frame.prims if p["id"].startswith("item0.tool.name")]
                    self.assertEqual([p["text"] for p in names], ["run_python"])
                    name = names[0]
                    self.assertEqual(name["size"], frame.px(T.FONT_SIZE_SMALL))
                    self.assertGreaterEqual(name["y"], rows[-1]["y"] + rows[-1]["h"])
                    preview = prim(display, "item0.tool.code.bg")
                    self.assertGreaterEqual(preview["y"], name["y"] + name["h"])
                    code_rows = [p for p in frame.prims if p["id"].startswith("item0.tool.code.l")]
                    self.assertEqual([p["text"] for p in code_rows], ["pass"] * T.CODE_PREVIEW_LINES)
                    self.assertEqual(prim(display, "item0.tool.code.more")["text"], "… 2 more lines")
                    self.assertGreaterEqual(hit(display, "item0.tool.run")["y"],
                                            preview["y"] + preview["h"])

                    self.assertEqual(card["h"], height)
                    for p in frame.prims:
                        self.assertGreaterEqual(p["y"], card["y"], p["id"])
                        self.assertLessEqual(p["y"] + p["h"], card["y"] + height, p["id"])
                        if p["t"] == "text":
                            self.assertGreaterEqual(p["x"], card["x"] + frame.px(T.CARD_PAD), p["id"])
                            self.assertLessEqual(p["x"] + measure(p["font"], p["size"], p["text"]),
                                                 card["x"] + card["w"] - frame.px(T.CARD_PAD), p["id"])
                    self.assertEqual(item, original)
                    actions = [("approve", 0), ("reject", 0)]
                    if width != 240:
                        actions.append(("approve_always", 0))
                    self.assertEqual([h["action"] for h in frame.hits], actions)


class ApprovalScopeTest(unittest.TestCase):
    def display(self, index=0, **flags):
        session = state.new_session()
        session["items"] = [state.item_user("Continue.") for _ in range(index)]
        item = state.item_tool("tool", "Pending approval", "")
        item.update(status="awaiting", **flags)
        session["items"].append(item)
        # Leave room for the longer "Always allow renders" button.
        return layout.build(session, 640, 600, 1, measure)

    def test_ordinary_allowance_tip_explains_scope_and_exceptions(self):
        tip = hit(self.display(), "item0.tool.always")["tip"]
        self.assertTrue(tip)
        for phrase in ("ordinary steps", "edits", "code", "files", "this chat",
                       "Renders/bakes ask separately", "installs and paid steps ask",
                       "reopen this chat", "new chat"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, tip)
        self.assertLessEqual(len(tip.splitlines()), 3)

    def test_heavy_allowance_tip_explains_separate_scope(self):
        tip = hit(self.display(heavy=True), "item0.tool.always")["tip"]
        self.assertTrue(tip)
        for phrase in ("only renders, bakes and simulations", "this chat",
                       "Code approval is separate", "installs and paid steps still ask",
                       "reopen this chat", "new chat"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, tip)
        self.assertLessEqual(len(tip.splitlines()), 3)

    def test_allowance_buttons_keep_action(self):
        for heavy in (False, True):
            for index in (0, 2):
                with self.subTest(heavy=heavy, index=index):
                    display = self.display(index, heavy=heavy)
                    self.assertEqual(hit(display, f"item{index}.tool.always")["action"],
                                     ("approve_always", index))

    def test_install_and_paid_cards_have_no_allowance(self):
        for flag in ("install", "paid_approval"):
            for heavy in (False, True):
                with self.subTest(flag=flag, heavy=heavy):
                    display = self.display(heavy=heavy, **{flag: True})
                    self.assertEqual(hit(display, "item0.tool.run")["action"], ("approve", 0))
                    self.assertNotIn("item0.tool.always", ids(display, "hits"))
                    self.assertFalse(any(h["action"][0] == "approve_always" for h in display["hits"]))


class ToolFailureCardTest(unittest.TestCase):
    def display(self, output, width=420, **changes):
        item = {"kind": "tool", "name": "shell", "summary": "Run command", "code": "",
                "status": "failed", "expanded": False, "output": output, **changes}
        session = state.new_session()
        session["items"].append(item)
        original = dict(item)
        display = layout.build(session, width, 600, 1, measure)
        self.assertEqual(session["items"], [original], "layout must not change the stored card")
        self.assertEqual(item["output"], output)
        return display

    def reason(self, display):
        reasons = [p for p in display["prims"] if p["id"].startswith("item0.tool.reason")]
        self.assertEqual(len(reasons), 1)
        reason = reasons[0]
        self.assertEqual(reason["t"], "text")
        self.assertEqual(reason["color"], T.TEXT_MUTED)
        title, card = prim(display, "item0.tool.summary"), prim(display, "item0.tool.card")
        self.assertGreaterEqual(reason["y"], title["y"] + title["h"])
        self.assertLessEqual(reason["y"] + reason["h"], card["y"] + card["h"])
        self.assertFalse(any(p["id"].startswith("item0.tool.output") for p in display["prims"]))
        self.assertEqual(hit(display, "item0.tool.card")["action"], ("toggle", 0))
        return reason

    def test_collapsed_traceback_shows_final_exception(self):
        output = ("Warning: unavailable device\nTraceback (most recent call last):\n"
                  "  File \"<string>\", line 1\nAssertionError: invalid result\nCleanup complete\n")
        reason = self.reason(self.display(output))
        self.assertEqual(reason["text"], "AssertionError: invalid result")

    def test_collapsed_shell_failure_shows_error_line(self):
        error = "zsh:1: read-only variable: status"
        self.assertEqual(self.reason(self.display("exit 1 in 0.1 s\n" + error))["text"], error)

    def test_collapsed_empty_output_shows_honest_fallback(self):
        for output in ("", "(no output)"):
            with self.subTest(output=output):
                self.assertEqual(self.reason(self.display(output))["text"], "Failed with no error output.")
        self.assertEqual(self.reason(self.display("", exit_code=2))["text"],
                         "Failed with no error output (exit 2).")

    def test_long_reason_is_ellipsized_to_narrow_card_width(self):
        for output in ("Error: " + "invalid input " * 20, "Error:" + "x" * 200):
            with self.subTest(output=output):
                display = self.display(output, width=180)
                reason = self.reason(display)
                card = prim(display, "item0.tool.card")
                self.assertTrue(reason["text"].endswith("…"))
                self.assertTrue(output.startswith(reason["text"][:-1]))
                self.assertNotIn("\n", reason["text"])
                self.assertLessEqual(right(reason), card["x"] + card["w"] - T.CARD_PAD)

    def test_collapsed_success_and_running_cards_keep_only_the_title(self):
        for status in ("done", "running"):
            with self.subTest(status=status):
                display = self.display("Error: old output", status=status)
                tool_ids = [p["id"] for p in display["prims"] if p["id"].startswith("item0.tool.")]
                self.assertEqual(tool_ids, ["item0.tool.card", "item0.tool.glyph", "item0.tool.summary"])
                self.assertEqual(prim(display, "item0.tool.card")["h"],
                                 prim(display, "item0.tool.summary")["h"] + 14)

    def test_expanded_failure_keeps_full_output_without_reason_line(self):
        output = "Warning: unavailable device\nError: invalid input\nCleanup complete"
        display = self.display(output, expanded=True, code="pass")
        self.assertFalse(any(p["id"].startswith("item0.tool.reason") for p in display["prims"]))
        lines = [p for p in display["prims"] if p["id"].startswith("item0.tool.output")]
        self.assertEqual([p["text"] for p in lines], output.splitlines())
        self.assertTrue(all(p["color"] == T.ERROR and p["font"] == "mono" for p in lines))
        self.assertEqual(prim(display, "item0.tool.code.l0")["text"], "pass")
        self.assertEqual(hit(display, "item0.tool.card")["action"], ("toggle", 0))


class ErrorCardTest(unittest.TestCase):
    def test_a_dropped_connection_offers_try_again_while_it_is_the_newest_item(self):
        session = state.new_session()
        session["items"].append(state.item_error("The connection to ChatGPT dropped before it could answer.", retry=True))
        hits = {h["id"]: h for h in build(session)["hits"]}
        self.assertEqual(hits["item0.error.retry"]["action"], ("resume", 0))
        session["busy"] = True
        self.assertNotIn("item0.error.retry", ids(build(session), "hits"))
        session["busy"] = False
        session["items"].append(state.item_user("next"))
        self.assertNotIn("item0.error.retry", ids(build(session), "hits"))

    def test_other_errors_have_no_button(self):
        session = state.new_session()
        session["items"].append(state.item_error("HTTP 400: bad model"))
        self.assertFalse([h for h in build(session)["hits"] if h["id"].startswith("item0.error")])


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
                               "Session chat allowance: 25% used, 75% left", "Week chat allowance: 50% used, 50% left"])

    def test_reservations_drive_ring_tooltip_and_usage_note_with_defensive_fallback(self):
        for value, expected in ((0.91, 0.91), ("0.9", 0.9), (0, 0), (1.2, 1),
                                (None, 0.82), ("bad", 0.82), (float("nan"), 0.82),
                                (float("inf"), 0.82), (-1, 0.82), (True, 0.82), ({}, 0.82), ([], 0.82)):
            with self.subTest(value=value):
                account = {key: {"used": 0.82, "used_with_reservations": value} for key in ("session", "week")}
                display = build(account=account)
                ring = next(p for p in display["prims"] if p["id"] == "input.ring.session")
                self.assertAlmostEqual(ring["share"], 1 - expected)
                for label in ("Session", "Week"):
                    self.assertIn(f"{label} chat allowance: {round(expected * 100)}% used, {round((1 - expected) * 100)}% left",
                                  hit(display, "input.rings")["tip"])
                self.assertEqual(layout.usage_note(account)[1], expected)
        for window in (None, [], "bad", {"used": "nan", "used_with_reservations": False}):
            display = build(account={"session": window, "week": window})
            self.assertEqual(len([p for p in display["prims"] if p["t"] == "ring"]), 1)

    def test_high_chat_usage_is_visible_beside_credits_with_room_at_narrow_widths(self):
        for width in (240, 320, 420):
            for scale in (1, 2):
                for plan in ("free", "pro", "max"):
                    with self.subTest(width=width, scale=scale, plan=plan):
                        session = state.new_session()
                        session["input"] = "Keep working"
                        ui = {"credits": "100 credits ($1.00)", "provider": "LOOPCUT",
                              "account": {"plan": plan, "session": {"used": 0.3, "used_with_reservations": 0.74}}}
                        drawn = layout.build(session, width * scale, 600 * scale, scale, measure, ui=ui)
                        lines = [p for p in drawn["prims"] if p["id"].startswith("input.credits")]
                        self.assertEqual(lines[0]["text"], "100 credits ($1.00)")
                        self.assertIn("Chat: 74% of this 5-hour window used", " ".join(p["text"] for p in lines))
                        for line in lines:
                            self.assertLessEqual(line["x"] + measure("ui", line["size"], line["text"]),
                                                 (width - T.PAD - T.CARD_PAD) * scale)
                        model = prim(drawn, "input.model")
                        self.assertLess(lines[-1]["y"] + 16 * scale, model["y"])
                        input_lines = [p for p in drawn["prims"] if p["id"] == "input.l0"]
                        self.assertTrue(input_lines)
                        self.assertLess(max(p["y"] + 16 * scale for p in input_lines), lines[0]["y"])

    def test_chat_footer_threshold_fuller_window_and_provider_filtering(self):
        for used, expected in ((0.69, ""), (0.7, "Chat: 70% of this 5-hour window used"),
                               (1, "Chat: 100% of this 5-hour window used")):
            with self.subTest(used=used):
                drawn = build(credits="100 credits", account={"session": {"used": used}})
                text = " ".join(p["text"] for p in drawn["prims"] if p["id"].startswith("input.credits"))
                if expected:
                    self.assertIn(expected, text)
                else:
                    self.assertEqual(text, "100 credits This chat: 0 credits")
                    self.assertEqual(prim(drawn, "input.zone")["y"], prim(build(credits="100 credits"), "input.zone")["y"])
        account = {"session": {"used": 0.74}, "week": {"used": 0.91}}
        self.assertIn("Chat: 91% of this week used", hit(build(account=account), "input.credits")["tip"])
        for provider in ("OPENAI", "CUSTOM", "CHATGPT"):
            with self.subTest(provider=provider):
                drawn = build(account=account, provider=provider, credits="100 credits", chatgpt_plan=True)
                self.assertNotIn("Chat:", hit(drawn, "input.credits")["tip"])
                self.assertNotIn("chat allowance", hit(drawn, "input.rings")["tip"])
                self.assertEqual([p["id"] for p in drawn["prims"] if p["t"] == "ring"], ["input.ring.context"])

    def test_chat_footer_agrees_with_refusal_override_and_reset(self):
        from datetime import datetime
        account = {"session": {"used": 0.3}}
        reset = "2099-10-21T18:00:00Z"
        notice = {"item": state.item_limit("Used up.", "session_limit", "free", reset, None)}
        now = datetime.fromisoformat(reset).timestamp()
        self.assertEqual(layout.usage_note(account, notice, now - 1), ("Chat: 100% of this 5-hour window used", 1))
        self.assertEqual(layout.usage_note(account, notice, now), ("", 0.3))

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

    def test_note_explains_delivery_and_offers_one_action_while_helpers_run(self):
        session = chat(wave(worker("a", "working")), busy=True)
        note = dict(state.item_user("make it smaller"), queued=True)
        session["items"].append(note)
        display = build(session)
        delivery = lambda display: " ".join(p["text"] for p in display["prims"]
                                            if p["id"].startswith("item1.delivery."))
        self.assertEqual(delivery(display),
                         "Queued for the main agent; helper can read this before another request")
        self.assertEqual(hit(display, "item1.send_now")["action"], ("send_queued_now", 1))
        self.assertEqual(texts(display)["item1.send_now.label.l0"], "Stop helper and send now")
        self.assertEqual([h["id"] for h in display["hits"] if h["id"].startswith("item1.")],
                         ["item1.send_now"])
        session["items"][0]["rows"][0]["status"] = "done"
        display = build(session)
        self.assertEqual(delivery(display), "Queued for the main agent")
        self.assertNotIn("item1.send_now", ids(display, "hits"))

    def test_delivery_is_conditional_during_work_checking_and_merging(self):
        for status in ("working", "checking", "merging"):
            for count in (1, 2):
                with self.subTest(status=status, count=count):
                    session = chat(wave(*(worker(str(n), status) for n in range(count))), busy=True)
                    note = dict(state.item_user("smaller"), queued=True)
                    session["items"].append(note)
                    before = deepcopy(note)
                    display = build(session)
                    delivery = " ".join(p["text"] for p in display["prims"]
                                        if p["id"].startswith("item1.delivery."))
                    helper = "helper" if count == 1 else "helpers"
                    self.assertEqual(delivery,
                                     f"Queued for the main agent; {helper} can read this before another request")
                    self.assertEqual(texts(display)["item1.send_now.label.l0"], f"Stop {helper} and send now")
                    self.assertEqual(note, before, "the parent's card remains queued after helper delivery")

    def test_sending_note_explains_the_stop_and_cannot_be_sent_twice(self):
        session = chat(wave(worker("a", "working")), busy=True)
        session["items"].append(dict(state.item_user("smaller"), queued=True, sending=True))
        display = build(session)
        delivery = " ".join(p["text"] for p in display["prims"] if p["id"].startswith("item1.delivery."))
        self.assertEqual(delivery, "Stopping helper; queued for the main agent")
        self.assertNotIn("item1.send_now", ids(display, "hits"))

    def test_note_and_action_wrap_within_a_narrow_card(self):
        session = chat(wave(worker("a", "working")), busy=True)
        note = dict(state.item_user("smaller"), queued=True)
        session["items"].append(note)
        for width, scale in ((240, 1), (420, 1.5)):
            frame = layout.Frame(width, 600, scale, measure, session=session)
            height = layout._item_user(frame, 1, note, 0, 0, width)
            label = " ".join(p["text"] for p in frame.prims if p["id"].startswith("item1.send_now.label"))
            self.assertEqual(label, "Stop helper and send now")
            for p in frame.prims:
                if p["t"] == "text":
                    self.assertLessEqual(p["x"] + measure("ui", p["size"], p["text"]), width)
                    self.assertLessEqual(p["y"] + p["h"], height)


class DelegateDetailsTest(unittest.TestCase):
    def test_plan_diagnostics_and_budget_stay_behind_details(self):
        for repair in ("needed", "resolved"):
            for expanded in (False, True):
                item = state.item_tool("delegate", "Check a helper plan", '{"workers": [{"id": "chair"}]}')
                item.update(status="failed", expanded=expanded, plan_repair="needed", plan_turn="turn1",
                            plan_workers=[["chair", "modeling"]],
                            output="workers[0].checks[0]: missing kind. Budget: $0.40")
                session = chat(item)
                if repair == "resolved":
                    retry = state.item_tool("delegate", "Start a helper", item["code"])
                    retry.update(status="done", plan_turn="turn1", plan_workers=[["chair", "modeling"]])
                    session["items"].append(retry)
                display = build(session)
                visible = " ".join(texts(display).values())
                self.assertNotIn("workers[0]", visible)
                self.assertNotIn("$0.40", visible)
                self.assertNotIn("item0.tool.reason", ids(display, "prims"))
                expected = ("Adjusted the helper plan and retried" if repair == "resolved"
                            else "Helper plan needs adjustment")
                # The collapsed line may ellipsize at narrow widths.
                self.assertTrue(expected.startswith(texts(display)["item0.tool.summary"].rstrip("…")))
                if repair == "resolved":
                    self.assertNotEqual(prim(display, "item0.tool.glyph")["color"], T.ERROR)
                item.update(expanded=True, details_expanded=True)
                self.assertIn("workers[0].checks[0]", " ".join(texts(build(session)).values()))

    def test_successful_helper_output_keeps_internal_budget_in_details(self):
        item = state.item_tool("delegate", "Start a helper", "Internal ceiling: $0.40")
        item.update(status="done", expanded=True, output="Budget: $0.400 of $0.40 left")
        self.assertNotIn("$0.40", " ".join(texts(build(chat(item))).values()))
        item["details_expanded"] = True
        self.assertIn("$0.40", " ".join(texts(build(chat(item))).values()))

    def test_arguments_stay_behind_details_even_in_an_open_or_awaiting_card(self):
        for status in ("running", "awaiting", "done"):
            item = state.item_tool("delegate", "Ask helpers", '{"workers": []}')
            item.update(status=status, expanded=True)
            session = chat(item)
            display = build(session)
            self.assertNotIn("item0.tool.code.bg", ids(display, "prims"))
            self.assertEqual(hit(display, "item0.tool.details")["action"], ("toggle_details", 0))
            self.assertEqual(texts(display)["item0.tool.details.label"], "Details")
            item["details_expanded"] = True
            display = build(session)
            self.assertIn("item0.tool.code.bg", ids(display, "prims"))
            self.assertEqual(texts(display)["item0.tool.details.label"], "Hide details")


class StreamingIndicatorTest(unittest.TestCase):
    def setUp(self):
        self.session = state.new_session()
        self.session["busy"] = True
        self.reply = state.item_assistant()
        self.reply["text"] = "Reply in progress."
        self.session["items"].append(self.reply)

    def queue_note(self):
        note = state.item_user("Another note.")
        note["queued"] = True
        self.session["items"].append(note)

    def assert_indicator(self, display, prefix, word="Thinking"):
        self.assertEqual([id for id in ids(display, "prims") if id.endswith(".dots")],
                         [prefix + ".dots"])
        self.assertEqual("".join(p["text"] for p in display["prims"]
                                 if p["id"].startswith(prefix + ".")), word + "…")

    def assert_no_indicator(self, display):
        self.assertFalse([id for id in ids(display, "prims") if id.endswith(".dots")])

    def test_streaming_reply_with_text_shows_one_indicator(self):
        display = build(self.session)
        self.assertEqual(prim(display, "item0.p0.l0")["text"], self.reply["text"])
        self.assert_indicator(display, "item0.thinking")

    def test_queued_note_preserves_streaming_indicator(self):
        self.queue_note()
        display = build(self.session)
        self.assertIn("item1.queued", ids(display, "prims"))
        self.assert_indicator(display, "item0.thinking")
        self.assertFalse([id for id in ids(display, "prims") if id.startswith("chat.working.")])

    def test_completed_reply_with_text_has_no_indicator(self):
        self.reply["streaming"] = False
        self.session["busy"] = False
        display = build(self.session)
        self.assertIn("item0.p0.l0", ids(display, "prims"))
        self.assert_no_indicator(display)

    def test_pending_approval_takes_precedence_over_stream(self):
        tool = state.item_tool("tool", "Pending approval", "")
        tool["status"] = "awaiting"
        self.session["items"].append(tool)
        display = build(self.session)
        self.assertEqual(prim(display, "item1.tool.glyph")["color"], T.WARN)
        self.assertEqual(hit(display, "item1.tool.run")["action"], ("approve", 1))
        self.assert_no_indicator(display)

    def test_empty_stream_shows_one_indicator_with_or_without_queued_note(self):
        self.reply["text"] = ""
        self.assert_indicator(build(self.session), "item0.thinking")
        self.queue_note()
        self.assert_indicator(build(self.session), "item0.thinking")

    def test_stopped_session_has_no_indicator_even_with_stale_stream_flag(self):
        self.session["busy"] = False
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self.reply["streaming"] = streaming
                self.assert_no_indicator(build(self.session))

    def test_tool_status_takes_precedence_with_queued_note(self):
        tool = state.item_tool("tool", "Tool activity", "")
        self.session["items"].append(tool)
        self.queue_note()
        self.assert_indicator(build(self.session), "chat.working", "Working")
        tool["status"] = "awaiting"
        display = build(self.session)
        self.assertEqual(hit(display, "item1.tool.run")["action"], ("approve", 1))
        self.assert_no_indicator(display)


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
    def test_elevation_notice_reserves_space_and_not_now_has_an_action(self):
        banner = {"text": "Updating Loopcut to 0.2.10…", "button": ("Not now", "update_defer"),
                  "dismiss": False, "progress": None,
                  "detail": "Windows will ask to allow the update after Blender closes."}
        session = state.new_session()
        session["items"].append(state.item_user("hi"))
        plain, notice = build(session), build(session, update=banner)
        bg = next(p for p in notice["prims"] if p["id"] == "update.bg")
        card = lambda d: next(p for p in d["prims"] if p["id"] == "item0.user.card")
        self.assertGreater(bg["h"], T.BANNER_HEIGHT)
        self.assertEqual(card(notice)["y"] - card(plain)["y"], bg["h"])
        self.assertEqual(hit(notice, "update.button")["action"], ("update_defer", None))
        self.assertIn("update.detail", ids(notice, "prims"))

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

    def test_starter_prompts_send_and_scope_explains_the_boundary(self):
        display = build()
        self.assertTrue(3 <= len(layout.EXAMPLE_REQUESTS) <= 4)
        for n, request in enumerate(layout.EXAMPLE_REQUESTS):
            self.assertEqual(hit(display, f"chat.hint{n}")["action"], ("example_request", n))
            self.assertEqual(hit(display, f"chat.hint{n}")["tip"], "Send this to Loopcut")
            shown = " ".join(v for k, v in sorted(texts(display).items()) if k.startswith(f"chat.hint{n}.label.l"))
            self.assertEqual(shown, request)
        self.assertEqual(texts(display)["chat.hints.title.l0"], "Try one:")
        scope = " ".join(value for key, value in texts(display).items() if key.startswith("chat.hints.scope"))
        self.assertIn("inspect and edit", scope)
        self.assertIn("can't see other apps", scope)
        self.assertIn("approval", scope)

    def test_starters_return_in_every_new_chat_without_the_first_run_note(self):
        display = build(hints_dismissed=True)
        self.assertFalse([p for p in display["prims"] if p["id"].startswith("chat.hints.scope")])
        self.assertEqual(len([h for h in display["hits"] if h["action"][0] == "example_request"]),
                         len(layout.EXAMPLE_REQUESTS))
        self.assertIn("chat.empty0.l0", ids(display, "prims"))
        session = state.new_session()
        session["items"].append(state.item_user("Make it red"))
        self.assertFalse([h for h in build(session)["hits"] if h["action"][0] == "example_request"])

    def test_starters_wait_in_the_input_until_a_model_is_connected(self):
        display = build(needs_setup=True)
        self.assertEqual(hit(display, "chat.hint0")["tip"], "Fill the input; connect a model above to send it")
        self.assertEqual(texts(display)["chat.hints.title.l0"], "Then try one:")
        self.assertGreater(hit(display, "chat.hint0")["y"], hit(display, "chat.signin.loopcut")["y"])

    def test_own_key_provider_without_its_key_asks_for_the_key_not_a_sign_in(self):
        """F0287: picking OpenAI with no key brought back the first-run sign-in card, both accounts connected."""
        display = build(needs_setup=True, key_needed="OpenAI", chatgpt_plan=True, loopcut_signed_in=True)
        actions = {h["id"]: h["action"] for h in display["hits"]}
        self.assertNotIn("chat.signin.loopcut", actions)
        self.assertNotIn("chat.signin.chatgpt", actions)
        self.assertEqual(actions["chat.key.add"], ("own_key", None))
        self.assertEqual(actions["chat.key.chatgpt"], ("use_provider", "CHATGPT"))
        self.assertEqual(actions["chat.key.loopcut"], ("use_provider", "LOOPCUT"))
        self.assertIn("Add your OpenAI key", texts(display).values())
        alone = {h["id"] for h in build(needs_setup=True, key_needed="OpenAI")["hits"]}
        self.assertIn("chat.key.add", alone)
        self.assertFalse({"chat.key.chatgpt", "chat.key.loopcut"} & alone, "no route back that is not connected")
        self.assertIn("chat.signin.chatgpt", ids(build(needs_setup=True), "hits"), "first run: the setup card")

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

    def test_settings_menu_reads_in_full_in_a_narrow_panel(self):
        """F0284: at panel width the rows read "Project no generation:…" twice and "Project no…"."""
        for width in (260, 420):
            for signed_in in (False, True):
                with self.subTest(width=width, signed_in=signed_in):
                    session = state.new_session()
                    display = layout.build(session, width, 600, 1.0, measure, "fast", None,
                                           {"selection": [], "context_budget": 24_000, "account": None,
                                            "service_settings": True, "service_signed_in": signed_in,
                                            "project_override": "on", "service_policy": {"private_mode": True}})
                    rows = [p for p in display["prims"] if p["id"].startswith("service.settings.row")]
                    names = [p["text"] for p in rows if p["id"].endswith(".name")]
                    self.assertFalse([n for n in names if n.endswith("…") and n != "All settings…"], names)
                    for p in rows:
                        self.assertLessEqual(right(p), width, p["id"])
                    self.assertIn("This .blend file", names)
                    self.assertIn("✓  No generation", names, "the file's choice that is on")
                    self.assertIn("✓  Private mode", names)
                    actions = {h["action"]: h for h in display["hits"] if h["id"].startswith("service.settings.row")}
                    self.assertEqual({a[1] for a in actions if a[0] == "project_generation"}, {"inherit", "on", "off"})
                    self.assertIn("Account restrictions still apply", actions[("project_generation", "off")]["tip"])
                    self.assertNotIn(None, [h["action"] for h in display["hits"]], "the caption is not a button")

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


class HelperPreviewTest(unittest.TestCase):
    def test_latest_preview_opens_and_missing_files_are_omitted(self):
        row = worker("a", "working", image_path="/saved/preview.png")
        display = build(chat(wave(row)))
        preview = prim(display, "item0.wave.row0.image")
        self.assertEqual(preview["path"], row["image_path"])
        self.assertEqual(hit(display, "item0.wave.row0.image")["action"], ("reference_open", row["image_path"]))
        card = prim(display, "item0.wave.card")
        self.assertLessEqual(preview["y"] + preview["h"], card["y"] + card["h"])
        missing = build(chat(wave(row)), missing_paths=[row["image_path"]])
        self.assertNotIn("item0.wave.row0.image", ids(missing, "prims"))
        self.assertNotIn("item0.wave.row0.image", ids(missing, "hits"))


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
    def test_live_elapsed_time_latest_action_and_singular_step(self):
        row = worker("a", "working", started_at=100, steps=1, summary="Adjust the shape")
        session = chat(wave(row), busy=True)
        display = build(session, now=165)
        self.assertEqual(texts(display)["item0.wave.row0.detail"], "1m 05s · modeling · 1 step")
        self.assertEqual(texts(display)["item0.wave.row0.summary.l0"], "Adjust the shape")
        display = build(session, now=166)
        self.assertIn("1m 06s", texts(display)["item0.wave.row0.detail"])
        row.update(status="done", elapsed=66, steps=2)
        self.assertEqual(texts(build(session, now=999))["item0.wave.row0.detail"],
                         "1m 06s · modeling · 2 steps")

    def test_reloaded_worker_does_not_keep_counting_time(self):
        row = worker("a", "stopped", started_at=100, steps=1)
        self.assertEqual(texts(build(chat(wave(row)), now=999))["item0.wave.row0.detail"], "modeling · 1 step")

    def test_finished_row_keeps_status_final_summary_rounded_duration_role_steps_and_checks(self):
        row = worker("a", "done", merged=True, started_at=100, elapsed=65.6, steps=2, checks="1/1",
                     summary="Finished the shape.")
        session = chat(wave(row))
        for now in (166, 999, 9999):
            display = build(session, now=now)
            self.assertEqual(texts(display)["item0.wave.row0.status"], "merged")
            self.assertEqual(texts(display)["item0.wave.row0.detail"], "1m 06s · modeling · 2 steps · checks 1/1")
            self.assertEqual(texts(display)["item0.wave.row0.summary.l0"], "Finished the shape.")
        for elapsed, expected in ((0.9, "modeling · 2 steps · checks 1/1"),
                                  (1.1, "1s · modeling · 2 steps · checks 1/1"),
                                  (59.6, "1m 00s · modeling · 2 steps · checks 1/1")):
            row["elapsed"] = elapsed
            self.assertEqual(texts(build(session, now=9999))["item0.wave.row0.detail"], expected)

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


class SceneChangesPreviewTest(unittest.TestCase):
    SIZES = ((240, 1), (420, 1), (640, 1.5))
    LONG_ENTRY = "~ " + " ".join(f"value{n}" for n in range(60))

    def display(self, item, width=420, scale=1):
        frame = layout.Frame(width, 600, scale, measure, session=chat(item))
        layout._item_changes(frame, 0, item, 0, 0, width)
        return {"prims": frame.prims, "hits": frame.hits}

    def details(self, display):
        return [p for p in display["prims"] if p["id"].startswith("item0.changes.l")]

    def test_collapsed_long_entry_reserves_a_row_for_show_more(self):
        for width, scale in self.SIZES:
            with self.subTest(width=width, scale=scale):
                item = state.item_changes("1 changed", [self.LONG_ENTRY], "cp1")
                display = self.display(item, width, scale)
                rows = self.details(display)
                self.assertEqual(len(rows), layout.CHANGES_PREVIEW_LINES - 1)
                more = prim(display, "item0.changes.more")
                self.assertRegex(more["text"], r"^Show \d+ more$")
                self.assertGreaterEqual(more["y"], rows[-1]["y"] + rows[-1]["h"])
                self.assertEqual(hit(display, "item0.changes.more")["action"], ("toggle", 0))

    def test_expanded_long_entry_shows_complete_text_and_show_less(self):
        for width, scale in self.SIZES:
            with self.subTest(width=width, scale=scale):
                item = state.item_changes("1 changed", [self.LONG_ENTRY], "cp1")
                item["expanded"] = True
                display = self.display(item, width, scale)
                rows = self.details(display)
                self.assertGreater(len(rows), layout.CHANGES_PREVIEW_LINES)
                self.assertEqual(" ".join(p["text"] for p in rows), self.LONG_ENTRY[2:])
                for row in rows:
                    self.assertEqual(row["font"], "mono")
                    self.assertEqual(row["size"], round(T.FONT_SIZE_CODE * scale))
                    self.assertLessEqual(right(row), width - round(T.CARD_PAD * scale))
                self.assertIn("item0.changes.more", ids(display, "prims"))
                self.assertEqual(prim(display, "item0.changes.more")["text"], "Show less")
                self.assertEqual(hit(display, "item0.changes.more")["action"], ("toggle", 0))

    def test_short_entries_within_budget_have_no_toggle(self):
        for count in (0, 3, layout.CHANGES_PREVIEW_LINES):
            for expanded in (False, True):
                with self.subTest(count=count, expanded=expanded):
                    lines = [f"~ value{n}" for n in range(count)]
                    item = state.item_changes(f"{count} changed", lines, "cp1")
                    item["expanded"] = expanded
                    display = self.display(item)
                    self.assertEqual([p["text"] for p in self.details(display)],
                                     [line[2:] for line in lines])
                    self.assertNotIn("item0.changes.more", ids(display, "prims"))
                    self.assertNotIn("item0.changes.more", ids(display, "hits"))

    def test_stored_changes_prefixes_and_keep_undo_are_preserved(self):
        lines = ["+ " + self.LONG_ENTRY[2:], "- Previous value", "~ Updated value"]
        item = state.item_changes("3 changed", lines, "cp1")
        for expanded in (False, True, False):
            with self.subTest(expanded=expanded):
                item["expanded"] = expanded
                original = deepcopy(item)
                display = self.display(item)
                self.assertEqual(item, original)
                self.assertIs(item["lines"], lines)
                self.assertEqual(hit(display, "item0.changes.keep")["action"], ("keep_changes", 0))
                self.assertEqual(hit(display, "item0.changes.undo")["action"], ("restore", "cp1"))
                self.assertEqual(prim(display, "item0.changes.keep.label")["text"], "Keep")
                self.assertEqual(prim(display, "item0.changes.undo.label")["text"], "Undo all")
                signs = [p for p in display["prims"] if p["id"].startswith("item0.changes.sign")]
                first_rows = [p for p in self.details(display) if p["id"].endswith(".l0")]
                self.assertEqual([p["text"] for p in signs], [line[:1] for line in lines[:len(first_rows)]])
                for n, sign in enumerate(signs):
                    self.assertEqual(sign["y"], prim(display, f"item0.changes.l{n}.l0")["y"])


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


class InputScrollAndEditTest(unittest.TestCase):
    LONG = " ".join(f"word{n}" for n in range(120))

    def session(self, text, cursor, anchor=None, **extra):
        session = state.new_session()
        session.update(input=text, cursor=cursor, anchor=anchor, focused=True, **extra)
        return session

    def shown(self, display):
        return [p["text"] for p in display["prims"] if p["id"].startswith("input.l")]

    def test_the_box_scrolls_to_the_caret_not_always_to_the_tail(self):
        session = self.session(self.LONG, 0)
        display = build(session)
        lines, _ = layout.input_rows(display, self.LONG, measure)
        self.assertGreater(len(lines), T.INPUT_MAX_LINES)
        self.assertEqual(self.shown(display)[0], lines[0][1].rstrip())
        self.assertEqual(len(self.shown(display)), T.INPUT_MAX_LINES)
        self.assertIn("input.caret", ids(display, "prims"))
        self.assertEqual(layout.input_index(display, display["input"]["x"], display["input"]["y"] + 1, measure), 0)
        session["cursor"] = len(self.LONG)
        self.assertEqual(self.shown(build(session))[-1], lines[-1][1].rstrip())

    def test_a_selection_from_a_row_start_draws_nothing_past_the_row_above(self):
        text = "a long request that will wrap differently at each width of the panel"
        lines, _ = layout.input_rows(build(self.session(text, 0)), text, measure)
        display = build(self.session(text, len(text), anchor=lines[1][0]))
        box = display["input"]
        rows_hit = sorted({round((p["y"] - box["y"]) // box["line_height"])
                           for p in display["prims"] if p["id"].startswith("input.selection")})
        self.assertEqual(rows_hit[0], 1)

    def test_a_user_message_offers_edit_and_confirms_in_edit_wording(self):
        items = [{"kind": "user", "text": "make it red", "checkpoint": "c1"}]
        display = build(self.session("", 0, items=items))
        self.assertEqual(hit(display, "item0.checkpoint.edit")["action"], ("edit_ask", 0))
        display = build(self.session("", 0, items=items, confirm_restore=0, confirm_edit=True))
        ask = " ".join(p["text"] for p in display["prims"] if p["id"].startswith("item0.checkpoint.ask"))
        self.assertIn("the conversation from here is set aside", ask)
        self.assertEqual(hit(display, "item0.checkpoint.confirm")["action"], ("restore_confirm", 0))


if __name__ == "__main__":
    unittest.main()
