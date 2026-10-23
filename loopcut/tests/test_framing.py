"""framing.py, frame_camera's math, is pure: the frame Blender computes from sensor, fit, aspect and
shift, the look-at rotation, the exact fit and the coverage report. Also how the agent treats the
tool: a scene change that asks first, except report_only. The real thing, evaluated vertices checked
with world_to_camera_view, is loopcut/harness/camera_check.py."""

import json
import math
import random
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, camera, framing as F, llm, tools  # noqa: E402

CHANGES_SCENE = agent._changes_scene  # test_agent_loop stubs these two; keep the real ones.
TOOL_SCHEMAS = agent._tool_schemas
SAME_WHILE_UNCHANGED = agent._same_while_unchanged
HD, PORTRAIT = (1920, 1080), (1080, 1920)


def close(a, b, places=9) -> bool:
    return all(abs(x - y) < 10 ** -places for x, y in zip(a, b))


def cloud(seed: int, count: int = 300) -> list:
    rng = random.Random(seed)
    return [(rng.uniform(-2, 3), rng.uniform(-1, 1), rng.uniform(0, 4)) for _ in range(count)]


def box(center=(0.0, 0.0, 0.0), size=(2.0, 2.0, 2.0)) -> list:
    return [tuple(c + s / 2 * sign for c, s, sign in zip(center, size, signs))
            for signs in ((x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1))]


class ParentGeometryTest(unittest.TestCase):
    def test_camera_parent_uses_only_visible_descendants_and_instances(self):
        class Object:
            def __init__(self, name, kind="MESH", visible=True, children=()):
                self.name, self.type, self.children_recursive = name, kind, children
                self.original = self
                self.visible_get = lambda **kwargs: visible

        leaf = Object("Visible")
        hidden = Object("Hidden", visible=False)
        instance_owner = Object("Instance", "EMPTY")
        nested = Object("Nested", "EMPTY", children=[leaf, hidden, instance_owner])
        parent = Object("Parent", "EMPTY", children=[nested, leaf, hidden, instance_owner])
        instanced = Object("Instanced geometry", visible=False)  # Its visible instancer owns visibility.
        instances = [types.SimpleNamespace(object=o, is_instance=False, matrix_world=None)
                     for o in (parent, nested, leaf, hidden)]
        instances.append(types.SimpleNamespace(object=instanced, is_instance=True, parent=instance_owner,
                                               matrix_world=None))
        depsgraph = types.SimpleNamespace(object_instances=instances, view_layer=object())
        numpy = types.SimpleNamespace(vstack=lambda arrays: [p for a in arrays for p in a])
        with mock.patch.dict(sys.modules, {"numpy": numpy}), \
                mock.patch.object(camera, "_local_points", side_effect=lambda obj, budget: [obj.name]), \
                mock.patch.object(camera, "_world", side_effect=lambda points, matrix: points):
            rows, notes = camera._geometry([parent], depsgraph)
        self.assertEqual(rows, {"Parent": ["Visible", "Instanced geometry"]})
        self.assertEqual(notes, [])

    def test_viewport_bounds_use_transformed_descendants_not_parent_origin(self):
        class Vector(tuple):
            def __add__(self, other):
                return Vector(a + b for a, b in zip(self, other))

            def __sub__(self, other):
                return Vector(a - b for a, b in zip(self, other))

            def __truediv__(self, value):
                return Vector(a / value for a in self)

            @property
            def length(self):
                return math.sqrt(sum(a * a for a in self))

        class Transform:
            def __matmul__(self, point):
                return point + Vector((20, -5, 10))

        child = types.SimpleNamespace(name="Child", type="MESH", bound_box=box(), matrix_world=Transform(),
                                      visible_get=lambda: True)
        hidden = types.SimpleNamespace(name="Hidden", type="MESH", bound_box=box(size=(200, 200, 200)),
                                       visible_get=lambda: False)
        for kind in ("EMPTY", "LIGHT", "ARMATURE", "MESH"):
            parent = types.SimpleNamespace(name="Parent", type=kind, bound_box=[(-1, -1, -1)] * 8,
                                           children_recursive=[child, hidden])
            with self.subTest(kind=kind), mock.patch.dict(sys.modules, {"mathutils": types.SimpleNamespace(Vector=Vector)}):
                center, radius = tools._bounding_sphere([parent, child])
            self.assertEqual(center, (20, -5, 10))
            self.assertAlmostEqual(radius, math.sqrt(3))


