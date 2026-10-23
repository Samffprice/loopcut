"""worker_capture's choices that need no render: which view a role's look takes, how its tiles are
laid out at 1024 px, what counts as a backdrop when nothing is focused, which names a brief's
checks make the subjects, and how exposure numbers read. The renders themselves, their sizes,
labels, timings and that a look leaves no trace, are loopcut/harness/worker_host_check.py."""

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

from loopcut import worker_capture as W  # noqa: E402


class UnlitTest(unittest.TestCase):
    """A contact sheet cell of a scene with no light of its own gets the studio world (worker_capture.cell)."""

    def scene(self, engine: str, *lights: tuple) -> types.SimpleNamespace:
        objects = [types.SimpleNamespace(type="MESH", hide_render=False, data=None)] + [
            types.SimpleNamespace(type="LIGHT", hide_render=hidden, data=types.SimpleNamespace(energy=energy))
            for hidden, energy in lights]
        return types.SimpleNamespace(render=types.SimpleNamespace(engine=engine), objects=objects)

    def test_no_light_that_renders_is_unlit(self):
        self.assertTrue(W.unlit(self.scene("BLENDER_EEVEE")))
        self.assertTrue(W.unlit(self.scene("CYCLES", (True, 400.0), (False, 0.0))), "hidden, or at zero power")
        self.assertFalse(W.unlit(self.scene("BLENDER_EEVEE", (False, 400.0))))
        self.assertFalse(W.unlit(self.scene("BLENDER_WORKBENCH")), "Workbench lights the scene itself")


class PlanTest(unittest.TestCase):
    def test_lighting_and_camera_look_through_the_camera_by_default(self):
        """The product-as-a-dot look: a lighting worker's default was a three-quarter of the whole set."""
        for role in ("lighting", "camera"):
            for angle in (None, "three_quarter"):
                self.assertEqual(W.plan(role, angle, True, False), ("role", "single", ("camera",)), (role, angle))
            self.assertEqual(W.plan(role, None, False, False), ("role", "single", ("three_quarter",)), "no camera yet")
            self.assertEqual(W.plan(role, None, True, True), ("role", "single", ("three_quarter",)),
                             "a focus on objects frames them")
            self.assertEqual(W.plan(role, "front", True, False), ("role", "single", ("front",)))

    def test_modeling_frames_its_own_work_from_three_sides(self):
        self.assertEqual(W.plan("modeling", None, True, False), ("role", "detail", W.DETAIL_ANGLES))
        self.assertEqual(W.plan("modeling", "three_quarter", True, True), ("role", "detail", W.DETAIL_ANGLES))
        self.assertEqual(W.plan("modeling", "side", True, False), ("role", "single", ("side",)))
        self.assertEqual(W.plan("modeling", "sheet", True, False), ("role", "grid", W.SHEET_ANGLES))
        self.assertIn("back", W.VIEWS)

    def test_the_final_look_uses_the_scenes_own_settings_through_the_camera(self):
        for role in ("modeling", "materials", "lighting", "camera", "animation"):
            self.assertEqual(W.plan(role, "final", True, False), ("final", "single", ("camera",)), role)
            self.assertEqual(W.plan(role, "final", False, False), ("final", "single", ("three_quarter",)), role)
            self.assertEqual(W.plan(role, "final", True, True), ("final", "single", ("three_quarter",)), role)
        self.assertIn("final", W.ANGLES)

    def test_modeling_and_materials_frame_their_own_work_unless_told(self):
        """Without a focus a final look went through the pack's camera (analysis §5.5): modeling and
        materials frame their own work at every angle but an explicit camera."""
        for role in W.OWN_FOCUS:
            for angle in (None, "three_quarter", "front", "sheet", "final"):
                self.assertTrue(W.own_focus(role, angle, None), (role, angle))
            self.assertFalse(W.own_focus(role, "camera", None))
            self.assertFalse(W.own_focus(role, "final", ["Rug"]), "a focus of its own wins")
        for role in ("lighting", "camera", "animation"):
            self.assertFalse(W.own_focus(role, "final", None), role)
        self.assertEqual(W.plan("modeling", "final", True, W.own_focus("modeling", "final", None)),
                         ("final", "single", ("three_quarter",)))

    def test_animation_frames_share_one_view(self):
        self.assertEqual(W.plan("animation", None, True, False), ("role", "grid", ("frames:three_quarter",)))
        self.assertEqual(W.plan("animation", None, True, False, own_camera=True), ("role", "grid", ("frames:camera",)),
                         "a worker moving a camera sees through it")
        self.assertEqual(W.plan("animation", "sheet", True, False), ("role", "grid", ("frames:camera",)))
        self.assertEqual(W.plan("animation", "sheet", False, False), ("role", "grid", ("frames:three_quarter",)))
        self.assertEqual(W.plan("animation", "side", True, False), ("role", "grid", ("frames:side",)))

    def test_a_camera_workers_sheet_is_its_cameras(self):
        self.assertEqual(W.plan("camera", "sheet", True, False), ("role", "grid", ("cameras",)))
        self.assertEqual(W.plan("camera", "camera", True, True), ("role", "single", ("camera",)))


