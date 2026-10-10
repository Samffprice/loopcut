"""inspect_texture's pure half: reading an image the way an Image Texture node does (extension,
interpolation), the Mapping node, and the points a face is sampled at. The Blender half is
loopcut/harness/uv_check.py.

    python3 -m unittest discover -s loopcut/tests
"""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import texture_tools as T  # noqa: E402

RED, GREEN = (1.0, 0.0, 0.0, 1.0), (0.0, 1.0, 0.0, 1.0)


def two_texels(x, y):
    """A 2 x 1 image: red on the left, green on the right."""
    return RED if x == 0 else GREEN


class SampleTest(unittest.TestCase):
    def test_each_extension_reads_past_the_edge_its_own_way(self):
        self.assertEqual([T.texel_index(i, 4, "REPEAT") for i in (-1, 4, 5)], [3, 0, 1])
        self.assertEqual([T.texel_index(i, 4, "EXTEND") for i in (-1, 4)], [0, 3])
        self.assertEqual([T.texel_index(i, 4, "CLIP") for i in (-1, 2, 4)], [None, 2, None])
        self.assertEqual([T.texel_index(i, 4, "MIRROR") for i in (4, 5, 7, 8, -1)], [3, 2, 0, 0, 0])

    def test_closest_reads_the_texel_under_the_point_and_linear_blends_between_centres(self):
        self.assertEqual(T.sample(two_texels, 2, 1, 0.49, 0.5, "Closest"), RED)
        self.assertEqual(T.sample(two_texels, 2, 1, 0.51, 0.5, "Closest"), GREEN)
        self.assertEqual(T.sample(two_texels, 2, 1, 0.25, 0.5), RED)  # A texel centre.
        self.assertEqual(T.sample(two_texels, 2, 1, 0.5, 0.5), (0.5, 0.5, 0.0, 1.0))
        self.assertEqual(T.sample(two_texels, 2, 1, 1.25, 0.5, "Closest"), RED)  # Repeat: 1.25 is 0.25.
        self.assertEqual(T.sample(two_texels, 2, 1, 1.25, 0.5, "Closest", "CLIP"), (0.0, 0.0, 0.0, 0.0))
        clipped = T.sample(two_texels, 2, 1, 0.0, 0.5, "Linear", "CLIP")  # Half a texel of nothing beside it.
        self.assertEqual(clipped, (0.5, 0.0, 0.0, 0.5))


class MappingTest(unittest.TestCase):
    def test_point_moves_scales_and_rotates_and_texture_undoes_it(self):
        self.assertEqual(T.mapping((0.25, 0.5, 0.0), "POINT", (0.5, 0, 0), (0, 0, 0), (1, 1, 1)), (0.75, 0.5, 0.0))
        self.assertEqual(T.mapping((0.25, 0.5, 0.0), "POINT", (0, 0, 0), (0, 0, 0), (4, 2, 1)), (1.0, 1.0, 0.0))
        turned = T.mapping((1.0, 0.0, 0.0), "POINT", (0, 0, 0), (0, 0, math.pi / 2), (1, 1, 1))
        self.assertAlmostEqual(turned[0], 0.0)
        self.assertAlmostEqual(turned[1], 1.0)
        forward = T.mapping((0.3, 0.7, 0.0), "POINT", (0.1, 0.2, 0.0), (0.2, 0.1, 0.4), (2, 3, 1))
        back = T.mapping(forward, "TEXTURE", (0.1, 0.2, 0.0), (0.2, 0.1, 0.4), (2, 3, 1))
        for a, b in zip(back, (0.3, 0.7, 0.0)):
            self.assertAlmostEqual(a, b)
        self.assertEqual(T.mapping((0.25, 0.5, 0.0), "VECTOR", (9, 9, 9), (0, 0, 0), (2, 2, 2)), (0.5, 1.0, 0.0))


class FacePointsTest(unittest.TestCase):
    def test_a_square_face_is_sampled_around_its_centre_by_area(self):
        centre, points = T.face_points([(0.0, 0.0), (0.5, 0.0), (0.5, 0.5), (0.0, 0.5)])
        self.assertEqual(centre, (0.25, 0.25))
        self.assertEqual(len(points), 8)
        self.assertAlmostEqual(sum(w for _, _, w in points), 0.25)
        self.assertTrue(all(0 < u < 0.5 and 0 < v < 0.5 for u, v, _ in points))

    def test_a_face_with_no_uv_area_is_read_at_its_corners_mean(self):
        self.assertEqual(T.face_points([(0.2, 0.2)] * 4), ((0.2, 0.2), [(0.2, 0.2, 1.0)]))

    def test_the_weighted_mean_and_the_text(self):
        self.assertEqual(T.mean([(RED, 3.0), (GREEN, 1.0)]), (0.75, 0.25, 0.0, 1.0))
        self.assertEqual(T.rgba((0.5, 0.25, 0.0, 1.0)), "0.500 0.250 0.000 1.000")
        self.assertEqual(T.describe("inspect_texture", {"target": "Wood"}), ("Inspect the textures of Wood", ""))
        self.assertIsNone(T.describe("inspect_uv", {}))


if __name__ == "__main__":
    unittest.main()
