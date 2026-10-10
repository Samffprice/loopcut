"""inspect_uv's and uv_unwrap's pure half: the per-triangle map, islands, the overlap raster, gutters,
the audit's numbers, the atlas planner and the box, cylinder and sphere projections. The Blender half
is loopcut/harness/uv_check.py.

    python3 -m unittest discover -s loopcut/tests
"""

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import uv_tools as U  # noqa: E402


def quad(x0, y0, size=1.0):
    """A flat square in 3D, wound counter-clockwise seen from +Z."""
    return [(x0, y0, 0.0), (x0 + size, y0, 0.0), (x0 + size, y0 + size, 0.0), (x0, y0 + size, 0.0)]


def square(u0, v0, size):
    return [(u0, v0), (u0 + size, v0), (u0 + size, v0 + size), (u0, v0 + size)]


def loose(*layouts, size=1.0):
    """(co, faces, uvs) of one loose 3D square per UV layout, each its own island."""
    co, faces, uvs = [], [], []
    for n, uv in enumerate(layouts):
        faces.append([len(co) + k for k in range(4)])
        co += quad(3.0 * n, 0.0, size)
        uvs.append(uv)
    return co, faces, uvs


def signed_area(uv) -> float:
    return sum(uv[k][0] * uv[(k + 1) % len(uv)][1] - uv[(k + 1) % len(uv)][0] * uv[k][1] for k in range(len(uv))) / 2


class TriangleTest(unittest.TestCase):
    def test_a_uniform_scale_has_equal_singular_values_and_its_area(self):
        area3, uv_area, high, low = U.triangle((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0), (0.5, 0), (0, 0.5))
        self.assertAlmostEqual(area3, 0.5)
        self.assertAlmostEqual(uv_area, 0.125)
        self.assertAlmostEqual(high, 0.5)
        self.assertAlmostEqual(low, 0.5)

    def test_a_shear_stretches_by_the_golden_ratio_squared(self):
        """(x, y) -> (x + y, y): singular values 1.618 and 0.618, whatever the triangle's place in space."""
        _, _, high, low = U.triangle((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0), (1, 0), (1, 1))
        self.assertAlmostEqual(high / low, (3 + math.sqrt(5)) / 2)
        # The same triangle stood up in the XZ plane and moved: the same map.
        _, _, high2, low2 = U.triangle((5, 2, 1), (6, 2, 1), (5, 2, 2), (0, 0), (1, 0), (1, 1))
        self.assertAlmostEqual(high2, high)
        self.assertAlmostEqual(low2, low)

    def test_mirrored_uvs_have_negative_area_and_a_line_has_no_small_value(self):
        self.assertLess(U.triangle((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0), (0, 0.5), (0.5, 0))[1], 0)
        _, uv_area, high, low = U.triangle((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0), (0.5, 0), (1, 0))
        self.assertEqual(uv_area, 0)
        self.assertGreater(high, 0)
        self.assertAlmostEqual(low, 0)

    def test_a_triangle_without_3d_area_is_left_out(self):
        self.assertEqual(U.triangle((0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 0), (1, 0), (0, 1))[0], 0.0)


class IslandsTest(unittest.TestCase):
    def test_faces_sharing_uv_corners_are_one_island_and_a_seam_splits_them(self):
        faces = [[0, 1, 2, 3], [1, 4, 5, 2]]
        joined = [square(0, 0, 0.2), [(0.2, 0), (0.4, 0), (0.4, 0.2), (0.2, 0.2)]]
        self.assertEqual(U.islands(faces, joined), ([0, 0], 1))
        cut = [square(0, 0, 0.2), [(0.5, 0), (0.7, 0), (0.7, 0.2), (0.5, 0.2)]]
        self.assertEqual(U.islands(faces, cut), ([0, 1], 2))


