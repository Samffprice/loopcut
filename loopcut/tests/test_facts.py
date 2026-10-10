"""facts.py's pure half: which planned box a deliverable belongs to and how far out it stands, and the
line the delegate result shows. measure() runs in Blender (harness/m4_e2e_check.py)."""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import facts  # noqa: E402

BOXES = [[[-0.38, 0.02, 0], [-0.22, 0.18, 0.26]], [[-0.08, 0.02, 0], [0.08, 0.18, 0.34]],
         [[0.22, 0.02, 0], [0.38, 0.18, 0.24]]]


def fact(**more) -> dict:
    base = {"tris": 480, "base_tris": 480, "modifiers": [], "faces": 250, "smooth": 0.0, "non_manifold": 0,
            "materials": {"Shop_Glass": 120, "Shop_PotionRed": 100, "Shop_WoodDark": 30}, "size": [0.14, 0.14, 0.33],
            "bounds": [[-0.07, 0.03, 0], [0.07, 0.17, 0.33]], "origin": [0, 0.1, 0], "origin_off": 0.0,
            "planned": {"box": 2, "count": 3, "out": {}}}
    base.update(more)
    return base


class PlannedTest(unittest.TestCase):
    def test_the_box_it_overlaps_most_and_how_far_out(self):
        self.assertEqual(facts.planned([[-0.07, 0.03, 0], [0.07, 0.17, 0.33]], BOXES), {"box": 2, "count": 3, "out": {}})
        sign = facts.planned([[-2.875, 0.5, 0], [-1.6, 0.9, 2.3]], [[[-2.7, 0.5, 0], [-1.6, 0.9, 2.3]]])
        self.assertEqual(sign, {"box": 1, "count": 1, "out": {"x-": 0.175}}, "LOW sign: 17.5 cm out of its box")
        self.assertEqual(facts.planned([[1, 1, 0], [1.2, 1.2, 0.3]], BOXES)["box"], 3, "none overlaps: the nearest")

    def test_no_box_no_plan(self):
        self.assertIsNone(facts.planned([[0, 0, 0], [1, 1, 1]], []))
        self.assertIsNone(facts.planned(None, BOXES))

    def test_outside_uses_the_tolerance(self):
        self.assertEqual(facts.outside(fact(planned={"box": 1, "count": 1, "out": {"x-": 0.175, "z+": 0.01}})),
                         {"x-": 0.175})


class TextTest(unittest.TestCase):
    def test_one_line_with_every_fact(self):
        self.assertEqual(facts.text("RedPotion", fact()),
                         "RedPotion: 480 tris; no modifiers; 0% smooth; 0 non-manifold edges; 0.14 x 0.14 x 0.33 m; "
                         "inside box 2; origin 0 mm from its base centre; faces Shop_Glass 120, Shop_PotionRed 100, "
                         "Shop_WoodDark 30")

    def test_what_differs_from_the_brief_shows(self):
        shelf = fact(tris=1820, base_tris=1100, modifiers=["Bevel"], smooth=1.0, non_manifold=120, origin_off=0.009,
                     planned={"box": 1, "count": 1, "out": {"x-": 0.175, "z+": 0.004}})
        self.assertEqual(facts.text("Shelf", shelf),
                         "Shelf: 1,820 tris (base 1,100); modifiers Bevel; 100% smooth; 120 non-manifold edges; "
                         "0.14 x 0.14 x 0.33 m; outside its box x- 175 mm, z+ 4 mm; origin 9 mm from its base centre; "
                         "faces Shop_Glass 120, Shop_PotionRed 100, Shop_WoodDark 30")

    def test_more_than_it_measures(self):
        self.assertEqual(facts.text("+more", 3), "+3 more not measured")

    def test_the_uv_line_is_measured_evidence_or_nothing(self):
        uv = {"meshes": 2, "mapped": 2, "images": True, "islands": 14, "outside": 0, "overlap": [], "area": 0.4,
              "angle": 1.04, "stretched": 0.02, "density": [180, 420], "skipped": []}
        self.assertEqual(facts.uv_text("Crate", uv), "Crate UVs: all meshes mapped, 14 islands, all inside 0-1, no "
                         "overlap, stretch 1.04 (2% of area over 1.5x), 180-420 px/m at 1024 px")
        bad = {**uv, "mapped": 1, "outside": 3, "overlap": ["Crate_Lid"], "density": [300], "skipped": ["Crate_Rope"]}
        self.assertEqual(facts.uv_text("Crate", bad), "Crate UVs: 1 of 2 meshes mapped, 14 islands, 3 faces outside "
                         "0-1, overlap in Crate_Lid, stretch 1.04 (2% of area over 1.5x), 300 px/m at 1024 px, not "
                         "audited (over 20,000 faces): Crate_Rope")
        self.assertEqual(facts.uv_text("Sign", {**uv, "meshes": 1, "mapped": 0, "area": 0.0, "density": []}),
                         "Sign UVs: none on its 1 mesh, and its materials read images")
        self.assertEqual(facts.uv_text("Sign", None), "", "no map and no image: nothing to say")
        self.assertEqual(facts.uv_text("Sign", {"error": "KeyError: 'x'"}), "Sign UVs: not measured (KeyError: 'x')")


