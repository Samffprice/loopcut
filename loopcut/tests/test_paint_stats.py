"""paint_stats: the advisory words and lines (pure Python), and the numpy half on synthetic images:
designed patches, the same under noise, value-matched accents, popping ones and accents in the
darks. Without numpy here (the system Python), the numpy half runs in Blender's bundled Python
(tools/Blender.app next to the repository, or LOOPCUT_BLENDER). The Blender half, and the
calibration blends, are loopcut/harness/paint_stats_check.py.

    python3 -m unittest discover -s loopcut/tests
"""

import glob
import importlib.util
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))

from loopcut import paint_stats as P  # noqa: E402

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
IN_CHILD = os.environ.get("LOOPCUT_PAINT_STATS_CHILD") == "1"


def stats(**more) -> dict:
    base = {"texels": 5000, "levels": [0.45, 0.58, 0.72], "ratio": 1.6, "patch": 40.0, "noise": 0.06,
            "noise_size": 0.012, "accents": 0.12, "hue": 80, "accent_dl": 0.03, "accent_chroma": 0.9,
            "accent_dark": 0.0}
    base.update(more)
    return base


class WordsTest(unittest.TestCase):
    def test_designed_patches_with_value_matched_accents_read_designed(self):
        self.assertEqual(P.verdict(stats()), [])
        self.assertEqual(P.text("Lid", stats(), 0.5),
                         "Lid: 3 values 1:1.6, patches ~20 px, noise 6%, accents 12% ΔL 0.03 C x0.9 - reads designed")

    def test_each_problem_has_its_words(self):
        self.assertEqual(P.verdict(stats(noise=0.4, noise_size=0.09)), ["noise-like"])
        self.assertEqual(P.verdict(stats(noise=0.4, noise_size=0.02)), [], "a quiet fine texture is not noise")
        self.assertEqual(P.verdict(stats(ratio=3.2)), ["values far apart"])
        self.assertEqual(P.verdict(stats(accent_dl=0.3)), ["accents pop"])
        self.assertEqual(P.verdict(stats(accent_chroma=2.4)), ["accents pop"], "too saturated pops at any value")
        self.assertEqual(P.verdict(stats(accent_dark=0.8)), ["accents in darks"])
        self.assertEqual(P.verdict(stats(accents=0.005, accent_dl=0.4)), [], "a few stray texels are not judged")
        self.assertEqual(P.verdict(stats(ratio=1.15, accents=0.0)), ["restrained"])
        self.assertEqual(P.verdict(stats(ratio=1.15, accents=0.0, noise=0.5, noise_size=0.06)), ["noise-like"])

    def test_the_line_says_texels_without_a_camera_and_leaves_out_unjudged_accents(self):
        line = P.text("PotionBottleOne_Cork", stats(ratio=1.1, accents=0.0, levels=[0.6]))
        self.assertEqual(line, "PotionBottleOne_Cork: 1 value 1:1.1, patches ~40 texels, noise 6% - restrained")
        self.assertLessEqual(len(line) / 3.5, 40, "about 40 tokens a part at most")

    def test_summary_counts_the_words_and_names_the_worst_parts(self):
        parts = [dict(stats(ratio=3.0, noise=0.44, noise_size=0.1, accent_dl=0.37), part="Sign_Amber"),
                 dict(stats(ratio=1.1, accents=0.0), part="Sign_Ink"),
                 dict(stats(), part="Sign_Cream")]
        self.assertEqual(P.text_of("Sign", parts),
                         "Sign paint (3 parts, advisory): noise-like 1, values far apart 1, accents pop 1, "
                         "restrained 1, "
                         "reads designed 1; Sign_Amber 1:3.0 noise 44% ΔL 0.37 C x0.9")
        self.assertEqual(P.text_of("Sign", {"error": "ValueError: x"}), "Sign paint: not measured (ValueError: x)")
        self.assertEqual(P.text_of("Sign", []), "")
        self.assertEqual(P.text_of("Sign", None), "")

    def test_the_worker_reads_its_problem_parts_whole_and_the_rest_by_name(self):
        parts = [dict(stats(ratio=2.5), part=f"Bad{n}") for n in range(8)]
        parts += [dict(stats(ratio=1.1, accents=0.0), part="Calm"), dict(stats(), part="Good")]
        lines = P.brief_lines(parts, most=6)
        self.assertEqual(lines[0], "Paint stats (measured, advisory: never blocks report_back):")
        self.assertEqual(len([line for line in lines if line.startswith("- Bad")]), 6)
        self.assertIn("- +2 more reading a problem: inspect_texture on the object lists every part", lines)
        self.assertIn("- restrained (lightest:darkest under 1:1.35, no accents): Calm", lines)
        self.assertEqual(lines[-1], "- reads designed: Good")
        self.assertEqual(P.brief_lines([]), [])

    def test_only_a_base_colour_image_on_a_uv_map_is_paint(self):
        image = object()
        roughness = {"image": image, "uv_map": "", "paths": ["Color -> Principled BSDF.Roughness"]}
        glow = {"image": image, "uv_map": "", "paths": ["Color -> Principled BSDF.Emission Color"]}
        generated = {"image": image, "uv_map": None, "paths": ["Color -> Principled BSDF.Base Color"]}
        colour = {"image": image, "uv_map": "UVMap", "paths": ["Color -> Mix.A -> Principled BSDF.Base Color"]}
        self.assertIsNone(P.base_colour([roughness, glow, generated]))
        self.assertIs(P.base_colour([roughness, colour]), colour)