class FrameTest(unittest.TestCase):
    def test_matches_blenders_view_frame_for_the_default_camera(self):
        # Camera.view_frame for 50 mm, 36 mm AUTO at 1920x1080: corners (+-0.5, +-0.28125) at z -1.3889.
        view = F.frame(HD)
        self.assertAlmostEqual(view.width / 2, 0.5 / 1.38889, places=4)
        self.assertAlmostEqual(view.height / 2, 0.28125 / 1.38889, places=4)
        self.assertEqual(view.fit, "HORIZONTAL")

    def test_auto_puts_the_sensor_width_on_the_longer_side(self):
        portrait = F.frame(PORTRAIT)
        self.assertAlmostEqual(portrait.height, 36 / 50)
        self.assertAlmostEqual(portrait.width, 36 / 50 * 1080 / 1920)
        self.assertEqual(portrait.fit, "VERTICAL")

    def test_fixed_fits_use_their_own_sensor_side(self):
        vertical = F.frame(HD, "VERTICAL", sensor_width=36, sensor_height=24)
        self.assertAlmostEqual(vertical.height, 24 / 50)
        self.assertAlmostEqual(vertical.width, 24 / 50 * 1920 / 1080)
        horizontal = F.frame(PORTRAIT, "HORIZONTAL")
        self.assertAlmostEqual(horizontal.width, 36 / 50)
        self.assertAlmostEqual(horizontal.height, 36 / 50 * 1920 / 1080)

    def test_pixel_aspect_and_shift(self):
        wide = F.frame((1000, 1000), pixel_aspect=(2.0, 1.0))
        self.assertAlmostEqual(wide.height, wide.width / 2)
        shifted = F.frame(PORTRAIT, shift=(0.1, -0.2))  # Shift is a fraction of the fitted side, here the height.
        self.assertAlmostEqual(shifted.center_x, 0.1 * 36 / 50)
        self.assertAlmostEqual(shifted.center_y, -0.2 * 36 / 50)

    def test_orthographic_frame_is_per_unit_scale(self):
        view = F.frame(HD, ortho=True, lens=85)
        self.assertEqual((view.width, view.ortho), (1.0, True))
        self.assertAlmostEqual(view.height, 1080 / 1920)