class DeliverablePreviewTest(unittest.TestCase):
    """Drive the capture reused by the wave card, replacing render I/O but keeping target/bounds math."""

    def obj(self, name, uid, offset=(0, 0, 0), size=(0.4, 0.3, 0.2), kind="MESH", **props):
        class Transform:
            def __matmul__(self, point):
                return tuple(a + b for a, b in zip(point, offset))

        obj = mock.Mock()
        obj.name, obj.session_uid, obj.id_type, obj.type = name, uid, "OBJECT", kind
        obj.children_recursive, obj.material_slots = [], []
        obj.hide_render = False
        obj.get.side_effect = lambda key, default=None: props.get(key, default)
        obj.visible_get.return_value = True
        obj.evaluated_get.return_value = obj
        obj.matrix_world = Transform()
        obj.bound_box = [(x * size[0], y * size[1], z * size[2])
                         for x in (-0.5, 0.5) for y in (-0.5, 0.5) for z in (-0.5, 0.5)]
        return obj

    def test_ownership_selects_new_owned_and_deliverable_children_only(self):
        from loopcut import ownership
        root = self.obj("Machine", 1, kind="EMPTY")
        child = self.obj("Body", 2)
        root.children_recursive = [child]
        created = self.obj("New key", 3)
        owned = self.obj("Existing key", 4, lc_spawn_name="Existing key")
        floor = self.obj("Floor", 5, size=(200, 200, 0), lc_spawn_name="Floor")
        temp = self.obj("lc_temp", 6, lc_temp=True)
        scene = types.SimpleNamespace(objects=[root, child, created, owned, floor, temp])
        blender = types.SimpleNamespace(data=types.SimpleNamespace(objects={"Machine": root}))
        with mock.patch.object(W, "bpy", blender), mock.patch.object(ownership, "created_ids", return_value=[created, temp]), \
                mock.patch.dict(W._role, deliverable="Machine", owned={"OBJECT:Existing key"}):
            own = W.own_objects(scene)
            self.assertEqual(own, [root, child, created, owned])
            self.assertEqual(W._default_targets(scene, "modeling", own), [child, created, owned])

    def capture(self, folder, objects, own, angle=None):
        scene = types.SimpleNamespace(objects=objects, camera=object())
        cameras = []

        def new_camera(name, data):
            camera = types.SimpleNamespace(name=name, data=data)
            cameras.append(camera)
            return camera

        scene.collection = types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda camera: None))
        blender = types.SimpleNamespace(
            context=types.SimpleNamespace(scene=scene, evaluated_depsgraph_get=lambda: "graph",
                                          view_layer=types.SimpleNamespace(update=lambda: None)),
            data=types.SimpleNamespace(cameras=types.SimpleNamespace(new=lambda name: types.SimpleNamespace()),
                                       objects=types.SimpleNamespace(new=new_camera)))
        restore = mock.MagicMock()
        restore.__enter__.return_value = restore
        restore.keep.side_effect = lambda obj: obj
        with mock.patch.object(W, "bpy", blender), \
                mock.patch.dict(sys.modules, mathutils=types.SimpleNamespace(Vector=tuple)), \
                mock.patch.dict(W._role, role="modeling", folder=Path(folder), width=1024, count=1, cameras=[]), \
                mock.patch.object(W, "own_objects", return_value=own), \
                mock.patch.object(W, "repair_meshes", return_value=[]), \
                mock.patch.object(W, "_Restore", return_value=restore), \
                mock.patch.object(W, "_output", return_value=False), \
                mock.patch.object(W, "_shade", return_value=(None, "clay")), \
                mock.patch.object(W, "_provenance", return_value="clay"), \
                mock.patch.object(W, "_hidden_by", return_value=(0, [])), \
                mock.patch.object(W, "_text", return_value="Image attached: deliverable"), \
                mock.patch.object(W, "_left_out", return_value=""), \
                mock.patch.object(W, "_compose") as compose, mock.patch.object(W, "_render") as render:
            try:
                shot = W._look_in(angle, None, None, None, None, False, 1, "", False, (), (), False, None, None)
            except W.ToolError:
                render.assert_not_called()
                compose.assert_not_called()
                raise
        return shot, cameras, compose.call_args.args[1], render

    def test_preview_fits_evaluated_owned_bounds_without_floor_camera_or_empty_mesh(self):
        body = self.obj("Machine", 1, offset=(8, -3, 0.1))
        empty = self.obj("Unbuilt part", 2, offset=(-100, 100, 0), size=(0, 0, 0))
        floor = self.obj("Private scene floor", 3, size=(200, 200, 0))
        neighbour = self.obj("Other deliverable", 4, offset=(200, 0, 0))
        # Original bounds differ: the evaluated (modified) geometry must determine the view.
        evaluated = self.obj("Evaluated machine", 5, offset=(8, -3, 0.1), size=(0.5, 0.4, 0.2))
        body.evaluated_get.return_value = evaluated
        with tempfile.TemporaryDirectory() as folder:
            shot, cameras, tiles, render = self.capture(folder, [body, empty, floor, neighbour], [body, empty])
        self.assertIsNotNone(shot.image_path)
        self.assertEqual(render.call_count, 3)
        self.assertEqual([t.view for t in tiles], list(W.DETAIL_ANGLES))
        for camera, tile in zip(cameras, tiles):
            basis = W.framing.euler_basis(W.VIEWS[tile.view])
            frame = W.framing.frame(tile.rect[2:], lens=camera.data.lens, sensor_width=camera.data.sensor_width)
            projected = [W.framing.project(evaluated.matrix_world @ p, camera.location, basis, frame)
                         for p in evaluated.bound_box]
            spans = [max(p[i] for p in projected) - min(p[i] for p in projected) for i in (0, 1)]
            self.assertAlmostEqual(max(spans), 1 - 2 * W.MARGIN)
            self.assertTrue(all(W.MARGIN - 1e-8 <= p[i] <= 1 - W.MARGIN + 1e-8
                                for p in projected for i in (0, 1)))

    def test_nothing_built_does_not_render_the_private_scene_or_saved_camera(self):
        floor = self.obj("Private scene floor", 1, size=(200, 200, 0))
        root = self.obj("Machine", 2, kind="EMPTY")
        empty = self.obj("Unbuilt mesh", 3, size=(0, 0, 0))
        with tempfile.TemporaryDirectory() as folder:
            for own in ([], [root], [empty]):
                with self.subTest(own=own), self.assertRaises(W.ToolError):
                    self.capture(folder, [floor, *own], own)
            for angle in ("final", "camera"):
                for own in ([], [empty]):
                    with self.subTest(angle=angle, own=own), self.assertRaises(W.ToolError):
                        self.capture(folder, [floor, *own], own, angle)

    def test_invalid_world_bounds_skip_before_fitting_or_rendering(self):
        class Scale:
            def __init__(self, factor):
                self.factor = factor

            def __matmul__(self, point):
                return tuple(v * self.factor for v in point)

        for size, factor in ((1e200, 1e200), (1, float("nan")), (1, 1e30), (1, 0)):
            with self.subTest(size=size, factor=factor), tempfile.TemporaryDirectory() as folder:
                body = self.obj("Machine", 1, size=(size,) * 3)
                body.matrix_world = Scale(factor)
                with mock.patch.object(W.framing, "fit", wraps=W.framing.fit) as fit:
                    with self.assertRaisesRegex(W.ToolError, "world bounds|Nothing visible"):
                        self.capture(folder, [body], [body])
                    fit.assert_not_called()

    def test_small_geometry_is_inside_camera_clip_planes(self):
        for size in (1e-4, 1e-5, 1.0):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as folder:
                body = self.obj("Tiny part", 1, size=(size,) * 3)
                _, cameras, tiles, _ = self.capture(folder, [body], [body])
                for camera, tile in zip(cameras, tiles):
                    basis = W.framing.euler_basis(W.VIEWS[tile.view])
                    frame = W.framing.frame(tile.rect[2:], lens=camera.data.lens,
                                            sensor_width=camera.data.sensor_width)
                    coverage = W.framing.coverage(body.bound_box, camera.location, basis, frame,
                                                  clip=(camera.data.clip_start, camera.data.clip_end))
                    self.assertEqual(coverage["clips"], [], tile.view)
                    self.assertEqual(coverage["inside"], 1.0)
                    self.assertGreaterEqual(camera.data.clip_start, 1e-6)

    def test_geometry_below_supported_clip_scale_is_clearly_skipped(self):
        body = self.obj("Microscopic part", 1, size=(1e-8,) * 3)
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(W.ToolError, "too small"):
            self.capture(folder, [body], [body])

    def test_materials_prefer_owned_objects_to_unrelated_material_users(self):
        own, other = self.obj("My part", 1), self.obj("Other part", 2)
        other.material_slots = [types.SimpleNamespace(material=object())]
        scene = types.SimpleNamespace(objects=[own, other])
        with mock.patch.object(W, "_mine", return_value=True):
            self.assertEqual(W._default_targets(scene, "materials", [own]), [own])
            # Material-only ownership can still frame the objects wearing that material.
            self.assertEqual(W._default_targets(scene, "materials", []), [other])

    def test_scene_subjects_ignore_empty_mesh_bounds(self):
        empty = self.obj("Empty mesh", 1, size=(0, 0, 0))
        built = self.obj("Built mesh", 2)
        scene = types.SimpleNamespace(objects=[empty, built])
        blender = types.SimpleNamespace(context=types.SimpleNamespace(evaluated_depsgraph_get=lambda: "graph"))
        with mock.patch.object(W, "bpy", blender), mock.patch.dict(W._role, subjects=[]), \
                mock.patch.dict(sys.modules, mathutils=types.SimpleNamespace(Vector=tuple)):
            self.assertEqual(W._subjects(scene), [built])


