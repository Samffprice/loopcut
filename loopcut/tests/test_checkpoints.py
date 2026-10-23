"""Checkpoint logic that needs no Blender: file naming, pruning, the on-disk index."""

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import checkpoints as cp  # noqa: E402
from loopcut import state  # noqa: E402
from loopcut.ui import layout  # noqa: E402

NOW = time.mktime((2026, 9, 18, 14, 30, 5, 0, 0, -1))
MB = 1024 * 1024


def entry(n: int, size_mb: int, kind: str = "turn", expired: bool = False) -> dict:
    return {"id": f"{n:032x}", "kind": kind, "size": size_mb * MB, "expired": expired}


class BeginTurnTest(unittest.TestCase):
    def test_new_message_starts_after_history_even_when_its_text_was_sent_before(self):
        session = state.new_session()
        session["items"] = [state.item_user("smaller"), state.item_error("Stopped.")]
        session["messages"] = [{"role": "user", "content": "smaller"}]
        cp.begin_turn(session, state.item_user("smaller"))
        pending = session["pending_checkpoint"]
        self.assertEqual((pending["item_index"], pending["message_index"]), (2, 1))
        self.assertEqual(pending["input"], "smaller")

    def test_reused_note_starts_at_its_own_position_even_when_an_earlier_note_is_equal(self):
        session = state.new_session()
        note = state.item_user("smaller")
        session["items"] = [state.item_user("smaller"), note, state.item_error("Stopped.")]
        cp.begin_turn(session, note)
        self.assertEqual(session["pending_checkpoint"]["item_index"], 1)


class RestoredPathTest(unittest.TestCase):
    def test_goes_beside_the_original_and_never_onto_it(self):
        path = cp.restored_path("/work/desk.blend", Path("/data/restored"), NOW, exists=lambda p: False)
        self.assertEqual(path, Path("/work/desk.restored-20260918-143005.blend"))

    def test_does_not_stack_suffixes_when_restoring_a_restored_file(self):
        path = cp.restored_path("/work/desk.restored-20260101-000000-2.blend", Path("/d"), NOW, exists=lambda p: False)
        self.assertEqual(path.name, "desk.restored-20260918-143005.blend")

    def test_never_returns_an_existing_file(self):
        taken = {Path("/work/desk.restored-20260918-143005.blend"), Path("/work/desk.restored-20260918-143005-2.blend")}
        path = cp.restored_path("/work/desk.blend", Path("/d"), NOW, exists=lambda p: p in taken)
        self.assertEqual(path.name, "desk.restored-20260918-143005-3.blend")

    def test_unsaved_scene_goes_to_the_fallback_folder_not_the_store(self):
        path = cp.restored_path("", Path("/data/restored"), NOW, exists=lambda p: False)
        self.assertEqual(path, Path("/data/restored/untitled.restored-20260918-143005.blend"))


class PruningTest(unittest.TestCase):
    def test_nothing_expires_under_budget(self):
        self.assertEqual(cp.plan_pruning([entry(n, 10) for n in range(20)], 200 * MB), [])

    def test_expires_oldest_first_but_keeps_first_newest_and_last_pre_restore(self):
        entries = [entry(n, 10) for n in range(10)]
        entries[2]["kind"] = entries[4]["kind"] = "pre_restore"
        expired = cp.plan_pruning(entries, 0)
        kept = [e["id"] for e in entries if e["id"] not in expired]
        self.assertEqual(kept, [entry(n, 0)["id"] for n in (0, 4, 5, 6, 7, 8, 9)])

    def test_stops_as_soon_as_it_fits_and_ignores_already_expired(self):
        entries = [entry(0, 10), entry(1, 50, expired=True), *[entry(n, 10) for n in range(2, 10)]]
        self.assertEqual(cp.plan_pruning(entries, 75 * MB), [entry(2, 0)["id"], entry(3, 0)["id"]])


