"""acceptance.py's pure half: quote normalisation and matching against the user's words, the
errors validate() gives for every malformed check, ids, titles and the Verified block. Measuring
on a real scene is loopcut/harness/acceptance_check.py."""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import acceptance as A  # noqa: E402

USER = ["Make a cosy reading nook: an armchair on the rug, a side table and a floor lamp — the "
        "lamp’s bulb inside its shade. Keep the bottle, the box and their materials unchanged.",
        "Also a close shot of the perfume bottle's cap, 1080x1920 please. Don't touch the rug!"]


def good(kind: str, **fields) -> dict:
    return {"kind": kind, "quote": "an armchair on the rug", **fields}


VALID = [
    good("exists", name="Armchair"), good("exists", name="Armchair", type="empty"),
    good("count", prefix="Armchair_Leg", type="MESH", min=4, max=4), good("count", collection="Books", min=3),
    good("dims", name="Armchair", size=[0.8, None, 0.95]), good("dims", name="Armchair", size=[None, None, 1], tol=0.01),
    good("contact", a="Armchair", relation="rests_on", b="Rug"), good("contact", a="Lamp", relation="clear_of", b="Wall", gap=0.05),
    good("suspended", a="Sign", b="Bracket"),
    good("suspended", a="Sign", a_part="material:Wood", b="Sign", b_part="group:Chains", ground="Rug", clear=0.3, tol=0.01),
    good("attached_to", a="Sign", a_part="attribute:board", b="Post"), good("above", a="Lamp", b="Table", gap=0.4),
    good("below", a="Rug", b="Lamp", b_part="material:Brass"),
    good("no_overlap", names=["Armchair", "SideTable", "FloorLamp"]),
    good("in_frame", camera="Cam", names=["Bottle"]), good("in_frame", camera="Cam", names=["Cap"], min_inside=0.9,
                                                           region=[0.3, 0.3, 0.7, 0.9]),
    good("in_frame", camera="Cam", names=["Bottle"], frame=120),
    good("faces_max", name="Armchair", max=20000), good("origin_base", name="Armchair"),
    good("origin_base", name="Armchair", tol=0.01), good("origin_base", name="Sign", origin_xy=[-1.8, 0.7]),
    good("material_on", name="Armchair", material="Pal_Wood"),
    good("light_inside", light="Bulb", container="Shade"), good("light", name="Key"),
    good("light", name="Key", type="spot", energy=[100, 300], color=[1, 0.9, 0.8], tol=0.1, spot_size=[None, 45]),
    good("setting", path="render.resolution_x", equals=1080), good("setting", path="scene.render.engine", equals="CYCLES"),
    good("setting", path="cycles.samples", min=64, max=512), good("setting", path="camera", equals="Cam"),
    good("animated", name="Cam", path="location", frames=[1, 120]),
    good("unchanged", names=["Bottle", "Box"]), good("unchanged", names=["Bottle"], tokens={"OBJECT:Bottle": "abc"}),
    good("preserved", names=["Bottle"], snapshot="s1"),
    good("preserved", names=["Bottle"], snapshot="s1", exclude=["materials"], baseline={"Bottle": {}}),
    good("file", path="renders/shot.png"), good("file", path="//renders/frame_####.png"),
]


