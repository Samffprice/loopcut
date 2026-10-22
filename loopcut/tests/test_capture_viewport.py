"""Live captures with fake RNA and drawing; tools_check.py checks the actual GPU images."""

import contextlib
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

from loopcut import tools  # noqa: E402


ANGLES = ("three_quarter", "front", "side", "top", "camera", "sheet", "user")
CAPTIONS = {angle.replace("_", " ") for angle in tools.SHEET_ANGLES}  # A sheet tile's view, not an object.


class Shading:
    """The dynamic RNA enum: Workbench alone has no Material Preview."""

    def __init__(self, scene):
        self.scene = scene
        self.type, self.color_type, self.light = "SOLID", "OBJECT", "MATCAP"
        self.use_scene_lights, self.use_scene_world = True, True
        self.use_scene_lights_render, self.use_scene_world_render = False, False

    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, value):
        if value == "MATERIAL" and self.scene.render.engine == "BLENDER_WORKBENCH":
            raise TypeError('enum "MATERIAL" not found in (\'WIREFRAME\', \'SOLID\', \'RENDERED\')')
        self._type = value


class CaptureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.scene = types.SimpleNamespace(
            render=types.SimpleNamespace(engine="BLENDER_WORKBENCH", resolution_x=1920, resolution_y=1080),
            camera=mock.Mock(name="Camera"),
            objects=[types.SimpleNamespace(name="Cube", type="MESH", visible_get=lambda: True,
                                           bound_box=[(-1, -1, -1), (1, 1, 1)])],
            view_settings=types.SimpleNamespace(view_transform="AgX"))
        self.scene.camera.name = "Camera"
        self.shading = Shading(self.scene)
        self.space = types.SimpleNamespace(shading=self.shading, overlay=types.SimpleNamespace(show_overlays=True),
                                           region_3d=mock.Mock())
        override = {"region": types.SimpleNamespace(width=800, height=600),
                    "area": types.SimpleNamespace(spaces=types.SimpleNamespace(active=self.space))}
        pixels = types.SimpleNamespace(shape=(480, 640, 4))
        # No numpy/mathutils/GPU dependency: this suite checks state, not pixel math.
        self.enterContext(mock.patch.dict(sys.modules, {"numpy": types.SimpleNamespace(
            concatenate=mock.Mock(return_value=pixels))}))
        self.enterContext(mock.patch.object(tools, "bpy", types.SimpleNamespace(
            context=types.SimpleNamespace(scene=self.scene, evaluated_depsgraph_get=lambda: None))))
        self.enterContext(mock.patch.object(tools, "_editor_context", return_value=contextlib.nullcontext()))
        self.enterContext(mock.patch.object(tools, "_view3d_override", return_value=override))
        self.framed = self.enterContext(mock.patch.object(tools, "_framed_view", return_value=(None, None, mock.Mock())))
        self.labels = self.enterContext(mock.patch.object(tools, "_labels", return_value=[("Cube", 10.0, 20.0)]))
        self.enterContext(mock.patch.object(tools, "_nearest_first", return_value=""))
        self.enterContext(mock.patch.object(tools.scratch, "folder", return_value=self.folder))
        self.draw = self.enterContext(mock.patch.object(tools, "_draw_offscreen", return_value=pixels))
        self.save = self.enterContext(mock.patch.object(tools, "_save_pixels",
                                                       side_effect=lambda pixels, path: path.write_bytes(b"capture")))

    def state(self):
        shading = self.shading
        return (self.scene.render.engine, shading.type, shading.color_type, shading.light,
                self.space.overlay.show_overlays, shading.use_scene_lights, shading.use_scene_world,
                shading.use_scene_lights_render, shading.use_scene_world_render)

    def capture(self, engine, expected_engine, **kwargs):
        self.scene.render.engine = engine
        before = self.state()
        during = []
        self.draw.side_effect = lambda *args: during.append(self.state()) or self.draw.return_value
        result = tools.capture_viewport(**kwargs)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.image_path.read_bytes(), b"capture")
        self.assertEqual(self.state(), before, "every changed setting must be restored")
        self.assertTrue(during, "the capture must draw")
        for state in during:
            self.assertEqual(state, (expected_engine, "MATERIAL", "OBJECT", "MATCAP", False,
                                     False, False, False, False))

    def test_workbench_default_material_uses_eevee_and_restores(self):
        self.capture("BLENDER_WORKBENCH", "BLENDER_EEVEE")

    def test_workbench_material_camera_and_sheet_use_eevee_and_restore(self):
        for angle in ("front", "camera", "sheet"):
            with self.subTest(angle=angle):
                self.capture("BLENDER_WORKBENCH", "BLENDER_EEVEE", angle=angle)

    def test_material_supported_engines_are_unchanged(self):
        # The RNA rule also offers MATERIAL for third-party engines, without a capability flag.
        for engine in ("BLENDER_EEVEE", "CYCLES", "CUSTOM_ENGINE"):
            with self.subTest(engine=engine):
                self.capture(engine, engine)

    def test_parent_focus_frames_visible_geometry_at_every_sheet_angle(self):
        cube = self.scene.objects[0]
        hidden = types.SimpleNamespace(name="Hidden", type="MESH", visible_get=lambda: False,
                                       bound_box=[(-100, -100, -100), (100, 100, 100)])
        nested = types.SimpleNamespace(name="Nested", type="EMPTY", visible_get=lambda: True,
                                       children_recursive=[cube, hidden])
        parent = types.SimpleNamespace(name="Group", type="EMPTY", visible_get=lambda: True,
                                       children_recursive=[nested, cube, hidden])
        self.scene.objects.extend([parent, nested, hidden])
        tools.bpy.data = types.SimpleNamespace(objects={o.name: o for o in self.scene.objects})
        for angle in ("front", "sheet"):
            with self.subTest(angle=angle):
                self.framed.reset_mock()
                self.look(focus=[parent.name, cube.name], angle=angle)
                # A sheet also computes the three-quarter eye for the nearest-first list.
                self.assertEqual(self.framed.call_count, 5 if angle == "sheet" else 1)
                for call in self.framed.call_args_list:
                    self.assertEqual(call.args[1], [cube])

    def test_geometry_free_mesh_and_hidden_only_parent(self):
        cube = self.scene.objects[0]
        parent = types.SimpleNamespace(name="Group", type="MESH", bound_box=[(-1, -1, -1)] * 8,
                                       children_recursive=[cube])
        tools.bpy.data = types.SimpleNamespace(objects={parent.name: parent})
        self.look(focus=[parent.name])
        self.assertEqual(self.framed.call_args.args[1], [cube])
        cube.visible_get = lambda: False
        with self.assertRaisesRegex(tools.ToolError, "Nothing visible to frame"):
            self.look(focus=[parent.name])

    def test_geometry_target_keeps_its_own_bounds(self):
        cube = self.scene.objects[0]
        cube.children_recursive = [types.SimpleNamespace(name="Distant", type="MESH")]
        self.assertEqual(tools._framing_targets([cube]), [cube])

    def test_draw_exception_restores_engine_shading_overlays_and_lighting(self):
        for engine, shading in (("BLENDER_WORKBENCH", "SOLID"), ("BLENDER_WORKBENCH", "RENDERED"),
                                ("BLENDER_WORKBENCH", "WIREFRAME"), ("BLENDER_EEVEE", "MATERIAL"),
                                ("CYCLES", "MATERIAL")):
            for angle in ("front", "camera", "sheet"):
                with self.subTest(engine=engine, shading=shading, angle=angle):
                    self.scene.render.engine, self.shading.type = engine, shading
                    before = self.state()
                    error = RuntimeError("draw failed")
                    self.draw.side_effect = error
                    with self.assertRaises(RuntimeError) as raised:
                        tools.capture_viewport(angle=angle)
                    self.assertIs(raised.exception, error)
                    self.assertEqual(self.state(), before)
        self.save.assert_not_called()

    def test_save_exception_happens_after_restoring_state(self):
        before = self.state()
        self.save.side_effect = OSError("cannot write image")
        with self.assertRaisesRegex(OSError, "cannot write image"):
            tools.capture_viewport()
        self.assertEqual(self.state(), before)

    def test_workbench_distinct_and_user_looks_keep_their_engine(self):
        before = self.state()

        def draw(*args):
            self.assertEqual(self.scene.render.engine, "BLENDER_WORKBENCH")
            self.assertEqual((self.shading.type, self.shading.color_type, self.shading.light,
                              self.space.overlay.show_overlays), ("SOLID", "RANDOM", "STUDIO", False))
            return self.draw.return_value

        self.draw.side_effect = draw
        tools.capture_viewport(style="distinct")
        self.assertEqual(self.state(), before)
        self.space.overlay.show_overlays = False
        before = self.state()

        def draw_user(*args):
            self.assertEqual(self.state(), before)
            return self.draw.return_value

        self.draw.side_effect = draw_user
        tools.capture_viewport(angle="user")
        self.assertEqual(self.state(), before)

    # Object names are drawn only when asked: on a close shot through the camera they covered the hands
    # and pistol being pose-matched (2026-10-17), and the owner wants every look the model works from clean.
    def names(self):
        return [text for call in self.draw.call_args_list for text, _, _ in call.args[7] if text not in CAPTIONS]

    def look(self, **kwargs):
        self.draw.reset_mock()
        self.labels.reset_mock()
        result = tools.capture_viewport(**kwargs)
        self.assertTrue(result.ok, result.text)
        return result

    def test_no_capture_draws_names_unless_asked(self):
        for angle in ANGLES:
            for asked in ({}, {"labels": False}):
                with self.subTest(angle=angle, **asked):
                    result = self.look(angle=angle, **asked)
                    self.assertEqual(self.names(), [])
                    self.labels.assert_not_called()
                    self.assertNotIn("labelled", result.text)

    def test_labels_true_draws_names_and_says_so(self):
        for angle in ANGLES:
            with self.subTest(angle=angle):
                result = self.look(angle=angle, labels=True)
                self.assertEqual(self.names(), ["Cube"] * (4 if angle == "sheet" else 1))
                self.assertIn("; objects are labelled with their names", result.text)

    def test_sheet_tiles_keep_their_view_captions(self):
        for asked in (False, True):
            with self.subTest(labels=asked):
                self.look(angle="sheet", labels=asked)
                self.assertEqual([call.args[7][0][0] for call in self.draw.call_args_list],
                                 ["three quarter", "front", "side", "top"])

    def test_labels_must_be_true_or_false(self):
        with self.assertRaisesRegex(tools.ToolError, "labels must be true or false"):
            tools.capture_viewport(labels="yes")
        self.draw.assert_not_called()

    def test_the_tool_passes_labels_through(self):
        from loopcut import shaders
        with mock.patch.object(shaders, "before_look", side_effect=lambda fn, *args, **kwargs: fn()):
            self.assertNotIn("labelled", tools.capture_viewport_tool(angle="camera").text)
            self.assertIn("labelled", tools.capture_viewport_tool(angle="camera", labels=True).text)

    def compare(self, **kwargs):
        from loopcut import shaders, video_attach
        self.draw.reset_mock()
        reference = self.folder / "reference.png"
        fake_numpy = types.SimpleNamespace(ones=lambda *args, **kwargs: mock.MagicMock(), float32="float32")
        clip_frame = (reference, 34, "take.mp4 clip frame 94 (3.10 s)")
        self.scene.frame_current, self.scene.frame_set = 1, mock.Mock()
        with mock.patch.object(tools, "_reference", return_value=({"name": "pose.png"}, reference)), \
                mock.patch.object(tools, "_pixels", return_value=types.SimpleNamespace(shape=(480, 640, 4))), \
                mock.patch.object(video_attach, "frame_for", return_value=clip_frame), \
                mock.patch.object(shaders, "before_look", side_effect=lambda fn, *args, **kwargs: fn()), \
                mock.patch.dict(sys.modules, {"numpy": fake_numpy}):
            result = tools.compare_with_reference_tool("pose.png", **kwargs)
        self.assertTrue(result.ok, result.text)
        self.assertIn(" on the right, at the same height.", result.text)
        return result

    def test_compare_draws_no_names_unless_asked(self):
        for angle in ("three_quarter", "front", "side", "top", "camera", "user"):
            for extra in ({}, {"frame": 94}):
                with self.subTest(angle=angle, **extra):
                    result = self.compare(angle=angle, **extra)
                    self.assertEqual(self.names(), [])
                    self.assertNotIn("labelled", result.text)
                    result = self.compare(angle=angle, labels=True, **extra)
                    self.assertEqual(self.names(), ["Cube"])
                    self.assertIn("; objects are labelled with their names", result.text)
                    self.assertFalse(self.compare(angle=angle, labels=False, **extra).text.count("labelled"))


