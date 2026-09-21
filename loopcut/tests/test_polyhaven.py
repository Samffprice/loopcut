"""The Poly Haven tools' pure parts: ranking, map selection, path safety, wiring. No Blender and
no network; the real import is covered by harness/polyhaven_check.py."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, polyhaven, tools  # noqa: E402

ASSETS = {
    "wooden_table_02": {"name": "Wooden Table 02", "type": 2, "tags": ["wooden", "worn"],
                        "categories": ["furniture", "table"], "download_count": 500,
                        "dimensions": [1134.4, 706.0, 799.5], "polycount": 196},
    "ArmChair_01": {"name": "Arm Chair 01", "type": 2, "tags": ["chair", "wood", "varnished"],
                    "categories": ["furniture", "seating"], "download_count": 32051},
    "brick_wall_02": {"name": "Brick Wall 02", "type": 1, "tags": ["red"], "categories": ["wall", "brick"],
                      "download_count": 9000, "dimensions": [2000, 2000]},
    "lakeside_sunrise": {"name": "Lakeside Sunrise", "type": 0, "tags": ["sun", "lake"],
                         "categories": ["outdoor", "sunrise-sunset"], "evs_cap": 22},
}

FILES = {
    "Diffuse": {"1k": {"jpg": {"url": "https://dl/x_diff_1k.jpg", "md5": "a"}}},
    "Rough": {"1k": {"jpg": {"url": "https://dl/x_rough_1k.jpg"}}},
    "nor_gl": {"1k": {"jpg": {"url": "https://dl/x_nor_gl_1k.jpg"}}, "2k": {"jpg": {"url": "u"}}},
    "nor_dx": {"1k": {"jpg": {"url": "https://dl/x_nor_dx_1k.jpg"}}},
    "AO": {"1k": {"jpg": {"url": "https://dl/x_ao_1k.jpg"}}},
    "arm": {"1k": {"jpg": {"url": "https://dl/x_arm_1k.jpg"}}},
    "blend": {"1k": {"blend": {"url": "https://dl/x.blend", "include": {}}}},
}


class RankingTest(unittest.TestCase):
    def test_more_matching_words_rank_first_then_name_hits_then_popularity(self):
        found = polyhaven.match_assets("wood table", ASSETS)
        self.assertEqual([slug for slug, _ in found], ["wooden_table_02", "ArmChair_01"])

    def test_a_word_that_matches_nothing_is_ignored(self):
        found = polyhaven.match_assets("red brick wall", ASSETS)
        self.assertEqual(found[0][0], "brick_wall_02")

    def test_no_match_is_empty_and_limit_holds(self):
        self.assertEqual(polyhaven.match_assets("spaceship", ASSETS), [])
        self.assertEqual(len(polyhaven.match_assets("furniture", ASSETS, limit=1)), 1)

    def test_description_carries_what_the_model_needs_to_choose(self):
        line = polyhaven.describe_asset(1, "wooden_table_02", ASSETS["wooden_table_02"])
        self.assertIn("1.13x0.71x0.80 m", line)
        self.assertIn("196 faces", line)
        self.assertIn("2.0x2.0 m tile", polyhaven.describe_asset(2, "brick_wall_02", ASSETS["brick_wall_02"]))
        self.assertIn("22 EV", polyhaven.describe_asset(3, "lakeside_sunrise", ASSETS["lakeside_sunrise"]))


class MapSelectionTest(unittest.TestCase):
    def test_picks_principled_roles_and_skips_dx_normals_and_ao(self):
        picked = polyhaven.pick_texture_maps(FILES, "1k")
        self.assertEqual(set(picked), {"base_color", "roughness", "normal"})
        self.assertEqual(picked["base_color"]["url"], "https://dl/x_diff_1k.jpg")

    def test_arm_stands_in_for_a_missing_roughness_map(self):
        files = {k: v for k, v in FILES.items() if k != "Rough"}
        self.assertIn("arm", polyhaven.pick_texture_maps(files, "1k"))

    def test_a_color_map_not_named_diffuse_still_gives_a_base_color(self):
        files = {"col_1": FILES["Diffuse"], "col_2": FILES["Diffuse"]}
        self.assertIn("base_color", polyhaven.pick_texture_maps(files, "1k"))

    def test_missing_resolution_yields_nothing_and_available_lists_what_exists(self):
        self.assertEqual(polyhaven.pick_texture_maps(FILES, "4k"), {})
        self.assertEqual(polyhaven.available(FILES), "1k, 2k")


class SafetyTest(unittest.TestCase):
    def test_slugs_are_letters_digits_underscore_hyphen(self):
        self.assertTrue(polyhaven.valid_slug("wooden_table_02"))
        self.assertTrue(polyhaven.valid_slug("ArmChair-01"))
        for bad in ("", "../etc", "a b", "x/y", "a" * 101):
            self.assertFalse(polyhaven.valid_slug(bad), bad)

    def test_include_paths_cannot_escape_the_asset_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(polyhaven.safe_include(root, "textures/a.jpg"), (root / "textures/a.jpg").resolve())
            for bad in ("../a.jpg", "/etc/passwd", "textures/../../a.jpg", ""):
                self.assertIsNone(polyhaven.safe_include(root, bad), bad)

    def test_bad_arguments_are_errors_the_model_can_read(self):
        result = tools.execute("import_polyhaven", json.dumps({"asset_id": "../x"}))
        self.assertFalse(result.ok)
        self.assertIn("letters, digits", result.text)
        result = tools.execute("import_polyhaven", json.dumps({"asset_id": "ok", "resolution": "16k"}))
        self.assertFalse(result.ok)
        self.assertIn("1k, 2k, 4k", result.text)
        result = tools.execute("search_polyhaven", json.dumps({"query": "x", "type": "sounds"}))
        self.assertFalse(result.ok)
        self.assertIn("hdris, textures, models", result.text)


class WiringTest(unittest.TestCase):
    def test_tools_are_offered_dispatched_gated_and_labelled(self):
        # agent._tool_schemas is stubbed by another test module; the dispatch table is the contract here.
        names = [schema["function"]["name"] for schema in polyhaven.SCHEMAS]
        self.assertEqual(set(names), set(polyhaven.DISPATCH))
        self.assertIn("search_polyhaven", names)
        self.assertIn("import_polyhaven", names)
        self.assertIn("import_polyhaven", tools.CHANGES_SCENE, "an import takes a checkpoint and asks first")
        self.assertNotIn("search_polyhaven", tools.CHANGES_SCENE)
        self.assertEqual(polyhaven.describe("search_polyhaven", {"query": "oak", "type": "models"}),
                         ("Search Poly Haven models: oak", ""))
        self.assertEqual(polyhaven.describe("import_polyhaven", {"asset_id": "oak_01"})[0],
                         "Import Poly Haven asset oak_01 (1k)")
        self.assertIsNone(polyhaven.describe("run_python", {}))
        self.assertIn("search_polyhaven", agent.SYSTEM_PROMPT)

    def test_thumbnail_url_asks_the_cdn_for_the_sheet_size(self):
        record = {"thumbnail_url": "https://cdn.polyhaven.com/asset_img/thumbs/ArmChair_01.png?width=256&height=256&v=abbc4732"}
        self.assertEqual(polyhaven.thumbnail_url("ArmChair_01", record),
                         "https://cdn.polyhaven.com/asset_img/thumbs/ArmChair_01.png?v=abbc4732&width=256&height=256")
        self.assertTrue(polyhaven.thumbnail_url("x", {}).startswith("https://cdn.polyhaven.com/asset_img/thumbs/x.png?"))

    def test_cache_lives_under_loopcut_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["LOOPCUT_DATA_DIR"] = tmp
            try:
                self.assertEqual(polyhaven.cache_root(), Path(tmp) / "polyhaven")
            finally:
                del os.environ["LOOPCUT_DATA_DIR"]


if __name__ == "__main__":
    unittest.main()