class BasisTest(unittest.TestCase):
    def assert_orthonormal(self, basis):
        right, up, back = basis
        for axis in basis:
            self.assertAlmostEqual(F.dot(axis, axis), 1.0)
        self.assertAlmostEqual(F.dot(right, up), 0.0)
        self.assertTrue(close(F._cross(right, up), back), "right-handed, looking down -back")

    def test_named_views_look_where_capture_viewport_does(self):
        right, up, back = F.direction_basis("front")
        self.assertTrue(close(back, (0, -1, 0)) and close(up, (0, 0, 1)) and close(right, (1, 0, 0)))
        self.assertTrue(close(F.direction_basis("side")[2], (1, 0, 0)))
        right, up, back = F.direction_basis("Top ")
        self.assertTrue(close(back, (0, 0, 1)) and close(up, (0, 1, 0)))
        right, up, back = F.direction_basis("three_quarter")
        self.assertAlmostEqual(right[2], 0.0)
        self.assertTrue(back[0] > 0 and back[1] < 0 and 0.3 < back[2] < 0.6, "front right, a little above")

    def test_look_at_has_no_roll_and_faces_the_subject(self):
        for vector in ((1, -1, 0.5), (-3, 2, -1), (0, 0, -1), (0.001, 0, 5)):
            basis = F.look_basis(vector)
            self.assert_orthonormal(basis)
            self.assertTrue(close(basis[2], F._unit(vector)))
            if abs(F._unit(vector)[2]) < 0.99:
                self.assertAlmostEqual(basis[0][2], 0.0, msg="right axis level: world up stays up")
                self.assertGreater(basis[1][2], 0.0)
        euler = F.euler_basis(F.NAMED["three_quarter"])
        self.assertTrue(all(close(a, b) for a, b in zip(euler, F.look_basis(euler[2]))))

    def test_direction_spellings(self):
        for keep in (None, "", "keep", " KEEP"):
            self.assertIsNone(F.direction_basis(keep))
        spelled = [F.direction_basis(d) for d in ("1,-1,0.5", "[1, -1, 0.5]", [1, -1, 0.5], ("1", "-1", ".5"))]
        self.assertTrue(all(b == spelled[0] for b in spelled))
        for bad in ("diagonal", "1,2", "0,0,0", [1, "a", 2], 7, "1,2,nan"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                F.direction_basis(bad)


class FitTest(unittest.TestCase):
    def check(self, points, basis, view, margin, label=""):
        result = F.fit(points, basis, view, margin)
        seen = F.coverage(points, result.location, basis, view, result.ortho_scale or 1.0)
        left, right, bottom, top = seen["margins"]
        tight, loose = ((left, right), (bottom, top)) if result.limit == "width" else ((bottom, top), (left, right))
        self.assertEqual(seen["inside"], 1.0, label)
        self.assertAlmostEqual(tight[0], margin, places=7, msg=label)
        self.assertAlmostEqual(tight[1], margin, places=7, msg=label)
        self.assertAlmostEqual(loose[0], loose[1], places=7, msg=f"{label}: centred on the looser axis")
        self.assertGreaterEqual(min(loose), margin - 1e-9, label)
        return result, seen

    def test_a_cube_seen_from_the_front_has_a_known_answer(self):
        # Height-limited at 16:9: the front face's top edge (z 1 at y -1) sits on the frame's top.
        result, _ = self.check(box(), F.direction_basis("front"), F.frame(HD), 0.0)
        self.assertEqual(result.limit, "height")
        half_height = 36 / 50 * 1080 / 1920 / 2
        self.assertTrue(close(result.location, (0.0, -1.0 - 1.0 / half_height, 0.0), places=7), result.location)

    def test_exact_all_inside_margin_and_centred_across_views_aspects_fits_and_shifts(self):
        views = [F.frame(HD), F.frame(PORTRAIT), F.frame(PORTRAIT, "VERTICAL"), F.frame((1000, 1000), "HORIZONTAL"),
                 F.frame(HD, shift=(0.15, -0.1)), F.frame(PORTRAIT, lens=18), F.frame(HD, lens=200)]
        for seed, direction in enumerate(("three_quarter", "front", "side", "top", "2,-1,0.3", "0,-0.2,-1")):
            for n, view in enumerate(views):
                for margin in (0.0, 0.08, 0.3):
                    self.check(cloud(seed), F.direction_basis(direction), view, margin, f"{direction} view{n} {margin}")

    def test_it_is_the_closest_position_not_a_bounding_sphere(self):
        points, basis, view = cloud(7), F.direction_basis("three_quarter"), F.frame(HD)
        result = F.fit(points, basis, view, 0.08)
        forward = tuple(-c for c in basis[2])
        closer = tuple(c + 1e-4 * f for c, f in zip(result.location, forward))
        self.assertLess(min(F.coverage(points, closer, basis, view)["margins"]), 0.08 - 1e-6)

    def test_wide_and_tall_subjects_pick_the_limiting_side(self):
        row = [p for x in range(5) for p in box((x * 3.0, 0, 0))]
        tall = box(size=(1, 1, 8))
        self.assertEqual(self.check(row, F.direction_basis("front"), F.frame(HD), 0.08)[0].limit, "width")
        self.assertEqual(self.check(tall, F.direction_basis("front"), F.frame(HD), 0.08)[0].limit, "height")
        self.assertEqual(self.check(tall, F.direction_basis("front"), F.frame(PORTRAIT), 0.08)[0].limit, "height")

    def test_a_flat_subject_seen_edge_on_still_fits(self):
        plane = [(x, y, 0.0) for x in (-2, 2) for y in (-1, 1)]
        result, _ = self.check(plane, F.direction_basis("front"), F.frame(HD), 0.08)
        self.assertLess(result.location[1], -1.0, "in front of the plane, not on it")

    def test_orthographic_fit_solves_the_scale(self):
        for direction in ("top", "three_quarter"):
            for view in (F.frame(HD, ortho=True), F.frame(PORTRAIT, ortho=True, shift=(0.1, 0.05))):
                result, seen = self.check(cloud(3), F.direction_basis(direction), view, 0.1, direction)
                self.assertGreater(result.ortho_scale, 0)
                depths = [F.to_camera(p, result.location, F.direction_basis(direction))[2] for p in cloud(3)]
                self.assertGreater(min(depths), 0, "the camera is in front of everything")

    def test_bad_input(self):
        basis, view = F.direction_basis("front"), F.frame(HD)
        for points, margin in (([(1, 1, 1)] * 3, 0.08), ([], 0.08), (box(), 0.45), (box(), -0.1)):
            with self.assertRaises(ValueError):
                F.fit(points, basis, view, margin)


class FitAroundTest(unittest.TestCase):
    """fit_around: aim pinned to the frame's middle; fill 1 fits all, fill > 1 crops around aim."""
    VIEWS = [F.frame(HD), F.frame(PORTRAIT), F.frame(HD, shift=(0.15, -0.1)), F.frame(PORTRAIT, "VERTICAL", lens=85)]

    def centred(self, result, aim, basis, view):
        u, v, depth = F.project(aim, result.location, basis, view, result.ortho_scale or 1.0)
        self.assertAlmostEqual(u, 0.5, places=7)
        self.assertAlmostEqual(v, 0.5, places=7)
        self.assertGreater(depth, 0)

    def spans(self, points, result, basis, view) -> tuple:
        seen = F.coverage(points, result.location, basis, view, result.ortho_scale or 1.0)
        left, right, bottom, top = seen["bounds"]
        return seen, (right - left, top - bottom)

    def test_fill_one_keeps_everything_inside_with_aim_centred(self):
        for seed, direction in enumerate(("three_quarter", "front", "2,-1,0.3")):
            points, basis = cloud(seed), F.direction_basis(direction)
            for view in self.VIEWS:
                for aim in ((0.5, 0.0, 2.0), (2.5, 0.8, 3.5)):  # The middle, then near a corner.
                    result = F.fit_around(points, basis, view, 0.08, aim)
                    self.centred(result, aim, basis, view)
                    seen, _ = self.spans(points, result, basis, view)
                    self.assertEqual(seen["inside"], 1.0)
                    self.assertAlmostEqual(min(seen["margins"]), 0.08, places=7, msg=f"{direction} {aim}: the closest")

    def test_fill_one_at_the_centre_is_the_whole_fit_when_the_subject_is_symmetric(self):
        basis, view = F.direction_basis("front"), F.frame(HD)
        whole = F.fit(box(), basis, view, 0.08)
        around = F.fit_around(box(), basis, view, 0.08, (0.0, 0.0, 0.0))
        self.assertTrue(close(whole.location, around.location, places=7), (whole.location, around.location))
        self.assertEqual(around.limit, whole.limit)
        self.assertAlmostEqual(around.span, 1 - 2 * 0.08, places=7)

    def test_fill_crops_around_aim_to_fill_times_the_whole_fit(self):
        for direction in ("three_quarter", "front", "side"):
            points, basis = cloud(4), F.direction_basis(direction)
            for view in self.VIEWS:
                for fill in (1.5, 2.0, 3.0):
                    aim = (2.0, 0.5, 3.2)  # The cap of a bottle: off-centre, near the top.
                    result = F.fit_around(points, basis, view, 0.08, aim, fill)
                    self.centred(result, aim, basis, view)
                    seen, spans = self.spans(points, result, basis, view)
                    side = spans[0] if result.limit == "width" else spans[1]
                    self.assertAlmostEqual(side, fill * (1 - 2 * 0.08), places=5, msg=f"{direction} {fill}")
                    self.assertAlmostEqual(result.span, side, places=5)
                    self.assertGreaterEqual(side, max(spans) - 1e-9, "the tighter side")
                    self.assertLess(seen["inside"], 1.0)
                    self.assertTrue(seen["clips"], "the crop is visible in the report")
                    self.assertNotIn("behind", seen["clips"])

    def test_orthographic_fill_divides_the_whole_fit_scale(self):
        basis = F.direction_basis("three_quarter")
        for view in (F.frame(HD, ortho=True), F.frame(PORTRAIT, ortho=True, shift=(0.1, 0.05))):
            whole = F.fit(cloud(3), basis, view, 0.1)
            aim = (1.0, 0.0, 1.0)
            for fill in (1.0, 2.5):
                result = F.fit_around(cloud(3), basis, view, 0.1, aim, fill)
                self.centred(result, aim, basis, view)
                if fill > 1:
                    self.assertAlmostEqual(result.ortho_scale, whole.ortho_scale / fill, places=9)
                else:
                    self.assertEqual(self.spans(cloud(3), result, basis, view)[0]["inside"], 1.0)
                depths = [F.to_camera(p, result.location, basis)[2] for p in cloud(3)]
                self.assertGreater(min(depths), 0, "in front of everything")

    def test_a_fill_out_of_reach_stops_in_front_of_the_subject(self):
        basis, view = F.direction_basis("front"), F.frame(HD)
        deep = box(size=(1.0, 20.0, 1.0))  # Long along the view: the near end stops the camera.
        result = F.fit_around(deep, basis, view, 0.08, (0.0, 9.0, 0.0), 10)
        depths = [F.to_camera(p, result.location, basis)[2] for p in deep]
        self.assertGreater(min(depths), 0)
        self.assertLess(result.span, 10 * (1 - 2 * 0.08))

    def test_bad_input(self):
        basis, view = F.direction_basis("front"), F.frame(HD)
        for fill in (0.5, 11, "2", True, float("nan")):
            with self.assertRaises(ValueError, msg=fill):
                F.fit_around(box(), basis, view, 0.08, (0, 0, 0), fill)
        with self.assertRaises(ValueError):
            F.fit_around([(1, 1, 1)] * 3, basis, view, 0.08, (0, 0, 0))


class RegionTest(unittest.TestCase):
    """region: the subject fitted into part of the frame, the shift kept; the margin that binds is named."""
    VIEWS = [F.frame(HD), F.frame(PORTRAIT), F.frame(HD, shift=(0.15, -0.1)), F.frame(PORTRAIT, "VERTICAL", lens=85)]

    def test_fit_puts_the_subject_in_the_region_with_the_margin(self):
        region = (0.4, 0.0, 1.0, 1.0)  # The right 60% of the frame.
        for seed, direction in enumerate(("three_quarter", "front", "2,-1,0.3")):
            points, basis = cloud(seed), F.direction_basis(direction)
            for n, view in enumerate(self.VIEWS):
                result = F.fit(points, basis, view, 0.05, region)
                seen = F.coverage(points, result.location, basis, view)
                left, right, bottom, top = seen["bounds"]
                label = f"{direction} view{n}"
                self.assertEqual(seen["inside"], 1.0, label)
                self.assertGreaterEqual(left, 0.45 - 1e-7, label)
                self.assertLessEqual(right, 0.95 + 1e-7, label)
                if result.limit == "width":
                    self.assertAlmostEqual(left, 0.45, places=7, msg=label)
                    self.assertAlmostEqual(right, 0.95, places=7, msg=label)
                else:
                    self.assertAlmostEqual(bottom, 0.05, places=7, msg=label)
                    self.assertAlmostEqual(top, 0.95, places=7, msg=label)
                    self.assertAlmostEqual((left + right) / 2, 0.7, places=6, msg=f"{label}: centred in the region")

    def test_orthographic_region(self):
        basis, view, region = F.direction_basis("three_quarter"), F.frame(HD, ortho=True, shift=(0.1, 0.0)), (0.0, 0.2, 0.5, 0.9)
        result = F.fit(cloud(3), basis, view, 0.02, region)
        seen = F.coverage(cloud(3), result.location, basis, view, result.ortho_scale)
        left, right, bottom, top = seen["bounds"]
        self.assertTrue(left >= 0.02 - 1e-9 and right <= 0.48 + 1e-9 and bottom >= 0.22 - 1e-9 and top <= 0.88 + 1e-9, seen)
        tight = (left, right) if result.limit == "width" else (bottom, top)
        self.assertTrue(close(tight, (0.02, 0.48) if result.limit == "width" else (0.22, 0.88), places=7), tight)

    def test_the_whole_region_is_the_old_fit(self):
        points, basis, view = cloud(5), F.direction_basis("three_quarter"), F.frame(HD, shift=(0.1, 0.2))
        self.assertEqual(F.fit(points, basis, view, 0.08), F.fit(points, basis, view, 0.08, F.FULL))
        self.assertEqual(F.fit_around(points, basis, view, 0.08, (0.5, 0, 2)),
                         F.fit_around(points, basis, view, 0.08, (0.5, 0, 2), 1.0, F.FULL))

    def test_aim_lands_in_the_middle_of_the_region(self):
        region = (0.4, 0.1, 1.0, 0.7)
        for view in self.VIEWS + [F.frame(HD, ortho=True)]:
            points, basis, aim = cloud(2), F.direction_basis("three_quarter"), (0.5, 0.0, 2.0)
            for fill in (1.0, 2.0):
                result = F.fit_around(points, basis, view, 0.05, aim, fill, region)
                u, v, _ = F.project(aim, result.location, basis, view, result.ortho_scale or 1.0)
                self.assertAlmostEqual(u, 0.7, places=7)
                self.assertAlmostEqual(v, 0.4, places=7)
                seen = F.coverage(points, result.location, basis, view, result.ortho_scale or 1.0)
                if fill == 1.0:
                    self.assertEqual(seen["inside"], 1.0)
                    left, right, bottom, top = seen["bounds"]
                    margins = {"left": left - 0.45, "right": 0.95 - right, "bottom": bottom - 0.15, "top": 0.65 - top}
                    self.assertAlmostEqual(min(margins.values()), 0.0, places=7)
                    self.assertAlmostEqual(margins[result.edge], 0.0, places=7, msg="the edge named is the one that binds")
                    self.assertEqual(result.limit, "width" if result.edge in ("left", "right") else "height")

    def test_the_binding_margin_is_named_not_the_longer_span(self):
        # Aim at the bottom of a wide, short subject: the top margin binds although the width spans more.
        basis, view = F.direction_basis("front"), F.frame((1000, 1000), "HORIZONTAL")
        points = [(x, 0.0, z) for x in (-0.45, 0.45) for z in (0.0, 0.6)]
        result = F.fit_around(points, basis, view, 0.0, (0.0, 0.0, 0.0))
        self.assertEqual((result.limit, result.edge), ("height", "top"))
        seen = F.coverage(points, result.location, basis, view)
        self.assertAlmostEqual(seen["margins"][3], 0.0, places=7)
        self.assertGreater(seen["bounds"][1] - seen["bounds"][0], seen["bounds"][3] - seen["bounds"][2], "wider than tall")

    def test_region_spellings_and_refusals(self):
        self.assertEqual(F.region_of(None), F.FULL)
        self.assertEqual(F.region_of("0.4,0,1,1"), (0.4, 0.0, 1.0, 1.0))
        self.assertEqual(F.region_of("[0.4, 0, 1, 1]"), (0.4, 0.0, 1.0, 1.0))
        self.assertEqual(F.region_of([0, 0.2, 0.5, 1]), (0.0, 0.2, 0.5, 1.0))
        for bad in ("0.4,0,1", [0.6, 0, 0.4, 1], [0, 0, 1.2, 1], "a,b,c,d", [0, 0, True, 1], [0, 0, float("nan"), 1]):
            with self.assertRaises(ValueError, msg=bad):
                F.region_of(bad)
        with self.assertRaises(ValueError):  # 0.1 wide cannot hold 0.08 on each side.
            F.fit(box(), F.direction_basis("front"), F.frame(HD), 0.08, (0.4, 0.0, 0.5, 1.0))


class FramesTest(unittest.TestCase):
    def test_frame_numbers(self):
        self.assertEqual(F.frame_numbers([1, 50, 100]), [1, 50, 100])
        self.assertEqual(F.frame_numbers("1, 50,50, 100"), [1, 50, 100])
        self.assertEqual(F.frame_numbers(7), [7])
        self.assertEqual(F.frame_numbers(["12", 13.0]), [12, 13])
        for bad in ("late", [1.5], [True], [], list(range(F.MAX_FRAMES + 1))):
            with self.assertRaises(ValueError, msg=bad):
                F.frame_numbers(bad)

    def test_a_line_per_frame_with_the_camera_where_it_moves_and_what_is_cut(self):
        inside = {"inside": 1.0, "bounds": (0.2, 0.8, 0.3, 0.7), "margins": (0.2, 0.2, 0.3, 0.3),
                  "offset": (0.0, 0.0), "clips": []}
        cut = {"inside": 0.5, "bounds": (0.6, 1.3, 0.3, 0.7), "margins": (0.6, -0.3, 0.3, 0.3),
               "offset": (0.45, 0.0), "clips": ["right"]}
        camera = {"name": "Shot", "lens": 50.0, "resolution": (1920, 1080), "fit": "AUTO, horizontal"}
        text = F.frames_report(camera, [(1, (0, -6, 0), [("A", inside), ("B", inside)], inside),
                                        (50, (0, -6, 0), [("A", inside), ("B", cut)], cut)], 1, [])
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith('Camera "Shot" as it is, at frames 1, 50 (the scene is back on frame 1)'))
        self.assertTrue(lines[2].startswith("- frame 1: x 0.200..0.800") and "not all inside" not in lines[2], lines[2])
        self.assertIn("clipped: right; not all inside: B 50.0%", lines[3])
        self.assertNotIn("camera at", text, "the camera did not move")
        moved = F.frames_report(camera, [(1, (0, -6, 0), [("A", inside)], None), (9, (1, -6, 0), [("A", inside)], None)], 1, [])
        self.assertIn("- frame 1: x 0.200..0.800", moved)
        self.assertIn("; camera at [1.000, -6.000, 0.000]", moved.splitlines()[-1])
        self.assertLess(len(moved), 600)