class FlagTest(unittest.TestCase):
    """E3: the worker's words against its measured facts, conservatively: a clear contradiction only."""

    BARREL = {"Barrel": fact(tris=1548, base_tris=1548, faces=700, smooth=1.0,
                             materials={"Shop_WoodLight": 500, "Shop_Iron": 200})}
    PALETTE = ["Shop_WoodLight", "Shop_Iron", "Shop_Gold", "Shop_Glass"]

    def test_the_replays_false_claims(self):
        """HIGH barrel said flat shading; LOW potions named a glass neck; both missed their band."""
        said = "A 14-stave barrel, flat shaded, ~600 tris, with Shop_Iron hoops and a Shop_Gold bung."
        self.assertEqual(facts.flags(said, self.BARREL, self.PALETTE),
                         ["says flat shading; smooth faces measured: Barrel 100%",
                          "names Shop_Gold; no face of its deliverables wears it",
                          "says 600 triangles; measured 1,548"])
        potions = {"RedPotion": fact(materials={"Shop_PotionRed": 200, "Shop_WoodDark": 50})}
        self.assertEqual(facts.flags("Three bottles with a Shop_Glass neck and cork.", potions, self.PALETTE),
                         ["names Shop_Glass; no face of its deliverables wears it"])

    def test_no_flag_without_a_clear_contradiction(self):
        for said in ("Smooth shaded, not flat-shaded; no Shop_Gold on it. 1,548 triangles.",
                     "Kept under 2000 triangles, inside the 400-1000 tris band... roughly 1,400 tris.",
                     "Built the barrel with Shop_WoodLight staves.", ""):
            self.assertEqual(facts.flags(said, self.BARREL, self.PALETTE), [], said)
        flat = {"Barrel": fact(smooth=0.04)}
        self.assertEqual(facts.flags("Flat shaded throughout.", flat, []), [], "4% smooth is flat")
        self.assertEqual(facts.flags("Flat shaded.", {}, []), [], "nothing measured, nothing to contradict")

    def test_a_missing_deliverable_is_flagged_whatever_it_says(self):
        """asset_pack's treasurechest_fix1: "Rebuilt TreasureChest complete ... 1566 tris" after one step
        that built nothing; nothing was measured, so no other flag could say so."""
        said = "Rebuilt TreasureChest complete: flat shaded, 1566 tris, Shop_Gold lock."
        missing = ["deliverable missing: no object named TreasureChest in its copy at report time"]
        self.assertEqual(facts.flags(said, {}, self.PALETTE, ["TreasureChest"]), missing)
        self.assertEqual(facts.flags("", {}, [], ["TreasureChest"]), missing, "a synthesised report too")
        self.assertEqual(facts.flags(said, self.BARREL, self.PALETTE, ["TreasureChest"])[0], missing[0],
                         "first, before what its other objects contradict")
        self.assertEqual(facts.flags(said, {}, self.PALETTE, []), [])

    def test_a_total_over_several_deliverables_counts(self):
        three = {"Red": fact(tris=300), "Blue": fact(tris=320), "Green": fact(tris=280)}
        self.assertEqual(facts.flags("Three bottles, 900 triangles in all.", three, []), [])
        self.assertEqual(facts.flags("Three bottles, 2,400 triangles.", three, []),
                         ["says 2,400 triangles; measured 300, 320, 280, 900 (the last in all)"])


if __name__ == "__main__":
    unittest.main()
