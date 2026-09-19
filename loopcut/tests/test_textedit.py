"""The input box's editing rules. Pure, so no Blender."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut.ui import textedit as te  # noqa: E402


def box(text: str = "", cursor: int | None = None) -> dict:
    return {"input": text, "cursor": len(text) if cursor is None else cursor, "anchor": None}


class TextEditTest(unittest.TestCase):
    def test_typing_inserts_at_the_caret(self):
        s = box("helo", 3)
        te.insert(s, "l")
        self.assertEqual((s["input"], s["cursor"]), ("hello", 4))

    def test_shift_arrows_select_and_typing_replaces_the_selection(self):
        s = box("move the cube", 9)
        te.move(s, "end", select=True)
        self.assertEqual(te.selected_text(s), "cube")
        te.insert(s, "lamp")
        self.assertEqual((s["input"], s["cursor"], te.selection(s)), ("move the lamp", 13, None))

    def test_arrow_without_shift_collapses_to_the_edge_of_the_selection(self):
        s = box("abcdef", 1)
        te.move(s, "right", select=True)
        te.move(s, "right", select=True)
        te.move(s, "left")
        self.assertEqual((s["cursor"], s["anchor"]), (1, None))

    def test_word_moves_and_word_delete(self):
        s = box("scale the  big.cube")
        te.move(s, "word_left")
        self.assertEqual(s["cursor"], 15)
        te.move(s, "word_left")
        self.assertEqual(s["cursor"], 11)
        te.delete(s, backwards=True, word=True)
        self.assertEqual(s["input"], "scale big.cube")
        te.delete(s, backwards=False, word=True)
        self.assertEqual(s["input"], "scale .cube")

    def test_line_home_and_end_stay_on_the_line(self):
        s = box("first\nsecond line\nthird", 9)
        te.move(s, "line_home")
        self.assertEqual(s["cursor"], 6)
        te.move(s, "line_end")
        self.assertEqual(s["cursor"], 17)

    def test_select_all_cut_and_paste_back(self):
        s = box("add a table")
        te.select_all(s)
        self.assertEqual(te.cut(s), "add a table")
        self.assertEqual((s["input"], s["cursor"]), ("", 0))
        te.insert(s, "add a table")
        self.assertEqual(s["input"], "add a table")

    def test_delete_at_the_ends_does_nothing(self):
        s = box("x", 0)
        te.delete(s, backwards=True)
        self.assertEqual(s["input"], "x")
        s = box("x", 1)
        te.delete(s, backwards=False)
        self.assertEqual(s["input"], "x")

    def test_undo_takes_back_a_run_of_typing_at_once_and_a_paste_separately(self):
        s = box()
        for n, char in enumerate("cube"):
            te.insert(s, char, now=n * 0.1)
        te.insert(s, " and a long pasted sentence", now=0.5)
        self.assertTrue(te.undo(s))
        self.assertEqual(s["input"], "cube")
        self.assertTrue(te.undo(s))
        self.assertEqual(s["input"], "")
        self.assertFalse(te.undo(s))

    def test_a_pause_starts_a_new_undo_step(self):
        s = box()
        te.insert(s, "a", now=0.0)
        te.insert(s, "b", now=5.0)
        te.undo(s)
        self.assertEqual(s["input"], "a")

    def test_completing_a_mention_replaces_what_was_typed(self):
        s = box("put it on @Tab please", 14)
        te.replace_range(s, 10, 14, "@Table ")
        self.assertEqual((s["input"], s["cursor"]), ("put it on @Table  please", 17))
        te.undo(s)
        self.assertEqual(s["input"], "put it on @Tab please")


if __name__ == "__main__":
    unittest.main()