class CoverageTest(unittest.TestCase):
    def test_inside_share_clipped_edges_margins_and_offset(self):
        basis, view = F.direction_basis("front"), F.frame(HD)
        half_w = view.width / 2
        points = [(0.0, 10.0, 0.0), (-half_w * 20, 10.0, 0.0), (0.0, -5.0, 0.0), (0.0, 500.0, 0.0)]
        seen = F.coverage(points, (0.0, 0.0, 0.0), basis, view, clip=(0.1, 100.0))
        self.assertAlmostEqual(seen["inside"], 0.25)
        self.assertEqual(seen["clips"], ["left", "far", "behind"])
        left, right, bottom, top = seen["margins"]
        self.assertAlmostEqual(left, -0.5)  # Twice the half width out: half a frame past the left edge.
        self.assertAlmostEqual(right, 0.5)
        self.assertAlmostEqual(bottom, top)
        self.assertAlmostEqual(seen["offset"][0], -0.5)

    def test_nothing_in_front(self):
        seen = F.coverage([(0.0, -5.0, 0.0)], (0.0, 0.0, 0.0), F.direction_basis("front"), F.frame(HD))
        self.assertIsNone(seen["bounds"])
        self.assertIn("not in view (behind)", F.row("Ghost", seen))


class ReportTest(unittest.TestCase):
    CAMERA = {"name": "Camera", "what": "framed from three_quarter with margin 0.08, fitted to the height",
              "location": (7.1234, -6.5, 4.25), "rotation": (62.0, 0.0, 38.0), "lens": 50.0, "ortho_scale": None,
              "distance": 11.2, "resolution": HD, "fit": "AUTO, horizontal"}

    def rows(self, count: int, clipped=()) -> list:
        view, basis = F.frame(HD), F.direction_basis("front")
        location = F.fit(box(), basis, view, 0.08).location
        out = []
        for n in range(count):
            points = box((1.5 if n in clipped else 0.0, 0, 0))
            out.append((f"Object {n}", F.coverage(points, location, basis, view)))
        return out

    def test_every_number_the_model_needs_within_budget(self):
        rows = self.rows(12, clipped={3, 10})
        subject = rows[0][1]
        text = F.report(self.CAMERA, rows, subject, ["clip_end 100 -> 132"])
        self.assertLessEqual(len(text) / 4, 500)
        self.assertIn('Camera "Camera" framed from three_quarter', text.splitlines()[0])
        self.assertIn("at [7.123, -6.500, 4.250], rotation [62.0, 0.0, 38.0] deg", text)
        self.assertIn("lens 50.0 mm, 11.200 m from the subject's centre; 1920x1080", text)
        listed = [line for line in text.splitlines() if line.startswith("- Object")]
        self.assertEqual(len(listed), F.MAX_ROWS)
        self.assertTrue(any("Object 3:" in line and "clipped: right" in line for line in listed))
        self.assertTrue(any("Object 10:" in line for line in listed), "clipped rows are listed first")
        self.assertIn("- 4 more objects: 4 fully inside.", text)
        self.assertIn("All together", text)
        row = next(line for line in listed if line.startswith("- Object 0:"))
        self.assertRegex(row, r"x 0\.\d{3}\.\.0\.\d{3} y 0\.080\.\.0\.920, 100% inside, margins L 0\.\d{3} "
                              r"R 0\.\d{3} B 0\.080 T 0\.080, offset [+-]0\.000 [+-]0\.000$")

    def test_almost_all_inside_is_not_rounded_up(self):
        self.assertEqual(F._share(0.9999), "99.9%")
        self.assertEqual(F._share(1.0), "100%")
        self.assertEqual(F.row("Empty", {"empty": True}), "- Empty: no geometry")

    def test_one_object_has_no_together_row(self):
        rows = self.rows(1)
        self.assertNotIn("All together", F.report(dict(self.CAMERA, ortho_scale=4.5), rows, rows[0][1], []))
        self.assertIn("ortho_scale 4.500", F.report(dict(self.CAMERA, ortho_scale=4.5), rows, rows[0][1], []))


