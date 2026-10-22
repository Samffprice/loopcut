"""Preloaded Python names and failed-step reporting with a bpy stand-in. No Blender launch."""

import contextlib
import copy
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
fake_bpy = types.ModuleType("bpy")  # The same minimal stand-in as test_live_file: whichever module loads first wins.
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import config, contact, cost_guard, live_file, op_notes, scene_diff, shape_key_guard, tools

N = types.SimpleNamespace
KEPT_CHANGES = "The code made these changes before the error; they stay in the scene."


class RunPythonTest(unittest.TestCase):
    def setUp(self):
        # Stock Blender has no WindowManager undo-group API. Only the existing undo push is needed.
        self.bpy = N(values={"object": {"location": 3, "material": "original"}, "user_edit": 7},
                     context=N(window_manager=N()), ops=N(ed=N(undo_push=mock.Mock())))
        self.enterContext(mock.patch.object(tools, "bpy", self.bpy))
        self.enterContext(mock.patch.object(tools, "_editor_context", side_effect=lambda _: contextlib.nullcontext()))
        self.enterContext(mock.patch.object(tools, "_preserve_host_areas", side_effect=lambda: contextlib.nullcontext([])))
        for module in (cost_guard, op_notes, live_file):
            self.enterContext(mock.patch.object(module, "watching_ops", side_effect=contextlib.nullcontext))
        for module, name, value in (
                (config, "run_timeout", 60), (cost_guard, "begin", None), (cost_guard, "check", None),
                (cost_guard, "take", ("", [])), (shape_key_guard, "install", None),
                (shape_key_guard, "check", None), (shape_key_guard, "take", []),
                (op_notes, "take", []), (live_file, "take", []), (contact, "recheck", [])):
            self.enterContext(mock.patch.object(module, name, return_value=value))
        self.enterContext(mock.patch.object(scene_diff, "snapshot", side_effect=lambda **_: copy.deepcopy(self.bpy.values)))
        self.enterContext(mock.patch.object(scene_diff, "diff", side_effect=lambda a, b: {
            "added": [], "removed": [], "changed": [], "unseen": [],
            "other": ["values changed"] if a != b else []}))
        self.enterContext(mock.patch.object(scene_diff, "for_model", side_effect=lambda d:
            "Scene changes: " + (", ".join(d["other"]) or "none")))
        self.mathutils = N(Vector=tuple, Matrix=list, Euler=tuple, Quaternion=tuple)
        self.bmesh = N(new=lambda: "new mesh")
        self.enterContext(mock.patch.dict(sys.modules, {"bmesh": self.bmesh, "mathutils": self.mathutils}))

    def test_error_keeps_deletes_creates_and_edits_and_reports_them(self):
        first = tools.run_python("bpy.values['previous_step'] = 42")
        self.assertTrue(first.ok, first.text)
        self.bpy.values["user_edit"] = 19
        before = copy.deepcopy(self.bpy.values)
        result = tools.run_python("del bpy.values['object']\nbpy.values['new'] = 4\n"
                                  "bpy.values['user_edit'] = 0\nraise NameError('missing helper')")
        self.assertFalse(result.ok)
        self.assertEqual(self.bpy.values, {"previous_step": 42, "new": 4, "user_edit": 0})
        self.assertEqual(result.scene_before, before)
        self.assertEqual(result.scene_after, self.bpy.values)
        self.assertIn("NameError: missing helper", result.text)
        self.assertIn(KEPT_CHANGES + "\nScene changes: values changed", result.text)

    def test_common_names_are_available_every_time_and_other_globals_are_fresh(self):
        for _ in range(2):
            result = tools.run_python("assert math.sqrt(9) == 3\nassert bmesh.new() == 'new mesh'\n"
                                      "assert Vector is mathutils.Vector\nassert Matrix is mathutils.Matrix\n"
                                      "assert Euler is mathutils.Euler\nassert Quaternion is mathutils.Quaternion\n"
                                      "assert __name__ == '__loopcut__'\n"
                                      "assert 'private_value' not in globals()\n"
                                      "private_value = 1\nVector = None\nimport fractions")
            self.assertTrue(result.ok, result.text)
        result = tools.run_python("assert 'fractions' not in globals()\nassert 'private_value' not in globals()")
        self.assertTrue(result.ok, result.text)
        description = next(s["function"]["description"] for s in tools.SCHEMAS
                           if s["function"]["name"] == "run_python")
        # The description names the common ones (every word is paid for on every step); Euler and Quaternion
        # are preloaded too, as the namespace test checks.
        for name in ("bpy", "math", "bmesh", "mathutils", "Vector", "Matrix"):
            self.assertIn(f"`{name}`", description)

    def test_call_uses_existing_undo_push_without_grouping_api(self):
        result = tools.run_python("bpy.values['new'] = 4", summary="Add value")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.bpy.values["new"], 4)
        self.bpy.ops.ed.undo_push.assert_called_once_with(message="Before Loopcut: Add value")

    def test_timeout_keeps_changes_and_restores_trace_hook(self):
        before = copy.deepcopy(self.bpy.values)
        old_trace = sys.gettrace()
        calls = iter([0, 0, 0, 2, 2])
        with mock.patch.object(config, "run_timeout", return_value=1), \
                mock.patch.object(tools.time, "monotonic", side_effect=lambda: next(calls, 2)):
            result = tools.run_python("bpy.values.clear()\nwhile True:\n    pass")
        self.assertFalse(result.ok)
        self.assertIn("Stopped:", result.text)
        self.assertIn("Whatever it changed before that is listed below.", result.text)
        self.assertEqual(self.bpy.values, {})
        self.assertEqual(result.scene_before, before)
        self.assertEqual(result.scene_after, {})
        self.assertIs(sys.gettrace(), old_trace)

    def test_errors_without_changes_do_not_claim_changes_remain(self):
        for code, error in (("if:", "SyntaxError"), ("raise ValueError('bad')", "ValueError: bad")):
            with self.subTest(code=code):
                before = copy.deepcopy(self.bpy.values)
                result = tools.run_python(code)
                self.assertFalse(result.ok)
                self.assertEqual(self.bpy.values, before)
                self.assertEqual(result.scene_before, result.scene_after)
                self.assertIn(error, result.text)
                self.assertIn("Scene changes: none", result.text)
                self.assertNotIn(KEPT_CHANGES, result.text)

    def test_kept_changes_message_survives_clipping_and_capture_sees_changes(self):
        from loopcut import shaders

        def capture(**kwargs):
            self.assertEqual(self.bpy.values, {})
            return tools.ToolResult("Image attached")

        with mock.patch.object(tools, "capture_viewport", side_effect=capture) as shot, \
                mock.patch.object(shaders, "before_look", side_effect=lambda fn, *a, **kw: fn()):
            result = tools.run_python("print('x' * 20000); bpy.values.clear(); raise ValueError('bad')",
                                      capture="front")
        self.assertFalse(result.ok)
        self.assertIn("chars omitted", result.text)
        self.assertEqual(result.text.count(KEPT_CHANGES), 1)
        self.assertIn("Image attached", result.text)
        self.assertEqual(result.guard_notes, [])
        shot.assert_called_once_with(angle="front")


if __name__ == "__main__":
    unittest.main()