class RasterTest(unittest.TestCase):
    def test_a_triangle_covers_the_cells_whose_centres_it_holds(self):
        cells = set(U.cover((0, 0), (1, 0), (0, 1), grid=8))
        self.assertEqual(len(cells), 36)  # Centres with i + j <= 7 on an 8 x 8 grid, the diagonal included.
        self.assertEqual(set(U.cover((1.2, 0), (1.9, 0), (1.2, 0.5), grid=8)), set())

    def test_stacked_islands_overlap_and_a_folded_one_is_found(self):
        tris = lambda u, v, s: [((u, v), (u + s, v), (u + s, v + s)), ((u, v), (u + s, v + s), (u, v + s))]  # noqa: E731
        result = U.overlap({0: tris(0.1, 0.1, 0.5), 1: tris(0.3, 0.3, 0.5), 2: tris(0.0, 0.8, 0.1)}, grid=64)
        self.assertEqual(result["pairs"], [(0, 1)])
        self.assertAlmostEqual(result["shared"] / 64 ** 2, 0.3 * 0.3, delta=0.01)
        self.assertEqual(result["folded"], [])
        folded = U.overlap({0: tris(0.1, 0.1, 0.5) + tris(0.1, 0.1, 0.5)}, grid=64)  # The same area twice.
        self.assertEqual(folded["folded"], [0])


class GutterTest(unittest.TestCase):
    def test_segment_distance(self):
        self.assertAlmostEqual(U.segment_distance((0, 0), (1, 0), (0, 0.3), (1, 0.3)), 0.3)
        self.assertEqual(U.segment_distance((0, 0), (1, 1), (0, 1), (1, 0)), 0.0)  # They cross.
        self.assertEqual(U.segment_distance((0, 0), (1, 0), (1, 0), (2, 1)), 0.0)  # They touch.
        self.assertAlmostEqual(U.segment_distance((0, 0), (1, 0), (2, 1), (3, 1)), math.sqrt(2))

    def test_the_closest_outlines_of_two_islands_within_the_limit(self):
        co, faces, uvs = loose(square(0.1, 0.1, 0.2), square(0.31, 0.1, 0.2), square(0.8, 0.8, 0.1))
        labels, _ = U.islands(faces, uvs)
        segments, length = U.boundary(co, faces, uvs, labels)
        self.assertEqual(len(segments), 12)
        self.assertAlmostEqual(length, 12.0)  # Three 1 m squares' outlines.
        closest = U.gutters(segments, 0.05)
        self.assertEqual(list(closest), [(0, 1)])
        self.assertAlmostEqual(closest[(0, 1)], 0.01, places=6)
        self.assertEqual(U.gutters(segments, 0.005), {})


class AuditTest(unittest.TestCase):
    def test_two_clean_squares_measure_their_density_gutter_and_border(self):
        gap = 8 / 1024
        co, faces, uvs = loose(square(4 / 1024, 4 / 1024, 0.25), square(4 / 1024 + 0.25 + gap, 4 / 1024, 0.25))
        s = U.audit(co, faces, uvs, resolution=1024, padding=8)
        self.assertEqual((s["faces"], s["islands"]), (2, 2))
        self.assertEqual((s["outside"], s["zero"], s["flipped"], s["overlapping"], s["folded"]), ([], [], [], [], []))
        self.assertAlmostEqual(s["density"][1], 256.0, places=3)  # A quarter of 1024 px over 1 m.
        self.assertAlmostEqual(s["angle"], 1.0)
        self.assertEqual((s["angle_share"], s["area_share"]), (0, 0))
        self.assertAlmostEqual(s["gutter"][0], 8.0, places=3)
        self.assertEqual(s["narrow"], 0)
        self.assertAlmostEqual(s["border"], 4.0, places=3)
        self.assertAlmostEqual(s["used"], 0.125, delta=0.01)
        self.assertAlmostEqual((s["area"], s["boundary"]), (2.0, 8.0))

    def test_stretch_off_density_outside_flipped_and_zero_area_faces(self):
        stretched = [(0.0, 0.0), (0.5, 0.0), (0.5, 0.125), (0.0, 0.125)]  # 2:1 on a square.
        flipped = [(0.6, 0.6), (0.6, 0.8), (0.8, 0.8), (0.8, 0.6)]
        zero = [(0.9, 0.1)] * 4
        outside = square(1.2, 0.2, 0.2)
        co, faces, uvs = loose(square(0.0, 0.5, 0.25), stretched, flipped, zero, outside)
        s = U.audit(co, faces, uvs)
        self.assertEqual((s["flipped"], s["zero"], s["outside"], s["tiles"]), ([2], [3], [4], [1002]))
        self.assertEqual([face for face, _ in s["worst"]], [1])
        self.assertAlmostEqual(s["worst"][0][1], 4.0)  # 0.5 by 0.125 on a square: 4 to 1.
        self.assertAlmostEqual(s["angle_share"], 0.25)  # One of the four faces with UV area.
        self.assertIsNone(s["border"], "a layout past the tile has no border to speak of")
        lines = "\n".join(U.object_lines("Sign", "UVMap", s, 8))
        self.assertIn("outside 0-1: 1 faces (u 0.00..1.40, v 0.00..0.80; tiles 1002): 4", lines)
        self.assertIn("zero-area: 3 | flipped: 2", lines)
        self.assertIn("worst faces 1 (4.0x)", lines)

    def test_the_target_and_the_spread_across_objects(self):
        co, faces, uvs = loose(square(0.1, 0.1, 0.25))
        text = "\n".join(U.object_lines("Crate", "UVMap", U.audit(co, faces, uvs), 8, density=512))
        self.assertIn("density px/m: 256 / 256 / 256 (min / median / max); median 0.50x the 512 target", text)
        self.assertIn("gutter: one island", text)
        self.assertEqual(U.across({"Crate": 400.0, "Lid": 200.0, "Empty": 0.0}),
                         "Across objects: median density 400 px/m (Crate) to 200 (Lid), 2.0x apart.")
        self.assertEqual(U.across({"Crate": 400.0}), "")


