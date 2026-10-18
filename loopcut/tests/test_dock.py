"""Docking the panel across save and reopen, with fake screens and the real dock code."""

import ast
import contextlib
import itertools
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from unittest.mock import Mock

ADDONS = Path(__file__).resolve().parents[2] / "scripts" / "addons_core"
POINTERS = itertools.count(1)  # Never reused, unlike id(): a reopen must not find the old panel by luck.


class Space:
    def __init__(self, text=None, header=True, footer=True, sidebar=False):  # A new Text Editor's defaults.
        self.text, self.show_region_header, self.show_region_footer, self.show_region_ui = (
            text, header, footer, sidebar)

    def copy(self):
        return Space(self.text, self.show_region_header, self.show_region_footer, self.show_region_ui)


class Area:
    def __init__(self, kind, width, space=None, order=None):
        self.pointer = next(POINTERS)
        self.order = self.pointer if order is None else order  # Place in screen.areabase: new areas go last.
        self.type, self.width, self.height = kind, width, 600
        self.spaces = N(active=space or Space())
        self.regions = [N(type="HEADER"), N(type="WINDOW")]
        self.tag_redraw = Mock()

    def as_pointer(self):
        return self.pointer

    @property
    def ui_type(self):
        return self.type

    @ui_type.setter
    def ui_type(self, kind):
        if kind != self.type:  # Blender makes a new space for the new editor.
            self.type, self.spaces.active = kind, Space()


class Screen:
    """One row of full-height areas. `areas` lists them as Blender does, in areabase order."""

    def __init__(self, row):
        self.row = row
        self.layout()

    @property
    def areas(self):
        return sorted(self.row, key=lambda a: a.order)

    def layout(self):
        x = 0
        for area in self.row:
            area.x, x = x, x + area.width


class Blender:
    def __init__(self, row):
        self.window = N(screen=Screen(row), as_pointer=lambda: 7)
        self.timers, self.override, self.closed = [], {}, []
        self.bpy = N(context=N(temp_override=self.temp_override),
                     ops=N(screen=N(area_split=self.area_split, area_close=self.area_close)),
                     app=N(timers=N(register=lambda fn, first_interval=0: self.timers.append(fn))))

    @property
    def row(self):
        return self.window.screen.row

    @contextlib.contextmanager
    def temp_override(self, **kwargs):
        self.override = kwargs
        try:
            yield
        finally:
            self.override = {}

    def area_split(self, direction, factor):
        view = self.override["area"]
        new = Area(view.type, view.width - round(view.width * factor))
        view.width -= new.width
        self.row.insert(self.row.index(view) + 1, new)
        self.window.screen.layout()

    def area_close(self):
        """screen_area_close: joined into the best aligned neighbour, the first in areabase on a tie."""
        area = self.override["area"]
        self.closed.append(area)
        index = self.row.index(area)
        neighbours = self.row[max(index - 1, 0):index] + self.row[index + 1:index + 2]
        min(neighbours, key=lambda a: a.order).width += area.width
        self.row.remove(area)
        self.window.screen.layout()

    def run_timers(self):
        while self.timers:
            self.timers.pop(0)()

    def reopen(self):
        """Save and open the file again: the same editors and settings in new areas, none of them a host."""
        self.window.screen = Screen([Area(a.type, a.width, a.spaces.active.copy(), a.order) for a in self.row])


class DockTest(unittest.TestCase):
    def load_host(self, blender):
        hosts = set()  # Outlives a reopen in the same Blender, holding pointers no area has any more.
        namespace = {"bpy": blender.bpy, "state": N(hosts=hosts), "NATIVE": False,
                     "HOST_UI_TYPE": "TEXT_EDITOR", "DOCK_FACTOR": 0.72, "_docking": set()}
        source = ADDONS / "loopcut" / "ui" / "host.py"
        names = {"hosts", "is_host", "find_panel", "has_panel", "_leftovers", "_show_in", "dock"}
        nodes = [node for node in ast.parse(source.read_text()).body if getattr(node, "name", None) in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        return N(**namespace)

    def dock(self, blender, host):
        then = Mock()
        host.dock(blender.window, then=then)
        self.assertTrue(host.has_panel(blender.window))  # Even before the timer: no second dock races it.
        blender.run_timers()
        panel = host.find_panel(blender.window)
        then.assert_called_once_with(panel)
        return panel

    def test_save_reopen_dock_keeps_one_panel_at_its_width(self):
        blender = Blender([Area("VIEW_3D", 1000), Area("PROPERTIES", 300)])
        host = self.load_host(blender)
        docked = [("VIEW_3D", 0, 720), ("TEXT_EDITOR", 720, 280), ("PROPERTIES", 1000, 300)]
        self.dock(blender, host)
        self.assertEqual([(a.type, a.x, a.width) for a in blender.row], docked)
        for _ in range(4):
            blender.reopen()
            self.assertFalse(host.has_panel(blender.window))  # The saved area is back, but empty.
            panel = self.dock(blender, host)
            self.assertEqual([(a.type, a.x, a.width) for a in blender.row], docked)
            self.assertIs(panel, blender.row[1])
        self.assertEqual(blender.closed, [])

    def test_dock_with_the_panel_open_uses_it(self):
        blender = Blender([Area("VIEW_3D", 1000), Area("PROPERTIES", 300)])
        host = self.load_host(blender)
        first = self.dock(blender, host)
        self.assertIs(self.dock(blender, host), first)
        self.assertEqual(len(blender.row), 3)

    def test_older_files_heal_and_users_editors_stay(self):
        # Saved by a build that split again on each reopen: four panel areas, oldest (and widest) first.
        view, properties = Area("VIEW_3D", 269), Area("PROPERTIES", 300)
        saved = [Area("TEXT_EDITOR", width, Space(header=False, footer=False)) for width in (280, 200, 145, 104)]
        script = Area("TEXT_EDITOR", 200, Space(text=N(name="script.py"), header=False, footer=False))
        notes = Area("TEXT_EDITOR", 150)  # Empty, header shown.
        bare = Area("TEXT_EDITOR", 150, Space(header=False))  # Empty, footer shown.
        blender = Blender([script, view, *reversed(saved), notes, bare, properties])
        host = self.load_host(blender)
        panel = self.dock(blender, host)
        self.assertIs(panel, saved[0])
        self.assertEqual(blender.closed, saved[:0:-1])
        healed = [("TEXT_EDITOR", 200), ("VIEW_3D", 718), ("TEXT_EDITOR", 280),
                  ("TEXT_EDITOR", 150), ("TEXT_EDITOR", 150), ("PROPERTIES", 300)]
        self.assertEqual([(a.type, a.width) for a in blender.row], healed)
        self.assertEqual(script.spaces.active.text.name, "script.py")
        self.assertTrue(notes.spaces.active.show_region_header and bare.spaces.active.show_region_footer)
        for _ in range(3):
            blender.reopen()
            users = [blender.row[i] for i in (0, 3, 4)]
            self.dock(blender, host)
            self.assertEqual([(a.type, a.width) for a in blender.row], healed)
            self.assertTrue(all(a in blender.row and not host.is_host(a) for a in users))
        self.assertEqual(len(blender.closed), 3)

    def test_no_viewport_and_nothing_saved(self):
        blender = Blender([Area("PROPERTIES", 300)])
        host = self.load_host(blender)
        with self.assertRaises(RuntimeError):
            host.dock(blender.window)
        self.assertFalse(host.has_panel(blender.window))


if __name__ == "__main__":
    unittest.main()
