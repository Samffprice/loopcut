"""Conversation persistence that needs no Blender."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import context as cx  # noqa: E402
from loopcut import conversations as cv  # noqa: E402
from loopcut import state  # noqa: E402
from loopcut.ui import layout  # noqa: E402


def call(call_id: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": "run_python", "arguments": "{}"}}


class ConversationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["LOOPCUT_DATA_DIR"] = self.tmp.name

    def session(self, project: str, text: str) -> dict:
        session = state.new_session()
        cv.add_project(session, project)
        session["items"].append(state.item_user(text))
        session["messages"].append({"role": "user", "content": text})
        return session

    def test_round_trip_keeps_the_conversation_and_drops_runtime_state(self):
        session = self.session("/work/desk.blend", "build   a\n desk")
        session["items"][0]["checkpoint"] = "ab" * 16
        session.update(focused=True, busy=True, scroll=120.0, input="next idea")
        cv.save(session)
        loaded = cv.load(session["id"])
        self.assertEqual((loaded["id"], loaded["items"], loaded["messages"]),
                         (session["id"], session["items"], session["messages"]))
        self.assertEqual((loaded["title"], loaded["input"], loaded["cursor"]), ("build a desk", "next idea", 9))
        self.assertEqual((loaded["focused"], loaded["busy"], loaded["scroll"], loaded["turn"]), (False, False, 0.0, None))

    def test_an_empty_conversation_is_not_written(self):
        cv.save(state.new_session())
        self.assertFalse(cv.root().exists())

    def test_work_in_flight_when_blender_closed_is_settled_on_load(self):
        session = self.session("/work/desk.blend", "go")
        reply = state.item_assistant()
        card = state.item_tool("run_python", "Add", "x")
        card["status"] = "awaiting"
        session["items"] += [reply, card]
        session["messages"].append({"role": "assistant", "content": None, "tool_calls": [call("c1")]})
        cv.save(session)
        loaded = cv.load(session["id"])
        self.assertFalse(loaded["items"][1]["streaming"])
        self.assertEqual(loaded["items"][2]["status"], "rejected")
        self.assertEqual(loaded["messages"][-1]["tool_call_id"], "c1")

    def test_close_open_tool_calls_answers_only_the_unanswered_in_place(self):
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": None, "tool_calls": [call("c1"), call("c2")]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": None, "tool_calls": [call("c3")]},
        ]
        cv.close_open_tool_calls(messages, "cut")
        self.assertEqual([(m["role"], m.get("tool_call_id")) for m in messages],
                         [("user", None), ("assistant", None), ("tool", "c1"), ("tool", "c2"),
                          ("user", None), ("assistant", None), ("tool", "c3")])
        before = json.dumps(messages)
        cv.close_open_tool_calls(messages, "cut")
        self.assertEqual(json.dumps(messages), before, "must be idempotent")

    def test_resume_picks_the_latest_for_this_file_only(self):
        old = self.session("/work/desk.blend", "old")
        cv.save(old)
        other = self.session("/work/chair.blend", "other file")
        cv.save(other)
        new = self.session("/work/desk.blend", "new")
        cv.save(new)
        meta = cv._folder(old["id"]) / "meta.json"
        meta.write_text(json.dumps({**json.loads(meta.read_text()), "updated": 1.0}))
        self.assertEqual(cv.resume_or_new("/work/desk.blend")["id"], new["id"])
        self.assertEqual(cv.resume_or_new("/work/../work/desk.blend")["id"], new["id"], "same file, other spelling")
        self.assertEqual([m["id"] for m in cv.list_for(cv.project_key("/work/desk.blend"))], [new["id"], old["id"]])
        fresh = cv.resume_or_new("/work/never-seen.blend")
        self.assertEqual((fresh["items"], fresh["projects"]), ([], [cv.project_key("/work/never-seen.blend")]))

    def test_a_conversation_belongs_to_every_file_it_worked_in(self):
        session = self.session("/work/desk.blend", "build")
        cv.add_project(session, "/work/desk.restored-20260918-140535.blend")
        cv.add_project(session, "/work/desk.blend")
        cv.save(session)
        self.assertEqual(len(session["projects"]), 2)
        for path in ("/work/desk.blend", "/work/desk.restored-20260918-140535.blend"):
            self.assertEqual(cv.resume_or_new(path)["id"], session["id"])

    def test_an_unsaved_scene_never_resumes_someone_elses_untitled_chat(self):
        cv.save(self.session("", "in an untitled scene"))
        self.assertEqual(cv.resume_or_new("")["items"], [])

    def test_a_damaged_file_is_skipped_and_left_on_disk(self):
        good = self.session("/work/desk.blend", "good")
        cv.save(good)
        bad = self.session("/work/desk.blend", "bad")
        cv.save(bad)
        damaged = cv._folder(bad["id"]) / "conversation.json"
        damaged.write_text('{"id": "%s", "items": [{"kind": "evil"}], "messages": []}' % bad["id"])
        self.assertEqual(cv.resume_or_new("/work/desk.blend")["id"], good["id"])
        self.assertTrue(damaged.is_file())
        with self.assertRaises(cv.ConversationError):
            cv.load("../../etc/passwd")

    def test_images_are_stored_once_and_expanded_only_on_the_wire(self):
        session = self.session("/work/desk.blend", "look")
        capture = Path(self.tmp.name) / "viewport.png"
        capture.write_bytes(b"\x89PNG fake")
        reference = cv.store_image(session, capture)
        self.assertEqual(reference, cv.store_image(session, capture))
        self.assertEqual(len(list((cv._folder(session["id"]) / "images").iterdir())), 1)
        session["messages"].append({"role": "user", cx.CAPTURE: True, "content": [
            {"type": "text", "text": "capture"}, {"type": "image_url", "image_url": {"url": reference}}]})
        cv.save(session)
        self.assertNotIn("base64", (cv._folder(session["id"]) / "conversation.json").read_text())

        wired = cv.wire_messages(session["id"], session["messages"])
        self.assertTrue(wired[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(session["messages"][-1]["content"][1]["image_url"]["url"], reference, "stored form untouched")

        cv.image_path(session["id"], reference).unlink()
        self.assertEqual(cv.wire_messages(session["id"], session["messages"])[-1]["content"][1]["type"], "text")
        with self.assertRaises(cv.ConversationError):
            cv.image_path(session["id"], cv.IMAGE_SCHEME + "../../../secret.png")

    def test_attached_images_outlive_captures_and_the_marker_is_never_sent(self):
        session = self.session("/work/desk.blend", "make it look like this")
        picture = Path(self.tmp.name) / "picture.png"
        picture.write_bytes(b"\x89PNG reference photo")
        session["messages"].append({"role": "user", cv.ATTACHED: True, "content": [
            {"type": "text", "text": "like this"},
            {"type": "image_url", "image_url": {"url": cv.store_image(session, picture)}}]})
        for number in range(cv.KEEP_IMAGES + 3):
            picture.write_bytes(b"\x89PNG capture %d" % number)
            session["messages"].append({"role": "user", cx.CAPTURE: True, "content": [
                {"type": "text", "text": "capture"},
                {"type": "image_url", "image_url": {"url": cv.store_image(session, picture)}}]})
        wired = [m for m in cv.wire_messages(session["id"], session["messages"]) if isinstance(m["content"], list)]
        self.assertEqual(wired[0]["content"][1]["type"], "image_url", "the reference photo is still sent")
        self.assertEqual([m["content"][1]["type"] for m in wired[1:]], ["text"] * 3 + ["image_url"] * cv.KEEP_IMAGES)
        self.assertTrue(all(cv.ATTACHED not in m for m in wired))
        self.assertIn(cv.ATTACHED, session["messages"][-cv.KEEP_IMAGES - 4], "stored form untouched")

    def test_a_draft_with_attachments_survives_and_a_forged_one_is_refused(self):
        session = self.session("/work/desk.blend", "hello")
        picture = Path(self.tmp.name) / "picture.png"
        picture.write_bytes(b"\x89PNG")
        session["attachments"] = [{"ref": cv.store_image(session, picture), "name": "picture.png"}]
        session["items"].append(state.item_user("", ["picture.png"]))
        cv.save(session)
        self.assertEqual(cv.load(session["id"])["attachments"], session["attachments"])
        path = cv._folder(session["id"]) / "conversation.json"
        body = json.loads(path.read_text())
        body["attachments"] = [{"ref": cv.IMAGE_SCHEME + "../../secret.png", "name": "x"}]
        path.write_text(json.dumps(body))
        with self.assertRaises(cv.ConversationError):
            cv.load(session["id"])

    def test_only_the_newest_captures_are_sent_and_all_stay_on_disk(self):
        session = self.session("/work/desk.blend", "look")
        capture = Path(self.tmp.name) / "viewport.png"
        for number in range(cv.KEEP_IMAGES + 2):  # One capture per step: only the newest step's is sent.
            capture.write_bytes(b"\x89PNG %d" % number)
            session["messages"].append({"role": "assistant", "content": f"step {number}"})
            session["messages"].append({"role": "user", cx.CAPTURE: True, "content": [
                {"type": "text", "text": "capture"},
                {"type": "image_url", "image_url": {"url": cv.store_image(session, capture)}}]})
        wired = [m["content"][1] for m in cv.wire_messages(session["id"], session["messages"])
                 if isinstance(m["content"], list)]
        self.assertEqual([part["type"] for part in wired], ["text"] * (cv.KEEP_IMAGES + 1) + ["image_url"])
        self.assertEqual(len(list((cv._folder(session["id"]) / "images").iterdir())), cv.KEEP_IMAGES + 2)

    def test_a_changes_card_survives_a_round_trip_and_a_malformed_one_is_refused(self):
        session = self.session("/work/desk.blend", "add a sphere")
        session["items"].append(state.item_changes("1 added", ["+ Sphere (mesh)"], "c" * 32))
        cv.save(session)
        self.assertEqual(cv.load(session["id"])["items"][-1]["lines"], ["+ Sphere (mesh)"])
        session["items"][-1]["lines"] = [{"not": "text"}]
        cv.save(session)
        with self.assertRaises(cv.ConversationError):
            cv.load(session["id"])


class HistoryLayoutTest(unittest.TestCase):
    def test_history_lists_rows_and_marks_the_open_one(self):
        session = state.new_session()
        rows = [{"id": session["id"], "title": "Build a desk", "turns": 3, "updated": 1000.0},
                {"id": "cd" * 16, "title": "", "turns": 1, "updated": 1000.0 - 7200}]
        display = layout.build(session, 420, 700, 1.0, lambda f, s, t: len(t) * s * 0.5, "m", {},
                               {"view": "history", "history": rows, "now": 1000.0})
        actions = [h["action"] for h in display["hits"]]
        self.assertIn(("open_conversation", "cd" * 16), actions)
        self.assertIn(("history_close", None), actions)
        texts = [p["text"] for p in display["prims"] if p["t"] == "text"]
        self.assertIn("3 messages · just now  ·  open now", texts)
        self.assertIn("1 message · 2 h ago", texts)
        self.assertIn("Untitled conversation", texts)


if __name__ == "__main__":
    unittest.main()