class StoreTest(unittest.TestCase):
    def test_pruning_deletes_files_marks_the_index_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as root:
            session_id = "ab" * 16
            store = cp.Store(Path(root), session_id)
            store.folder.mkdir(parents=True)
            for n in range(8):
                item = {**entry(n, 10), "label": "", "created": n}
                store.snapshot_path(item["id"]).write_bytes(b"blend")
                store.add(item, budget_bytes=65 * MB)
            reloaded = cp.Store(Path(root), session_id)
            expired = [e["id"] for e in reloaded.entries if e["expired"]]
            self.assertEqual(expired, [entry(1, 0)["id"], entry(2, 0)["id"]])
            for e in reloaded.entries:
                self.assertEqual(reloaded.snapshot_path(e["id"]).exists(), not e["expired"])

    def test_bundle_snapshot_is_indexed_for_collection_then_discarded(self):
        from unittest.mock import patch
        saved = []

        def save_as_mainfile(filepath, copy, compress):
            Path(filepath).write_bytes(b"live scene")
            saved.append((filepath, copy))
            return {"FINISHED"}
        fake = types.SimpleNamespace(ops=types.SimpleNamespace(wm=types.SimpleNamespace(save_as_mainfile=save_as_mainfile)),
                                     data=types.SimpleNamespace(filepath="/projects/shot.blend"))
        with tempfile.TemporaryDirectory() as root, patch.dict(sys.modules, {"bpy": fake}), \
                patch.dict("os.environ", {"LOOPCUT_DATA_DIR": root}), \
                patch("loopcut.config.checkpoint_budget_mb", return_value=2048):
            session = state.new_session()
            checkpoint_id = cp.for_bundle(session)
            store = cp.Store(Path(root), session["id"])
            entry = store.get(checkpoint_id)
            self.assertEqual((entry["kind"], entry["expired"], entry["in_place"]), ("bundle", False, False))
            self.assertEqual(saved, [(str(store.snapshot_path(checkpoint_id)), True)])  # A copy: the open file is untouched.
            self.assertEqual(session["pending_checkpoint"], None)  # The turn's own checkpoint is not this one.
            cp.discard(session, checkpoint_id)
            self.assertFalse(store.snapshot_path(checkpoint_id).exists())
            self.assertEqual(cp.Store(Path(root), session["id"]).entries, [])
            cp.discard(session, checkpoint_id)  # Already gone: nothing to do.

    def test_rejects_ids_that_are_not_ours(self):
        with self.assertRaises(cp.CheckpointError):
            cp.Store(Path("/tmp"), "../../etc")
        with tempfile.TemporaryDirectory() as root:
            store = cp.Store(Path(root), "cd" * 16)
            store.folder.mkdir(parents=True)
            (store.folder / cp.INDEX_NAME).write_text('[{"id": "../../evil", "expired": false}]')
            self.assertEqual(cp.Store(Path(root), "cd" * 16).entries, [])


class CheckpointLayoutTest(unittest.TestCase):
    @staticmethod
    def measure(font, size, text):
        return len(text) * size * 0.5

    def build(self, session, statuses=None):
        return layout.build(session, 420, 700, 1.0, self.measure, "m", statuses or {})

    def test_restore_needs_a_confirm_click_and_expired_offers_nothing(self):
        session = state.new_session()
        user = state.item_user("make a table")
        user["checkpoint"] = "11" * 16
        session["items"] = [user]
        actions = [h["action"] for h in self.build(session)["hits"]]
        self.assertIn(("restore_ask", 0), actions)
        self.assertNotIn(("restore_confirm", 0), actions)

        session["confirm_restore"] = 0
        actions = [h["action"] for h in self.build(session)["hits"]]
        self.assertIn(("restore_confirm", 0), actions)
        self.assertIn(("restore_cancel", 0), actions)

        actions = [h["action"] for h in self.build(session, {"11" * 16: "expired"})["hits"]]
        self.assertFalse([a for a in actions if a[0].startswith("restore")])

    def test_notice_offers_undo(self):
        session = state.new_session()
        session["items"] = [state.item_notice("Restored.", "Undo restore", "22" * 16)]
        self.assertIn(("restore", "22" * 16), [h["action"] for h in self.build(session)["hits"]])


class FreedWindow(Exception):
    """In Blender a segfault: temp_override reads a freed window's screen before checking it exists."""


