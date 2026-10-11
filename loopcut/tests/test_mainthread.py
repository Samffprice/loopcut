"""The main-thread pump's freeze log: a task or a late pump past SLOW_SECONDS is printed and kept,
named, so a tester's console says what held the interface (loopcut/track/FREEZE-2026-10-10.md)."""

import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import mainthread  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_700_000_000.0


class SlowTaskTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.enterContext(mock.patch.object(mainthread, "time", self.clock))
        self.enterContext(mock.patch.object(mainthread, "slow", mainthread.collections.deque(maxlen=20)))
        self.enterContext(mock.patch.object(mainthread, "_animating", return_value=None))
        self.enterContext(mock.patch.object(mainthread, "_redraw_requested", False))
        self.enterContext(mock.patch.object(mainthread, "_pumped_at", 0.0))
        self.busy = self.enterContext(mock.patch.object(mainthread, "_turn_running", return_value=True))
        while not mainthread._jobs.empty():
            mainthread._jobs.get_nowait()

    def pump(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mainthread._pump()
        return out.getvalue()

    def task(self, seconds: float, result="done"):
        def work():
            self.clock.now += seconds
            return result
        return work

    def test_a_slow_task_is_printed_and_kept_with_its_name(self):
        future = mainthread.run_on_main(mainthread.named("the capture_viewport tool", self.task(1.2, "image")))
        printed = self.pump()
        self.assertEqual(future.result(timeout=0), "image")
        self.assertIn("blocked for 1200 ms by the capture_viewport tool", printed)
        self.assertEqual([(what, ms) for _, what, ms in mainthread.slow], [("the capture_viewport tool", 1200)])

    def test_a_quick_task_leaves_no_line(self):
        mainthread.run_on_main(self.task(0.02))
        self.assertEqual(self.pump(), "")
        self.assertEqual(len(mainthread.slow), 0)

    def test_an_unnamed_task_goes_by_its_qualified_name(self):
        def save_checkpoint():
            self.clock.now += 0.4
        mainthread.run_on_main(save_checkpoint)
        self.assertIn("by SlowTaskTest.test_an_unnamed_task_goes_by_its_qualified_name.<locals>.save_checkpoint",
                      self.pump())

    def test_a_failing_slow_task_still_reaches_its_waiter_and_the_log(self):
        def broken():
            self.clock.now += 0.3
            raise RuntimeError("no viewport")
        future = mainthread.run_on_main(mainthread.named("the capture_viewport tool", broken))
        self.assertIn("300 ms", self.pump())
        with self.assertRaisesRegex(RuntimeError, "no viewport"):
            future.result(timeout=0)

    def test_a_late_pump_during_a_turn_is_logged_outside_any_task(self):
        self.pump()  # Sets the time the pump last returned.
        self.clock.now += mainthread._INTERVAL + 0.9
        printed = self.pump()
        self.assertIn("blocked for 900 ms by Blender's main thread outside Loopcut's tasks", printed)

    def test_a_late_pump_with_no_turn_running_is_not_logged(self):
        self.busy.return_value = False
        self.pump()
        self.clock.now += 5.0  # The user rendering, or Blender in the background: none of Loopcut's business.
        self.assertEqual(self.pump(), "")
        self.assertEqual(len(mainthread.slow), 0)

    def test_the_log_keeps_only_the_latest(self):
        for _ in range(25):
            mainthread.run_on_main(self.task(0.2))
            self.pump()
        self.assertEqual(len(mainthread.slow), 20)


if __name__ == "__main__":
    unittest.main()