# ------------------------------------------------------------------ the numpy half

def _lab(L, hue_deg=80.0, chroma=0.08):
    import numpy as np
    L = np.asarray(L, dtype=np.float32)
    h = np.broadcast_to(np.radians(np.asarray(hue_deg, dtype=np.float32)), L.shape)
    c = np.broadcast_to(np.asarray(chroma, dtype=np.float32), L.shape)
    return np.stack([L, c * np.cos(h), c * np.sin(h)], axis=-1).astype(np.float32)


def _patches(size=256):
    """Three values in big hard-edged patches: 0.45, 0.58 and 0.72."""
    import numpy as np
    y, x = np.mgrid[:size, :size]
    L = np.full((size, size), 0.58, dtype=np.float32)
    L[(x + 0.6 * y) < size * 0.5] = 0.45
    L[(x - 0.4 * y) > size * 0.55] = 0.72
    return L


def _blobs(size=256, step=40, width=12):
    """Squares of width texels every step texels: about 9% of the image."""
    import numpy as np
    y, x = np.mgrid[:size, :size]
    return ((x % step) < width) & ((y % step) < width)


@unittest.skipUnless(HAS_NUMPY, "numpy half: run in Blender's Python by NumpyInBlendersPythonTest")
class MeasureTest(unittest.TestCase):
    def test_designed_patches(self):
        import numpy as np
        found = P.measure(_lab(_patches()), np.ones((256, 256), dtype=bool))
        self.assertEqual(len(found["levels"]), 3)
        self.assertAlmostEqual(found["ratio"], 0.72 / 0.45, delta=0.02)
        self.assertGreater(found["patch"], 60)
        self.assertLess(found["noise"], 0.1)
        self.assertEqual(found["accents"], 0.0)
        self.assertEqual(P.verdict(found), [])

    def test_the_same_patches_under_noise_read_noise_like(self):
        import numpy as np
        noisy = _patches() + np.random.default_rng(1).normal(0, 0.1, (256, 256)).astype(np.float32)
        found = P.measure(_lab(noisy), np.ones((256, 256), dtype=bool))
        self.assertGreater(found["noise"], 0.5)
        self.assertGreater(found["noise_size"], 0.08)
        self.assertLess(found["patch"], 8)
        self.assertIn("noise-like", P.verdict(found))

    def test_value_matched_accents_do_not_pop_and_lighter_saturated_ones_do(self):
        import numpy as np
        L, blobs = _patches(), _blobs()
        matched = _lab(L, np.where(blobs, 350.0, 80.0))  # Pink on brass at the same lightness and chroma.
        found = P.measure(matched, np.ones((256, 256), dtype=bool))
        self.assertAlmostEqual(found["accents"], blobs.mean(), delta=0.01)
        self.assertLess(found["accent_dl"], 0.02)
        self.assertAlmostEqual(found["accent_chroma"], 1.0, delta=0.05)
        self.assertEqual(P.verdict(found), [])
        popping = _lab(np.where(blobs, L + 0.25, L), np.where(blobs, 290.0, 80.0), np.where(blobs, 0.2, 0.08))
        found = P.measure(popping, np.ones((256, 256), dtype=bool))
        self.assertGreater(found["accent_dl"], 0.2)
        self.assertGreater(found["accent_chroma"], 2)
        self.assertIn("accents pop", P.verdict(found))

    def test_accents_on_a_dark_part_are_in_the_darks(self):
        import numpy as np
        blobs = _blobs()
        dark = _lab(np.full((256, 256), 0.25, dtype=np.float32), np.where(blobs, 200.0, 280.0))
        found = P.measure(dark, np.ones((256, 256), dtype=bool))
        self.assertEqual(found["accent_dark"], 1.0)
        self.assertIn("accents in darks", P.verdict(found))

    def test_only_the_masked_texels_count(self):
        import numpy as np
        L = _patches()
        L[:, :128] = np.random.default_rng(2).uniform(0.1, 0.9, (256, 128))
        right = np.zeros((256, 256), dtype=bool)
        right[:, 128:] = True
        self.assertIn("noise-like", P.verdict(P.measure(_lab(L), ~right)))
        self.assertNotIn("noise-like", P.verdict(P.measure(_lab(L), right)))
        self.assertIsNone(P.measure(_lab(L), np.zeros((256, 256), dtype=bool)))

    def test_levels_merge_what_the_eye_cannot_tell_apart(self):
        import numpy as np
        values = np.concatenate([np.full(1000, 0.50), np.full(1000, 0.52), np.full(1000, 0.80)])
        self.assertEqual([round(v, 2) for v in P.levels(values)], [0.51, 0.8])
        self.assertEqual([round(v, 6) for v in P.levels(np.full(100, 0.4))], [0.4])

    def test_a_patch_is_the_run_a_texel_sits_in(self):
        import numpy as np
        y, x = np.mgrid[:64, :64]
        checker = ((x // 16 + y // 16) % 2).astype(np.int64)
        self.assertEqual(P.run_length(checker, np.ones((64, 64), dtype=bool)), 16.0)
        inside = np.zeros((64, 64), dtype=bool)
        inside[:, :8] = True  # The mask ends a run too.
        self.assertEqual(P.run_length(checker, inside), (8 * 8 * 64 + 16 * 16 * 32) / (8 * 64 * 2))

    def test_blur_weighs_only_the_mask(self):
        import numpy as np
        values = np.zeros((64, 64), dtype=np.float32)
        values[:, 32:] = 1.0
        inside = np.zeros((64, 64), dtype=bool)
        inside[:, 32:] = True
        smooth, reach = P.blur(values, inside.astype(np.float32), 3.0)
        self.assertTrue(np.allclose(smooth[:, 32:], 1.0, atol=1e-5), "nothing from outside the mask bleeds in")
        self.assertGreater(float(reach[32, 48]), 0.99)
        self.assertLess(float(reach[32, 4]), 0.01)

    def test_raster_covers_a_triangle_and_repeats_past_the_tile(self):
        import numpy as np
        mask = P.raster(np.array([[[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]]), 64, 64)
        self.assertAlmostEqual(mask.mean(), 0.5, delta=0.03)
        self.assertTrue(mask[1, 1] and not mask[62, 62])
        shifted = P.raster(np.array([[[1.0, 0.0], [2.0, 0.0], [1.0, 1.0]]]), 64, 64)
        self.assertEqual((mask != shifted).mean(), 0.0)

    def test_colour_conversions(self):
        import numpy as np
        white = P.oklab(P.linear(np.ones((1, 1, 3))))[0, 0]
        self.assertAlmostEqual(float(white[0]), 1.0, places=3)
        self.assertLess(abs(float(white[1])) + abs(float(white[2])), 1e-3)
        grey = P.oklab(P.linear(np.full((1, 1, 3), 0.5)))[0, 0]
        self.assertAlmostEqual(float(grey[0]), 0.5981, places=3)
        self.assertEqual(P.shrink(np.arange(16.0).reshape(4, 4, 1), 2)[..., 0].tolist(), [[2.5, 4.5], [10.5, 12.5]])

    def test_an_image_past_max_texels_is_measured_on_a_box_averaged_copy(self):
        """5-8K atlases get stats: read through a file-backed buffer, averaged by a whole stride, and
        the analysis step handed back in the image's own texels (so px sizes stay the original's)."""
        import numpy as np
        from loopcut import texture_tools as T

        class Image:
            name, size, source, is_float = "Big", (64, 48), "GENERATED", False
            colorspace_settings = types.SimpleNamespace(name="sRGB")

            def __init__(self, size=(64, 48)):
                self.size = size
                self.data = np.random.default_rng(4).random(size[0] * size[1] * 4).astype(np.float32)
                fill = lambda buffer: buffer.__setitem__(slice(None), self.data)  # noqa: E731
                self.pixels = types.SimpleNamespace(foreach_get=fill)

        saved = T.MAX_TEXELS, T.MAX_REDUCED, T.REDUCED
        # 64 x 48 = 3072 texels: past MAX_TEXELS, within MAX_REDUCED.
        T.MAX_TEXELS, T.MAX_REDUCED, T.REDUCED = 1000, 4000, 16
        try:
            pixels, image = T._Pixels(), Image()
            self.assertIsNone(pixels.of(image), "samples and the hash still leave it out")
            small, stride = pixels.reduced(image)
            self.assertEqual((stride, small.shape), (4, (12, 16, 4)))
            np.testing.assert_allclose(small, image.data.reshape(12, 4, 16, 4, 4).mean(axis=(1, 3)), atol=1e-6)
            lab, step = P.Lab(pixels).of(image, 6)
            self.assertEqual((step, lab.shape), (8, (6, 8, 3)), "a whole number of strides, at least the step asked")
            self.assertIsNone(T._Pixels().reduced(Image((100, 100))), "past MAX_REDUCED: not read")
            self.assertIsNone(P.Lab(T._Pixels()).of(Image((100, 100)), 1))
        finally:
            T.MAX_TEXELS, T.MAX_REDUCED, T.REDUCED = saved

    def test_a_2k_part_measures_in_well_under_a_second(self):
        import time
        import numpy as np
        L = _patches(1024) + np.random.default_rng(3).normal(0, 0.03, (1024, 1024)).astype(np.float32)
        lab = _lab(L, np.where(_blobs(1024), 350.0, 80.0))
        started = time.perf_counter()
        P.measure(lab, np.ones((1024, 1024), dtype=bool), 2.0)
        self.assertLess(time.perf_counter() - started, 1.5)


def _bundled_python() -> str | None:
    """Blender's own Python (it has numpy) beside LOOPCUT_BLENDER or tools/Blender.app next to the repo."""
    blender = os.environ.get("LOOPCUT_BLENDER") or str(REPO.parent / "tools/Blender.app/Contents/MacOS/Blender")
    root = Path(blender).resolve().parent
    found = sorted(glob.glob(str(root.parent / "Resources" / "*" / "python" / "bin" / "python3*")))
    found += sorted(glob.glob(str(root / "*" / "python" / "bin" / "python3*")))  # Linux and Windows layouts.
    return found[0] if found else None


@unittest.skipIf(HAS_NUMPY or IN_CHILD, "numpy is here: MeasureTest ran in this Python")
class NumpyInBlendersPythonTest(unittest.TestCase):
    def test_the_numpy_half_passes_in_blenders_python(self):
        python = _bundled_python()
        if python is None:
            self.skipTest("no numpy here and no Blender Python found (set LOOPCUT_BLENDER)")
        result = subprocess.run([python, "-m", "unittest", "-q", "test_paint_stats.MeasureTest"],
                                cwd=Path(__file__).parent,
                                capture_output=True, text=True, env={**os.environ, "LOOPCUT_PAINT_STATS_CHILD": "1"},
                                timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertRegex(result.stderr, r"Ran \d+ tests")
        self.assertNotIn("skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