class ShadingTest(unittest.TestCase):
    """capture_viewport's angle and shading are independent (GPT-6.1 Sol's exit interviews: captures
    did not combine the angle and shading a worker needed)."""

    def test_shading_is_one_name_or_a_list(self):
        self.assertEqual(W.shadings_of("albedo"), ("albedo",))
        self.assertEqual(W.shadings_of(["studio", "albedo", "studio"]), ("studio", "albedo"))
        self.assertEqual(W.shadings_of(None), ())
        with self.assertRaises(W.ToolError):
            W.shadings_of(["studio", "lookdev"])
        self.assertEqual(W.angles_of(["front", "back", "front"]), ("front", "back"))
        for bad in ("sheet", "final", "crops"):
            with self.assertRaises(W.ToolError):
                W.angles_of([bad])

    def test_a_panel_takes_the_asked_shading_else_the_final_or_the_roles(self):
        self.assertEqual(W.shading_for("modeling", "role", "albedo"), "albedo")
        self.assertEqual(W.shading_for("modeling", "final"), "scene")
        self.assertEqual({r: W.shading_for(r, "role") for r in W.ROLE_SHADING},
                         {"modeling": "clay", "materials": "studio", "lighting": "scene", "camera": "solid",
                          "animation": "solid"})
        self.assertEqual(W.plan("materials", "back", True, True), ("role", "single", ("back",)))

    def test_every_view_in_every_shading_side_by_side_at_most_six(self):
        tiles = [W._Tile("", "front"), W._Tile("", "back")]
        panels, dropped = W._panels(tiles, ["studio", "albedo"])
        self.assertEqual([(p.view, p.shading, p.label) for p in panels],
                         [("front", "studio", "front - studio"), ("front", "albedo", "front - albedo"),
                          ("back", "studio", "back - studio"), ("back", "albedo", "back - albedo")])
        self.assertEqual(dropped, 0)
        self.assertEqual(tiles[0].shading, "", "the planned tiles are left as they were")
        panels, dropped = W._panels([W._Tile(f"frame {f}", "camera", f) for f in range(4)], ["clay", "scene"])
        self.assertEqual((len(panels), dropped), (W.MAX_PANELS, 2))
        self.assertEqual(W._panels([W._Tile("front", "front")], ["clay"])[0][0].label, "front", "one shading: no suffix")

    def test_the_studio_light_shows_a_face_toward_the_view_near_its_albedo(self):
        """Arcane run W0025-W0044: studio.exr fixed in world space lit a cream label facing the front view at
        0.07-0.15 of its value, bluish, and workers repainted for an hour. Measured: a white world of strength w
        adds w to a white diffuse face, a sun of strength s adds s / pi (_studio sets s = STUDIO_KEY * pi)."""
        self.assertAlmostEqual(W.STUDIO_AMBIENT + W.STUDIO_KEY, 1.0, msg="a face toward the key shows its albedo")
        toward_view = W.KEY_FROM[2] / sum(v * v for v in W.KEY_FROM) ** 0.5
        self.assertTrue(0.85 <= W.STUDIO_AMBIENT + W.STUDIO_KEY * toward_view <= 0.95)
        self.assertGreater(W.KEY_FROM[1], 0, "from above")
        self.assertLess(W.KEY_FROM[0], 0, "from the left")


