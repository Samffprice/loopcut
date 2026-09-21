"""The Poly Pizza tools' pure parts: query parameters, result rows, credit lines, the key lookup and
the no-key error. The live API is covered by harness/polypizza_check.py when a key is set."""

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

from loopcut import credentials, polypizza, tools  # noqa: E402

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

    def test_search_params_are_capitalized_numeric_and_bounded(self):
        self.assertEqual(polypizza.search_params(), {"Limit": 8})
        self.assertEqual(polypizza.search_params("Nature", "CC0", True, 50), {"Limit": 12, "Category": 6, "License": 1, "Animated": 1})
        with self.assertRaises(tools.ToolError):
            polypizza.search_params(category="Trees")
        with self.assertRaises(tools.ToolError):
            polypizza.search_params(licence="MIT")

    def test_rows_and_credit(self):
        self.assertEqual(polypizza.describe_model(1, MODEL), "1. abc123: Low Poly Tree; 420 tris; CC-BY; by quaternius; Nature")
        self.assertIn("credit required. Attribution: Low Poly Tree by quaternius", polypizza.credit_line(MODEL))
        cc0 = polypizza.credit_line({**MODEL, "Licence": "CC0 1.0"})
        self.assertTrue(cc0.startswith("From Poly Pizza (https://poly.pizza/m/abc123), CC0 1.0: no credit required"), cc0)
        self.assertTrue(polypizza.credit_line(MODEL).startswith("From Poly Pizza (https://poly.pizza/m/abc123), CC-BY: credit required"))
        self.assertTrue(polypizza.is_glb(b"glTF\x02\x00"))
        self.assertFalse(polypizza.is_glb(b"<!DOCTYPE"))

    def test_key_comes_from_env_then_credentials_and_its_absence_is_explained(self):
        self.assertEqual(polypizza.api_key(), "")
        result = tools.execute("search_polypizza", json.dumps({"query": "tree"}))
        self.assertFalse(result.ok)
        self.assertIn("poly.pizza", result.text)
        self.assertIn("preferences", result.text)
        credentials.store(polypizza.KEY_ID, "stored-key")
        self.assertEqual(polypizza.api_key(), "stored-key")
        os.environ[polypizza.ENV_KEY] = "env-key"
        try:
            self.assertEqual(polypizza.api_key(), "env-key")
        finally:
            del os.environ[polypizza.ENV_KEY]

    def test_labels_gate_and_bad_id(self):
        self.assertIn("import_polypizza", tools.CHANGES_SCENE)
        self.assertEqual(polypizza.describe("import_polypizza", {"model_id": "abc"}), ("Import Poly Pizza model abc", ""))
        result = tools.execute("import_polypizza", json.dumps({"model_id": "../x"}))
        self.assertFalse(result.ok)
        self.assertIn("letters, digits", result.text)


if __name__ == "__main__":
    unittest.main()
