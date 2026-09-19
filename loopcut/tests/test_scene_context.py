"""The pure half of scene_context: finding @mentions and completing them while typing."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import scene_context as sc  # noqa: E402

NAMES = [("Table", "object"), ("Table Leg.001", "object"), ("Chair", "object"), ("Oak", "material"),
         ("Furniture", "collection"), ("Stable", "object")]


class MentionsTest(unittest.TestCase):
    def test_parse_bare_quoted_and_repeated(self):
        text = 'put @Chair next to @"Table Leg.001", then copy @Chair. Mail me at a@b.c'
        self.assertEqual(sc.parse_mentions(text), ["Chair", "Table Leg.001"])

    def test_prefix_is_only_offered_while_typing_a_mention(self):
        self.assertEqual(sc.mention_prefix("move @Ta", 8), "Ta")
        self.assertEqual(sc.mention_prefix("move @", 6), "")
        self.assertEqual(sc.mention_prefix('move @"Table L', 14), "Table L")
        self.assertIsNone(sc.mention_prefix("move @Table there", 17), "the mention ended at the space")
        self.assertIsNone(sc.mention_prefix("mail a@b", 8), "an @ inside a word is not a mention")
        self.assertIsNone(sc.mention_prefix('see @"Table Leg" ', 17))
        self.assertIsNone(sc.mention_prefix("no mention", 5))

    def test_candidates_put_prefix_matches_first(self):
        self.assertEqual([n for n, _ in sc.candidates_from(NAMES, "ta")], ["Table", "Table Leg.001", "Stable"])
        self.assertEqual(sc.candidates_from(NAMES, "oak"), [("Oak", "material")])
        self.assertEqual(len(sc.candidates_from(NAMES, "", limit=4)), 4)

    def test_names_with_spaces_are_quoted_so_they_parse_back(self):
        for name in ("Chair", "Table Leg.001"):
            self.assertEqual(sc.parse_mentions("x " + sc.mention_text(name) + " y"), [name])


if __name__ == "__main__":
    unittest.main()
