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
            objects=[types.SimpleNamespace(name="Cube", type="MESH", visible_get=lambda: True)],
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
        self.enterContext(mock.patch.object(tools, "_framed_view", return_value=(None, None, mock.Mock())))
        self.enterContext(mock.patch.object(tools, "_labels", return_value=[]))
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


if __name__ == "__main__":
    unittest.main()