class LabelsSchemaTest(unittest.TestCase):
    def properties(self, schemas, name):
        return next(s for s in schemas if s["function"]["name"] == name)["function"]["parameters"]["properties"]

    def test_capture_and_compare_offer_labels_default_off(self):
        for name in ("capture_viewport", "compare_with_reference"):
            with self.subTest(name=name):
                labels = self.properties(tools.SCHEMAS, name)["labels"]
                self.assertEqual(labels["type"], "boolean")
                self.assertTrue(labels["description"].endswith("default off"), labels["description"])
        capture = next(s for s in tools.SCHEMAS if s["function"]["name"] == "capture_viewport")
        self.assertNotIn("names", capture["function"]["description"], "the description no longer promises names")

    def test_the_clip_variant_of_compare_keeps_labels(self):
        from loopcut import video_attach
        session = {"references": [{"name": "take.mp4", "video": {"id": "c1"}}]}
        with mock.patch.object(video_attach.state, "session", return_value=session):
            properties = self.properties(video_attach.with_clip_tools(tools.SCHEMAS), "compare_with_reference")
        self.assertEqual((properties["frame"]["type"], properties["labels"]["type"]), ("integer", "boolean"))

    def test_a_workers_capture_offers_labels_default_off(self):
        from loopcut import workers
        capture = next(s for s in workers.schemas() if s["function"]["name"] == "capture_viewport")["function"]
        self.assertEqual(capture["parameters"]["properties"]["labels"]["type"], "boolean")
        self.assertNotIn("names", capture["description"])

    def test_the_prompt_does_not_promise_names_on_the_automatic_look(self):
        from loopcut import agent
        self.assertNotIn("names drawn on", agent.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