class WarningTest(unittest.TestCase):
    @staticmethod
    def panel(label, coverage=0.4, uniform=False, signature=(10.0, 200.0, 30.0), hidden=(0.0, [])):
        return {"label": label, "coverage": coverage, "uniform": uniform, "signature": list(signature),
                "hidden": hidden}

    def test_blank_panels(self):
        warnings = W.panel_warnings([self.panel("side", coverage=0.001, signature=(1, 2, 3)),
                                     self.panel("top", coverage=None, uniform=True, signature=(9, 9, 9)),
                                     self.panel("front", coverage=None)])
        self.assertEqual(warnings, ["side is blank: nothing of the subject is in view.",
                                    "top is blank: nothing of the subject is in view."])
        self.assertEqual(W.panel_warnings([self.panel("", uniform=True)]),
                         ["the image is blank: nothing of the subject is in view."])

    def test_a_panel_showing_the_same_picture_as_an_earlier_one(self):
        warnings = W.panel_warnings([self.panel("front"), self.panel("back", signature=(11.0, 201.0, 31.0)),
                                     self.panel("side", signature=(90.0, 20.0, 30.0))])
        self.assertEqual(warnings, ["back shows the same picture as front."])

    def test_something_in_front_of_the_subject_is_named_with_the_cure(self):
        warnings = W.panel_warnings([self.panel("front", hidden=(0.62, ["TreasureChest"])),
                                     self.panel("side", signature=(1, 1, 1), hidden=(0.1, ["Shelf"]))])
        self.assertEqual(warnings, ["In front, TreasureChest hides 62 % of the subject: isolate shows it alone."])