class ToolTest(unittest.TestCase):
    def test_schema_is_small_and_offered(self):
        schema = json.dumps(llm.wire_tools([camera.SCHEMAS[0]]))
        self.assertLessEqual(len(schema) / 4, 350)
        self.assertIn("frame_camera", [s["function"]["name"] for s in TOOL_SCHEMAS()])
        self.assertIn(camera.frame_camera, [camera.DISPATCH.get("frame_camera")])

    def test_a_scene_change_that_asks_first_unless_it_only_reports(self):
        self.assertIn("frame_camera", tools.CHANGES_SCENE)
        framing_call = json.dumps({"objects": ["Cube"], "direction": "front"})
        self.assertEqual(agent._approval("frame_camera", framing_call, ()), "gate")
        self.assertTrue(CHANGES_SCENE("frame_camera", framing_call), "takes a checkpoint first")
        self.assertFalse(SAME_WHILE_UNCHANGED("frame_camera", framing_call))
        for report in ('{"report_only": true}', '{"report_only": "true", "objects": ["Cube"]}'):
            self.assertEqual(agent._approval("frame_camera", report, ()), "")
            self.assertFalse(CHANGES_SCENE("frame_camera", report))
            self.assertTrue(SAME_WHILE_UNCHANGED("frame_camera", report), "the same report twice is a repeat")
        self.assertTrue(CHANGES_SCENE("frame_camera", '{"report_only": false}'))
        self.assertTrue(CHANGES_SCENE("run_python"), "other tools as before")

    def test_card_label(self):
        self.assertEqual(agent._describe(types.SimpleNamespace(name="frame_camera", arguments='{"objects":["Table"]}'))[0],
                         "Frame camera on Table")
        self.assertEqual(camera.describe("frame_camera", {"report_only": True})[0],
                         "Check camera coverage of everything visible")
        self.assertIsNone(camera.describe("run_python", {}))
        self.assertEqual(camera.describe("frame_camera", {"objects": ["Bottle"], "fill": 2.5, "aim": "Cap"})[0],
                         "Close shot of Bottle around Cap")
        self.assertEqual(camera.describe("frame_camera", {"objects": ["Bottle"], "fill": "1"})[0], "Frame camera on Bottle")

    def test_unknown_arguments_are_refused_before_blender_is_touched(self):
        result = tools.execute("frame_camera", json.dumps({"objects": ["Cube"], "zoom": 2}))
        self.assertFalse(result.ok)
        self.assertIn("Bad arguments", result.text)

    def test_a_close_shot_is_asked_for_with_fill_and_aim(self):
        properties = camera.SCHEMAS[0]["function"]["parameters"]["properties"]
        self.assertIn("close shot", properties["fill"]["description"])
        self.assertIn("aim", properties["fill"]["description"])
        self.assertIn('"x,y,z"', properties["aim"]["description"])
        for fill in (0.5, 25, "lots"):
            result = tools.execute("frame_camera", json.dumps({"objects": ["Cube"], "fill": fill}))
            self.assertFalse(result.ok)
            self.assertIn("fill must be", result.text)

    def test_region_and_frames_are_offered_and_checked_before_blender_is_touched(self):
        properties = camera.SCHEMAS[0]["function"]["parameters"]["properties"]
        self.assertIn("x0,y0,x1,y1", properties["region"]["description"])
        self.assertIn("report_only", properties["frames"]["description"])
        self.assertNotIn("frame", properties, "frames replaces it; frame is still taken")
        for arguments, words in (({"region": "0.6,0,0.4,1"}, "region needs"), ({"region": "left"}, "region is"),
                                 ({"frames": [1, 2]}, "frames works with report_only"),
                                 ({"frames": "late", "report_only": True}, "whole frame numbers"),
                                 ({"frame": 3, "frames": [4], "report_only": True}, "not frame too")):
            result = tools.execute("frame_camera", json.dumps({"objects": ["Cube"], **arguments}))
            self.assertFalse(result.ok, arguments)
            self.assertIn(words, result.text)


if __name__ == "__main__":
    unittest.main()
