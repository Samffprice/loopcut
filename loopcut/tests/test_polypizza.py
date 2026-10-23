"""The Poly Pizza tools' pure parts: query parameters, result rows, credit lines, the key lookup, and
without a key a search answered from BlenderKit or Poly Haven and an import steered to them. The live
API is covered by harness/polypizza_check.py when a key is set."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import assets, asset_picks, blenderkit, credentials, polyhaven, polypizza, state, tools  # noqa: E402

MODEL = {"ID": "abc123", "Title": "Low Poly Tree", "Tri Count": 420, "Licence": "CC-BY", "Category": "Nature",
         "Creator": {"Username": "quaternius"}, "Attribution": "Low Poly Tree by quaternius [CC-BY] via Poly Pizza",
         "Animated": False, "Thumbnail": "https://static.poly.pizza/abc123.png", "Download": "https://static.poly.pizza/abc123.glb"}


class PolyPizzaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["LOOPCUT_CONFIG_DIR"] = self.tmp.name
        os.environ.pop(polypizza.ENV_KEY, None)
        self.addCleanup(os.environ.pop, "LOOPCUT_CONFIG_DIR", None)
        state.reset()
        self.addCleanup(state.reset)
        for target, attribute in ((blenderkit, "_thumbnail"), (blenderkit, "sheet"), (polyhaven, "_thumbnail"),
                                  (polyhaven, "sheet"), (polypizza, "_thumbnail"), (polypizza, "sheet")):
            patcher = mock.patch.object(target, attribute, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(blenderkit, "_headers", return_value={})  # No BlenderKit key either.
        patcher.start()
        self.addCleanup(patcher.stop)

    def search(self, kit=(), haven=None, pizza=None):
        """search_assets(source="polypizza") with each library's API answering as given: an exception
        is raised, anything else returned."""
        def answer(value):
            return mock.Mock(side_effect=value) if isinstance(value, Exception) else mock.Mock(return_value=value)
        kit = kit if isinstance(kit, Exception) else {"results": list(kit), "count": len(kit)}
        with mock.patch.object(blenderkit, "get_json", answer(kit)) as kit_api, \
                mock.patch.object(polyhaven, "assets", answer(haven or {})) as haven_api, \
                mock.patch.object(polypizza, "get_json", answer(pizza or {})) as pizza_api:
            result = tools.execute("search_assets", json.dumps({"source": "polypizza", "query": "typewriter"}))
        return result, kit_api, haven_api, pizza_api

    def test_search_params_are_capitalized_numeric_and_bounded(self):
        self.assertEqual(polypizza.search_params(), {"Limit": 8})
        self.assertEqual(polypizza.search_params("Nature", "CC0", True, 50), {"Limit": 12, "Category": 6, "License": 1, "Animated": 1})
        with self.assertRaises(tools.ToolError):
            polypizza.search_params(category="Trees")
        with self.assertRaises(tools.ToolError):
            polypizza.search_params(licence="MIT")

    def test_rows_and_credit(self):
        self.assertEqual(polypizza.describe_model(1, MODEL),
                         "#1 Low Poly Tree (polypizza); 420 tris; CC-BY; by quaternius; Nature; id abc123")
        self.assertIn("credit required. Attribution: Low Poly Tree by quaternius", polypizza.credit_line(MODEL))
        cc0 = polypizza.credit_line({**MODEL, "Licence": "CC0 1.0"})
        self.assertTrue(cc0.startswith("From Poly Pizza (https://poly.pizza/m/abc123), CC0 1.0: no credit required"), cc0)
        self.assertTrue(polypizza.credit_line(MODEL).startswith("From Poly Pizza (https://poly.pizza/m/abc123), CC-BY: credit required"))
        self.assertTrue(polypizza.is_glb(b"glTF\x02\x00"))
        self.assertFalse(polypizza.is_glb(b"<!DOCTYPE"))

    def test_key_comes_from_env_then_credentials(self):
        self.assertEqual(polypizza.api_key(), "")
        credentials.store(polypizza.KEY_ID, "stored-key")
        self.assertEqual(polypizza.api_key(), "stored-key")
        os.environ[polypizza.ENV_KEY] = "env-key"
        try:
            self.assertEqual(polypizza.api_key(), "env-key")
        finally:
            del os.environ[polypizza.ENV_KEY]

    def test_labels_gate_and_bad_id(self):
        self.assertIn("import_asset", tools.CHANGES_SCENE)
        self.assertEqual(assets.describe("import_asset", {"source": "polypizza", "id": "abc"}),
                         ("Import Poly Pizza model abc", ""))
        result = tools.execute("import_asset", json.dumps({"source": "polypizza", "id": "../x"}))
        self.assertFalse(result.ok)
        self.assertIn("letters, digits", result.text)

    def test_keyless_search_answers_from_blenderkit_with_pick_numbers(self):
        # Hosted Fast, told to ask the user for a key, asked and ended the turn (2026-10-23).
        kit = [{"id": "1d00af94-b833-43ee-ae6e-53e40a3fb2c1", "name": "Typewriter", "isFree": True}]
        result, kit_api, haven_api, pizza_api = self.search(kit=kit)
        self.assertTrue(result.ok, result.text)
        lines = result.text.splitlines()
        self.assertEqual(lines[0], "Poly Pizza isn't set up (no API key), so these are from BlenderKit instead.")
        self.assertTrue(lines[1].startswith("1 of 1 BlenderKit models for 'typewriter'"), lines[1])
        self.assertTrue(lines[2].startswith("#1 Typewriter"), lines[2])
        self.assertIn(asset_picks.HINT, result.text)
        self.assertEqual(asset_picks.find(1)["source"], "blenderkit")
        self.assertIn("is_free%3Atrue", kit_api.call_args.args[0])  # Free assets: no BlenderKit key.
        haven_api.assert_not_called()
        pizza_api.assert_not_called()
        self.assertNotIn("ask the user", result.text)

    def test_keyless_search_falls_to_poly_haven_then_names_the_keyless_sources(self):
        haven = {"typewriter_01": {"name": "Typewriter", "type": 2}}
        offline = tools.ToolError("HTTP 503 at https://www.blenderkit.com/api/v1/search/")
        result, _, _, pizza_api = self.search(kit=offline, haven=haven)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.text.splitlines()[0],
                         "Poly Pizza isn't set up (no API key), so these are from Poly Haven instead.")
        self.assertEqual(asset_picks.find(1)["source"], "polyhaven")
        pizza_api.assert_not_called()
        result, _, _, _ = self.search()
        self.assertFalse(result.ok)
        self.assertTrue(result.text.startswith(
            "Poly Pizza isn't set up (it needs an API key), so BlenderKit and Poly Haven were searched instead: "
            "No BlenderKit models match 'typewriter'."), result.text)
        self.assertIn("No Poly Haven models match 'typewriter'.", result.text)
        self.assertTrue(result.text.endswith("Sources blenderkit, polyhaven and library need no key."), result.text)
        self.assertNotIn("ask the user", result.text)

    def test_keyed_search_is_poly_pizza_s_own(self):
        credentials.store(polypizza.KEY_ID, "stored-key")
        result, kit_api, _, pizza_api = self.search(pizza={"results": [MODEL], "total": 1})
        self.assertTrue(result.ok, result.text)
        self.assertTrue(result.text.startswith(
            "1 of 1 Poly Pizza models for 'typewriter':\n#1 Low Poly Tree (polypizza)"), result.text)
        self.assertEqual(pizza_api.call_args.args[1], {"x-auth-token": "stored-key"})
        kit_api.assert_not_called()

    def test_keyless_import_steers_to_the_keyless_sources(self):
        with mock.patch.object(polypizza, "get_json") as api:
            result = tools.execute("import_asset", json.dumps({"source": "polypizza", "id": "abc123"}))
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "Poly Pizza isn't set up (it needs an API key). Search blenderkit or polyhaven "
                                      "instead; they need no key.")
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