class ReviewTest(unittest.TestCase):
    """Angle crops, the camera-scale review: what one object's line says."""

    def test_size_on_screen_and_what_cuts_it(self):
        line = W.review_row("Bottle", [0.40, 0.30, 0.48, 0.50], 0.30, (1920, 1080), 11191,
                            {"mean": 0.64, "spread": 0.15, "saturation": 0.33, "clipped": 0.0, "dark": 0.0})
        self.assertEqual(line, "Bottle: 154x216 px on screen, about 7.2 px per cm; whole in frame; 11,191 px visible; "
                               "brightness 0.64 (spread 0.15), saturation 0.33, clipped 0.0 %, near black 0 %.")
        line = W.review_row("Shelf", [-0.1, -0.2, 0.3, 0.9], 2.0, (1920, 1080), 0, None, (0.8, ["Chest"]))
        self.assertIn("cut by the frame at the left and bottom; 80 % hidden behind Chest; no pixel", line)
        self.assertEqual(W.review_row("Sign", None, 1.0, (1920, 1080), 0, None), "Sign: not in view (behind the camera).")
        self.assertEqual(W.review_row("Sign", [1.2, 0.1, 1.4, 0.3], 1.0, (1920, 1080), 0, None),
                         "Sign: outside the frame.")

    def test_object_colours_stay_apart_once_8_bit(self):
        """_id_render draws each object in a flat colour and _id_mask reads it back within 24 of 255."""
        colours = W._id_colours()
        self.assertGreaterEqual(len(colours), W.MAX_CROPS)
        self.assertNotIn((0.0, 0.0, 0.0), colours, "black is everything else")

        def encoded(c):
            return [round(255 * (v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)) for v in c]
        codes = [encoded(c) for c in colours] + [[0, 0, 0]]
        for i, a in enumerate(codes):
            for b in codes[i + 1:]:
                self.assertGreater(max(abs(x - y) for x, y in zip(a, b)), 48, (a, b))

    def test_crops_pack_in_rows_or_not_at_all(self):
        def crop(w, h):
            return types.SimpleNamespace(shape=(h, w, 3))
        placed = W._shelves([(1, crop(600, 100)), (2, crop(500, 80)), (3, crop(300, 50))], 1024, 400, 4)
        self.assertEqual([(n, x, y) for n, _c, x, y in placed], [(1, 0, 0), (2, 0, 104), (3, 504, 104)])
        self.assertIsNone(W._shelves([(1, crop(600, 300)), (2, crop(600, 300))], 1024, 400, 4))
        self.assertIsNone(W._shelves([(1, crop(1100, 10))], 1024, 400, 4))


