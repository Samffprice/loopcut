"""Preloaded Python names and failed-step reporting with a bpy stand-in. No Blender launch."""

import contextlib
import copy
import io
import os
import sys
import threading
import time
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
RUN_TIMEOUT = config.run_timeout  # The real reader, for LOOPCUT_RUN_TIMEOUT; setUp stands in a constant.
SPLIT = "Split the work into smaller run_python calls."


class RunPythonTest(unittest.TestCase):
    def setUp(self):
        # Stock Blender has no WindowManager undo-group API. Only the existing undo push is needed.
        self.bpy = N(values={"object": {"location": 3, "material": "original"}, "user_edit": 7},
                     context=N(window_manager=N(), mode="OBJECT"), ops=N(ed=N(undo_push=mock.Mock())))
        self.bpy.ops.object = N(mode_set=mock.Mock(side_effect=lambda mode: setattr(self.bpy.context, "mode", mode)))
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
        self.assertEqual(self.bpy.ops.ed.undo_push.call_args_list,
                         [mock.call(message="Before Loopcut: Add value"), mock.call(message="Loopcut: Add value")])

    def test_every_step_pushes_the_state_after_it_changed_something_or_not(self):
        # 0.2.18: a step that changed nothing, then one that added an object, then Undo crashed Blender 5.2.2 on
        # the next redraw: two pushes in a row held the same state, and the added object sat past the newest.
        push = self.bpy.ops.ed.undo_push
        with mock.patch.object(config, "run_timeout", return_value=0.05):
            for summary, code in (("Look", "print(len(bpy.values))"), ("Nothing", "pass"),
                                  ("Fail", "raise ValueError('bad')"),
                                  ("Fail part way", "bpy.values['half'] = 1\nraise ValueError('bad')"),
                                  ("Stop", "while True:\n    pass"),
                                  ("Stop part way", "bpy.values['loop'] = 1\nwhile True:\n    pass"),
                                  ("", "bpy.values['plain'] = 1"), ("Long " * 20, "pass")):
                with self.subTest(summary=summary):
                    push.reset_mock()
                    tools.run_python(code, summary=summary)
                    what = summary or "run_python"
                    self.assertEqual(push.call_args_list, [mock.call(message=f"Before Loopcut: {what}"[:60]),
                                                           mock.call(message=f"Loopcut: {what}"[:60])])

    def test_the_push_after_follows_the_guard_and_precedes_the_capture_in_the_same_editor(self):
        from loopcut import shaders
        push, seen = self.bpy.ops.ed.undo_push, []

        def snapshot(**kwargs):
            seen.append(("snapshot", "guard" in kwargs, push.call_count))
            return copy.deepcopy(self.bpy.values)

        def capture(**kwargs):
            seen.append(("capture", push.call_count))
            return tools.ToolResult("Image attached")

        with mock.patch.object(scene_diff, "snapshot", side_effect=snapshot), \
                mock.patch.object(tools, "capture_viewport", side_effect=capture), \
                mock.patch.object(shaders, "before_look", side_effect=lambda fn, *a, **kw: fn()):
            result = tools.run_python("bpy.values['new'] = 4", summary="Add value", capture="front",
                                      editor="ShaderNodeTree")
        self.assertIn("Image attached", result.text)
        # A switch the cost guard makes while it snapshots is in the push, so a redo cannot bring the modifier back.
        self.assertEqual(seen, [("snapshot", False, 0), ("snapshot", True, 1), ("capture", 2)])
        self.assertEqual(tools._editor_context.call_args_list, [mock.call("ShaderNodeTree")] * 2)

    def test_the_push_after_can_be_off_and_a_failed_one_is_only_logged(self):
        push = self.bpy.ops.ed.undo_push
        with mock.patch.object(tools, "UNDO_AFTER", False):  # As in a worker's child Blender.
            tools.run_python("bpy.values['new'] = 4", summary="Add value")
        push.assert_called_once_with(message="Before Loopcut: Add value")
        push.reset_mock()
        push.side_effect = [None, RuntimeError("Operator bpy.ops.ed.undo_push.poll() failed, context is incorrect")]
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            result = tools.run_python("bpy.values['more'] = 5", summary="Add more")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.bpy.values["more"], 5)
        self.assertIn("Loopcut: no undo step after 'Loopcut: Add more'", log.getvalue())

    def test_timeout_keeps_changes_and_restores_trace_hook(self):
        before = copy.deepcopy(self.bpy.values)
        old_trace = sys.gettrace()
        calls = iter([0, 0, 0, 2, 2])
        with mock.patch.object(config, "run_timeout", return_value=1), \
                mock.patch.object(tools.time, "monotonic", side_effect=lambda: next(calls, 2)):
            result = tools.run_python("bpy.values.clear()\nwhile True:\n    pass")
        self.assertFalse(result.ok)
        self.assertEqual(result.timed_out, 1)
        self.assertIn("Stopped:", result.text)
        self.assertIn("Whatever it changed before that is listed below. " + SPLIT, result.text)
        self.assertEqual(self.bpy.values, {})
        self.assertEqual(result.scene_before, before)
        self.assertEqual(result.scene_after, {})
        self.assertIs(sys.gettrace(), old_trace)
        self.bpy.ops.object.mode_set.assert_not_called()

    def test_a_long_sleep_stops_at_the_deadline_and_a_sleep_on_another_thread_does_not(self):
        self.bpy.naps = []
        code = ("import threading, time\n"
                "def nap():\n    start = time.monotonic()\n    time.sleep(1.5)\n"
                "    bpy.naps.append(time.monotonic() - start)\n"
                "bpy.nap = threading.Thread(target=nap)\nbpy.nap.start()\n"
                "bpy.values['slept'] = 0\ntime.sleep(5)\nbpy.values['slept'] = 5")
        started = time.monotonic()
        with mock.patch.dict(os.environ, LOOPCUT_RUN_TIMEOUT="1"), mock.patch.object(config, "run_timeout", RUN_TIMEOUT):
            result = tools.run_python(code)
        took = time.monotonic() - started
        self.bpy.nap.join(5)
        self.assertGreaterEqual(took, 0.9)
        self.assertLess(took, 2)
        self.assertFalse(result.ok)
        self.assertEqual(result.timed_out, 1)
        self.assertIn("Stopped: the code ran for more than 1 s", result.text)
        self.assertIn(SPLIT, result.text)
        self.assertEqual(self.bpy.values["slept"], 0)  # Its changes so far stay.
        self.assertEqual(len(self.bpy.naps), 1, "the other thread's sleep was cut")
        self.assertGreaterEqual(self.bpy.naps[0], 1.4)

    def test_time_sleep_is_restored_after_a_stop_an_error_and_a_success(self):
        sleep = time.sleep
        self.bpy.seen = []
        with mock.patch.object(config, "run_timeout", return_value=0.2):
            for code, ok in (("time.sleep(5)", False), ("while True:\n    pass", False),
                             ("raise ValueError('bad')", False), ("time.sleep(0.01)", True)):
                with self.subTest(code=code):
                    result = tools.run_python("import time\nbpy.seen.append(time.sleep)\n" + code)
                    self.assertEqual(result.ok, ok, result.text)
                    self.assertIsNot(self.bpy.seen[-1], sleep)
                    self.assertIs(time.sleep, sleep)
                    self.assertEqual(bool(result.timed_out), "Stopped:" in result.text)
        # A reference the code kept sleeps as usual once its run is over, past its deadline or not.
        started = time.monotonic()
        self.bpy.seen[0](0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

    def test_a_stop_puts_an_object_the_code_left_in_edit_mode_back_in_object_mode(self):
        mode_set = self.bpy.ops.object.mode_set
        with mock.patch.object(config, "run_timeout", return_value=0.05):
            result = tools.run_python("bpy.context.mode = 'EDIT_MESH'\nwhile True:\n    pass")
            mode_set.assert_called_once_with(mode="OBJECT")
            self.assertEqual(self.bpy.context.mode, "OBJECT")
            self.assertTrue(result.timed_out)
            self.assertIn("The object it left in Edit Mode is back in Object Mode.", result.text)
            # An error is not a stop: the mode is the code's to fix, as before.
            result = tools.run_python("bpy.context.mode = 'EDIT_MESH'\nraise ValueError('bad')")
            self.assertEqual(mode_set.call_count, 1)
            self.assertFalse(result.timed_out)
            # Already in Edit Mode before the step: the user's mode stays.
            self.bpy.values["mode"] = self.bpy.context.mode = "EDIT_MESH"
            result = tools.run_python("while True:\n    pass")
            self.assertEqual(mode_set.call_count, 1)
            self.assertNotIn("Object Mode", result.text)
            # A failed switch is logged, never raised; the stop is reported as ever.
            self.bpy.values["mode"] = "OBJECT"
            self.bpy.context.mode = "OBJECT"
            mode_set.side_effect = RuntimeError("context is incorrect")
            log = io.StringIO()
            with contextlib.redirect_stdout(log):
                result = tools.run_python("bpy.context.mode = 'EDIT_MESH'\nwhile True:\n    pass")
        self.assertEqual(mode_set.call_count, 2)
        self.assertTrue(result.timed_out)
        self.assertIn(SPLIT, result.text)
        self.assertNotIn("Object Mode", result.text)
        self.assertIn("could not leave Edit Mode", log.getvalue())

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

    def test_a_double_slash_path_that_failed_in_a_never_saved_file_says_so_and_one_that_ran_does_not(self):
        from loopcut import api_docs
        self.enterContext(mock.patch.object(api_docs.bpy, "data", N(filepath=""), create=True))
        result = tools.run_python("path = '//TurboCar.blend'\nraise RuntimeError('Error: Cannot open file "
                                  "C:\\\\Program Files\\\\Loopcut\\\\Loopcut 5.2\\\\TurboCar.blend@ for writing')")
        self.assertIn(f"NOTE: {api_docs.UNSAVED_RELATIVE}", result.text)
        result = tools.run_python("path = '//TurboCar.blend'\nprint(path)")
        self.assertTrue(result.ok)
        self.assertNotIn("NOTE", result.text)

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



class UndoStepTest(unittest.TestCase):
    """tools.undo_before and undo_step: a change as one undo step, a push before it and one after it."""

    def setUp(self):
        self.push = mock.Mock()
        self.enterContext(mock.patch.object(tools, "bpy", N(ops=N(ed=N(undo_push=self.push)))))
        self.enterContext(mock.patch.object(tools, "_editor_context", side_effect=lambda _: contextlib.nullcontext()))

    def pushed(self):
        return [c.kwargs["message"] for c in self.push.call_args_list]

    def test_undo_step_pushes_before_and_after_also_when_the_change_raises(self):
        with tools.undo_step("Photo to 3D"):
            self.assertEqual(self.pushed(), ["Before Loopcut: Photo to 3D"])
        self.assertEqual(self.pushed(), ["Before Loopcut: Photo to 3D", "Loopcut: Photo to 3D"])
        self.push.reset_mock()
        with self.assertRaises(ValueError), tools.undo_step("Photo to 3D"):
            raise ValueError("the import stopped half way")
        self.assertEqual(self.pushed(), ["Before Loopcut: Photo to 3D", "Loopcut: Photo to 3D"])
        self.assertEqual(tools._undo_owed, [])

    def test_in_a_tool_call_each_change_owes_its_own_push_after(self):
        def tool():
            tools.undo_before("import asset")
            with tools.undo_step("Text to motion"):
                pass
            return tools.ToolResult("done")

        with mock.patch.dict(tools._DISPATCH, {"undo_probe": tool}):
            self.assertTrue(tools.execute("undo_probe", "{}").ok)
        self.assertEqual(self.pushed(), ["Before Loopcut: import asset", "Before Loopcut: Text to motion",
                                         "Loopcut: Text to motion", "Loopcut: import asset"])

    def test_no_push_after_a_failed_one_before_and_none_owed_outside_a_call(self):
        self.push.side_effect = [RuntimeError("Operator bpy.ops.ed.undo_push.poll() failed"), None]
        with tools.undo_step("Photo to 3D"):
            pass
        self.assertEqual(self.pushed(), ["Before Loopcut: Photo to 3D"], "the failed attempt only")
        self.push.reset_mock()
        self.push.side_effect = None
        self.assertTrue(tools.undo_before("import asset"))  # A worker's child loads an asset this way.
        self.assertEqual(self.pushed(), ["Before Loopcut: import asset"])
        self.assertEqual(tools._undo_owed, [])

    def test_an_unexpected_failure_to_push_is_logged_and_the_change_still_runs(self):
        self.push.side_effect = AttributeError("'Context' object has no attribute 'window_manager'")
        changed, log = False, io.StringIO()
        with contextlib.redirect_stdout(log), tools.undo_step("Photo to 3D"):
            changed = True
        self.assertTrue(changed)
        self.assertIn("Loopcut: no undo step before 'Photo to 3D'", log.getvalue())


if __name__ == "__main__":
    unittest.main()