class PlanTest(unittest.TestCase):
    def test_texels_and_the_most_density_a_size_holds(self):
        self.assertEqual(U.texels(1.0, 4.0, 512, 8), (262144.0, 8192.0))
        best = U.max_density(1.0, 4.0, 1024, 8)
        covered, gutters = U.texels(1.0, 4.0, best, 8)
        self.assertAlmostEqual(covered + gutters, U.PACKING * 1024 ** 2, places=3)
        self.assertEqual(U.max_density(0.0, 4.0, 1024, 8), 0.0)

    def test_a_shared_atlas_names_the_size_that_fits_and_its_neighbours(self):
        items = [{"name": "Barrel", "area": 1.9, "boundary": 21.0, "islands": 14},
                 {"name": "Lid", "area": 0.35, "boundary": None, "islands": 0}]
        lines = U.atlas_plan(items, 400, 8, 1024)
        text = "\n".join(lines)
        self.assertEqual(lines[0], "Plan for 400 px/m with 8 px padding (pack_islands fills about 75% of a tile):")
        self.assertIn("- Barrel: 1.90 m2, 14 islands: 0.30 Mpx of islands + 0.03 of gutters; 1024 alone", text)
        self.assertIn("- Lid: 0.35 m2, 6 islands assumed (no UVs yet)", text)
        self.assertIn("Shared atlas: 1024 fits", text)
        self.assertIn("512 holds", text)
        self.assertIn("2048 would be", text)
        self.assertIn("21 MB", text)
        self.assertEqual(items[1]["boundary"], None, "the caller's items are left as they were")

    def test_no_density_plans_the_most_the_resolution_holds_and_too_much_is_split(self):
        lines = U.atlas_plan([{"name": "Crate", "area": 24.0, "boundary": 48.0, "islands": 6}], None, 8, 1024)
        self.assertTrue(lines[0].startswith("At 1024 the most they hold together: 177 px/m."), lines[0])
        huge = U.atlas_plan([{"name": "Hall", "area": 400.0, "boundary": 200.0, "islands": 6},
                             {"name": "Door", "area": 2.0, "boundary": 6.0, "islands": 2}], 1000, 8, 1024)
        self.assertIn("more than one 8192 holds; split it", "\n".join(huge))

    def test_many_small_islands_are_called_fragmented(self):
        lines = U.atlas_plan([{"name": "Shelf", "area": 1.0, "boundary": 600.0, "islands": 606}], 400, 8, 1024)
        self.assertTrue(any(line.startswith("Fragmented: gutters take 600%") for line in lines), lines)


