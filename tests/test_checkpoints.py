"""Checkpoint logic that needs no Blender: file naming, pruning, the on-disk index."""

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import checkpoints as cp  # noqa: E402
from loopcut import state  # noqa: E402
from loopcut.ui import layout  # noqa: E402

NOW = time.mktime((2026, 9, 18, 14, 30, 5, 0, 0, -1))
MB = 1024 * 1024


def entry(n: int, size_mb: int, kind: str = "turn", expired: bool = False) -> dict:
    return {"id": f"{n:032x}", "kind": kind, "size": size_mb * MB, "expired": expired}


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


if __name__ == "__main__":
    unittest.main()
