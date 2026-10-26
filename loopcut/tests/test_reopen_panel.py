"""Loopcut opens with every file: load_post docks the panel when the file's own layout has none.

The real lifecycle and host modules, loaded under a private package against a private fake bpy (as
test_onboarding does), with fake windows and areas. No Blender.
"""

import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fake_preferences import AddonPreferences

PACKAGE = Path(__file__).resolve().parents[2] / "scripts/addons_core/loopcut"
NAME = "_reopen_loopcut"
N = types.SimpleNamespace


class Timers:
    """bpy.app.timers: registered functions run only when the test says so, as Blender's next ticks."""
    def __init__(self):
        self.pending = []

    def register(self, fn, first_interval=0.0, persistent=False):
        self.pending.append(fn)

    def is_registered(self, fn):
        return fn in self.pending

    def unregister(self, fn):
        self.pending.remove(fn)

    def run(self):
        while self.pending:
            self.pending.pop(0)()


class Area:
    def __init__(self, kind):
        self.type = kind

    def as_pointer(self):
        return id(self)


class Window:
    def __init__(self, *kinds):
        self.screen = N(areas=[Area(kind) for kind in kinds])
        self.workspace = Mock()

    def as_pointer(self):
        return id(self)


class Layout:
    """A menu's or header's layout, recording what it draws."""
    def __init__(self):
        self.records = []

    def operator(self, name, text="", **icon):
        self.records.append((name, text, icon))

    def separator(self, **kwargs):
        self.records.append("separator")


class ReopenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = types.ModuleType("bpy")
        cls.fake.types = types.SimpleNamespace(**{name: type(name, (), {}) for name in
            ("Operator", "Menu", "FileHandler", "SpaceTextEditor", "OperatorFileListElement")})
        cls.fake.types.AddonPreferences = AddonPreferences
        cls.fake.props = Mock()
        cls.fake.ops = Mock()
        handlers = types.ModuleType("bpy.app.handlers")
        handlers.persistent = lambda fn: fn
        cls.fake.app = types.ModuleType("bpy.app")
        cls.fake.app.version, cls.fake.app.background, cls.fake.app.handlers = (5, 2, 0), False, handlers
        cls.fake.app.timers = Timers()
        cls.fake.context = types.SimpleNamespace()
        cls.fake.msgbus = Mock()
        cls.fake.utils = types.ModuleType("bpy.utils")
        cls.fake.utils.previews = types.ModuleType("bpy.utils.previews")
        cls.modules = patch.dict(sys.modules, {"bpy": cls.fake, "bpy.app": cls.fake.app, "bpy.app.handlers": handlers,
            "bpy.utils": cls.fake.utils, "bpy.utils.previews": cls.fake.utils.previews,
            NAME + ".ui.draw": types.ModuleType(NAME + ".ui.draw")})
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)  # Runs even when the imports below fail.
        spec = importlib.util.spec_from_file_location(NAME, PACKAGE / "__init__.py",
                                                     submodule_search_locations=[str(PACKAGE)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[NAME] = package
        spec.loader.exec_module(package)
        for name in ("lifecycle", "settings", "state", "ui.host"):
            setattr(cls, name.replace("ui.", ""), importlib.import_module(NAME + "." + name))

    def setUp(self):
        self.timers = Timers()
        self.prefs = N(dock_on_startup=True)
        self.window = Window("VIEW_3D", "PROPERTIES")
        self.fake.context.window_manager = N(windows=[self.window])
        self.docked = []

        def dock(window, then=None):  # As host.dock: the window has a panel coming from now on.
            self.docked.append(window)
            self.host._docking.add(window.as_pointer())
        for target, name, value in ((self.fake.app, "timers", self.timers), (self.fake.app, "background", False),
                                    (self.host, "NATIVE", True), (self.host, "HOST_UI_TYPE", "LOOPCUT"),
                                    (self.host, "_docking", set()), (self.host, "dock", dock),
                                    (self.settings, "preferences", lambda: self.prefs),
                                    (self.state, "restoring", False),
                                    (self.lifecycle, "resume_for_open_file", Mock()),
                                    (self.lifecycle, "_on_undo_redo", Mock())):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def open_file(self):
        self.lifecycle._on_load_post()
        self.timers.run()

    def test_a_file_opened_without_the_panel_gets_it_docked_once(self):
        self.lifecycle._on_load_post()
        self.lifecycle._on_load_post()  # Open Recent again before the next tick: still one timer.
        self.assertEqual(self.timers.pending, [self.lifecycle._dock_after_load])
        self.assertEqual(self.docked, [])  # Not in load_post itself: from a timer, as at startup.
        self.timers.run()
        self.assertEqual(self.docked, [self.window])
        self.open_file()  # That dock still in flight (host._docking): not a second one.
        self.assertEqual(self.docked, [self.window])
        self.host._docking.clear()
        self.window.screen.areas.append(Area("LOOPCUT"))  # It finished.
        self.open_file()
        self.assertEqual(self.docked, [self.window])

    def assert_docks_only_after(self, change):
        """No dock as set up; after `change()`, the next file open docks. Without the latter half a test that
        nothing happens would pass on code that never docks after a load."""
        self.open_file()
        self.assertEqual(self.docked, [])
        change()
        self.open_file()
        self.assertEqual(self.docked, [self.window])

    def test_a_checkpoint_restore_is_not_a_file_open(self):
        restoring = patch.object(self.state, "restoring", True)
        restoring.start()
        self.assert_docks_only_after(restoring.stop)
        self.lifecycle._on_undo_redo.assert_called_once_with()

    def test_with_the_preference_off_the_file_keeps_its_layout(self):
        self.prefs.dock_on_startup = False
        self.assert_docks_only_after(lambda: setattr(self.prefs, "dock_on_startup", True))

    def test_a_layout_saved_with_the_panel_is_left_as_is(self):
        self.window.screen.areas.append(Area("LOOPCUT"))
        self.assert_docks_only_after(self.window.screen.areas.pop)

    def test_a_panel_in_another_window_counts(self):
        windows = self.fake.context.window_manager.windows
        windows.append(Window("VIEW_3D", "LOOPCUT"))
        self.assert_docks_only_after(windows.pop)

    def test_stock_blender_and_background_runs_never_dock(self):
        for target, name, value in ((self.host, "NATIVE", False), (self.fake.app, "background", True)):
            with self.subTest(name=name):
                self.docked.clear()
                self.host._docking.clear()
                setting = patch.object(target, name, value)
                setting.start()
                self.assert_docks_only_after(setting.stop)

    def test_the_label_says_every_file(self):
        source = (PACKAGE / "settings.py").read_text()
        self.assertIn('name="Open Loopcut with every file"', source)
        self.assertIn("dock_on_startup: bpy.props.BoolProperty(", source)  # Same name: stored values hold.

    # The way back: "Open Loopcut" in the top bar while the window shows no panel, and in the View menu.

    def draw_topbar(self, alignment="RIGHT"):
        layout = Layout()
        self.host._draw_topbar(N(layout=layout), N(region=N(alignment=alignment), window=self.window))
        return layout.records

    def test_the_top_bar_offers_open_loopcut_only_while_the_window_has_no_panel(self):
        self.assertEqual(self.draw_topbar(), [("loopcut.focus", "Open Loopcut", {"icon": "LOOPCUT"})])
        self.assertEqual(self.draw_topbar("LEFT"), [])  # The menus' side: once is enough.
        self.host._docking.add(self.window.as_pointer())
        self.assertEqual(self.draw_topbar(), [])  # Clicked: the dock is on its way.
        self.host._docking.clear()
        self.window.screen.areas.append(Area("LOOPCUT"))
        self.assertEqual(self.draw_topbar(), [])
        with patch.object(self.host.account, "mark_icon", return_value=42):
            self.window.screen.areas.pop()
            self.assertEqual(self.draw_topbar(), [("loopcut.focus", "Open Loopcut", {"icon_value": 42})])

    def test_the_view_menu_starts_with_open_loopcut(self):
        layout = Layout()
        self.host._draw_view_menu(N(layout=layout), N(window=self.window))
        self.assertEqual(layout.records, [("loopcut.focus", "Open Loopcut", {"icon": "LOOPCUT"}), "separator"])
        self.assertEqual(self.host.LOOPCUT_OT_focus.bl_idname, "loopcut.focus")
        self.assertIn(self.host.SHORTCUT, self.host.LOOPCUT_OT_focus.bl_description)  # The tooltip names it.

    def test_a_panel_closed_through_blenders_ui_takes_the_button_back_within_a_poll(self):
        publish = self.fake.msgbus.publish_rna
        publish.reset_mock()
        self.addCleanup(self.host._topbar.clear)
        self.host.sync_topbars()
        self.draw_topbar()  # "Open Loopcut" drawn.
        self.host.sync_topbars()
        publish.assert_not_called()  # Still right.
        self.window.screen.areas.append(Area("LOOPCUT"))  # Docked: the button must go.
        self.host.sync_topbars()
        publish.assert_called_once_with(key=self.window.workspace.path_resolve.return_value)
        self.window.workspace.path_resolve.assert_called_with("tools", False)
        self.draw_topbar()
        self.window.screen.areas.pop()  # Closed again: the button must come back.
        self.host.sync_topbars()
        self.assertEqual(publish.call_count, 2)
        sync = Mock()
        jobs, workspace = (importlib.import_module(NAME + "." + n) for n in ("job_tools", "workspace"))
        with patch.object(jobs, "refresh_ui", Mock()), patch.object(workspace, "poll_left", Mock()), \
                patch.object(self.host, "tag_redraw_all", Mock()), patch.object(self.host, "sync_topbars", sync):
            self.assertEqual(self.lifecycle._poll_jobs(), 1.0)
        sync.assert_called_once_with()  # lifecycle's poll, every second.

    def test_register_and_unregister_add_and_remove_both_draw_functions(self):
        menus = {"TOPBAR_HT_upper_bar": Mock(), "VIEW3D_MT_view": Mock()}
        with patch.dict(vars(self.fake.types), menus), \
                patch.object(self.fake.utils, "register_class", Mock(), create=True), \
                patch.object(self.fake.utils, "unregister_class", Mock(), create=True), \
                patch.object(self.host, "HOST_SPACE", Mock()), patch.object(self.host, "draw", Mock()), \
                patch.object(self.fake.context, "window_manager", N(keyconfigs=N(addon=None), windows=[])):
            self.host.register()
            menus["TOPBAR_HT_upper_bar"].append.assert_called_once_with(self.host._draw_topbar)
            menus["VIEW3D_MT_view"].prepend.assert_called_once_with(self.host._draw_view_menu)
            self.host.unregister()
            menus["TOPBAR_HT_upper_bar"].remove.assert_called_once_with(self.host._draw_topbar)
            menus["VIEW3D_MT_view"].remove.assert_called_once_with(self.host._draw_view_menu)


if __name__ == "__main__":
    unittest.main()