class AdvisoryTest(unittest.TestCase):
    """A brief's advisory list: the brief's own targets, measured and shown, never failing (owner
    decision 2026-09-29). Its kinds belong only there; any kind may join it without a quote."""

    ADVICE = [{"kind": "no_modifiers", "name": "Barrel"}, {"kind": "flat_shaded", "name": "Barrel"},
              {"kind": "flat_shaded", "name": "Barrel", "max_smooth": 0.1},
              {"kind": "inside_bounds", "name": "Barrel", "bounds": [[-1.35, -0.2, 0], [-0.75, 0.4, 0.9]], "tol": 0.02},
              {"kind": "inside_bounds", "name": "Potions", "bounds": [[[0, 0, 0], [1, 1, 1]], [[2, 0, 0], [3, 1, 1]]]},
              {"kind": "triangles", "name": "Barrel", "min": 400, "max": 1000}, {"kind": "triangles", "name": "B", "max": 9},
              {"kind": "dims", "name": "Barrel", "size": [None, None, 0.85], "tol": 0.05},
              {"kind": "material_on", "name": "Neck", "material": "Shop_Glass", "quote": "the brief's words"}]

    def test_valid_in_an_advisory_list_with_or_without_a_quote(self):
        self.assertEqual(A.validate(self.ADVICE, USER, advisory=True), [])

    def test_advisory_kinds_are_refused_among_the_users_checks(self):
        errors = A.validate([{"kind": "flat_shaded", "name": "Barrel", "quote": "an armchair on the rug"}], USER)
        self.assertEqual(errors, ["checks[0] kind: 'flat_shaded' is advisory only (a brief's advisory list)"])

    def test_their_fields_are_named(self):
        cases = [({"kind": "flat_shaded", "name": "B", "max_smooth": 2}, "max_smooth: a share of faces"),
                 ({"kind": "triangles", "name": "B"}, "min or max: at least one"),
                 ({"kind": "triangles", "name": "B", "min": 900, "max": 400}, "min: more than max"),
                 ({"kind": "triangles", "name": "B", "max": 1.5}, "max: a whole number of triangles"),
                 ({"kind": "inside_bounds", "name": "B", "bounds": [[1, 0, 0], [0, 1, 1]]}, "bounds: [[x0, y0, z0]"),
                 ({"kind": "no_modifiers"}, "name: missing"),
                 ({"kind": "no_modifiers", "name": "B", "quote": 3}, "quote: text, or leave it out")]
        for check, words in cases:
            errors = A.validate([check], advisory=True)
            self.assertEqual(len(errors), 1, (check, errors))
            self.assertIn(words, errors[0])

    def test_boxes_of_one_or_several(self):
        self.assertEqual(A.boxes_of([[0, 0, 0], [1, 1, 1]]), [[[0, 0, 0], [1, 1, 1]]])
        self.assertEqual(len(A.boxes_of(self.ADVICE[4]["bounds"])), 2)
        self.assertEqual(A.boxes_of("the planned box"), [])

    def test_results_are_marked_and_numbered_apart(self):
        results = A.run([{"kind": "triangles", "name": "B"}], advisory=True)  # Malformed: never measured.
        self.assertEqual((results[0]["id"], results[0]["advisory"], results[0]["status"]), ("a1", True, "unavailable"))


class QuoteTest(unittest.TestCase):
    def test_normalise_drops_case_punctuation_quotes_and_dashes(self):
        self.assertEqual(A.normalise("  The Lamp’s   BULB — inside!"), "the lamp's bulb inside")
        self.assertEqual(A.normalise("“close-up”, 0.45 m…"), "close up 0.45 m")
        self.assertEqual(A.normalise("Don't"), A.normalise("don’t"))
        self.assertEqual(A.normalise("ＦＵＬＬ　ｗｉｄｔｈ"), "full width")
        self.assertEqual(A.normalise(None), "")

    def test_quotes_found_word_for_word_in_any_user_message(self):
        for quote in ("an armchair on the rug", "An armchair, on the rug.", "the lamp's bulb inside its shade",
                      "the lamp’s bulb inside its shade", "keep the bottle, the box and their materials unchanged",
                      "close shot of the perfume bottle's cap", "1080x1920", "don't touch the rug"):
            self.assertTrue(A.quote_found(quote, USER), quote)
        self.assertTrue(A.quote_found("Make a cosy reading nook", USER[0]), "one message as text")

    def test_quotes_not_in_the_users_words_are_refused(self):
        for quote in ("an armchair on a rug", "armchair on the rug with cushions", "a render of the room", "",
                      "   ", "...", "arm", "chair on the rug"):  # Whole words only: "arm" is not "armchair".
            self.assertFalse(A.quote_found(quote, USER), quote)
        self.assertFalse(A.quote_found("an armchair on the rug", []))
        self.assertFalse(A.quote_found("reading nook ... perfume", USER), "an ellipsis stays in one message")

    def test_an_ellipsis_skips_words_in_order(self):
        self.assertTrue(A.quote_found("keep the bottle ... unchanged", USER))
        self.assertTrue(A.quote_found("keep the bottle… unchanged", USER))
        self.assertFalse(A.quote_found("unchanged ... keep the bottle", USER), "in order")