class ProjectTest(unittest.TestCase):
    CUBE = {  # Outward normal -> corners wound counter-clockwise seen from outside.
        (1, 0, 0): [(1, -1, -1), (1, 1, -1), (1, 1, 1), (1, -1, 1)],
        (-1, 0, 0): [(-1, 1, -1), (-1, -1, -1), (-1, -1, 1), (-1, 1, 1)],
        (0, 1, 0): [(1, 1, -1), (-1, 1, -1), (-1, 1, 1), (1, 1, 1)],
        (0, -1, 0): [(-1, -1, -1), (1, -1, -1), (1, -1, 1), (-1, -1, 1)],
        (0, 0, 1): [(-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)],
        (0, 0, -1): [(-1, 1, -1), (1, 1, -1), (1, -1, -1), (-1, -1, -1)],
    }

    def test_every_box_face_keeps_its_winding_and_its_own_group(self):
        groups = set()
        for normal, corners in self.CUBE.items():
            uv = U.project("cube", corners, normal, (0, 0, 0), step=16)
            self.assertAlmostEqual(signed_area(uv), 4.0, msg=normal)  # 2 m by 2 m, seen from outside.
            groups.add(round(min(u for u, _ in uv) / 16))
        self.assertEqual(len(groups), 6)

    def test_a_cylinder_side_unrolls_to_arc_length_by_height_and_its_caps_lie_flat(self):
        n, r, h = 16, 0.1, 0.3
        ring = [(r * math.cos(2 * math.pi * k / n), r * math.sin(2 * math.pi * k / n)) for k in range(n)]
        widths, heights = [], []
        for k in range(n):
            (x0, y0), (x1, y1) = ring[k], ring[(k + 1) % n]
            corners = [(x0, y0, 0), (x1, y1, 0), (x1, y1, h), (x0, y0, h)]
            normal = ((x0 + x1) / 2, (y0 + y1) / 2, 0)
            uv = U.project("cylinder", corners, normal, (0, 0, h / 2), step=8)
            self.assertGreater(signed_area(uv), 0, k)
            widths.append(max(u for u, _ in uv) - min(u for u, _ in uv))
            heights.append(max(v for _, v in uv) - min(v for _, v in uv))
        self.assertAlmostEqual(sum(widths), 2 * math.pi * r, places=6)  # No face spans the seam the long way round.
        self.assertTrue(all(abs(v - h) < 1e-9 for v in heights))
        top = U.project("cylinder", [(x, y, h) for x, y in ring], (0, 0, 1), (0, 0, h / 2), step=8)
        bottom = U.project("cylinder", [(x, y, 0) for x, y in reversed(ring)], (0, 0, -1), (0, 0, h / 2), step=8)
        self.assertGreater(signed_area(top), 0)
        self.assertGreater(signed_area(bottom), 0)
        self.assertNotEqual(round(min(u for u, _ in top) / 8), round(min(u for u, _ in bottom) / 8))

    def test_a_sphere_pole_takes_its_longitude_from_the_face(self):
        ring = lambda a: (math.cos(a) * 0.3, math.sin(a) * 0.3, 0.95)  # noqa: E731
        corners = [(0, 0, 1), ring(0.2), ring(0.5)]
        uv = U.project("sphere", corners, (0.1, 0.1, 1), (0, 0, 0), step=8)
        self.assertAlmostEqual(uv[0][0], 0.35, places=6)  # The pole at the others' mean longitude, radius 1.
        self.assertAlmostEqual(uv[0][1], math.pi / 2)


class DescribeTest(unittest.TestCase):
    def test_the_tool_cards(self):
        self.assertEqual(U.describe("uv_unwrap", {"objects": ["A", "B"], "method": "smart"}),
                         ("UV unwrap A, B (smart)", ""))
        self.assertEqual(U.describe("inspect_uv", {"objects": ["A"], "plan": True})[0], "Plan a texture atlas for A")
        self.assertEqual(U.describe("inspect_uv", {"objects": ["A"], "checker": True})[0],
                         "Measure the UVs of A with a checker")
        self.assertIsNone(U.describe("run_python", {}))


if __name__ == "__main__":
    unittest.main()
