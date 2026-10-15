"""Mouse event routing with the real operator methods and no Blender runtime."""

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ADDONS = Path(__file__).resolve().parents[2] / "scripts" / "addons_core"
sys.path.insert(0, str(ADDONS))

from loopcut import state  # noqa: E402
from loopcut.ui import layout, textedit  # noqa: E402


def measure(font, size, text):
    return len(text) * size * 0.6


class InputMouseTest(unittest.TestCase):
    def setUp(self):
        self.session = state.new_session()
        self.session.update(input="one two three", cursor=13)
        self.region = SimpleNamespace(type="WINDOW", x=40, y=60, width=420, height=600)
        self.area = SimpleNamespace(as_pointer=lambda: 1, regions=[self.region], tag_redraw=Mock())
        self.context = SimpleNamespace(area=self.area, window_manager=Mock(),
                                       window=SimpleNamespace(screen=SimpleNamespace(areas=[self.area])))
        self.displays = {1: layout.build(self.session, 420, 600, 1, measure)}
        self.action = Mock()
        self.close_popups = Mock()
        self.clock = Mock(return_value=1.0)
        inputs = SimpleNamespace(mouse_double_click_time=500, drag_threshold_mouse=3)
        namespace = {"bpy": SimpleNamespace(types=SimpleNamespace(Operator=object), props=Mock(),
                                            context=SimpleNamespace(preferences=SimpleNamespace(inputs=inputs))),
                     "time": SimpleNamespace(monotonic=self.clock),
                     "state": SimpleNamespace(session=lambda: self.session, ui={}),
                     "layout": layout, "textedit": textedit, "draw": SimpleNamespace(measure=measure),
                     "_displays": self.displays, "_close_popups": self.close_popups, "_do_action": self.action,
                     "_update_mentions": Mock()}
        # Load just the event router and its coordinate helpers, without importing Blender or services.
        source = ADDONS / "loopcut" / "ui" / "host.py"
        tree = ast.parse(source.read_text())
        names = {"LOOPCUT_OT_interact", "_local", "_window_region", "_find_area"}
        nodes = [node for node in tree.body if getattr(node, "name", None) in names or
                 (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "POPUP_ACTIONS"
                                                       for t in node.targets))]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        self.operator = namespace["LOOPCUT_OT_interact"]()
        self.operator.keyboard = False

    def event(self, index=0, kind="LEFTMOUSE", value="PRESS", shift=False):
        line = next(p for p in self.displays[1]["prims"] if p["id"] == "input.l0")
        return SimpleNamespace(type=kind, value=value, shift=shift,
                               mouse_x=self.region.x + line["x"] + measure("ui", line["size"], "a" * index),
                               mouse_y=self.region.y + self.region.height - line["y"] - line["h"] / 2)

    def test_first_click_and_modal_click_move_caret(self):
        self.assertEqual(self.operator.invoke(self.context, self.event(5)), {"RUNNING_MODAL"})
        self.assertEqual((self.session["cursor"], self.session["anchor"]), (5, None))
        self.operator.modal(self.context, self.event(2))
        self.assertEqual((self.session["cursor"], self.session["anchor"]), (2, None))

    def test_drag_extends_until_release_even_outside_region(self):
        self.operator.invoke(self.context, self.event(5))
        self.operator.modal(self.context, self.event(9, "MOUSEMOVE", "NOTHING"))
        self.assertEqual(textedit.selection(self.session), (5, 9))
        self.operator.modal(self.context, self.event(-10, "MOUSEMOVE", "NOTHING"))
        self.assertEqual((self.session["anchor"], self.session["cursor"]), (5, 0))
        self.operator.modal(self.context, self.event(-10, value="RELEASE"))
        self.operator.modal(self.context, self.event(10, "MOUSEMOVE", "NOTHING"))
        self.assertEqual((self.session["anchor"], self.session["cursor"]), (5, 0))

    def test_window_deactivate_clears_drag_and_double_click(self):
        for dragged in (False, True):
            with self.subTest(dragged=dragged):
                self.operator.invoke(self.context, self.event(5))
                if dragged:
                    self.operator.modal(self.context, self.event(9, "MOUSEMOVE", "NOTHING"))
                before = (self.session["anchor"], self.session["cursor"])
                event = SimpleNamespace(type="WINDOW_DEACTIVATE", value="NOTHING")
                self.assertEqual(self.operator.modal(self.context, event), {"PASS_THROUGH"})
                self.assertFalse(self.operator._selecting)
                self.assertIsNone(self.operator._input_click)
                for kind in ("MOUSEMOVE", "INBETWEEN_MOUSEMOVE"):
                    self.assertEqual(self.operator.modal(self.context, self.event(11, kind, "NOTHING")),
                                     {"PASS_THROUGH"})
                    self.assertEqual((self.session["anchor"], self.session["cursor"]), before)
                self.operator.modal(self.context, self.event(5))
                self.assertIsNone(textedit.selection(self.session))

    def test_mouse_move_after_missed_release_clears_drag(self):
        for kind in ("MOUSEMOVE", "INBETWEEN_MOUSEMOVE"):
            with self.subTest(kind=kind):
                self.operator.invoke(self.context, self.event(5))
                event = self.event(9, kind, "NOTHING")
                event.type_prev, event.value_prev = "LEFT_SHIFT", "RELEASE"
                self.assertEqual(self.operator.modal(self.context, event), {"RUNNING_MODAL"})
                self.assertEqual(textedit.selection(self.session), (5, 9))
                self.operator.invoke(self.context, self.event(5))
                event.type_prev = "LEFTMOUSE"
                self.assertEqual(self.operator.modal(self.context, event), {"PASS_THROUGH"})
                self.assertFalse(self.operator._selecting)
                self.assertIsNone(self.operator._input_click)
                self.assertEqual((self.session["anchor"], self.session["cursor"]), (None, 5))

    def test_double_click_and_shift_click_select_in_both_paths(self):
        for invoking in (True, False):
            with self.subTest(invoking=invoking):
                self.operator.invoke(self.context, self.event(2))
                handler = self.operator.invoke if invoking else self.operator.modal
                handler(self.context, self.event(5, value="DOUBLE_CLICK"))
                self.assertEqual(textedit.selected_text(self.session), "two")
                self.operator.modal(self.context, self.event(5, "MOUSEMOVE", "NOTHING"))
                self.assertEqual(textedit.selected_text(self.session), "two")
                handler(self.context, self.event(11, shift=True))
                self.assertEqual((self.session["anchor"], self.session["cursor"]), (4, 11))

    def test_other_actions_keep_their_dispatch_and_do_not_start_drag(self):
        event = self.event(2)
        hit = next(h for h in self.displays[1]["hits"] if h["id"] == "input.model")
        event.mouse_x = self.region.x + hit["x"] + hit["w"] / 2
        event.mouse_y = self.region.y + self.region.height - hit["y"] - hit["h"] / 2
        self.operator.invoke(self.context, event)
        self.action.assert_called_once_with(self.session, ("model_menu", None))
        self.operator.modal(self.context, self.event(2, "MOUSEMOVE", "NOTHING"))
        self.assertEqual(self.session["cursor"], 13)

    def test_shift_click_outside_input_passes_through_without_starting_modal(self):
        for target in ("header.new", "input.model", "background", "outside"):
            with self.subTest(target=target):
                event = self.event(2, shift=True)
                if target in {"background", "outside"}:
                    event.mouse_x = self.region.x + (1 if target == "background" else -1)
                else:
                    hit = next(h for h in self.displays[1]["hits"] if h["id"] == target)
                    event.mouse_x = self.region.x + hit["x"] + hit["w"] / 2
                    event.mouse_y = self.region.y + self.region.height - hit["y"] - hit["h"] / 2
                self.assertEqual(self.operator.invoke(self.context, event), {"PASS_THROUGH"})
                self.action.assert_not_called()
                self.close_popups.assert_not_called()
                self.context.window_manager.modal_handler_add.assert_not_called()
                self.assertFalse(self.session["focused"])
                self.assertEqual((self.session["anchor"], self.session["cursor"]), (None, 13))
        self.assertEqual(self.operator.invoke(self.context, self.event(5, shift=True)), {"RUNNING_MODAL"})
        self.assertEqual(textedit.selection(self.session), (5, 13))
        self.context.window_manager.modal_handler_add.assert_called_once_with(self.operator)

    def test_modal_double_press_uses_blender_timing_and_distance(self):
        self.operator.invoke(self.context, self.event(5))
        self.operator.modal(self.context, self.event(5, value="RELEASE"))
        self.clock.return_value = 1.1
        self.operator.modal(self.context, self.event(5))
        self.assertEqual(textedit.selected_text(self.session), "two",
                         "Blender converts DOUBLE_CLICK to PRESS for modal operators")
        self.operator.modal(self.context, self.event(5, value="RELEASE"))
        self.clock.return_value = 2.0
        self.operator.modal(self.context, self.event(5))
        self.assertIsNone(textedit.selection(self.session))
        self.operator.modal(self.context, self.event(5, value="RELEASE"))
        self.clock.return_value = 3.0
        self.operator.modal(self.context, self.event(5))
        self.assertIsNone(textedit.selection(self.session), "a slow second click only places the caret")
        self.operator.modal(self.context, self.event(5, value="RELEASE"))
        self.clock.return_value = 3.1
        self.operator.modal(self.context, self.event(9))
        self.assertIsNone(textedit.selection(self.session), "a distant second click only places the caret")

    def test_a_third_click_in_time_selects_the_line(self):
        self.session.update(input="one two three\nfour", cursor=0)
        self.displays[1] = layout.build(self.session, 420, 600, 1, measure)
        self.operator.invoke(self.context, self.event(5))
        self.operator.modal(self.context, self.event(5, value="RELEASE"))
        self.clock.return_value = 1.1
        self.operator.modal(self.context, self.event(5))
        self.assertEqual(textedit.selected_text(self.session), "two")
        self.operator.modal(self.context, self.event(5, value="RELEASE"))
        self.clock.return_value = 1.2
        self.operator.modal(self.context, self.event(5))
        self.assertEqual(textedit.selected_text(self.session), "one two three")
        self.operator.modal(self.context, self.event(5, value="RELEASE"))
        self.clock.return_value = 1.3
        self.operator.modal(self.context, self.event(5))
        self.assertIsNone(textedit.selection(self.session), "a fourth click starts over")


if __name__ == "__main__":
    unittest.main()
