"""bake_paint_masks and stylize_material, the pure half: argument checks, the key, the budget, the scale
facts and the tool card; where the tools are offered and how a worker's child runs them. The Blender
half (both tools on a small UV'd object, Cycles and EEVEE, glTF) is loopcut/harness/paint_tools_check.py.

    python3 -m unittest discover -s loopcut/tests
"""

import glob
import importlib.util
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import paint_tools as P  # noqa: E402
from loopcut import tools, worker_host, worker_ipc, workers  # noqa: E402

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
IN_CHILD = os.environ.get("LOOPCUT_PAINT_TOOLS_CHILD") == "1"


class KeyTest(unittest.TestCase):
    def test_a_direction_is_a_unit_vector_toward_the_light(self):
        self.assertEqual(P.direction([0, 0, 2]), (0.0, 0.0, 1.0))
        for text in ("0, 0, 3", "[0 0 3]", "(0,0,3)"):
            self.assertEqual(P.direction(text), (0.0, 0.0, 1.0), text)
        x, y, z = P.direction([-1, -1, 1])
        self.assertAlmostEqual(x * x + y * y + z * z, 1.0)
        with self.assertRaises(tools.ToolError):
            P.direction([0, 0, 0])

    def test_a_lights_name_is_not_a_direction(self):
        for name in ("Key", "Key.001", "Light 1 2 3", "Rim2", [1, 2], [1, "a", 3], None, True):
            self.assertIsNone(P.direction(name), name)

    def test_the_key_is_named_for_it_else_the_brightest(self):
        lights = [("Fill", "AREA", 30.0), ("Rim", "AREA", 400.0), ("KeyLight", "SPOT", 50.0)]
        self.assertEqual(P.pick_key(lights), "KeyLight")
        self.assertEqual(P.pick_key(lights[:2]), "Rim")
        self.assertEqual(P.pick_key([("Lamp", "POINT", 200.0), ("Sun", "SUN", 4.0)]), "Sun", "a sun's 4 is bright")
        self.assertIsNone(P.pick_key([]))


class ArgumentTest(unittest.TestCase):
    def test_paint_may_be_a_flat_srgb_colour(self):
        r, g, b = P.colour("#ff8000")
        self.assertEqual((r, b), (1.0, 0.0))
        self.assertAlmostEqual(g, 0.2158605, places=5)  # sRGB 128 decoded
        for name in ("Chest_Lid_paint", "ff8000", "#ff80", None, 3):
            self.assertIsNone(P.colour(name), name)

    def test_resolution_is_a_size_in_range(self):
        self.assertEqual(P.resolution_of(1024), 1024)
        self.assertEqual(P.resolution_of(2048.0), 2048)
        for bad in (64, 8192, 1024.5, "1024", True):
            with self.assertRaises(tools.ToolError, msg=bad):
                P.resolution_of(bad)

    def test_a_call_bakes_a_bounded_number_of_texels(self):
        P.check_budget(16, 1024)
        P.check_budget(1, 4096)
        with self.assertRaisesRegex(tools.ToolError, "at most 16 a call"):
            P.check_budget(17, 256)
        with self.assertRaisesRegex(tools.ToolError, "4 at a time, or a smaller resolution"):
            P.check_budget(5, 2048)

    def test_the_mask_is_baked_smaller_past_its_budget(self):
        self.assertEqual(P.mask_step(1, 1024), 1)
        self.assertEqual(P.mask_step(2, 1024), 1)
        self.assertEqual(P.mask_step(7, 1024), 2, "the calibration chest")
        self.assertEqual(P.mask_step(16, 1024), 4)
        self.assertEqual(P.mask_step(1, 4096), 4)
        self.assertEqual(P.mask_step(16, 256), 1, "never under 128 px a side")
        self.assertEqual(P.mask_step(16, 512), 2)

    def test_stylize_values_default_and_range(self):
        values = P.values_of()
        self.assertEqual({k: values[k] for k in P.DEFAULTS}, P.DEFAULTS)
        self.assertEqual(values["rim"], P.RIM)
        short = P.values_of(step=[0.1])
        self.assertEqual((short["terminator"], short["softness"], short["shadow"]), (0.1, 0.3, 0.72))
        self.assertEqual(P.values_of(rim=[])["rim"], ())
        wide = P.values_of(rim=[0.2, 0.3, 0.4, 0.25])
        self.assertEqual((wide["rim"], wide["rim_width"]), ((0.2, 0.3, 0.4), 0.25))
        self.assertEqual(P.values_of(unlit=0, ao=1)["unlit"], 0.0)
        for bad, words in (({"unlit": 1.5}, "unlit: 0 to 1"), ({"step": [0, 0]}, "step's softness"),
                           ({"step": [0, 0.3, 0.7, 1]}, "up to 3"), ({"rim": [1, 1]}, r"\[r, g, b\]"),
                           ({"rim": [-1, 0, 0]}, "0 or more"), ({"rim": [1, 1, 1, 0.9]}, "rim's rim_width"),
                           ({"ao": "strong"}, "ao: a number"), ({"step": "soft"}, "a list")):
            with self.assertRaisesRegex(tools.ToolError, words, msg=bad):
                P.values_of(**bad)


