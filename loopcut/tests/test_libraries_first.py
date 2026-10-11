"""C34, libraries first: what the agent is told about libraries, and what it is shown to choose and check
an asset with (asset_common's row text, measured size and kit notes). No network or Blender."""

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import agent, asset_common, tools, workspace  # noqa: E402


def box(low, high):
    return {"min": list(low), "max": list(high)}


class RowTextTest(unittest.TestCase):
    def test_clean_keeps_one_short_line(self):
        self.assertEqual(asset_common.clean("  Oak\n\tstool\x07 , worn  "), "Oak stool , worn")
        self.assertEqual(asset_common.clean("word " * 40, 20), "word word word word…")
        for value in (None, 3, ["a"], "", " \n "):
            self.assertEqual(asset_common.clean(value), "")

    def test_size_text_only_for_two_or_three_real_sizes(self):
        self.assertEqual(asset_common.size_text([0.5082, 0.5558, 0.9366]), "0.51x0.56x0.94 m")
        self.assertEqual(asset_common.size_text((2, 2)), "2.00x2.00 m")
        for bad in (None, [], [1], [1, 2, 3, 4], [1, 0, 1], [1, -1], [1, "2"], [True, 1], [1, float("nan"), 1],
                    [1, float("inf")]):
            self.assertEqual(asset_common.size_text(bad), "", bad)


class MeasuredTest(unittest.TestCase):
    def test_all_objects_together(self):
        line = asset_common.measured_line([box((-0.25, -0.25, 0), (0.25, 0.3, 0.45)), None,
                                           box((-0.2, -0.3, 0.4), (0.26, 0.2, 0.94)), "unavailable"])
        self.assertEqual(line, "Measured: 0.51x0.60x0.94 m (x, y, z), base at z 0.00.")
        self.assertEqual(asset_common.measured_line([None, "unavailable"]), "")
        self.assertEqual(asset_common.measured_line([]), "")

    def test_beside_the_listed_size_and_far_from_it(self):
        chair = [box((0, 0, 0), (0.5, 0.55, 0.94))]
        self.assertEqual(asset_common.measured_line(chair, [0.51, 0.56, 0.94]),
                         "Measured: 0.50x0.55x0.94 m (x, y, z), base at z 0.00. The library lists 0.51x0.56x0.94 m.")
        centimetres = asset_common.measured_line([box((0, 0, 0), (50, 55, 94))], [0.51, 0.56, 0.94])
        self.assertTrue(centimetres.endswith("It arrived at 100x that: check the units or the scale."), centimetres)
        tiny = asset_common.measured_line([box((0, 0, 0), (0.005, 0.005, 0.0094))], [0.51, 0.56, 0.94])
        self.assertIn("It arrived at 0.01x that", tiny)
        self.assertNotIn("lists", asset_common.measured_line(chair, None))
        self.assertNotIn("lists", asset_common.measured_line(chair, [0, 1, 1]))

    def test_measured_updates_the_view_layer_and_reads_world_bounds(self):
        updates = []
        context = types.SimpleNamespace(view_layer=types.SimpleNamespace(update=lambda: updates.append(1)))
        bounds = {"Seat": box((0, 0, 0.4), (0.5, 0.5, 0.45)), "Leg": box((0, 0, 0), (0.05, 0.05, 0.4)), "Empty": None}
        objects = [types.SimpleNamespace(name=name) for name in bounds]
        with patch.object(sys.modules["bpy"], "context", context, create=True), \
                patch.object(tools, "_world_bounds", side_effect=lambda obj: bounds[obj.name]):
            line = asset_common.measured(objects, [0.5, 0.5, 0.45])
        self.assertEqual(updates, [1], "children's matrix_world is stale until the view layer updates")
        self.assertEqual(line, "Measured: 0.50x0.50x0.45 m (x, y, z), base at z 0.00. The library lists 0.50x0.50x0.45 m.")


