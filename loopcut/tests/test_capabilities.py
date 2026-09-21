"""Dynamic engine membership must not be inferred from RNA's single static placeholder."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut import capabilities


class CapabilitiesTest(unittest.TestCase):
    def test_probe_leaves_the_actual_engine_unchanged(self):
        class Render:
            @property
            def engine(self):
                return "CYCLES"
            @engine.setter
            def engine(self, value):
                raise TypeError("bpy_struct: item.attr = val: enum \"__LOOPCUT_ENUM_PROBE__\" "
                                "not found in ('BLENDER_EEVEE', 'BLENDER_WORKBENCH', 'CYCLES')")
        render = Render()
        self.assertEqual(capabilities.render_engines(render), ["BLENDER_EEVEE", "BLENDER_WORKBENCH", "CYCLES"])
        self.assertEqual(render.engine, "CYCLES")

    def test_unexpected_api_error_is_not_misreported_as_no_cycles(self):
        class Render:
            def __setattr__(self, name, value):
                raise TypeError("API changed")
        with self.assertRaisesRegex(RuntimeError, "available render engines"):
            capabilities.render_engines(Render())


if __name__ == "__main__":
    unittest.main()
