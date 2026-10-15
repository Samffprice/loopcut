"""The input box's editing rules. Pure, so no Blender."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut.ui import layout  # noqa: E402
from loopcut.ui import textedit as te  # noqa: E402


def box(text: str = "", cursor: int | None = None) -> dict:
    return {"input": text, "cursor": len(text) if cursor is None else cursor, "anchor": None}


class TextEditTest(unittest.TestCase):
    def test_place_caret_clears_selection_clamps_and_ends_typing(self):
        s = box("abcdef", 5)
        s.update(anchor=2, input_undo_mark=("type", 1))
        te.place_caret(s, 3)
        self.assertEqual((s["cursor"], s["anchor"], s["input_undo_mark"]), (3, None, None))
        te.insert(s, "!")
        self.assertEqual(s["input"], "abc!def")
        te.place_caret(s, -10)
        self.assertEqual(s["cursor"], 0)
        te.place_caret(s, 100)
        self.assertEqual(s["cursor"], len(s["input"]))

    def test_extend_selection_keeps_anchor_when_drag_reverses(self):
        s = box("abcdef", 3)
        te.extend_selection(s, 5)
        self.assertEqual((s["anchor"], te.selected_text(s)), (3, "de"))
        te.extend_selection(s, 1)
        self.assertEqual((s["anchor"], te.selected_text(s)), (3, "bc"))
        te.extend_selection(s, 100)
        self.assertEqual((s["anchor"], s["cursor"]), (3, 6))

    def test_select_word_uses_word_boundaries_and_handles_empty_text(self):
        for index in (4, 5, 7):
            with self.subTest(index=index):
                s = box("one.two  three")
                te.select_word(s, index)
                self.assertEqual((s["anchor"], s["cursor"], te.selected_text(s)), (4, 7, "two"))
        s = box("one.two  three")
        te.select_word(s, 100)
        self.assertEqual(te.selected_text(s), "three")
        te.select_word(s, 8)
        self.assertEqual(te.selected_text(s), "  ")
        te.select_word(s, -1)
        self.assertEqual(te.selected_text(s), "one")
        s = box()
        te.select_word(s, 0)
        self.assertIsNone(te.selection(s))

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


def rows(text: str, columns: int):
    """layout.wrap() with a 10 px monospace font, `columns` characters to a row."""
    return layout.wrap(text, "ui", 13, columns * 10, lambda font, size, t: 10 * len(t))


def width(prefix: str) -> float:
    return 10 * len(prefix)


class WrappedBoxTest(unittest.TestCase):
    TEXT = "make a crate with planks\nand a lid that opens"

    def test_index_and_position_round_trip_at_several_widths(self):
        for columns in (8, 12, 20, 60):
            lines = rows(self.TEXT, columns)
            for index in range(len(self.TEXT) + 1):
                row, column = te.locate(lines, index)
                self.assertEqual(lines[row][0] + column, index, (columns, index))
                if column <= te.row_end(lines, row):
                    self.assertEqual(te.index_at(lines, row, width(lines[row][1][:column]), width), index,
                                     (columns, index))

    def test_up_and_down_keep_the_goal_column_across_a_short_row(self):
        text = "a long first line\nab\na long third line"
        lines = rows(text, 40)
        s = box(text, 9)
        te.vertical(s, lines, width, down=True)
        self.assertEqual(s["cursor"], text.index("ab") + 2)
        te.vertical(s, lines, width, down=True)
        self.assertEqual(s["cursor"], text.rindex("a long") + 9)
        te.vertical(s, lines, width, down=True)
        self.assertEqual(s["cursor"], len(text))
        for _ in range(3):
            te.vertical(s, lines, width, down=False)
        self.assertEqual(s["cursor"], 0)

    def test_a_word_wider_than_the_box_splits_without_trapping_up_and_down(self):
        text = "a" * 20 + "\n" + "b" * 10
        lines = rows(text, 10)
        self.assertEqual(te.row_end(lines, 0), 9)
        s = box(text)
        for _ in range(2):
            te.vertical(s, lines, width, down=False)
        self.assertEqual(s["cursor"], 9)
        te.vertical(s, lines, width, down=False)
        self.assertEqual(s["cursor"], 0)

    def test_shift_down_selects_and_other_moves_end_the_run(self):
        text = "aaaaaaaa\nbb\ncccccccc"
        lines = rows(text, 40)
        s = box(text, 3)
        te.vertical(s, lines, width, down=True, select=True)
        self.assertEqual(te.selected_text(s), "aaaaa\nbb")
        s = box(text, 3)
        te.vertical(s, lines, width, down=True)
        te.move(s, "left")
        te.move(s, "right")
        te.vertical(s, lines, width, down=False)
        self.assertEqual(s["cursor"], 2)  # From column 2 now, not the old goal of 3.

    def test_select_line_and_the_view_following_the_caret(self):
        s = box("make it red\nthen render", 0)
        te.select_line(s, 15)
        self.assertEqual(te.selected_text(s), "then render")
        self.assertEqual(te.first_row(10, 2, 3, 12), 2)
        self.assertEqual(te.first_row(0, 7, 3, 12), 5)
        self.assertEqual(te.first_row(4, 5, 3, 12), 4)
        self.assertEqual(te.first_row(99, 11, 3, 12), 9)


if __name__ == "__main__":
    unittest.main()
