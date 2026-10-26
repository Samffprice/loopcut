"""Inspection boundaries, asynchronous waiting and attachment-time freshness."""
import sys
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import agent, inspection, jobs, llm, tools


class InspectionTests(unittest.TestCase):
    def test_request_limits_bound_the_total_work(self):
        inspection.validate_request("final_lighting", ["A", "B"], [1, 60, 120], ["Cube"], 384, 16, 90)
        invalid = [dict(purpose="unknown"), dict(cameras=["a"] * 2), dict(frames=[True]),
                   dict(frames=[1, 1]), dict(frames=[]), dict(focus="Cube"), dict(samples=65),
                   dict(max_side=641), dict(budget_seconds=181),
                   dict(cameras=["A", "B", "C", "D"], frames=[1, 2, 3, 4])]
        defaults = dict(purpose="geometry", cameras=[], frames=[1], focus=[], max_side=384, samples=16, budget_seconds=90)
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(jobs.JobError):
                inspection.validate_request(**(defaults | override))

    def test_more_than_five_frames_keep_five_from_first_to_last(self):
        # Auto-rig round 7: GPT-6 Luna asked a walk's [1, 4, 7, 10, 13, 238] and got "Choose 1 to 5 frames."
        self.assertEqual(inspection.pick_frames([1, 4, 7, 10, 13, 238]), ([1, 4, 7, 13, 238], [10]))
        kept, left_out = inspection.pick_frames(list(range(120, 0, -1)))
        self.assertEqual((kept, len(left_out)), ([1, 31, 61, 90, 120], 115), "sorted, spread, the ends kept")
        self.assertEqual(inspection.pick_frames([30, 1, 59]), ([30, 1, 59], []), "five or fewer: as asked")
        for asked, error in (([1, 2, 3, 4, 5, True], "frame must be an integer"), ([1, 2, 3, 4, 5, 2], "unique"),
                             ([1, 2, 3, 4, 5, 10 ** 7], "frame must be an integer")):
            with self.subTest(asked=asked), self.assertRaisesRegex(jobs.JobError, error):
                kept, _ = inspection.pick_frames(asked)
                inspection.validate_request("geometry", [], kept, [], 384, 16, 90)
        self.assertEqual(inspection.frames_note({"frames": [1, 4, 7, 13, 238], "frames_left_out": [10]}),
                         "\nInspected 5 of the 6 frames asked (at most 5 at a time), spread from first to last; "
                         "left out: 10.")
        self.assertEqual(inspection.frames_note({"frames": [1]}), "")

    def test_several_cameras_keep_fewer_frames_within_twelve_views(self):
        # Three or four cameras with six frames still failed red: "At most 12 camera/frame combinations".
        walk = [1, 4, 7, 10, 13, 238]
        for cameras, kept in ((["A", "B", "C"], [1, 7, 10, 238]), (["A", "B", "C", "D"], [1, 7, 238]),
                              (["A", "B"], [1, 4, 7, 13, 238])):
            with self.subTest(cameras=len(cameras)):
                picked, left_out = inspection.pick_frames(walk, cameras)
                self.assertEqual(picked, kept)
                inspection.validate_request("geometry", cameras, picked, [], 384, 16, 90)
        self.assertEqual(inspection.pick_frames([1, 2, 3, 4, 5], ["A", "B", "C"])[0], [1, 2, 4, 5], "five, three cameras")
        self.assertEqual(inspection.frames_note({"frames": [1, 7, 10, 238], "frames_left_out": [4, 13],
                                                 "cameras": ["A", "B", "C"]}),
                         "\nInspected 4 of the 6 frames asked (at most 4 at a time with 3 cameras), spread from first "
                         "to last; left out: 4, 13.")

    def test_the_signal_says_exposure_and_colour_in_numbers(self):
        icy = jobs.color_signal([0.58, 0.74, 0.91], 0.0)
        self.assertEqual(icy["mean_hue"], 211)
        self.assertAlmostEqual(icy["mean_saturation"], 0.363, places=3)
        self.assertFalse(icy["too_bright"])
        dark = jobs.color_signal([0.05, 0.045, 0.04], 0.0)
        self.assertLess(dark["mean_luminance"], 0.05)
        self.assertEqual(dark["mean_hue"], 30, "warm, however dim")
        blown = jobs.color_signal([0.97, 0.97, 0.97], 0.62)
        self.assertEqual((blown["clipped"], blown["too_bright"], blown["mean_hue"], blown["mean_saturation"]),
                         (0.62, True, None, 0.0), "grey has no hue")
        self.assertFalse(jobs.color_signal([0.5, 0.5, 0.5], jobs.TOO_BRIGHT)["too_bright"])
        self.assertEqual(jobs.color_signal([1.2, -0.1, 0.0], 0.0)["mean_hue"], 0, "clamped into 0..1")

    def test_inspection_requires_one_bounded_approval(self):
        self.assertEqual(agent._approval("inspect_scene", '{"purpose":"geometry"}', ()), "heavy")
        self.assertFalse(agent._changes_scene("inspect_scene"))

    def test_cancel_wait_sends_cancel_without_blocking_the_main_thread(self):
        with patch.object(jobs, "cancel") as cancel, self.assertRaises(llm.Cancelled):
            inspection.wait_result(Path("job"), lambda: True, lambda fn: self.fail("must not run on main"))
        cancel.assert_called_once_with(Path("job"))

    def test_wait_schedules_only_final_freshness_check_on_main(self):
        result = tools.ToolResult("verified")
        calls = []
        def on_main(fn):
            calls.append(fn)
            future = Future()
            future.set_result(fn())
            return future
        with patch.object(jobs, "status", return_value={"state": "complete"}), \
                patch.object(inspection, "_finish", return_value=(result, False)):
            self.assertIs(inspection.wait_result(Path("job"), lambda: False, on_main), result)
        self.assertEqual(len(calls), 1)

    def on_main(self, fn):
        future = Future()
        future.set_result(fn())
        return future

    def test_a_stale_or_failed_look_names_no_job_id_to_the_user(self):
        # Auto-rig round 7: "Inspection d404909579fc4adc9daf315eb69a48ff is stale: ..." on a red card.
        job = "d404909579fc4adc9daf315eb69a48ff"
        row = {"id": job, "state": "complete", "live_token": {"generation": 1}}
        with patch.object(jobs, "status", return_value=row), patch.object(inspection, "live_token", return_value={"generation": 2}):
            result, stale = inspection._finish(Path(job))
        self.assertTrue(stale)
        self.assertEqual((result.ok, result.text), (False, inspection.STALE))
        self.assertIn("stale", result.text)
        self.assertEqual(result.model_note, f"Inspection job {job}.", "the model may have it")
        with patch.object(jobs, "status", return_value={**row, "state": "failed", "error": "Renderer exited"}):
            result, stale = inspection._finish(Path(job))
        self.assertEqual((stale, result.text), (False, "The inspection failed: Renderer exited"))
        self.assertNotIn(job, result.text)

    def wait(self, finishes):
        """wait_result where _finish gives `finishes` in turn."""
        results = iter(finishes)
        retried = []
        def retry(path):
            retried.append(path)
            return tools.ToolResult("running", pending_job=Path("again"))
        with patch.object(jobs, "status", return_value={"state": "complete"}), \
                patch.object(inspection, "_finish", side_effect=lambda path: next(results)), \
                patch.object(inspection, "retry", side_effect=retry):
            return inspection.wait_result(Path("job"), lambda: False, self.on_main), retried

    def test_a_stale_look_is_tried_once_more(self):
        stale = (tools.ToolResult(inspection.STALE, ok=False), True)
        result, retried = self.wait([stale, (tools.ToolResult("Image attached"), False)])
        self.assertEqual((result.ok, result.text, retried), (True, "Image attached" + inspection.RETRIED_NOTE, [Path("job")]))
        result, retried = self.wait([stale, stale])
        self.assertEqual((result.ok, result.text, retried), (False, inspection.STALE, [Path("job")]), "once only")
        with patch.object(jobs, "status", return_value={"state": "complete"}), \
                patch.object(inspection, "_finish", return_value=stale), \
                patch.object(inspection, "retry", return_value=tools.ToolResult("Camera gone", ok=False)):
            self.assertEqual(inspection.wait_result(Path("job"), lambda: False, self.on_main).text, "Camera gone")

    def test_a_look_paused_for_says_so(self):
        row = {"id": "a" * 32, "state": "complete", "live_token": {"generation": 1}, "output": "/out",
               "scene_revision": "f" * 64, "frames": [1], "paused_playback": True}
        metadata = {"sheet_sha256": "s", "scene_revision": "f" * 64, "views": []}
        with patch.object(jobs, "status", return_value=row), patch.object(inspection, "live_token", return_value={"generation": 1}), \
                patch.object(jobs, "read_json", return_value=metadata), patch.object(jobs, "digest", return_value="s"):
            self.assertTrue(inspection._finish(Path("a" * 32))[0].text.endswith(inspection.PAUSED_NOTE))
            row["paused_playback"] = False
            self.assertNotIn(inspection.PAUSED_NOTE, inspection._finish(Path("a" * 32))[0].text)

    def blender(self):
        """A stand-in for the bpy playback reads: is_animation_playing Blender-wide, true for a timer or a
        scrub, as ED_screen_animation_playing is; the operators run the playback handlers as Blender's do."""
        test = self
        class Thing:
            count = 0
            def __init__(self, **fields):
                Thing.count += 1
                self.pointer = Thing.count
                self.__dict__.update(fields)
            def as_pointer(self):
                return self.pointer
            def __eq__(self, other):
                return isinstance(other, Thing) and other.pointer == self.pointer
            __hash__ = object.__hash__
        class Screen(Thing):
            @property
            def is_animation_playing(self):
                return fake.timer is not None or any(w.screen.is_scrubbing for w in fake.windows)
        class Override:
            def __init__(self, **context):
                self.context = context
            def __enter__(self):
                fake.context.window, fake.context.area = self.context["window"], self.context.get("area")
            def __exit__(self, *exc):
                fake.context.window = fake.context.area = None
        def cancel(restore_frame):
            fake.calls.append(("pause", fake.context.window.name, restore_frame))
            fake.timer = None
            inspection._playback_stopped(None, None)
        def play():
            area = fake.context.area
            scene = area.scene if area is not None else fake.context.window.scene
            fake.calls.append(("play", fake.context.window.name, area.type if area else None, scene.name))
            fake.timer = fake.context.window
            inspection._playback_started(scene, None)
        A, B, C = Thing(name="A", frame_current=10), Thing(name="B", frame_current=20), Thing(name="Edit", frame_current=5)
        sequencer = Thing(type="SEQUENCE_EDITOR", scene=C)
        windows = [Thing(name="w1", scene=A, screen=Screen(is_scrubbing=False, areas=[])),
                   Thing(name="w2", scene=B, screen=Screen(is_scrubbing=False, areas=[sequencer]))]
        fake = types.SimpleNamespace(
            timer=None, calls=[], windows=windows, scenes=(A, B, C), sequencer=sequencer,
            context=types.SimpleNamespace(window_manager=types.SimpleNamespace(windows=windows), temp_override=Override,
                                          window=None, area=None),
            data=types.SimpleNamespace(scenes=[A, B, C]),
            ops=types.SimpleNamespace(screen=types.SimpleNamespace(animation_cancel=cancel, animation_play=play)))
        for name in ("_paused", "_playback_events"):
            test.addCleanup(setattr, inspection, name, getattr(inspection, name))
        test.addCleanup(inspection._playing.clear)
        inspection._paused = None
        inspection._playing.clear()
        return fake

    def user_plays(self, fake, window, area=None):
        with fake.context.temp_override(window=window, area=area):
            fake.ops.screen.animation_play()

    def test_scrubbing_is_not_playing(self):
        # Screen.is_animation_playing is also true while the playhead is dragged: "pausing" did nothing
        # then, and the batch's end started playback nobody had started.
        fake = self.blender()
        with patch.dict(sys.modules, {"bpy": fake}):
            fake.windows[0].screen.is_scrubbing = True
            self.assertTrue(fake.windows[1].screen.is_animation_playing, "Blender-wide, as Blender's is")
            self.assertFalse(inspection.playing())
            self.assertFalse(inspection.pause_playback())
            inspection.resume_paused()
        self.assertEqual(fake.calls, [])
        self.assertFalse(inspection.paused())

    def test_playback_pauses_and_plays_again_where_it_played(self):
        fake = self.blender()
        w1, w2 = fake.windows
        with patch.dict(sys.modules, {"bpy": fake}):
            self.user_plays(fake, w2)  # Window two, not windows[0]; its own scene B.
            self.assertTrue(inspection.playing())
            self.assertTrue(inspection.pause_playback())
            self.assertFalse(inspection.pause_playback(), "nothing plays now")
            inspection.resume_paused()
            self.assertFalse(inspection.paused())
            fake.timer = None
            self.user_plays(fake, w2, area=fake.sequencer)  # A sequencer plays its own scene.
            inspection.pause_playback()
            inspection.resume_paused()
        self.assertEqual(fake.calls, [("play", "w2", None, "B"), ("pause", "w2", False), ("play", "w2", None, "B"),
                                      ("play", "w2", "SEQUENCE_EDITOR", "Edit"), ("pause", "w2", False),
                                      ("play", "w2", "SEQUENCE_EDITOR", "Edit")])

    def test_nothing_plays_again_once_the_user_took_over(self):
        fake = self.blender()
        w1, w2 = fake.windows
        B = fake.scenes[1]
        def took_over(change):
            fake.timer, w2.scene, B.frame_current = None, B, 20
            self.user_plays(fake, w2)
            inspection.pause_playback()
            change()
            fake.calls.clear()
            inspection.resume_paused()
            return fake.calls, inspection.paused()
        def play_then_stop():  # Space twice.
            self.user_plays(fake, w2)
            with fake.context.temp_override(window=w2):
                fake.ops.screen.animation_cancel(restore_frame=False)
        with patch.dict(sys.modules, {"bpy": fake}):
            self.assertEqual(took_over(play_then_stop), ([], False))
            self.assertEqual(took_over(lambda: setattr(B, "frame_current", 99)), ([], False), "scrubbed or stepped")
            self.assertEqual(took_over(lambda: setattr(w2, "scene", fake.scenes[0])), ([], False), "another scene")
            self.assertEqual(took_over(lambda: None), ([("play", "w2", None, "B")], False), "nothing changed")

    def test_pause_or_resume_failing_never_ends_the_turn(self):
        fake = self.blender()
        def refuse(**kwargs):
            raise RuntimeError("Operator bpy.ops.screen.animation_cancel.poll() failed, context is incorrect")
        with patch.dict(sys.modules, {"bpy": fake}):
            self.user_plays(fake, fake.windows[0])
            fake.ops.screen.animation_cancel = refuse
            self.assertFalse(inspection.pause_playback())
            inspection._paused = {"window": 404, "scene": 404, "area": None, "frame": 1, "events": 0}
            inspection.resume_paused()  # A window that is gone: nothing, no raise.
        self.assertFalse(inspection.paused())

    def test_the_agent_plays_again_only_what_a_look_paused_and_never_from_a_worker(self):
        ran = []
        def on_main(fn):
            ran.append(fn)
            return self.on_main(fn)
        lead, worker = types.SimpleNamespace(role="single"), types.SimpleNamespace(role="worker")
        with patch("loopcut.mainthread.run_on_main", side_effect=on_main), \
                patch.object(inspection, "resume_paused") as resume:
            agent._resume_playback(lead)
            self.assertEqual(ran, [], "nothing paused: no main-thread call")
            with patch.object(inspection, "_paused", {"window": 2}):
                agent._resume_playback(worker)
                self.assertEqual(ran, [], "the lead's pause, not a worker's to resume")
                agent._resume_playback(lead)
        resume.assert_called_once_with()

    def test_later_mutation_in_same_batch_omits_old_inspection_image(self):
        card = {"status": "done", "output": "inspection from revision 1"}
        message = {"role": "tool", "content": "inspection from revision 1"}
        with patch.object(agent, "_inspection_current", return_value=False):
            images = agent._fresh_images([("image", "look", False, {"generation": 1}, card, message)])
        self.assertEqual(images, [])
        self.assertEqual(card["status"], "failed")
        self.assertIn("omitted", message["content"])

    def test_fresh_and_historical_noninspection_images_are_kept(self):
        with patch.object(agent, "_inspection_current", return_value=True):
            self.assertEqual(agent._fresh_images([("a", "look", False, {}, {}, {}),
                                                 ("b", "last render", True, None, {}, {})]),
                             [("a", "look", False), ("b", "last render", True)])

    def test_inspection_does_not_change_automatic_viewport_angle(self):
        self.assertEqual(agent._asked_angle("inspect_scene", '{"cameras":["Side"]}'), "")


if __name__ == "__main__":
    unittest.main()
