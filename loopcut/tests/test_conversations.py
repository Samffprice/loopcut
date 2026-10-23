"""Conversation persistence that needs no Blender."""

import base64
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

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

    def test_0_2_11_code_failures_load_display_and_resave_without_new_fields(self):
        # Generated with v0.2.11's state constructors and conversations.save, before turn metadata.
        source = Path(__file__).with_name("fixtures") / "conversation-v0.2.11.json"
        body = json.loads(source.read_text(encoding="utf-8"))
        folder = cv.root() / body["id"]
        folder.mkdir(parents=True)
        (folder / "conversation.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        loaded = cv.load(body["id"])
        self.assertEqual(loaded["items"], body["items"])
        self.assertEqual(loaded["messages"], body["messages"])
        self.assertEqual(layout._code_repairs(loaded["items"], False), {1: "continued"})
        display = layout.build(loaded, 900, 4000, 1.0, lambda f, s, t: len(t) * s * 0.5)
        prims = {p["id"]: p for p in display["prims"]}
        self.assertEqual(prims["item1.tool.glyph"]["color"], layout.T.TEXT_MUTED)
        self.assertEqual(prims["item6.tool.glyph"]["color"], layout.T.ERROR)
        self.assertEqual(prims["item6.tool.output.l0"]["text"], "SyntaxError: invalid syntax")
        self.assertIn("Stopped.", [p.get("text") for p in display["prims"]])
        self.assertIn("Scene checkpoint saved.", [p.get("text") for p in display["prims"]])
        cv.save(loaded)
        self.assertEqual(cv.load(body["id"])["items"], body["items"])

    def test_an_empty_conversation_is_not_written(self):
        cv.save(state.new_session())
        self.assertFalse(cv.root().exists())

    def preview_session(self, path):
        session = self.session("/work/desk.blend", "build")
        row = state.wave_row("chair", "Chair", "modeling")
        row.update(status="done", image_path=str(path))
        session["items"].append(state.item_wave("w1", 1, [row]))
        return session

    def assert_preview_dropped(self, path):
        session = self.preview_session(path)
        cv.save(session)
        with mock.patch("builtins.print") as log:
            loaded = cv.load(session["id"])
        expected_row = dict(session["items"][-1]["rows"][0])
        del expected_row["image_path"]
        self.assertEqual(loaded["items"][-1], {**session["items"][-1], "rows": [expected_row]})
        self.assertEqual(loaded["items"][:-1], session["items"][:-1])
        self.assertEqual(loaded["messages"], session["messages"])
        log.assert_called_once()
        self.assertIn("preview", log.call_args.args[0].lower())
        self.assertIn(str(path), log.call_args.args[0])

    def test_wave_preview_rejects_a_well_shaped_external_path(self):
        with tempfile.TemporaryDirectory() as external:
            path = Path(external) / "images" / ("c" * 64 + ".png")
            path.parent.mkdir()
            path.write_bytes(b"external capture")
            self.assert_preview_dropped(path)

    def test_wave_preview_rejects_directory_and_file_symlink_escapes(self):
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external) / "images"
            outside.mkdir()
            image = outside / ("c" * 64 + ".png")
            image.write_bytes(b"external capture")
            linked = Path(self.tmp.name) / "linked"
            linked.symlink_to(external, target_is_directory=True)
            local = Path(self.tmp.name) / "images"
            local.mkdir()
            (local / image.name).symlink_to(image)
            for path in (linked / "images" / image.name, local / image.name):
                with self.subTest(path=path):
                    self.assert_preview_dropped(path)

    def test_wave_preview_accepts_alternate_case_data_root(self):
        data_root = Path(self.tmp.name) / "LoopcutData"
        data_root.mkdir()
        alternate = data_root.with_name("loopcutdata")
        if not alternate.exists():
            self.skipTest("requires a case-insensitive filesystem")
        self.assertTrue(data_root.samefile(alternate))
        for folder in (data_root / "conversations" / ("a" * 32), data_root / "workers" / "job" / "chair"):
            path = folder / "images" / ("c" * 64 + ".png")
            path.parent.mkdir(parents=True)
            path.write_bytes(b"owned capture")
            session = self.preview_session(path)
            with mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": str(data_root)}):
                cv.save(session)
            with self.subTest(folder=folder), mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": str(alternate)}):
                self.assertEqual(cv.load(session["id"])["items"][-1]["rows"][0]["image_path"], str(path))

    def test_wave_preview_uses_filesystem_identity_for_alternate_case_root(self):
        data_root = Path(self.tmp.name).resolve() / "loopcutdata"
        alternate = data_root.with_name("LoopcutData")
        path = alternate / "workers" / "job" / "chair" / "images" / ("c" * 64 + ".png")
        session = self.preview_session(path)
        # Model a case-insensitive volume even when this test runs on a case-sensitive host.
        def samefile(left, right):
            return str(left).casefold() == str(right).casefold()

        with mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": str(data_root)}):
            cv.save(session)
            with mock.patch.object(Path, "samefile", samefile):
                self.assertEqual(cv.load(session["id"])["items"][-1]["rows"][0]["image_path"], str(path))

    def test_wave_preview_resolution_failure_drops_only_the_preview(self):
        path = Path(self.tmp.name) / "images" / ("c" * 64 + ".png")
        resolve = Path.resolve

        for error in (OSError("unreadable preview"), RuntimeError("symlink loop"), ValueError("bad path")):
            def fail_preview(candidate, *args, **kwargs):
                if candidate == path:
                    raise error
                return resolve(candidate, *args, **kwargs)

            with self.subTest(error=error), mock.patch.object(Path, "resolve", fail_preview):
                self.assert_preview_dropped(path)

    def test_wave_preview_accepts_conversation_and_worker_captures_under_data_root(self):
        for folder in (Path(self.tmp.name) / "conversations" / ("a" * 32),
                       Path(self.tmp.name) / "workers" / "chat" / "job" / "w1" / "chair"):
            path = folder / "images" / ("c" * 64 + ".png")
            session = self.preview_session(path)
            cv.save(session)
            self.assertEqual(cv.load(session["id"])["items"][-1]["rows"][0]["image_path"], str(path))

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

    def test_a_conversation_from_before_the_tools_were_merged_restores(self):
        session = self.session("/work/desk.blend", "a chair")
        card = state.item_tool("import_polyhaven", "Import Poly Haven asset chair_01 (1k)", "")
        card["status"] = "done"
        session["items"] += [state.item_assistant(), card]
        old = {"id": "c1", "type": "function",
               "function": {"name": "import_polyhaven", "arguments": json.dumps({"asset_id": "chair_01"})}}
        session["messages"] += [{"role": "assistant", "content": None, "tool_calls": [old]},
                                {"role": "tool", "tool_call_id": "c1", "content": "Imported"}]
        cv.save(session)
        loaded = cv.load(session["id"])
        self.assertEqual(loaded["items"][-1], card, "the card shows what ran then")
        function = loaded["messages"][1]["tool_calls"][0]["function"]
        self.assertEqual((function["name"], json.loads(function["arguments"])),
                         ("import_asset", {"source": "polyhaven", "id": "chair_01"}))
        display = layout.build(loaded, 420, 700, 1.0, lambda f, s, t: len(t) * s * 0.5, "m", {}, {})
        texts = [p["text"] for p in display["prims"] if p["t"] == "text"]
        self.assertIn("Import Poly Haven asset chair_01 (1k)", texts)
        loaded["items"][-1]["expanded"] = True
        display = layout.build(loaded, 420, 700, 1.0, lambda f, s, t: len(t) * s * 0.5, "m", {}, {})
        texts = [p["text"] for p in display["prims"] if p["t"] == "text"]
        self.assertIn("import_polyhaven", texts, "the expanded card names the tool that ran then")

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

    def test_the_wire_names_the_type_the_stored_bytes_hold(self):
        # Every stored image is <sha>.png, but a clip's sheets are JPEG (video_worker.draw_sheet).
        session = self.session("/work/desk.blend", "like this clip")
        source = Path(self.tmp.name) / "sheet"
        refs = {}
        for media, data in (("image/jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF sheet"),
                            ("image/png", b"\x89PNG\r\n\x1a\n capture"),
                            ("image/webp", b"RIFF\x10\x00\x00\x00WEBPVP8 still"),
                            ("image/png", b"made up")):
            source.write_bytes(data)
            refs[cv.store_image(session, source)] = (media, data)
        self.assertTrue(all(ref.endswith(".png") for ref in refs), "stored names keep their old form")
        session["messages"].append({"role": "user", cv.ATTACHED: True, "content": [
            {"type": "text", "text": "time it like this"},
            *({"type": "image_url", "image_url": {"url": ref}} for ref in refs)]})
        wired = cv.wire_messages(session["id"], session["messages"])[-1]["content"][1:]
        for part, (media, data) in zip(wired, refs.values()):
            self.assertEqual(part["image_url"]["url"], f"data:{media};base64,{base64.b64encode(data).decode()}")

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

    def test_full_reference_mapping_preserves_detail_pinning_and_missing_full_copy(self):
        session = self.session("/work/desk.blend", "look")
        picture = Path(self.tmp.name) / "picture.png"
        picture.write_bytes(b"small")
        small = cv.store_image(session, picture)
        picture.write_bytes(b"full")
        full = cv.store_image(session, picture)
        message = {"role": "user", cv.ATTACHED: True, "content": [
            {"type": "text", "text": "copy this"},
            {"type": "image_url", "image_url": {"url": small, "detail": "low"}}]}
        session["messages"].append(message)
        mapping = {small: full}
        wired = cv.wire_messages(session["id"], session["messages"], full_refs=mapping)
        image = wired[-1]["content"][1]["image_url"]
        self.assertEqual(base64.b64decode(image["url"].split(",", 1)[1]), b"full")
        self.assertEqual(image["detail"], "low")
        self.assertEqual(message["content"][1]["image_url"], {"url": small, "detail": "low"})
        capture = {**message, cv.ATTACHED: False, cx.CAPTURE: True}
        wired_capture = cv.wire_messages(session["id"], [capture], full_refs=mapping)
        self.assertEqual(base64.b64decode(wired_capture[0]["content"][1]["image_url"]["url"].split(",", 1)[1]), b"small")
        unpinned = cv.wire_messages(session["id"], session["messages"], frozenset({small}), full_refs=mapping)
        self.assertEqual(unpinned[-1]["content"][1]["type"], "text")
        cv.image_path(session["id"], full).unlink()
        wired = cv.wire_messages(session["id"], session["messages"], full_refs=mapping)
        self.assertEqual(base64.b64decode(wired[-1]["content"][1]["image_url"]["url"].split(",", 1)[1]), b"small")

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

    def test_references_survive_a_round_trip_and_a_malformed_one_is_refused(self):
        session = self.session("/work/desk.blend", "copy this")
        picture = Path(self.tmp.name) / "picture.png"
        picture.write_bytes(b"\x89PNG")
        ref = cv.store_image(session, picture)
        session["references"] = [{"ref": ref, "full": ref, "name": "picture.png", "pinned": False}]
        cv.save(session)
        self.assertEqual(cv.load(session["id"])["references"], session["references"])
        session["references"] = [{"ref": "loopcut-image:../evil.png", "name": "x", "pinned": True}]
        cv.save(session)
        with self.assertRaises(cv.ConversationError):
            cv.load(session["id"])

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


class CardImagePathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["LOOPCUT_DATA_DIR"] = self.tmp.name

    def test_a_saved_card_may_only_point_at_a_stored_capture(self):
        session = state.new_session()
        card = state.item_tool("capture_viewport", "Look", "")
        card["status"] = "done"
        session["items"].append(card)
        good = str(cv.root() / session["id"] / "images" / ("c" * 64 + ".png"))
        for value, ok in ((good, True), ("", True), ("/etc/passwd", False), ("images/" + "c" * 64 + ".png", False),
                          (f"/x/images/../../etc/{'c' * 64}.png", False), ("/x/images/photo.png", False),
                          (None, False), (42, False)):
            card["image_path"] = value
            cv.save(session)
            if ok:
                self.assertEqual(cv.load(session["id"])["items"][0]["image_path"], value)
            else:
                with self.subTest(value=value), mock.patch("builtins.print") as log:
                    loaded = cv.load(session["id"])
                    expected = dict(card)
                    del expected["image_path"]
                    self.assertEqual(loaded["items"], [expected])
                    log.assert_called_once()
                    self.assertIn("preview", log.call_args.args[0].lower())

    def test_an_omitted_stale_inspection_takes_its_picture_off_the_card(self):
        from unittest import mock
        from loopcut import agent
        card, message = {"status": "done", "output": "", "image_path": "/x/images/" + "d" * 64 + ".png"}, {"content": ""}
        with mock.patch.object(agent, "_inspection_current", lambda token: False):
            self.assertEqual(agent._fresh_images([("ref", "scene", False, "token", card, message)]), [])
        self.assertNotIn("image_path", card)


if __name__ == "__main__":
    unittest.main()