class LayoutTest(unittest.TestCase):
    def test_single_tiles_are_1024_on_their_long_side(self):
        self.assertEqual(W.layout("single", 1, 1024, 0.75), ((1024, 768), [(0, 0, 1024, 768)]))
        self.assertEqual(W.layout("single", 1, 1024, 1080 / 1920), ((1024, 576), [(0, 0, 1024, 576)]))
        self.assertEqual(W.layout("single", 1, 1024, 1920 / 1080), ((576, 1024), [(0, 0, 576, 1024)]), "portrait")

    def test_grids(self):
        size, rects = W.layout("grid", 4, 1024, 0.75)
        self.assertEqual(size, (1024, 768))
        self.assertEqual(rects, [(0, 0, 512, 384), (512, 0, 512, 384), (0, 384, 512, 384), (512, 384, 512, 384)])
        size, rects = W.layout("grid", 6, 1024, 0.75)
        self.assertEqual((size, len(rects), rects[-1]), ((1023, 512), 6, (682, 256, 341, 256)))
        size, rects = W.layout("grid", 3, 1024, 0.75)
        self.assertEqual((size, len(rects)), ((1024, 768), 3), "a row of two and one below")

    def test_detail_is_a_large_square_and_two_beside_it(self):
        size, rects = W.layout("detail", 3, 1024, 0.75)
        self.assertEqual(size, (1024, 672))
        self.assertEqual(rects, [(0, 0, 672, 672), (672, 0, 352, 336), (672, 336, 352, 336)])
        for x, y, w, h in rects:
            self.assertLessEqual(x + w, size[0])
            self.assertLessEqual(y + h, size[1])


class BackdropTest(unittest.TestCase):
    @staticmethod
    def box(centre, size):
        return (tuple(c - s / 2 for c, s in zip(centre, size)), tuple(c + s / 2 for c, s in zip(centre, size)))

    def test_a_studio_floor_under_a_small_product_is_left_out(self):
        boxes = {"Floor": self.box((0, 0, 0), (10, 10, 0.02)), "Bottle": self.box((0, 0, 0.1), (0.08, 0.08, 0.2)),
                 "Cap": self.box((0, 0, 0.22), (0.03, 0.03, 0.04))}
        self.assertEqual(W.backdrops(boxes), {"Floor"})

    def test_a_cove_or_room_shell_around_everything_is_left_out(self):
        boxes = {"Cove": self.box((0, 0, 1.5), (6, 4, 3)), "Watch": self.box((0, 0, 0.9), (0.05, 0.05, 0.02)),
                 "Stand": self.box((0, 0, 0.45), (0.4, 0.4, 0.9))}
        self.assertEqual(W.backdrops(boxes), {"Cove"})

    def test_a_rug_or_wall_in_a_furnished_room_stays(self):
        boxes = {"Rug": self.box((0, 0, 0.01), (2, 1.4, 0.015)), "Table": self.box((0, 0, 0.36), (0.9, 0.6, 0.72)),
                 "Wall": self.box((0, 2, 1.25), (4, 0.1, 2.5)), "Sofa": self.box((0, 1.2, 0.4), (2.1, 0.9, 0.8))}
        self.assertEqual(W.backdrops(boxes), set())

    def test_never_everything(self):
        self.assertEqual(W.backdrops({"Plane": self.box((0, 0, 0), (10, 10, 0))}), set())
        self.assertEqual(W.backdrops({}), set())