class Window:
    def __init__(self, panel: bool):
        self.freed = False
        self.screen = types.SimpleNamespace(areas=[types.SimpleNamespace(type="VIEW_3D")] +
                                            ([types.SimpleNamespace(type="LOOPCUT")] if panel else []))


class FileReadBlender:
    """Windows and the bits of bpy that host._restore_later touches. read_file() is what an in-place
    restore does to the UI: the snapshot's windows replace these, which are freed."""

    def __init__(self, panels: list[bool], clicked: int):
        self.windows = [Window(p) for p in panels]
        self.window, self.timers, self.focused_in = self.windows[clicked], [], []
        blender = self
        context = type("Context", (), {
            "window": property(lambda _: blender.window),
            "window_manager": property(lambda _: types.SimpleNamespace(windows=list(blender.windows))),
            "temp_override": lambda _, window: blender.temp_override(window)})()
        self.bpy = types.SimpleNamespace(
            context=context, ops=types.SimpleNamespace(loopcut=types.SimpleNamespace(focus=self.focus)),
            app=types.SimpleNamespace(timers=types.SimpleNamespace(
                register=lambda fn, first_interval=0: self.timers.append(fn))))

    def read_file(self, panels: list[bool]):
        for window in self.windows:
            window.freed = True
        self.windows, self.window = [Window(p) for p in panels], None  # A file read leaves no context window.

    def temp_override(self, window):
        import contextlib
        if window.freed:
            raise FreedWindow
        self.window = window
        return contextlib.nullcontext()

    def focus(self):
        self.focused_in.append(self.window)
        return {"FINISHED"}


class EditAfterRestoreTest(unittest.TestCase):
    """Edit on a past message restores the scene, then gives the box the keys (0.2.6 crashed here)."""

    def run_restore(self, before: list[bool], clicked: int, after: list[bool], edit=True, fails=False):
        import ast
        blender = FileReadBlender(before, clicked)
        session = state.new_session()

        def restore(_session, checkpoint_id):
            if fails:
                raise cp.CheckpointError("That checkpoint has expired.")
            blender.read_file(after)
        namespace = {"bpy": blender.bpy, "state": types.SimpleNamespace(session=lambda: session, item_error=state.item_error),
                     "checkpoints": types.SimpleNamespace(restore=restore, CheckpointError=cp.CheckpointError),
                     "tag_redraw_all": lambda: None, "_runtime": {}, "NATIVE": True, "HOST_UI_TYPE": "LOOPCUT"}
        source = Path(__file__).resolve().parents[2] / "scripts" / "addons_core" / "loopcut" / "ui" / "host.py"
        names = {"is_host", "find_panel", "_restore_later", "_window_after_load"}
        nodes = [node for node in ast.parse(source.read_text()).body if getattr(node, "name", None) in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        namespace["_restore_later"]("ab" * 16, edit=edit)
        self.assertEqual(blender.focused_in, [], "nothing happens before the click is handled")
        while blender.timers:
            blender.timers.pop(0)()
        return blender, session

    def test_edit_focuses_the_box_through_a_window_from_after_the_file_read(self):
        blender, _ = self.run_restore([False, True], 1, [False, True])
        self.assertEqual(blender.focused_in, [blender.windows[1]])
        self.assertFalse(blender.focused_in[0].freed)

    def test_the_clicked_windows_place_wins_while_it_shows_the_panel(self):
        blender, _ = self.run_restore([True, True], 1, [True, True])
        self.assertEqual(blender.focused_in, [blender.windows[1]])

    def test_a_window_that_shows_the_panel_beats_one_that_lost_it(self):
        blender, _ = self.run_restore([False, True], 1, [True, False])
        self.assertEqual(blender.focused_in, [blender.windows[0]])

    def test_restore_and_a_refused_edit_leave_the_keys_alone(self):
        blender, _ = self.run_restore([True], 0, [True], edit=False)
        self.assertEqual(blender.focused_in, [])
        blender, session = self.run_restore([True], 0, [True], fails=True)
        self.assertEqual(blender.focused_in, [])
        self.assertEqual(session["items"][-1]["kind"], "error")


if __name__ == "__main__":
    unittest.main()