class ScaleTest(unittest.TestCase):
    def test_screen_px_a_metre(self):
        # 960 x 640, 50 mm on a 36 mm sensor: 1333 px a metre at 1 m; AUTO fits the longer side.
        self.assertAlmostEqual(P.px_per_metre(960, 640, 50, 36, 24, "AUTO", 1.0), 960 * 50 / 36)
        self.assertAlmostEqual(P.px_per_metre(640, 960, 50, 36, 24, "AUTO", 2.0), 960 * 50 / 72)
        self.assertAlmostEqual(P.px_per_metre(960, 640, 50, 36, 24, "VERTICAL", 1.0), 640 * 50 / 24)
        self.assertAlmostEqual(P.px_per_metre(960, 640, 50, 36, 24, "HORIZONTAL", 1.0, ortho_scale=4), 240)
        self.assertIsNone(P.px_per_metre(960, 640, 50, 36, 24, "AUTO", -1.0), "behind the camera")

    def test_a_parts_scale_facts(self):
        line = P.texel_line(0.0016, 0.69, 500.0, "Cam")
        self.assertIn("69 % of the image, texel 1.6 mm", line)
        self.assertIn("Cam: 1.2 texels a screen px, a 6-12 px brush is 12-24 mm, 8-15 texels", line)
        self.assertEqual(P.texel_line(0.001, 0.5, None, ""), "50 % of the image, texel 1 mm")

    def test_defaults_follow_the_object(self):
        self.assertEqual(P.edge_default(0.8), 0.015)
        self.assertAlmostEqual(P.edge_default(0.13), 0.0052)
        self.assertEqual(P.edge_default(0.8, bevel_width=0.03), 0.06, "2x the Bevel modifier's width")
        self.assertEqual(P.ao_default(0.8), 0.15)
        self.assertAlmostEqual(P.ao_default(0.13), 0.039)

    def test_images_are_named_for_the_part(self):
        self.assertEqual(P.part_label("Lid", 0, 1), "Lid")
        self.assertEqual(P.part_label("Lid", 2, 3), "Lid_2")


@unittest.skipUnless(HAS_NUMPY, "numpy half: run in Blender's Python by NumpyInBlendersPythonTest")
class UpsampleTest(unittest.TestCase):
    def test_bilinear_between_texel_centres(self):
        import numpy as np
        small = np.array([[[0.0], [1.0]], [[2.0], [3.0]]], np.float32)
        big = P.upsample(small, 2)
        self.assertEqual(big.shape, (4, 4, 1))
        self.assertEqual(big[0, 0, 0], 0.0, "the border is held")
        self.assertEqual(big[3, 3, 0], 3.0)
        self.assertAlmostEqual(float(big[1, 1, 0]), 0.75)  # a quarter of the way to each neighbour
        self.assertAlmostEqual(float(big.mean()), float(small.mean()))
        self.assertIs(P.upsample(small, 1), small)