class BriefTest(unittest.TestCase):
    def test_subjects_are_the_objects_the_checks_name(self):
        checks = [{"kind": "in_frame", "camera": "Cam", "names": ["Bottle", "Cap"]},
                  {"kind": "rests_on", "name": "Bottle", "target": "Plinth"},
                  {"kind": "no_overlap", "a": "Bottle", "b": "Box"}, {"kind": "light", "light": "Key"}]
        self.assertEqual(W.subject_names(checks), ["Bottle", "Cap", "Plinth", "Box"])

    def test_configure_keeps_the_brief(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            W.configure("lighting", Path(folder) / "captures", brief={
                "deliverable": "Rim", "prefix": "Rim", "owned": ["LIGHT:Key"],
                "checks": [{"kind": "in_frame", "camera": "Cam_Hero", "names": ["Bottle"]}]})
            self.assertEqual((W._role["cameras"], W._role["subjects"], W._role["owned"], W._role["width"]),
                             (["Cam_Hero"], ["Bottle"], {"LIGHT:Key"}, 1024))
            with self.assertRaises(W.ToolError):
                W.configure("final", Path(folder))


class SignalTest(unittest.TestCase):
    def test_exposure_line(self):
        text = W.signal_text({"mean": 0.31, "subject": 0.52, "background": 0.24, "clipped": 0.004, "dark": 0.12,
                              "hue": 210, "saturation": 0.18})
        self.assertEqual(text, "Exposure (mean display brightness 0-1): subject 0.52, frame 0.31, around the subject "
                               "0.24, clipped 0.4 %, near black 12 %, tint hue 210 deg, saturation 0.18.")
        self.assertNotIn("subject", W.signal_text({"mean": 0.5, "clipped": 0.0, "dark": 0.0}))



class ReferenceTest(unittest.TestCase):
    """A brief's reference beside a look: one small tile, never taller than the look, and only a real file."""

    def test_the_tile_keeps_the_images_shape_at_a_small_size(self):
        self.assertEqual(W.reference_size((1920, 1080)), (W.REFERENCE_SIDE, 180))
        self.assertEqual(W.reference_size((768, 1024)), (240, W.REFERENCE_SIDE))
        self.assertEqual(W.reference_size((768, 1024), max_height=200), (150, 200), "no taller than the look")
        self.assertEqual(W.reference_size((1920, 1080), 384, 288), (384, 216), "a contact sheet cell")
        self.assertEqual(W.reference_size((1080, 1920), 384, 288), (162, 288))

    def test_configure_keeps_a_reference_only_when_its_file_exists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "ref.png"
            image.write_bytes(b"png")
            W.configure("materials", Path(folder) / "looks", brief={"reference": {"path": str(image), "name": "ref1.jpg"}})
            self.assertEqual(W._role["reference"], {"path": image, "name": "ref1.jpg"})
            W.configure("materials", Path(folder) / "looks", brief={"reference": {"path": str(image) + "x", "name": "x"}})
            self.assertIsNone(W._role["reference"])
            W.configure("materials", Path(folder) / "looks", brief={})
            self.assertIsNone(W._role["reference"])

class LabelsTest(unittest.TestCase):
    """Object names on a worker's look only when asked: on a close shot they covered what was being judged."""

    def labels(self, **kwargs):
        blender = types.SimpleNamespace(context=types.SimpleNamespace(scene=None))
        with mock.patch.object(W, "_look", return_value="shot") as look, mock.patch.object(W, "bpy", blender), \
                mock.patch.object(W.view_layers, "production"), \
                mock.patch.dict(W._role, {"role": "modeling", "count": 0, "reference": None}):
            self.assertEqual(W.look(**kwargs), "shot")
        return look.call_args.kwargs["labels"]

    def test_names_only_when_asked(self):
        self.assertIs(self.labels(), False)
        self.assertIs(self.labels(angle="camera"), False)
        self.assertIs(self.labels(angles=["front", "top"]), False)
        self.assertIs(self.labels(labels=True), True)
        with self.assertRaisesRegex(W.ToolError, "labels must be true or false"):
            W.look(labels="yes")


if __name__ == "__main__":
    unittest.main()
