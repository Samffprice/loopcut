"""repeats.py: repeated failures are annotated, never refused; a read the model already has is not run a third time."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut import repeats  # noqa: E402


def failed_run(error: str, line: int = 3) -> str:
    """A run_python result as tools.run_python writes it when the code raised."""
    return (f"making the plane\nTraceback (most recent call last):\n  File \"<loopcut>\", line {line}, in <module>\n"
            f"    plane.modifiers['Scatter'].node_group.inputs\n{error}\n\nScene changes: none")


def run_key(summary: str) -> tuple:
    return repeats.call_key("run_python", f'{{"code": "x", "summary": "{summary}"}}')


class NormalisingTest(unittest.TestCase):
    def test_trivially_different_errors_collide(self):
        a = repeats.normalised(repeats.error_line(failed_run(
            "AttributeError: 'NoneType' object has no attribute 'inputs'", line=3)))
        b = repeats.normalised(repeats.error_line(failed_run(
            "AttributeError: 'Object' object has no attribute 'node_tree'", line=12)))
        self.assertEqual(a, b)
        self.assertEqual(repeats.normalised("RuntimeError: <bpy_struct, Object at 0x7f00a1> freed, 2.5 s"),
                         repeats.normalised("RuntimeError: <bpy_struct, Object at 0x10ce3> freed, 11 s"))
        self.assertEqual(repeats.normalised('KeyError: \'bpy_prop_collection[key]: key "Principled BSDF" not found\''),
                         repeats.normalised('KeyError: \'bpy_prop_collection[key]: key "Glass BSDF" not found\''))

    def test_different_errors_do_not(self):
        self.assertNotEqual(repeats.normalised("AttributeError: 'Object' object has no attribute 'inputs'"),
                            repeats.normalised("TypeError: 'Object' object is not subscriptable"))

    def test_what_went_wrong_is_read_from_each_kind_of_result(self):
        self.assertEqual(repeats.error_line(failed_run("KeyError: 'Scatter'")), "KeyError: 'Scatter'")
        self.assertEqual(repeats.error_line("printed\nTraceback (most recent call last):\n  File \"<loopcut>\", "
                                            "line 1\nRuntimeError: Error: Cannot apply\nmodifier\n\nScene changes: none"),
                         "RuntimeError: Error: Cannot apply modifier")
        self.assertEqual(repeats.error_line("step 1\nStopped: the code ran for more than 60 s, which usually means "
                                            "a loop\n\nScene changes: none"), "Stopped: the code ran for more than 60 s")
        self.assertEqual(repeats.error_line("No such object(s): Plane. Similar names: Plane.001."),
                         "No such object(s): Plane. Similar names: Plane.001.")

    def test_identical_calls_share_a_key_whatever_the_order_or_spacing(self):
        self.assertEqual(repeats.call_key("search_assets", '{"source": "polyhaven", "query": "wood table"}'),
                         repeats.call_key("search_assets", '{ "query":"wood table","source":"polyhaven" }'))
        self.assertNotEqual(repeats.call_key("search_assets", '{"source": "polyhaven", "query": "wood table"}'),
                            repeats.call_key("search_assets", '{"source": "polyhaven", "query": "oak table"}'))
        self.assertNotEqual(repeats.call_key("search_assets", '{"source": "polyhaven", "query": "wood table"}'),
                            repeats.call_key("search_assets", '{"source": "blenderkit", "query": "wood table"}'))


class NoProgressTest(unittest.TestCase):
    def test_eight_failures_of_the_same_normalised_call_stop(self):
        guard = repeats.NoProgress()
        a = repeats.call_key("get_object_info", '{"name":"Cube", "detail": 2}')
        b = repeats.call_key("get_object_info", '{ "detail":2, "name": "Cube" }')
        for attempt in range(1, 9):
            self.assertEqual(guard.record(a if attempt % 2 else b, False, False), attempt == 8)

    def test_scene_change_or_explicit_reset_clears_every_key(self):
        keys = [run_key("A"), run_key("B")]
        for reset in (lambda g: g.record(run_key("Change"), False, True),
                      lambda g: g.record(run_key("Change"), True, True), lambda g: g.reset()):
            with self.subTest(reset=reset):
                guard = repeats.NoProgress()
                for _ in range(7):
                    for key in keys:
                        self.assertFalse(guard.record(key, False, False))
                reset(guard)
                for _ in range(7):
                    for key in keys:
                        self.assertFalse(guard.record(key, False, False))
                for key in keys:
                    self.assertTrue(guard.record(key, False, False))

    def test_alternating_failures_are_counted_per_call(self):
        a = run_key("A")
        for b in (run_key("B"), ("other_tool", a[1])):
            with self.subTest(other=b):
                guard = repeats.NoProgress()
                for _ in range(7):
                    self.assertFalse(guard.record(a, False, False))
                    self.assertFalse(guard.record(b, False, False))
                self.assertTrue(guard.record(a, False, False))

    def test_success_resets_its_key_only(self):
        guard = repeats.NoProgress()
        a, b = run_key("A"), run_key("B")
        for _ in range(7):
            self.assertFalse(guard.record(a, False, False))
            self.assertFalse(guard.record(b, False, False))
        self.assertFalse(guard.record(a, True, False))
        for _ in range(7):
            self.assertFalse(guard.record(a, False, False))
        self.assertTrue(guard.record(b, False, False))
        self.assertTrue(guard.record(a, False, False))

    def test_successful_reads_of_other_keys_do_not_reset_failures(self):
        guard = repeats.NoProgress()
        key = run_key("Try")
        for _ in range(7):
            self.assertFalse(guard.record(key, False, False))
            self.assertFalse(guard.record(("get_scene_info", "{}"), True, False))
        self.assertTrue(guard.record(key, False, False))

    def test_refusals_count_like_failures(self):
        guard = repeats.NoProgress()
        key = run_key("Try")
        for _ in range(3):
            self.assertFalse(guard.record(key, False, False))
            self.assertFalse(guard.skip(key))
        self.assertFalse(guard.skip(key))
        self.assertTrue(guard.skip(key))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = repeats.Ledger()

    def test_the_same_error_is_listed_the_second_time_and_called_not_working_after(self):
        error = "AttributeError: 'NoneType' object has no attribute 'inputs'"
        self.assertEqual(self.ledger.record(4, run_key("Add scatter"), failed_run(error), ok=False), "")
        second = self.ledger.record(6, run_key("Fix socket"), failed_run(error, line=9), ok=False)
        self.assertEqual(second, '\n\n(Same error as before. Already tried: step 4 "Add scatter".)')
        third = self.ledger.record(7, run_key("Try again"), failed_run(error), ok=False)
        self.assertIn("This error has now come up 3 times. Already tried: step 4 \"Add scatter\", "
                      "step 6 \"Fix socket\".", third)
        self.assertIn("This approach is not working", third)
        self.assertIn("tell the user what is blocking you", third)
        self.assertEqual(third.count("\n\n"), 1, "one line")
        self.assertEqual(self.ledger.refusal(run_key("Try again")), "", "code is never refused")

    def test_the_list_of_attempts_stays_short(self):
        error = "KeyError: 'Scatter'"
        for step in range(1, 9):
            note = self.ledger.record(step, run_key(f"try {step}"), failed_run(error), ok=False)
        self.assertIn('Already tried: 3 earlier, then step 4 "try 4", step 5 "try 5", step 6 "try 6", step 7 "try 7".', note)

    def test_the_same_error_from_another_tool_is_another_error(self):
        self.ledger.record(1, run_key("a"), failed_run("KeyError: 'Scatter'"), ok=False)
        key = repeats.call_key("get_object_info", '{"names": ["Scatter"]}')
        self.assertEqual(self.ledger.record(2, key, "KeyError: 'Scatter'", ok=False), "")

    def test_identical_shell_failure_points_to_files_on_the_second_attempt_ignoring_summary(self):
        command = 'python3 -c "from pathlib import Path; print(Path.cwd())"'
        for step in (1, 2, 3):
            key = repeats.call_key("shell", json.dumps({"command": command, "summary": f"Attempt {step}"}))
            text = (f'exit 1 in {step / 10:.1f} s\n  File "<string>", line 1\n    "from\n    ^\n'
                    'SyntaxError: unterminated string literal (detected at line 1)')
            note = self.ledger.record(step, key, text, ok=False)
            if step == 1:
                self.assertEqual(note, "")
            else:
                self.assertIn(f"same shell command has failed the same way {step} times this turn", note)
                self.assertIn("write_file", note)
                self.assertIn("python3 file.py", note)
                self.assertNotIn("inspect_api", note)

    def test_different_shell_errors_do_not_count_as_identical_failures(self):
        key = repeats.call_key("shell", '{"command":"python script.py"}')
        for step, error in enumerate(("ValueError: bad value", "TypeError: bad type"), 1):
            self.assertEqual(self.ledger.record(step, key, f"exit 1 in 0.1 s\n{error}", ok=False), "")
        second = self.ledger.record(3, key, "exit 1 in 0.2 s\nValueError: bad value", ok=False)
        self.assertIn("same way 2 times this turn", second)
        fresh = repeats.Ledger()
        self.assertEqual(fresh.record(1, key, "exit 1 in 0.1 s\nValueError: bad value", ok=False), "")

    def test_refused_inline_shell_repeats_get_the_same_working_path(self):
        key = repeats.call_key("shell", '{"command":"python -c x"}')
        error = "Not run: inline Python in Windows shell commands can change the code. Write a .py file."
        self.assertEqual(self.ledger.record(1, key, error, ok=False), "")
        self.assertIn("write_file", self.ledger.record(2, key, error, ok=False))

    def test_failing_runs_in_a_row_are_nudged_whatever_the_errors(self):
        errors = ["NameError: name 'x' is not defined", "KeyError: 'Scatter'",
                  "TypeError: bpy_struct: item.attr = val: enum \"X\" not found", "ValueError: bad value"]
        notes = [self.ledger.record(step, run_key(f"s{step}"), failed_run(e), ok=False) for step, e in enumerate(errors, 1)]
        self.assertEqual(notes[:3], ["", "", ""])
        self.assertIn("4 run_python calls in a row have failed. Step back", notes[3])
        self.ledger.record(5, run_key("works"), "OK (no output)\n\nScene changes: none", ok=True)
        self.assertEqual(self.ledger.record(6, run_key("s6"), failed_run("IndexError: list index out of range"), ok=False), "")

    def test_a_streak_of_one_error_gets_one_note_not_two(self):
        for step in range(1, 5):
            note = self.ledger.record(step, run_key(f"s{step}"), failed_run("KeyError: 'Scatter'"), ok=False)
        self.assertIn("This error has now come up 4 times", note)
        self.assertNotIn("in a row", note)

    def test_other_tools_do_not_break_a_streak_of_failing_runs(self):
        for step in range(1, 4):
            self.ledger.record(step, run_key(f"s{step}"), failed_run(f"ValueError: v{'x' * step}"), ok=False)
            self.ledger.record(step, repeats.call_key("inspect_api", f'{{"path": "p{step}"}}'), "docs", ok=True)
        self.assertIn("in a row", self.ledger.record(4, run_key("s4"), failed_run("OSError: o"), ok=False))

    def test_the_same_read_is_nudged_the_second_time_and_not_run_the_third(self):
        key = repeats.call_key("search_assets", '{"source": "polyhaven", "query": "wood table"}')
        self.assertEqual(self.ledger.record(1, key, "3 results: ...", ok=True, read=True, where="step 1"), "")
        self.assertEqual(self.ledger.refusal(key), "")
        second = self.ledger.record(2, key, "3 results: ...", ok=True, read=True, where="step 2")
        self.assertIn("Same result as your identical call at step 1", second)
        refusal = self.ledger.refusal(key, in_view=lambda where: where == "step 2")
        self.assertTrue(refusal.startswith("Not run: you made this same call at steps 1 and 2"))
        self.assertIn("the result has not changed: see step 2", refusal)

    def test_a_scene_change_or_a_different_result_lets_a_read_run_again(self):
        key = repeats.call_key("get_object_info", '{"names": ["Cube"]}')
        self.ledger.record(1, key, "cube v1", ok=True, read=True)
        self.ledger.record(2, key, "cube v1", ok=True, read=True)
        self.ledger.scene_changed()
        self.assertEqual(self.ledger.refusal(key), "")
        self.assertEqual(self.ledger.record(3, key, "cube v2", ok=True, read=True), "")
        self.assertEqual(self.ledger.record(4, key, "cube v3", ok=True, read=True), "", "the user edited it")
        self.assertEqual(self.ledger.refusal(key), "")

    def test_a_read_whose_result_was_folded_away_runs_again(self):
        key = repeats.call_key("get_scene_info", "{}")
        for step in (1, 2):
            self.ledger.record(step, key, "12 objects", ok=True, read=True, where=step)
        self.assertEqual(self.ledger.refusal(key, in_view=lambda where: False), "")

    def test_a_failing_read_gets_the_error_note_not_the_same_result_note(self):
        key = repeats.call_key("get_object_info", '{"names": ["Plane"]}')
        self.ledger.record(1, key, "No such object(s): Plane.", ok=False, read=True)
        second = self.ledger.record(2, key, "No such object(s): Plane.", ok=False, read=True)
        self.assertIn("Same error as before", second)
        self.assertNotIn("Same result", second)
        self.assertTrue(self.ledger.refusal(key).startswith("Not run"))


def status_key(**arguments) -> tuple:
    return repeats.call_key("render_job", json.dumps({"action": "status", "job_id": "j1", **arguments}))


RUNNING = '{"id": "j1", "state": "running", "completed": 4, "total": 48}'


class PollingTest(unittest.TestCase):
    """An eval turn spent 38 of 60 steps on a render job: status, then run_python sleeping, 19 times."""

    def setUp(self):
        self.ledger = repeats.Ledger()

    def test_polling_a_running_job_is_pointed_to_wait_from_the_third_time(self):
        notes = [self.ledger.record(step, status_key(), RUNNING, ok=True) for step in (1, 2, 3, 4)]
        self.assertEqual(notes[:2], ["", ""])
        self.assertIn("Status checked 3 times this turn while the job runs", notes[2])
        self.assertIn("call status with wait=<seconds>", notes[2])
        self.assertIn("Status checked 4 times", notes[3])

    def test_waiting_or_a_finished_job_is_not_polling(self):
        done = '{"id": "j1", "state": "complete"}'
        for step in range(1, 5):
            self.assertEqual(self.ledger.record(step, status_key(wait=120), RUNNING, ok=True), "")
            self.assertEqual(self.ledger.record(step, status_key(), done, ok=True), "")
        self.assertEqual(self.ledger.record(5, status_key(), RUNNING, ok=True), "", "the first real poll")

    def test_listing_jobs_while_one_runs_counts_too(self):
        listed = repeats.call_key("render_job", '{"action": "status"}')
        notes = [self.ledger.record(step, listed, f"[{RUNNING}]", ok=True) for step in (1, 2, 3)]
        self.assertIn("wait=<seconds>", notes[2])

    def test_sleeping_right_after_a_render_job_call_is_nudged(self):
        sleep = repeats.call_key("run_python", json.dumps({"code": "import time\ntime.sleep(30)", "summary": "Wait"}))
        self.assertEqual(self.ledger.record(1, sleep, "Scene changes: none", ok=True), "", "no job call before it")
        self.ledger.record(2, status_key(), RUNNING, ok=True)
        note = self.ledger.record(2, sleep, "Scene changes: none", ok=True)
        self.assertIn("Sleeping in run_python holds Blender's main thread", note)
        self.assertIn("render_job status with wait=<seconds>", note)
        listing = repeats.call_key("run_python", json.dumps({"code": "print(os.listdir(out))", "summary": "Look"}))
        self.ledger.record(3, status_key(), RUNNING, ok=True)
        self.assertEqual(self.ledger.record(3, listing, "frame_0000001.png", ok=True), "", "only sleeping is caught")


if __name__ == "__main__":
    unittest.main()
