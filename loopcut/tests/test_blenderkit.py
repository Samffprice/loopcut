"""The BlenderKit tools' pure parts: search URL, rows, file choice by resolution, credit, key lookup
(env, credentials, the BlenderKit add-on's own preferences) and the gate. The live API is covered by
harness/blenderkit_check.py."""

import json
import os
import sys
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)
# The fake is shared by every test module; the context this one needs is added to whichever won.
if not hasattr(sys.modules["bpy"], "context"):
    sys.modules["bpy"].context = types.SimpleNamespace(preferences=types.SimpleNamespace(addons={}))

from loopcut import assets, blenderkit, credentials, state, tools  # noqa: E402

# Search API bodies keyed by the query sent, recorded 2026-10-25 (trimmed to the fields read).
RECORDED = json.loads((Path(__file__).parent / "fixtures" / "blenderkit-search-2026-10-25.json")
                      .read_text(encoding="utf-8"))["bodies"]

FILES = [{"fileType": "blend", "downloadUrl": "https://www.blenderkit.com/api/v1/downloads/a/"},
         {"fileType": "resolution_0_5K", "downloadUrl": "https://www.blenderkit.com/api/v1/downloads/b/"},
         {"fileType": "resolution_1K", "downloadUrl": "https://www.blenderkit.com/api/v1/downloads/c/"},
         {"fileType": "thumbnail", "downloadUrl": "https://www.blenderkit.com/api/v1/downloads/t/"}]
ASSET = {"id": "8c2e3c07", "assetBaseId": "base1", "name": "Dining chair", "assetType": "model", "isFree": True,
         "canDownload": True, "license": "royalty_free", "faceCount": 20384, "dimensionX": 1.2, "dimensionY": 0.5,
         "dimensionZ": 0.9, "author": {"fullName": "dleon3D"}, "files": FILES}


class BlenderKitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["LOOPCUT_CONFIG_DIR"] = self.tmp.name
        os.environ.pop(blenderkit.ENV_KEY, None)
        self.addCleanup(os.environ.pop, "LOOPCUT_CONFIG_DIR", None)
        sys.modules["bpy"].context.preferences.addons = {}

    def test_search_url_puts_filters_in_the_query(self):
        url = blenderkit.search_url("office chair", "model", 50, free_only=True)
        self.assertEqual(url, "https://www.blenderkit.com/api/v1/search/?query=office+chair+asset_type%3Amodel+is_free%3Atrue&page_size=12")
        self.assertNotIn("is_free", blenderkit.search_url("x", "hdr", 3, free_only=False))

    def test_rows_credit_and_file_choice(self):
        self.assertEqual(blenderkit.describe_asset(1, ASSET),
                         "#1 Dining chair (blenderkit); 1.20x0.50x0.90 m; 20384 faces; free; by dleon3D; id 8c2e3c07")
        paid = blenderkit.describe_asset(2, {**ASSET, "isFree": False, "canDownload": False})
        self.assertIn("Full plan; needs a BlenderKit key", paid)
        self.assertEqual(blenderkit.credit_line(ASSET),
                         "From BlenderKit (https://www.blenderkit.com/asset-gallery-detail/base1/), Royalty Free (commercial use, no credit needed), by dleon3D.")
        self.assertEqual(blenderkit.pick_file(FILES, "1k")[0], "resolution_1K")
        self.assertEqual(blenderkit.pick_file(FILES, "4k")[0], "resolution_1K", "nearest lower resolution")
        self.assertEqual(blenderkit.pick_file(FILES, "original")[0], "blend")
        self.assertEqual(blenderkit.pick_file(FILES[:1], "0.5k")[0], "blend", "the original when no variant exists")
        self.assertIsNone(blenderkit.pick_file([], "1k"))
        with self.assertRaises(tools.ToolError):
            blenderkit.pick_file(FILES, "8k")

    def test_rows_read_the_live_apis_parameters(self):
        # The live search API (2026-10-10) has null top-level sizes: they, the face count and the style are in
        # dictParameters; the detail endpoint lists them as parameters. Rows used to lose all three.
        live = {"id": "v1", "name": "Vintage Wooden Chair", "isFree": True, "dimensionX": None, "faceCount": None,
                "description": "A classic vintage\nwooden chair with a ladder-style backrest, natural wood grain "
                               "and subtle wear typical of older furniture pieces.",
                "tags": ["chair", "wooden"], "author": {"fullName": "Ann"},
                "dictParameters": {"dimensionX": 0.5082, "dimensionY": 0.5558, "dimensionZ": 0.9366,
                                   "faceCount": 72794, "modelStyle": "realistic"}}
        row = blenderkit.describe_asset(3, live)
        self.assertTrue(row.startswith("#3 Vintage Wooden Chair (blenderkit); 0.51x0.56x0.94 m; 72794 faces; "
                                       "realistic style; A classic vintage wooden chair"), row)
        self.assertTrue(row.endswith("; id v1"), row)
        self.assertIn("…; free; by Ann", row)
        self.assertNotIn("\n", row)
        self.assertLessEqual(len(row.split("; ")[5]), 90)
        detail = {"parameters": [{"parameterType": "dimensionX", "value": 2}, {"parameterType": "dimensionY", "value": 1},
                                 {"parameterType": "dimensionZ", "value": "0.5"}, {"parameterType": "faceCount", "value": 9}]}
        self.assertEqual(blenderkit.dimensions(detail), [2.0, 1.0, 0.5])
        self.assertEqual(blenderkit.parameter(detail, "faceCount"), 9)
        self.assertIsNone(blenderkit.dimensions({"dictParameters": {"dimensionX": 1, "dimensionY": 0, "dimensionZ": 1}}))
        self.assertIsNone(blenderkit.dimensions({}))
        tags_only = blenderkit.describe_asset(1, {**live, "description": "", "dictParameters": {}})
        self.assertIn("; chair, wooden; free", tags_only)

    def search(self, query, type="model", served=None):
        """search_blenderkit against recorded API bodies (no network): the body recorded for the query it
        sends, or `served` for any query. Returns (its result or the ToolError, the queries sent)."""
        sent = []
        def get_json(url, headers=None):
            sent.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0])
            return served if served is not None else RECORDED.get(sent[-1], {"count": 0, "results": []})
        with mock.patch.object(blenderkit, "get_json", get_json), \
                mock.patch.object(blenderkit, "_thumbnail", return_value=None), \
                mock.patch.object(blenderkit, "sheet", return_value=None):
            try:
                return blenderkit.search_blenderkit(query, type), sent
            except tools.ToolError as ex:
                return ex, sent

    def test_style_and_resolution_words_are_left_out_of_the_search(self):
        # rel018-ja2: the user asked for 本物っぽい (real-looking), every query had "realistic", and every search
        # came back 8 of 10000 led by "Uncut summer grass for scettering", for a lamp, a rug or a cat alike.
        state.reset()
        self.addCleanup(state.reset)
        result, sent = self.search("realistic floor lamp reading lamp")
        self.assertEqual(sent, ["floor lamp reading lamp asset_type:model is_free:true"])
        rows = [line for line in result.text.splitlines() if line.startswith("#")]
        self.assertEqual(rows[0].split(" (")[0], "#1 Arc Floor Lamp")
        self.assertNotIn("grass", result.text)
        self.assertTrue(result.text.startswith("8 of 368 BlenderKit models for 'floor lamp reading lamp' (free assets):\n"
                                               "Searched without 'realistic'"), result.text[:200])
        self.assertEqual(blenderkit.search_words("Photorealistic, hyper-realistic 4K oak table (realism)"),
                         ("oak table", ["Photorealistic,", "hyper-realistic", "4K", "(realism)"]))
        for kept in ("low poly tree", "stylized lamp", "high quality sofa", "real wood chair", "k2 mountain"):
            self.assertEqual(blenderkit.search_words(kept), (kept, []))
        self.assertEqual(self.search("wood floor realistic", "material")[1], ["wood floor asset_type:material is_free:true"])
        error, sent = self.search("photorealistic 4k")
        self.assertIsInstance(error, tools.ToolError)
        self.assertIn("Give words for what the asset is", str(error))
        self.assertEqual(sent, [], "nothing to search for")

    def test_the_generic_list_at_the_count_cap_reads_as_no_match(self):
        # The recorded 10000 body served for other words, as for any word BlenderKit matches on most models.
        state.reset()
        self.addCleanup(state.reset)
        generic = RECORDED["realistic floor lamp reading lamp asset_type:model is_free:true"]
        error, _ = self.search("cozy reading nook armchair", served=generic)
        self.assertIsInstance(error, tools.ToolError)
        self.assertEqual(str(error), "BlenderKit returned its generic list for 'cozy reading nook armchair', not "
                                     "matches. Search with the object's own name, such as 'floor lamp'.")
        self.assertNotIn("fewer or more general", str(error), "that sends the model back toward the problem")
        mixed = RECORDED["realistic floor lamp asset_type:model is_free:true"]
        result, _ = self.search("floor lamps", served=mixed)
        self.assertIn("#1 Moon Floor Lamp", result.text, "at the cap, but some names match: the rows as they are")
        self.assertIn("Uncut summer grass", result.text)
        result, _ = self.search("cozy reading nook armchair", served={**generic, "count": 9999})
        self.assertIn("Uncut summer grass", result.text, "under the cap the rows are the library's answer")
        self.assertFalse(blenderkit.unrelated("椅子", 10000, generic["results"]), "no words to compare: not judged")
        for plural, name in (("benches", "Park Bench"), ("lamps", "Arc Floor Lamp"), ("berries", "Berry Bowl"),
                             ("glasses", "Wine Glass"), ("bench", "Garden Benches")):
            self.assertFalse(blenderkit.unrelated(plural, 10000, [{"name": name}]), (plural, name))
        self.assertTrue(blenderkit.unrelated("benches", 10000, [{"name": "Realistic Hair"}]))

    def test_key_from_env_then_credentials_then_the_blenderkit_addon(self):
        self.assertEqual(blenderkit.api_key(), "")
        addon = types.SimpleNamespace(preferences=types.SimpleNamespace(api_key=" addon-key "))
        sys.modules["bpy"].context.preferences.addons = {"bl_ext.blender_org.blenderkit": addon}
        self.assertEqual(blenderkit.api_key(), "addon-key")
        credentials.store(blenderkit.KEY_ID, "stored-key")
        self.assertEqual(blenderkit.api_key(), "stored-key")
        os.environ[blenderkit.ENV_KEY] = "env-key"
        try:
            self.assertEqual(blenderkit.api_key(), "env-key")
        finally:
            del os.environ[blenderkit.ENV_KEY]

    def test_labels_gate_and_bad_arguments(self):
        self.assertIn("import_asset", tools.CHANGES_SCENE)
        self.assertEqual(assets.describe("import_asset", {"source": "blenderkit", "id": "abc"}),
                         ("Import BlenderKit asset abc (1k)", ""))
        self.assertEqual(assets.describe("search_assets", {"source": "blenderkit", "query": "sky", "type": "hdri"}),
                         ("Search BlenderKit hdr: sky", ""))
        result = tools.execute("import_asset", json.dumps({"source": "blenderkit", "id": "../x"}))
        self.assertFalse(result.ok)
        self.assertIn("letters, digits", result.text)
        result = tools.execute("search_assets", json.dumps({"source": "blenderkit", "query": "x", "type": "brush"}))
        self.assertFalse(result.ok)
        self.assertIn("model, material, hdr", result.text)


if __name__ == "__main__":
    unittest.main()
