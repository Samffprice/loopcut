"""Panel host preservation with fake windows, areas, and context overrides."""

import ast
import contextlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ADDONS = Path(__file__).resolve().parents[2] / "scripts" / "addons_core"
sys.path.insert(0, str(ADDONS))
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import config, contact, cost_guard, live_file, op_notes, scene_diff, shape_key_guard, tools  # noqa: E402
from loopcut import ui  # noqa: E402

N = types.SimpleNamespace
NOTICE = "Loopcut panel's area was restored; show previews in another editor."


class Area:
    def __init__(self, kind, width=400):
        self._type = self.ui_type = kind
        self.width, self.height = width, 600
        self.regions = [N(type="HEADER"), N(type="WINDOW")]
        self.tag_redraw = mock.Mock()
        self.type_changes = []

    def as_pointer(self):
        return id(self)

    @property
    def type(self):
        return self._type

    @type.setter
    def type(self, value):
        self.type_changes.append(value)
        self._type = self.ui_type = value  # Changing the editor resets its UI subtype.


class HostAreaGuardTest(unittest.TestCase):
    def setUp(self):
        self.panel, self.regular = Area("TEXT_EDITOR", width=1200), Area("VIEW_3D")
        self.window = N(screen=N(areas=[self.panel, self.regular]))
        self.context = N(window_manager=N(windows=[self.window]), temp_override=self.temp_override)
        enums = [N(identifier=kind) for kind in ("TEXT_EDITOR", "VIEW_3D", "IMAGE_EDITOR", "CONSOLE")]
        self.bpy = N(app=N(background=False), context=self.context,
                     types=N(Area=N(bl_rna=N(properties={"type": N(enum_items=enums)}))),
                     ops=N(ed=N(undo_push=mock.Mock())))
        self.enterContext(mock.patch.object(tools, "bpy", self.bpy))
        self.enterContext(mock.patch.object(tools, "_python_namespace", side_effect=lambda: {"bpy": self.bpy}))
        # Use the real registry functions without importing the drawing and operator machinery.
        self.host = types.ModuleType("loopcut.ui.host")
        self.host.NATIVE, self.host.HOST_UI_TYPE = False, "TEXT_EDITOR"
        self.host.state = N(hosts={self.panel.as_pointer()})
        source = ADDONS / "loopcut" / "ui" / "host.py"
        nodes = [node for node in ast.parse(source.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name in {"hosts", "is_host"}]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), vars(self.host))
        self.enterContext(mock.patch.object(ui, "host", self.host, create=True))
        self.enterContext(mock.patch.dict(sys.modules, {"loopcut.ui.host": self.host}))
        for module, name, result in (
            (config, "run_timeout", 60), (shape_key_guard, "install", None),
            (shape_key_guard, "check", None), (shape_key_guard, "take", []),
            (cost_guard, "begin", None), (cost_guard, "take", ("", [])),
            (op_notes, "take", []), (live_file, "take", []),
            (scene_diff, "snapshot", {}), (scene_diff, "diff", {"other": []}),
            (scene_diff, "for_model", "Scene changes: none"), (scene_diff, "is_empty", True),
            (contact, "recheck", []),
        ):
            self.enterContext(mock.patch.object(module, name, return_value=result))
        for module in (cost_guard, op_notes, live_file):
            self.enterContext(mock.patch.object(module, "watching_ops", side_effect=contextlib.nullcontext))

    @contextlib.contextmanager
    def temp_override(self, **kwargs):
        missing = object()
        saved = {key: getattr(self.context, key, missing) for key in kwargs}
        vars(self.context).update(kwargs)
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is missing:
                    delattr(self.context, key)
                else:
                    setattr(self.context, key, value)

    def assert_restored(self, result, area=None, kind="TEXT_EDITOR", ui_type=None):
        area = area or self.panel
        self.assertEqual((area.type, area.ui_type), (kind, ui_type or kind))
        self.assertTrue(self.host.is_host(area))
        area.tag_redraw.assert_called_once_with()
        self.assertEqual(result.text.count(NOTICE), 1)

    def test_run_restores_host_after_success(self):
        result = tools.run_python("bpy.context.window.screen.areas[0].type = 'IMAGE_EDITOR'\nprint('done')")
        self.assertTrue(result.ok)
        self.assertIn("done", result.text)
        self.assert_restored(result)

    def test_run_restores_host_after_error(self):
        result = tools.run_python("bpy.context.window.screen.areas[0].type = 'IMAGE_EDITOR'\n"
                                  "raise ValueError('failed')")
        self.assertFalse(result.ok)
        self.assertIn("ValueError: failed", result.text)
        self.assert_restored(result)

    def test_run_restores_host_after_timeout(self):
        clock = N(now=0)
        self.bpy.expire = lambda: setattr(clock, "now", 61)
        with mock.patch.object(tools, "time", N(monotonic=lambda: clock.now)):
            result = tools.run_python("bpy.context.window.screen.areas[0].type = 'IMAGE_EDITOR'\n"
                                      "bpy.expire()\npass")
        self.assertFalse(result.ok)
        self.assertIn("Stopped: the code ran", result.text)
        self.assert_restored(result)

    def test_run_restores_all_windows_and_registration(self):
        other = Area("TEXT_EDITOR")
        self.context.window_manager.windows.append(N(screen=N(areas=[other])))
        self.host.hosts().add(other.as_pointer())
        self.bpy.prune_hosts = self.host.hosts().clear
        result = tools.run_python("for window in bpy.context.window_manager.windows:\n"
                                  "    window.screen.areas[0].type = 'IMAGE_EDITOR'\n"
                                  "bpy.prune_hosts()")
        self.assert_restored(result)
        self.assert_restored(result, other)

    def test_run_restores_ui_type_only_change(self):
        result = tools.run_python("bpy.context.window.screen.areas[0].ui_type = 'UV'")
        self.assert_restored(result)

    def test_run_restores_native_host(self):
        self.host.NATIVE, self.host.HOST_UI_TYPE = True, "LOOPCUT"
        self.panel.type = "LOOPCUT"
        self.host.hosts().clear()
        result = tools.run_python("bpy.context.window.screen.areas[0].type = 'IMAGE_EDITOR'")
        self.assert_restored(result, kind="LOOPCUT")
        self.assertEqual(self.host.hosts(), set())

    def test_run_unchanged_host_is_quiet(self):
        result = tools.run_python("print('done')")
        self.assertTrue(result.ok)
        self.assertIn("done", result.text)
        self.assertNotIn(NOTICE, result.text)
        self.assertEqual(self.panel.type_changes, [])
        self.panel.tag_redraw.assert_not_called()

    def test_run_without_hosts_is_quiet(self):
        self.host.hosts().clear()
        result = tools.run_python("bpy.context.window.screen.areas[0].type = 'IMAGE_EDITOR'")
        self.assertTrue(result.ok)
        self.assertEqual(self.panel.type, "IMAGE_EDITOR")
        self.assertNotIn(NOTICE, result.text)
        self.panel.tag_redraw.assert_not_called()

    def test_background_run_does_not_inspect_hosts(self):
        self.bpy.app.background = True
        with mock.patch.object(self.host, "is_host", side_effect=AssertionError("UI accessed")):
            result = tools.run_python("bpy.context.window.screen.areas[0].type = 'IMAGE_EDITOR'")
        self.assertTrue(result.ok)
        self.assertEqual(self.panel.type, "IMAGE_EDITOR")
        self.assertNotIn(NOTICE, result.text)

    def test_editor_skips_existing_host(self):
        self.regular.type = "TEXT_EDITOR"
        with tools._editor_context("TEXT_EDITOR"):
            self.assertIs(self.context.area, self.regular)
            self.assertIs(self.context.region, self.regular.regions[1])
        self.assertEqual(self.panel.type_changes, [])

    def test_editor_borrows_only_regular_area(self):
        self.regular.type = "IMAGE_EDITOR"
        self.regular.ui_type = "UV"
        with tools._editor_context("CONSOLE"):
            self.assertIs(self.context.area, self.regular)
            self.assertEqual(self.regular.type, "CONSOLE")
            self.assertEqual(self.panel.type, "TEXT_EDITOR")
        self.assertEqual((self.regular.type, self.regular.ui_type), ("IMAGE_EDITOR", "UV"))
        self.assertEqual(self.panel.type_changes, [])

    def test_editor_host_only_raises(self):
        self.window.screen.areas = [self.panel]
        for editor in ("TEXT_EDITOR", "IMAGE_EDITOR"):
            with self.subTest(editor=editor), self.assertRaisesRegex(tools.ToolError, "No editor area"):
                with tools._editor_context(editor):
                    pass
        self.assertEqual(self.panel.type_changes, [])


if __name__ == "__main__":
    unittest.main()