class KitTest(unittest.TestCase):
    def test_rows_sharing_a_file_in_one_library(self):
        notes = asset_common.kit_notes([("Mine", "kits/cafe.blend"), None, ("Mine", "lamp.blend"),
                                        ("Mine", "kits/cafe.blend"), ("Mine", "kits/cafe.blend")])
        self.assertEqual(notes, dict.fromkeys((0, 3, 4), "kit: cafe (rows 1, 4, 5)"))
        self.assertEqual(asset_common.kit_notes([]), {})


class SheetTest(unittest.TestCase):
    def test_fit_box_keeps_the_whole_image_centred(self):
        self.assertEqual(asset_common.fit_box(320, 160, (160, 160)), (160, 80, 0, 40), "a 2:1 HDRI, whole")
        self.assertEqual(asset_common.fit_box(100, 200, (160, 160)), (80, 160, 40, 0))
        self.assertEqual(asset_common.fit_box(512, 512, (160, 160)), (160, 160, 0, 0))
        self.assertEqual(asset_common.fit_box(10000, 1, (160, 160)), (160, 1, 0, 79))

    def test_search_sheets_fit_and_contact_sheets_still_crop(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("needs numpy (Blender's Python has it)")
        def pixels(path, height=None):  # A 2:1 image, red with a transparent right half, at `height`.
            height = height or 80
            tile = np.zeros((height, height * 2, 4), dtype=np.float32)
            tile[:, :height, 0] = tile[:, :height, 3] = 1.0
            return tile
        saved = {}
        with patch.object(asset_common, "_pixels", side_effect=pixels), \
                patch.object(asset_common, "_save_pixels", side_effect=lambda canvas, out: saved.update(canvas=canvas)):
            folder = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, folder, True)
            asset_common.sheet(["wide.png", None], "s.png", folder=folder, fit=True)
            fitted = saved["canvas"]
            asset_common.sheet(["wide.png"], "c.png", folder=folder)
            cropped = saved["canvas"]
        self.assertEqual(fitted.shape, (160, 4 * 160 + 3 * 4, 4))
        self.assertTrue(np.allclose(fitted[40:120, 0:80, 0], 1.0), "the image's left half, red")
        self.assertTrue(np.allclose(fitted[40:120, 80:160, :3], 0.15), "its transparent half shows the sheet's grey")
        self.assertTrue(np.allclose(fitted[:40, 0:160, :3], 0.15), "letterboxed above and below")
        self.assertTrue(np.allclose(fitted[..., 3], 1.0))
        self.assertTrue(np.allclose(cropped[:, 0:160, 0], 1.0), "unchanged: scaled to the height, cut to the width")


class PromptTest(unittest.TestCase):
    """Libraries first, with judgement: owner direction 2026-10-10. Both loops' prompts say it; the workspace
    one as what the environment and the user are, never as steps (its own test bans a workflow)."""

    def test_the_single_agent_is_told_libraries_first_and_when_not(self):
        text = agent.SYSTEM_PROMPT
        for words in ("Libraries first", "not the user's own work", "search_assets before building one",
                      "plain shape or flat colour needs no search", "thumbnail sheet", "description, size and polycount",
                      "matches the scene's existing look", "a mix that reads well is fine", "measured size",
                      "swap a wrong pick", "nothing fits or the user asks", "never replace what the user made",
                      "Say which library", "attribution line for CC-BY"):
            self.assertIn(words, text)

    def test_the_workspace_agent_is_told_what_the_user_prefers(self):
        text = workspace.PROMPT
        for words in ("search_assets and import_asset", "Loopcut's index", "thumbnail sheet",
                      "description, size and polycount", "the size it measures", "prefers a fitting library asset",
                      "a build when nothing fits or when they ask", "What they made or are modelling stays theirs",
                      "real-world size", "a mix that reads well is fine", "which library each came from"):
            self.assertIn(words, text)
        for prescribed in ("search_assets before", "Always", "must", "First,", "Then"):
            self.assertNotIn(prescribed, text.split("search_assets and import_asset bring")[1].split("\n- ")[0])


if __name__ == "__main__":
    unittest.main()