class ValidateTest(unittest.TestCase):
    def test_every_kind_has_a_valid_example(self):
        self.assertEqual(set(A.KINDS), {c["kind"] for c in VALID})
        self.assertEqual(A.validate(VALID), [])
        self.assertEqual(A.validate(VALID, USER), [])
        self.assertEqual(A.validate([]), [])

    def assertRefused(self, check: dict, *words: str):
        errors = A.validate([check])
        self.assertEqual(len(errors), 1, errors)
        for word in words:
            self.assertIn(word, errors[0])
        self.assertTrue(errors[0].startswith("checks[0]"), errors[0])

    def test_errors_name_the_field(self):
        cases = [
            ("not a check", "a check is"),
            ({"quote": "x"}, "kind", "not one of"),
            ({"kind": "bigness", "quote": "x"}, "kind", "'bigness'"),
            (good("exists"), "(exists) name: missing"),
            (good("exists", name=""), "name: a name"),
            (good("exists", name="A", type="blob"), "type: an object type"),
            (good("exists", name="A", size=[1, 1, 1]), "size: not a field of exists"),
            (good("count", prefix="A", collection="B", min=1), "exactly one"),
            (good("count", prefix="A"), "min or max"),
            (good("count", prefix="A", min=-1), "min: a whole number"),
            (good("count", prefix="A", min=1.5), "min: a whole number"),
            (good("count", prefix="A", min=3, max=2), "min: more than max"),
            (good("dims", name="A", size=[1, 1]), "size: [x, y, z]"),
            (good("dims", name="A", size=[None, None, None]), "at least one number"),
            (good("dims", name="A", size=[1, -1, 1]), "size"),
            (good("dims", name="A", size=[1, 1, 1], tol=-0.1), "tol: a distance"),
            (good("dims", name="A", size=[1, True, 1]), "size"),
            (good("contact", a="A", relation="on", b="B"), "relation: one of rests_on"),
            (good("contact", a="A", relation="touches", b="A"), "the same object"),
            (good("contact", a="A", relation="clear_of", b="B", gap="5cm"), "gap: a distance"),
            (good("suspended", a="S", b="S"), "b: the same as a; for two regions of one mesh give a_part and b_part"),
            (good("suspended", a="S", a_part="material:Wood", b="S", b_part="material:Wood"), "the same as a"),
            (good("suspended", a="S", a_part="Wood", b="B"), "a_part: material:NAME, group:NAME or attribute:NAME"),
            (good("attached_to", a="S", b="B", b_part="colour:red"), "b_part: material:NAME"),
            (good("suspended", a="S", b="B", clear=-1), "clear: a distance in m, 0 to 10"),
            (good("above", a="S", b="B", clear=1), "clear: not a field of above"),
            (good("below", a="S"), "(below) b: missing"),
            (good("preserved", names=["A"]), "(preserved) snapshot: missing"),
            (good("preserved", names=["A"], snapshot="s1", exclude=["colour"]), "exclude: a list of aspects to leave out"),
            (good("preserved", names=["A"], snapshot="s1", exclude=list(A.ASPECTS)), "exclude"),
            (good("no_overlap", names=["A"]), "names: 2 to 5"),
            (good("no_overlap", names=["A", "B", "C", "D", "E", "F"]), "names: 2 to 5"),
            (good("no_overlap", names=["A", "A"]), "different names"),
            (good("in_frame", camera="C", names=[]), "names: 1 to"),
            (good("in_frame", camera="C", names=["A"], min_inside=0), "min_inside"),
            (good("in_frame", camera="C", names=["A"], min_inside=1.5), "min_inside"),
            (good("in_frame", camera="C", names=["A"], region=[0.5, 0, 0.4, 1]), "region"),
            (good("in_frame", camera="C", names=["A"], region=[0, 0, 1]), "region"),
            (good("in_frame", names=["A"]), "camera: missing"),
            (good("in_frame", camera="C", names=["A"], frame=1.5), "frame: a whole frame number"),
            (good("light", name="K", type="MESH"), "type: a light type: POINT, SUN, SPOT, AREA"),
            (good("light", name="K", energy=[300, 100]), "energy: [min, max]"),
            (good("light", name="K", energy=[None, None]), "energy"),
            (good("light", name="K", energy=100), "energy"),
            (good("light", name="K", color=[1, 2, 0]), "color: [r, g, b]"),
            (good("light", name="K", color=[1, 1, 1], tol=2), "tol: a colour difference"),
            (good("light", name="K", spot_size=[-5, 30]), "spot_size: [min, max] cone angle in degrees"),
            (good("light", name="K", frame=3), "frame: not a field of light"),
            (good("faces_max", name="A", max=0), "max: a whole number"),
            (good("origin_base", name="A", origin_xy=[1, 2, 0]), "origin_xy: [x, y]"),
            (good("origin_base", name="A", origin_xy="post"), "origin_xy: [x, y]"),
            (good("faces_max", name="A", max="lots"), "max"),
            (good("setting", path="render.film_transparent", equals=1), "path: one of"),
            (good("setting", path="render.fps", equals=True), "equals: a number"),
            (good("setting", path="render.resolution_x"), "equals, or min"),
            (good("setting", path="render.resolution_x", equals=1, min=0), "one or the other"),
            (good("setting", path="render.engine", min=1), "min: a number, for a numeric setting"),
            (good("animated", name="A", path="location", frames=[1]), "frames"),
            (good("animated", name="A", path="location", frames=[5, 5]), "frames"),
            (good("animated", name="A", path="", frames=[1, 2]), "path"),
            (good("unchanged", names="Bottle"), "names: 1 to"),
            (good("unchanged", names=["Bottle"], tokens=["x"]), "tokens"),
            (good("file", path="/tmp/out.png"), "relative to the .blend"),
            (good("file", path="///tmp/out.png"), "relative"),
        ]
        for check, *words in cases:
            with self.subTest(check=check):
                self.assertRefused(check, *words)

    def test_quote_missing_or_not_the_users(self):
        self.assertIn("quote: the user's words", A.validate([{"kind": "exists", "name": "A"}])[0])
        self.assertIn("quote: the user's words", A.validate([{"kind": "exists", "name": "A", "quote": 3}])[0])
        errors = A.validate([{"kind": "exists", "name": "A", "quote": "a velvet armchair"}], USER)
        self.assertEqual(len(errors), 1)
        self.assertIn("'a velvet armchair' is not in the user's messages", errors[0])
        self.assertEqual(A.validate([{"kind": "exists", "name": "A", "quote": "a velvet armchair"}]), [],
                         "without the user's messages only the form is checked")

    def test_several_problems_all_reported_with_positions(self):
        errors = A.validate([VALID[0], good("contact", a="A", relation="near"), {"kind": "dims", "quote": ""}],
                            where="workers[2].checks")
        self.assertTrue(any(e.startswith("workers[2].checks[1] (contact) b: missing") for e in errors), errors)
        self.assertTrue(any(e.startswith("workers[2].checks[1] (contact) relation") for e in errors), errors)
        self.assertTrue(any(e.startswith("workers[2].checks[2] (dims) quote") for e in errors), errors)
        self.assertEqual(A.validate("checks"), ["checks: a list of checks"])


