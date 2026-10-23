"""check_contact's pure half: pairs and relations, supports, the vertical measurements, verdicts,
fixes and result lines. The geometry itself is loopcut/harness/contact_check.py."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import contact as C, llm, state  # noqa: E402

TOL = 0.002


def pair(a="A", relation="rests_on", b="B", gap=0.0) -> dict:
    return {"a": a, "relation": relation, "b": b, "gap": gap, "error": ""}


def support(part, offset, how="gap", xy=(0.0, 0.0), alone=False) -> dict:
    return {"part": part, "xy": xy, "offset": offset, "how": how, "alone": alone}


def measure(distance=1.0, toward=(0, 0, -1.0), depth=0.0, out=(0, 0, 0), meet=False) -> dict:
    return {"distance": distance, "toward": toward, "depth": depth, "out": out, "meet": meet}


class PairsTest(unittest.TestCase):
    def test_an_unknown_relation_fails_that_pair_only_and_names_the_valid_ones(self):
        parsed = C.parse([{"a": "Box", "relation": "on_top_of", "b": "Floor"},
                          {"a": "Box", "relation": "rests_on", "b": "Floor"}])
        self.assertEqual(parsed[0]["error"], "unknown relation 'on_top_of': use rests_on, touches, clear_of, inside")
        self.assertEqual(parsed[1]["error"], "")

    def test_relations_are_read_loosely_but_only_as_the_four(self):
        self.assertEqual(C.relation_of("Rests on"), "rests_on")
        self.assertEqual(C.relation_of("clear-of"), "clear_of")
        self.assertEqual(C.relation_of("on"), "")
        self.assertEqual(C.relation_of(None), "")

    def test_malformed_pairs_say_why(self):
        parsed = C.parse(["Box on Floor", {"a": "Box", "relation": "touches"},
                          {"a": "Box", "relation": "clear_of", "b": "Wall", "gap": -1},
                          {"a": "Box", "relation": "clear_of", "b": "Wall", "gap": 0.05}])
        self.assertIn("not a pair", parsed[0]["error"])
        self.assertEqual(parsed[1]["error"], "needs both a and b")
        self.assertEqual(parsed[2]["error"], "gap is a distance in m, 0 or more")
        self.assertEqual((parsed[3]["error"], parsed[3]["gap"]), ("", 0.05))

    def test_sampling_is_capped_and_the_same_every_time(self):
        self.assertEqual(list(C.sample(5, cap=10)), [0, 1, 2, 3, 4])
        self.assertEqual(list(C.sample(10, cap=4)), [0, 3, 6, 9])
        self.assertLessEqual(len(C.sample(250_001)), C.SAMPLE_CAP)
        self.assertEqual(list(C.sample(0)), [])

    def test_the_support_band_catches_a_short_foot_not_a_seat(self):
        self.assertEqual(C.band(0.1), 0.01)
        self.assertAlmostEqual(C.band(0.4), 0.02)
        self.assertEqual(C.band(0.9), 0.03)


class SupportsTest(unittest.TestCase):
    def test_three_feet_of_one_mesh_stay_three_supports(self):
        # Feet 0-1, 2-3, 4-5 (the third 8 mm short) rise to a base ring 6, 7, 8 at 20 mm.
        heights = {0: 0.0, 1: 0.01, 2: 0.0, 3: 0.01, 4: 0.008, 5: 0.018, 6: 0.02, 7: 0.02, 8: 0.02}
        edges = [(0, 1), (2, 3), (4, 5), (1, 6), (3, 7), (5, 8), (6, 7), (7, 8), (8, 6)]
        groups = C.supports(heights, edges, TOL)
        self.assertEqual([sorted(g)[:2] for g in groups], [[0, 1], [2, 3], [4, 5]])
        self.assertEqual([g[0] for g in groups], [0, 2, 4], "each support starts at its lowest vertex")

    def test_a_dent_shallower_than_the_tolerance_is_not_a_support(self):
        heights = {0: 0.0, 1: 0.0005, 2: 0.001, 3: 0.0}
        self.assertEqual(len(C.supports(heights, [(0, 2), (2, 1), (1, 3)], TOL)), 1)
        self.assertEqual(len(C.supports(heights, [(0, 2), (2, 1), (1, 3)], 0.0001)), 2)

    def test_separate_pieces_are_separate_supports(self):
        self.assertEqual(len(C.supports({0: 0.0, 1: 0.0, 2: 0.0}, [], TOL)), 3)

    def test_a_base_resting_on_its_own_feet_is_not_a_support(self):
        points = {0: (-0.1, 0, 0.0), 1: (0.1, 0, 0.0),                    # two feet
                  2: (-0.15, -0.1, 0.01), 3: (0.15, 0.1, 0.01),           # the base's underside, over both
                  4: (0.5, 0, 0.008)}                                     # a short foot out to the side
        kept = C.lowest_parts([[0], [1], [2, 3], [4]], points, TOL)
        self.assertEqual(kept, [[0], [1], [4]])
        self.assertEqual(C.lowest_parts([[0], [1], [2, 3], [4]], points, TOL, limit=2), [[0], [1]])


class VerticalTest(unittest.TestCase):
    RUG = [(0.015, 7), (0.0, 8)]  # (z, face) top down: a 15 mm rug lying on z = 0.

    def test_resting_floating_and_into(self):
        self.assertEqual(C.offset_below(0.015, self.RUG, TOL), (0.0, 7, "gap"))
        offset, face, how = C.offset_below(0.023, self.RUG, TOL)
        self.assertEqual((round(offset, 6), face, how), (0.008, 7, "gap"))
        offset, face, how = C.offset_below(0.005, self.RUG, TOL)
        self.assertEqual((round(offset, 6), how), (-0.01, "into"))

    def test_a_leg_that_reaches_the_rug_s_underside_went_through_it(self):
        offset, face, how = C.offset_below(0.0, self.RUG, TOL)
        self.assertEqual((round(offset, 6), face, how), (-0.015, 7, "through"))
        offset, _, how = C.offset_below(-0.01, self.RUG, TOL)
        self.assertEqual((round(offset, 6), how), (-0.025, "through"))

    def test_a_plane_is_a_sheet(self):
        plane = [(0.0, 3)]
        self.assertEqual(C.offset_below(0.004, plane, TOL)[2], "gap")
        offset, _, how = C.offset_below(-0.005, plane, TOL)
        self.assertEqual((round(offset, 6), how), (-0.005, "through"))

    def test_a_shelf_above_the_one_rested_on_does_not_count(self):
        bookcase = [(1.02, 1), (1.0, 2), (0.52, 3), (0.5, 4)]  # Two shelves, top down.
        self.assertEqual(C.offset_below(0.52, bookcase, TOL), (0.0, 3, "gap"))

    def test_a_line_that_misses_b_measures_nothing(self):
        self.assertIsNone(C.offset_below(0.0, [], TOL))
        self.assertIsNone(C.offset_above(0.0, [], TOL))

    def test_from_below_a_table_top_over_its_legs(self):
        top = [(0.755, 10), (0.725, 11)]
        offset, face, how = C.offset_above(0.72, top, TOL)
        self.assertEqual((round(offset, 6), face, how), (0.005, 11, "gap"))
        offset, face, how = C.offset_above(0.73, top, TOL)
        self.assertEqual((round(offset, 6), face, how), (-0.005, 11, "into"))


class RestsOnTest(unittest.TestCase):
    def test_a_lamp_with_a_short_foot_moves_that_foot(self):
        found = [support("Foot.001", 0.0, alone=True), support("Foot.002", 0.0001, alone=True),
                 support("Foot.003", 0.008, xy=(0.12, 0.0), alone=True)]
        result = C.rests_on(pair("Lamp", b="Table"), found, TOL, "Lamp")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["fixes"], [("Foot.003", (0.0, 0.0, -0.008))])
        self.assertEqual(result["line"], "3 supports: 2 touching, 1 floating. Fix: move Foot.003 by (0, 0, -0.008) m")
        self.assertEqual(result["details"], ["Foot.003 at (0.12, 0): gap 8.0 mm"])

    def test_a_chair_through_a_rug_is_lifted_whole(self):
        found = [support(f"Leg.00{k}", -0.015, "through", alone=True) for k in range(1, 5)]
        result = C.rests_on(pair("Chair", b="Rug"), found, TOL, "Chair")
        self.assertEqual(result["fixes"], [("Chair", (0.0, 0.0, 0.015))])
        self.assertEqual(result["line"], "4 supports: 4 through Rug. Fix: move Chair by (0, 0, 0.015) m")
        self.assertEqual(result["details"][0], "Leg.001 at (0, 0): through Rug, 15.0 mm below its top")

    def test_one_mesh_that_is_uneven_says_so_instead_of_moving_a_part(self):
        found = [support("Chair", 0.0), support("Chair", 0.0), support("Chair", 0.006, xy=(0.2, 0.2))]
        result = C.rests_on(pair("Chair", b="Floor"), found, TOL, "Chair")
        self.assertEqual(result["fixes"], [])
        self.assertIn("Then Chair at (0.2, 0.2) would still float 6.0 mm: Chair is tilted or uneven", result["line"])

    def test_floating_as_a_whole_moves_down_by_the_smallest_gap(self):
        result = C.rests_on(pair("Top", b="Legs"), [support("Top", 0.005)], TOL, "Top")
        self.assertEqual((result["status"], result["fixes"]), ("fail", [("Top", (0.0, 0.0, -0.005))]))

    def test_every_support_within_tolerance_passes(self):
        result = C.rests_on(pair(), [support("A", 0.0), support("A", -0.0015)], TOL, "A")
        self.assertEqual((result["status"], result["line"], result["fixes"]),
                         ("pass", "2 supports touching (within 1.5 mm)", []))

    def test_a_support_not_over_b_fails(self):
        result = C.rests_on(pair("Lamp", b="Table"), [support("Foot.001", 0.0), support("Foot.002", None, "")],
                            TOL, "Lamp")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["details"], ["Foot.002 at (0, 0): not over Table"])
        nowhere = C.rests_on(pair("Lamp", b="Table"), [support("Lamp", None, "")], TOL, "Lamp", over=(1.0, 0.0, 0.75))
        self.assertEqual(nowhere["line"], "no part of Lamp is over Table. Fix: move Lamp by (1, 0, 0.75) m")

    def test_failing_supports_are_listed_up_to_a_limit(self):
        found = [support("Rock", 0.01 * k) for k in range(1, 11)]
        details = C.rests_on(pair(), found, TOL, "Rock")["details"]
        self.assertEqual(len(details), C.SUPPORT_LINES + 1)
        self.assertEqual(details[-1], "(+4 more)")


class AnyDirectionTest(unittest.TestCase):
    def test_touches(self):
        self.assertEqual(C.touches(pair(relation="touches"), measure(0.0005), TOL)["status"], "pass")
        self.assertEqual(C.touches(pair(relation="touches"), measure(meet=True), TOL)["line"], "in contact")
        gap = C.touches(pair(relation="touches"), measure(0.012, (0, -0.012, 0)), TOL)
        self.assertEqual((gap["status"], gap["fixes"]), ("fail", [("A", (0, -0.012, 0))]))
        into = C.touches(pair(relation="touches"), measure(depth=0.1, out=(0, 0, 0.1), meet=True), TOL)
        self.assertEqual(into["line"], "100.0 mm into B. Fix: move A by (0, 0, 0.1) m")

    def test_clear_of_moves_away_by_what_is_missing_rounded_up(self):
        close = C.clear_of(pair(relation="clear_of", gap=0.05), measure(0.03, (0.03, 0, 0)), TOL)
        self.assertEqual(close["line"], "30.0 mm apart, needs 50.0 mm. Fix: move A by (-0.02, 0, 0) m")
        into = C.clear_of(pair(relation="clear_of"), measure(depth=0.1, out=(0, 0, 0.1), meet=True), TOL)
        self.assertEqual(into["fixes"], [("A", (0.0, 0.0, 0.102))])
        touching = C.clear_of(pair(relation="clear_of"), measure(0.0, (0, 0, 0), meet=True), TOL, apart=(0, 0, 2.0))
        self.assertEqual(touching["line"], "touching. Fix: move A by (0, 0, 0.002) m")
        self.assertEqual(C.clear_of(pair(relation="clear_of"), measure(0.004), TOL)["status"], "pass")

    def test_inside(self):
        self.assertEqual(C.inside(pair(relation="inside"), {"outside": 0.0, "worst": 0.001, "back": (0, 0, 0),
                                                            "larger": []}, TOL)["status"], "pass")
        out = C.inside(pair(relation="inside"), {"outside": 0.5, "worst": 0.05, "back": (-0.05, 0, 0), "larger": []}, TOL)
        self.assertEqual(out["line"], "50% of A's vertices outside B's convex hull, up to 50.0 mm. Fix: move A by (-0.05, 0, 0) m")
        big = C.inside(pair(relation="inside"), {"outside": 0.5, "worst": 0.05, "back": (-0.05, 0, 0), "larger": ["x"]}, TOL)
        self.assertEqual(big["fixes"], [])
        self.assertIn("A is larger than B along x", big["line"])


class ReportTest(unittest.TestCase):
    def test_unavailable_is_counted_apart_and_never_as_a_pass(self):
        passed = {**pair("Box", b="Floor"), "status": "pass", "line": "1 support touching", "fixes": []}
        missing = C.unavailable(pair("Lampp", b="Table"), 'no object named "Lampp"')
        failed = {**pair("Lamp", b="Table"), "status": "fail", "line": "3 supports: 2 touching, 1 floating",
                  "details": ["Foot.003 at (0.12, 0): gap 8.0 mm"], "notes": ["Lamp: union of its 6 child meshes"]}
        text = C.report([passed, missing, failed], TOL)
        self.assertEqual(text.splitlines(), [
            "Contact: 1 pass, 1 fail, 1 unavailable (tolerance 2.0 mm; moves are world-space, nothing was moved).",
            "pass: Box rests_on Floor: 1 support touching.",
            'unavailable: Lampp rests_on Table: no object named "Lampp".',
            "FAIL: Lamp rests_on Table (Lamp: union of its 6 child meshes): 3 supports: 2 touching, 1 floating.",
            "  Foot.003 at (0.12, 0): gap 8.0 mm"])

    def test_a_change_of_status_is_one_line(self):
        old = {**pair("Box", b="Floor"), "status": "pass", "line": "1 support touching"}
        new = {**pair("Box", b="Floor"), "status": "fail", "line": "1 support: 1 floating. Fix: move Box by (0, 0, -0.012) m"}
        self.assertEqual(C.change_line(old, new), "contact pass -> FAIL: Box rests_on Floor: 1 support: 1 floating. "
                                                  "Fix: move Box by (0, 0, -0.012) m.")

    def test_numbers(self):
        self.assertEqual(C.vector((0.0, -0.0, -0.00800001)), "(0, 0, -0.008)")
        self.assertEqual(C._mm(0.0123), "12.3 mm")
        self.assertEqual(C._mm(2.55), "2.55 m")

    def test_the_card_names_the_pairs(self):
        arguments = {"pairs": [{"a": "Lamp", "relation": "rests_on", "b": "Table"},
                               {"a": "Box", "relation": "touches", "b": "Wall"}, {"a": "X", "relation": "inside", "b": "Y"}]}
        self.assertEqual(C.describe("check_contact", arguments),
                         ("Check contact: Lamp rests_on Table, Box touches Wall, +1 more", ""))
        self.assertIsNone(C.describe("run_python", {}))

    def test_the_schema_stays_small(self):
        import json
        self.assertLess(len(json.dumps(llm.wire_tools(C.SCHEMAS))) / 4, 320)


class WatchTest(unittest.TestCase):
    def setUp(self):
        state.reset()

    def test_nothing_watched_reads_nothing(self):
        with mock.patch.object(C, "_Shapes", side_effect=AssertionError("read the scene")):
            self.assertEqual(C.recheck(), [])

    def test_watching_keeps_valid_pairs_once_each(self):
        box = {**pair("Box", b="Floor"), "status": "pass", "line": "", "fingerprint": 1}
        bad = C.unavailable({**pair("Box", "on_top_of", "Floor"), "error": "unknown relation"}, "unknown relation")
        C._watch([box, bad], TOL)
        note = C._watch([dict(box, status="fail")], TOL)
        watched = state.session()["contact_watch"]
        self.assertEqual([(e["pair"]["a"], e["result"]["status"]) for e in watched], [("Box", "fail")])
        self.assertEqual(note, "Watching 1 pair: measured again after each run_python, a change of status listed "
                               "in its Scene changes.")

    def test_a_recheck_reports_only_changes_of_status(self):
        box = {**pair("Box", b="Floor"), "status": "pass", "line": "1 support touching"}
        lamp = {**pair("Lamp", b="Table"), "status": "fail", "line": "3 supports: 2 touching, 1 floating"}
        C._watch([box, lamp], TOL)
        now = {"Box": dict(box, status="fail", line="1 support: 1 floating"), "Lamp": dict(lamp, line="other detail")}
        shapes = mock.MagicMock()
        with mock.patch.object(C, "_Shapes", return_value=shapes), \
                mock.patch.object(C, "_evaluate", side_effect=lambda p, *_: now[p["a"]]):
            self.assertEqual(C.recheck(), ["contact pass -> FAIL: Box rests_on Floor: 1 support: 1 floating."])
        shapes.free.assert_called_once()
        self.assertEqual(state.session()["contact_watch"][0]["result"]["status"], "fail")

    def test_a_failing_recheck_is_one_line_not_a_lost_step(self):
        C._watch([{**pair("Box", b="Floor"), "status": "pass", "line": ""}], TOL)
        with mock.patch.object(C, "_Shapes", return_value=mock.MagicMock()), \
                mock.patch.object(C, "_evaluate", side_effect=KeyError("Box")):
            self.assertEqual(C.recheck(), ["contact recheck failed: KeyError('Box')"])


if __name__ == "__main__":
    unittest.main()