class SurfaceTest(unittest.TestCase):
    def test_workers_alone_are_offered_them_and_their_child_runs_them(self):
        from loopcut import agent
        names = [s["function"]["name"] for s in workers.schemas()]
        for name in P.DISPATCH:
            self.assertIn(name, names)
            self.assertIn(name, worker_host.CHILD_TOOLS)
            self.assertIn(name, tools.CHANGES_SCENE)
            self.assertNotIn(name, {s["function"]["name"] for s in agent._tool_schemas()}, "workers do the work")
        self.assertTrue(tools.is_heavy("bake_paint_masks", {}))
        self.assertEqual(worker_ipc.timeout_for("tool", "bake_paint_masks"), worker_ipc.TIMEOUTS["bake"])
        child = (REPO / "scripts/addons_core/loopcut/worker_child.py").read_text()
        guarded = child[child.index("guarded = name in"):child.index("mark = self._before_call()")]
        for name in P.DISPATCH:
            self.assertIn(f'"{name}"', guarded, "rolled back like run_python when it touches what is not owned")

    def test_the_schemas_match_the_functions(self):
        import inspect
        for schema in P.SCHEMAS:
            function = schema["function"]
            parameters = inspect.signature(P.DISPATCH[function["name"]]).parameters
            self.assertEqual(set(function["parameters"]["properties"]), set(parameters), function["name"])
            for name in function["parameters"]["required"]:
                self.assertIs(parameters[name].default, inspect.Parameter.empty, name)

    def test_the_registry_routes_each_call(self):
        found = tools.ToolResult("found")
        with mock.patch.object(P, "stylize_material", autospec=True, return_value=found) as target, \
                mock.patch.dict(P.DISPATCH, {"stylize_material": P.stylize_material}):
            result = tools.execute("stylize_material", json.dumps({"object": "Lid", "paint": "Lid_paint",
                                                                     "step": [0, 0.3, 0.72]}))
        self.assertEqual(result.text, "found")
        target.assert_called_once_with(object="Lid", paint="Lid_paint", step=[0, 0.3, 0.72])
        refused = tools.execute("bake_paint_masks", json.dumps({"objects": ["A"], "camera": "Cam"}))
        self.assertFalse(refused.ok)
        self.assertIn("Bad arguments", refused.text)

    def test_the_tool_card(self):
        self.assertEqual(P.describe("bake_paint_masks", {"objects": ["A", "B", "C", "D"]})[0],
                         "Bake paint masks for A, B, C, +1 more")
        self.assertEqual(P.describe("stylize_material", {"object": "Lid", "slot": 1})[0],
                         "Stylise the material of Lid (slot 1)")
        self.assertIsNone(P.describe("run_python", {}))


def _bundled_python() -> str | None:
    blender = os.environ.get("LOOPCUT_BLENDER") or str(REPO.parent / "tools/Blender.app/Contents/MacOS/Blender")
    root = Path(blender).resolve().parent
    found = sorted(glob.glob(str(root.parent / "Resources" / "*" / "python" / "bin" / "python3*")))
    found += sorted(glob.glob(str(root / "*" / "python" / "bin" / "python3*")))
    return found[0] if found else None


@unittest.skipIf(HAS_NUMPY or IN_CHILD, "numpy is here: UpsampleTest ran in this Python")
class NumpyInBlendersPythonTest(unittest.TestCase):
    def test_the_numpy_half_passes_in_blenders_python(self):
        python = _bundled_python()
        if python is None:
            self.skipTest("no numpy here and no Blender Python found (set LOOPCUT_BLENDER)")
        result = subprocess.run([python, "-m", "unittest", "-q", "test_paint_tools.UpsampleTest"],
                                cwd=Path(__file__).parent, capture_output=True, text=True,
                                env={**os.environ, "LOOPCUT_PAINT_TOOLS_CHILD": "1"}, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertRegex(result.stderr, r"Ran 1 test\b")
        self.assertNotIn("skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