class ShapeTest(unittest.TestCase):
    def test_ids_in_brief_order_unless_given_and_types_upper(self):
        checks = A.normalised([good("exists", name="A", type="mesh"), {**good("exists", name="B"), "id": "keep1"},
                               good("exists", name="C")])
        self.assertEqual([c["id"] for c in checks], ["c1", "keep1", "c3"])
        self.assertEqual(checks[0]["type"], "MESH")
        self.assertEqual(VALID[1]["type"], "empty", "the caller's check is not changed")

    def test_preservation_says_what_changed_with_the_numbers(self):
        """A snapshot record against a later one, aspect by aspect (acceptance.record makes them)."""
        was = {"type": "MESH", "origin": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1],
               "materials": {"Bottle": ["Glass"]}, "dims": [0.1, 0.1, 0.3], "bounds": [[-0.05, -0.05, 0], [0.05, 0.05, 0.3]],
               "verts": 8, "faces": 6, "topology": "t1", "vertices": "v1", "sample": [[0, 0, 0], [0.05, 0.05, 0.3]]}
        self.assertEqual(A.preservation(was, dict(was)), [])
        painted = {**was, "materials": {"Bottle": ["Glass_Arcane"]}}
        self.assertEqual(A.preservation(was, painted), ["materials Bottle: Glass -> Glass_Arcane"])
        self.assertEqual(A.preservation(was, painted, exclude=["materials"]), [], "a look pass excludes materials")
        moved = {**was, "origin": [0, 0.012, 0], "bounds": [[-0.05, -0.038, 0], [0.05, 0.062, 0.3]]}
        self.assertEqual(A.preservation(was, moved), ["origin (0, 0, 0) -> (0, 0.012, 0) (12.0 mm)",
                                                      "bounds (-0.05, -0.05, 0)..(0.05, 0.05, 0.3) -> (-0.05, -0.038, 0).."
                                                      "(0.05, 0.062, 0.3) (up to 12.0 mm)"])
        pushed = {**was, "dims": [0.1, 0.1, 0.35], "bounds": [[-0.05, -0.05, 0], [0.05, 0.05, 0.35]], "vertices": "v2",
                  "sample": [[0, 0, 0], [0.05, 0.05, 0.35]]}
        self.assertEqual(A.preservation(was, pushed, exclude=["bounds"]),
                         ["size (0.1, 0.1, 0.3) -> (0.1, 0.1, 0.35) m (z +50.0 mm)",
                          "vertices moved, up to 50.0 mm at the sampled ones"])
        remodel = {**was, "verts": 12, "faces": 10, "topology": "t2", "vertices": "v3"}
        self.assertEqual(A.preservation(was, remodel), ["topology 8 vertices, 6 faces -> 12, 10"])
        self.assertEqual(A.preservation(was, remodel, exclude=["topology", "vertices"]), [], "a remodel excludes both")
        self.assertEqual(A.preservation(was, {**was, "rotation": [0, 0, 15.0], "scale": [1.2, 1.2, 1.2]}),
                         ["rotation (0, 0, 0)° -> (0, 0, 15)°", "scale (1, 1, 1) -> (1.2, 1.2, 1.2)"])

    def test_titles(self):
        titles = [A.title(c) for c in VALID]
        self.assertIn("contact Armchair rests_on Rug", titles)
        self.assertIn("in_frame Cam: Bottle", titles)
        self.assertIn("no_overlap Armchair, SideTable, FloorLamp", titles)
        self.assertIn("light_inside Bulb in Shade", titles)
        self.assertIn("count Armchair_Leg", titles)
        self.assertIn("suspended Sign[material:Wood] from Sign[group:Chains]", titles)
        self.assertIn("above Lamp over Table", titles)
        self.assertIn("below Rug under Lamp[material:Brass]", titles)
        self.assertIn("preserved Bottle as s1 except materials", titles)
        self.assertTrue(all(titles), titles)

    def test_verified_block_counts_and_lists_what_does_not_hold(self):
        results = [{"id": "c1", "kind": "exists", "title": "exists Armchair", "status": "pass", "detail": "ok"},
                   {"id": "c2", "kind": "contact", "title": "contact Armchair rests_on Rug", "status": "fail",
                    "detail": "4 supports: 2 touching, 2 floating. Fix: move Armchair by (0, 0, -0.012) m"},
                   {"id": "c3", "kind": "in_frame", "status": "unavailable", "detail": "Cap has no geometry"}]
        block = A.verified_block(results)
        self.assertEqual(block.splitlines()[0], "Verified by Loopcut on this scene: 1 of 3 checks.")
        self.assertIn("- c2 contact Armchair rests_on Rug: FAIL, 4 supports: 2 touching, 2 floating. Fix:", block)
        self.assertIn("- c3 in_frame: could not measure, Cap has no geometry", block)
        self.assertNotIn("c1", block)
        self.assertEqual(A.verified_block(results[:1]), "Verified by Loopcut on this scene: 1 of 1 checks.")


class RunTest(unittest.TestCase):
    def test_malformed_checks_are_unavailable_without_touching_blender(self):
        results = A.run([good("contact", a="A", relation="near", b="B"), "junk"])
        self.assertEqual([r["status"] for r in results], ["unavailable", "unavailable"])
        self.assertEqual(results[0]["id"], "c1")
        self.assertTrue(results[0]["detail"].startswith("relation: one of"), results[0]["detail"])
        self.assertEqual(set(results[0]) >= {"id", "kind", "quote", "status", "detail"}, True)


if __name__ == "__main__":
    unittest.main()
