"""Before a long render, the cloud or this computer (render_offer.py, 0.2.19).

The trigger: Loopcut's own measured local frame, never a number from the model; the choice card in the paid call's
place; the laptop pick's local job and the model's result; the short-of-credits variant. No Blender: bpy is a stand-in
and the quote comes from an in-memory module.
"""

import json
import math
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import (agent, checkpoints, cloud, cloud_account, cloud_http, config, conversations, files,  # noqa: E402
                     job_tools, jobs, mainthread, render_offer, spending, state, tools)
from loopcut.ui import layout, theme as T  # noqa: E402

import bpy  # noqa: E402  The stand-in above, or the one another test module installed first.

KEY = ("/projects/garden.blend", "Scene")
UHD = {"engine": "BLENDER_EEVEE", "width": 3840, "height": 2160, "samples": 64}
PROJECT = {"id": "local", "no_generation": False}
RENDER = {"name": "render", "version": 1, "backend": "managed", "kind": "assist", "latency": "long",
          "cost": {"credits_estimate": 5, "quote_required": True}, "file_inputs": [],
          "approval": {"confirm_over_credits": 100}, "title": "Render scene"}


def scene(engine="BLENDER_EEVEE", x=3840, y=2160, pct=100, samples=64, fps=24, camera="Camera"):
    render = types.SimpleNamespace(engine=engine, resolution_x=x, resolution_y=y, resolution_percentage=pct,
                                   fps=fps, fps_base=1.0)
    return types.SimpleNamespace(name="Scene", render=render, cycles=types.SimpleNamespace(samples=samples),
                                 eevee=types.SimpleNamespace(taa_render_samples=samples),
                                 camera=types.SimpleNamespace(name=camera) if camera else None)


def job_row(seconds=31.0, rendered=1, **changes):
    row = {"id": "c" * 32, "kind": "render", "source": KEY[0], "scene": KEY[1], **UHD, "frames": [900],
           "state": "complete", "created": time.time() - 100, "updated": time.time() - 10,
           "progress": {"rendered": rendered, "completed": rendered, "total": 1, "elapsed_seconds": seconds * rendered}}
    row.update(changes)
    return row


class Timings(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(render_offer, "_timings", [])
        patcher.start()
        self.addCleanup(patcher.stop)
        render_offer.take_last()


class TimingTest(Timings):
    """What a frame took here: see_render's renders and render jobs' frames, matched exactly."""

    def test_frame_line(self):
        self.assertEqual(render_offer.frame_line(31.4), "31 s a frame")
        self.assertEqual(render_offer.frame_line(4.25), "4.2 s a frame")
        self.assertEqual(render_offer.frame_line(125), "2 min 5 s a frame")
        self.assertEqual(render_offer.frame_line(120), "2 min a frame")
        for nothing in (0, None, float("nan"), -3):
            self.assertEqual(render_offer.frame_line(nothing), "")

    def test_only_the_same_scene_engine_size_and_samples_count(self):
        render_offer.record(KEY, UHD, 31.0)
        self.assertEqual(render_offer.seconds_per_frame(KEY, UHD), 31.0)
        for other in ({**UHD, "samples": 128}, {**UHD, "width": 1920, "height": 1080}, {**UHD, "engine": "CYCLES"}):
            with self.subTest(other=other):
                self.assertIsNone(render_offer.seconds_per_frame(KEY, other), "no guess from other settings")
        self.assertIsNone(render_offer.seconds_per_frame(("/projects/other.blend", "Scene"), UHD))

    def test_render_jobs_count_and_the_latest_timing_wins(self):
        self.assertEqual(render_offer.seconds_per_frame(KEY, UHD, [job_row(40.0)]), 40.0)
        render_offer.record(KEY, UHD, 31.0)  # Made now, after the job's last frame.
        self.assertEqual(render_offer.seconds_per_frame(KEY, UHD, [job_row(40.0)]), 31.0)
        later = job_row(36.0, rendered=3, updated=10 ** 12)
        self.assertEqual(render_offer.seconds_per_frame(KEY, UHD, [job_row(40.0), later]), 36.0)

    def test_a_jobs_frames_after_its_cold_first_one(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            files = [{"file": f"frame_{k:07d}.png"} for k in (1, 2, 3)]
            for k, record in enumerate(files):
                path = Path(folder) / record["file"]
                path.write_bytes(b"png")
                os.utime(path, (1000.0 + 12 + 4 * k, 1000.0 + 12 + 4 * k))  # Ready at 12 s, 16 s, 20 s.
            progress = {"rendered": 3, "completed": 3, "total": 3, "elapsed_seconds": 20.0, "files": files}
            row = job_row(output=folder, progress=progress)
            self.assertEqual(render_offer.seconds_per_frame(KEY, UHD, [row]), 4.0, "not 20 / 3")
            self.assertEqual(render_offer.job_line(row), "4 s a frame")
            resumed = job_row(output=folder, progress={**progress, "reused": 1})
            self.assertAlmostEqual(render_offer.seconds_per_frame(KEY, UHD, [resumed]), 20 / 3, msg="no file times")
            gone = job_row(output=folder + "-moved", progress=progress)
            self.assertAlmostEqual(render_offer.seconds_per_frame(KEY, UHD, [gone]), 20 / 3)

    def test_jobs_without_a_rendered_frame_or_of_another_kind_do_not_count(self):
        rows = [job_row(rendered=0), job_row(kind="inspection"), job_row(scene="Other"),
                job_row(progress={"rendered": 1, "elapsed_seconds": 0})]
        self.assertIsNone(render_offer.seconds_per_frame(KEY, UHD, rows))
        self.assertEqual(render_offer.job_line(job_row(31.0)), "31 s a frame")
        self.assertEqual(render_offer.job_line(job_row(rendered=0)), "")

    def test_a_day_old_timing_is_never_used(self):
        render_offer.record(KEY, UHD, 31.0)
        later = time.time() + render_offer.MAX_AGE + 60
        self.assertIsNone(render_offer.seconds_per_frame(KEY, UHD, now=later))
        with patch.object(render_offer, "_timings", []):
            self.assertIsNone(render_offer.seconds_per_frame(KEY, UHD, [job_row(updated=time.time() - 25 * 3600)]))
            self.assertEqual(render_offer.seconds_per_frame(KEY, UHD, [job_row(40.0)]), 40.0)

    def test_cycles_counts_only_frames_rendered_on_the_cpu_as_the_laptop_job_renders(self):
        cycles = {"engine": "CYCLES", "width": 1920, "height": 1080, "samples": 128}
        render_offer.record(KEY, {**cycles, "device": "GPU"}, 3.0)  # see_render on the Metal GPU.
        self.assertIsNone(render_offer.seconds_per_frame(KEY, cycles), "a GPU frame understates the laptop job")
        self.assertEqual(render_offer.seconds_per_frame(KEY, cycles, [job_row(20.0, **cycles)]), 20.0,
                         "render jobs render Cycles on the CPU")
        render_offer.record(KEY, {**cycles, "device": "CPU"}, 22.0)
        self.assertEqual(render_offer.seconds_per_frame(KEY, cycles), 22.0)

    def test_the_device_a_render_of_the_scene_uses(self):
        def device(scene_device, kind):
            current = scene(engine="CYCLES")
            current.cycles.device = scene_device
            addon = types.SimpleNamespace(preferences=types.SimpleNamespace(compute_device_type=kind))
            preferences = types.SimpleNamespace(addons={"cycles": addon})
            with patch.object(bpy, "context", types.SimpleNamespace(preferences=preferences), create=True):
                return render_offer.settings(current)["device"]
        self.assertEqual([device("GPU", "METAL"), device("GPU", "NONE"), device("CPU", "METAL")], ["GPU", "CPU", "CPU"])
        self.assertEqual(render_offer.settings(scene())["device"], "GPU", "EEVEE")

    def test_eevee_next_is_eevee(self):
        render_offer.record(KEY, {**UHD, "engine": "BLENDER_EEVEE"}, 31.0)
        self.assertEqual(render_offer.seconds_per_frame(KEY, UHD), 31.0)
        self.assertEqual(render_offer.settings(scene(engine="BLENDER_EEVEE_NEXT"))["engine"], "BLENDER_EEVEE")


class TestRenderRowTest(Timings):
    """The test render's own card says what a frame took ("31 s a frame")."""

    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        from loopcut import attachments, scratch
        for target in (patch.object(attachments, "_to_png", return_value=(768, 432)),
                       patch.object(scratch, "folder", return_value=Path(self.temp.name)),
                       patch.object(files.cost_guard, "render_refusal", return_value=""),
                       patch.object(bpy, "context", types.SimpleNamespace(scene=scene()), create=True),
                       patch.object(bpy, "data", types.SimpleNamespace(filepath=KEY[0]), create=True)):
            target.start()
            self.addCleanup(target.stop)

    def fake_render(self, seconds):
        """bpy.ops.render.render that writes its PNG and takes `seconds` on the monotonic clock."""
        clock = iter([100.0, 100.0 + seconds])
        image = types.SimpleNamespace(media_type="", file_format="", color_depth="8", color_mode="RGBA")
        current = bpy.context.scene
        current.render.filepath, current.render.use_file_extension, current.render.image_settings = "", True, image

        def render(write_still):
            Path(current.render.filepath).write_bytes(b"png")
        ops = types.SimpleNamespace(render=types.SimpleNamespace(render=render))
        return (patch.object(bpy, "ops", ops, create=True),
                patch.object(files.time, "monotonic", side_effect=lambda: next(clock)))

    def test_a_full_render_records_its_frame_and_says_so_on_its_card(self):
        ops, clock = self.fake_render(31.2)
        with ops, clock:
            result = files.see_render(render=True, full=True)
        self.assertEqual(result.outcome, "31 s a frame")
        self.assertAlmostEqual(render_offer.seconds_per_frame(KEY, UHD), 31.2)

    def test_a_preview_records_at_its_own_size_without_a_card_line(self):
        ops, clock = self.fake_render(2.0)
        with ops, clock:
            result = files.see_render(render=True)
        self.assertEqual(result.outcome, "")
        self.assertIsNone(render_offer.seconds_per_frame(KEY, UHD), "a preview's size and samples are not the user's")
        preview = {"engine": "BLENDER_EEVEE", "width": 768, "height": 432, "samples": 32}
        self.assertEqual(render_offer.seconds_per_frame(KEY, preview), 2.0)

    def test_no_line_from_an_earlier_render(self):
        render_offer.record(KEY, UHD, 31.0)
        with patch.object(files, "_render_now", return_value="render"):
            self.assertEqual(files.see_render(render=True, full=True).outcome, "")

    def test_a_render_jobs_status_card_says_what_a_frame_took(self):
        row = job_row(31.0)
        self.assertEqual(job_tools._status(row, False).outcome, "31 s a frame")
        self.assertEqual(job_tools._status(job_row(rendered=0), False).outcome, "")


class PrepareTest(Timings):
    """render_offer.prepare: the trigger, on Blender's thread, before the quote."""

    def prepare(self, arguments, current=None, rows=()):
        with patch.object(bpy, "context", types.SimpleNamespace(scene=current or scene()), create=True), \
                patch.object(bpy, "data", types.SimpleNamespace(filepath=KEY[0]), create=True), \
                patch.object(checkpoints, "data_root", return_value=Path("/nowhere")), \
                patch.object(jobs, "list_jobs", return_value=list(rows)):
            return render_offer.prepare(arguments)

    ALL = {"frame_start": 1, "frame_end": 1800, "engine": "eevee"}

    def test_no_timing_means_no_offer(self):
        self.assertIsNone(self.prepare(self.ALL))

    def test_an_unsaved_file_gets_no_offer(self):
        render_offer.record(("", "Scene"), UHD, 31.0)  # Every unsaved file's Scene: someone else's frames, maybe.
        with patch.object(bpy, "context", types.SimpleNamespace(scene=scene()), create=True), \
                patch.object(bpy, "data", types.SimpleNamespace(filepath=""), create=True), \
                patch.object(checkpoints, "data_root", return_value=Path("/nowhere")), \
                patch.object(jobs, "list_jobs", return_value=[]):
            self.assertIsNone(render_offer.prepare(self.ALL))

    def test_under_fifteen_minutes_means_no_offer(self):
        render_offer.record(KEY, UHD, 31.0)
        self.assertIsNone(self.prepare({**self.ALL, "frame_end": 29}))  # 29 × 31 s = 899 s.

    def test_over_fifteen_minutes_offers_the_same_frames_here(self):
        render_offer.record(KEY, UHD, 31.0)
        offer = self.prepare(self.ALL)
        self.assertEqual(offer, {**UHD, "frames": [1, 1800, 1], "count": 1800, "fps": 24.0, "camera": "Camera",
                                 "seconds_per_frame": 31.0, "key": list(KEY), "denoise": True})
        self.assertEqual(self.prepare({**self.ALL, "frame_end": 30})["count"], 30)  # 930 s.
        stepped = self.prepare({**self.ALL, "frame_step": 2, "camera": "Wide"})
        self.assertEqual((stepped["count"], stepped["frames"], stepped["camera"]), (900, [1, 1800, 2], "Wide"))

    def test_a_render_jobs_frames_are_a_timing_too(self):
        self.assertEqual(self.prepare(self.ALL, rows=[job_row(31.0)])["seconds_per_frame"], 31.0)

    def test_settings_the_cloud_would_not_render_alike_mean_no_offer(self):
        render_offer.record(KEY, UHD, 31.0)
        cases = [{**self.ALL, "resolution_pct": 50},         # 1920×1080, not the measured 4K.
                 {**self.ALL, "samples": 128},               # Not the measured 64.
                 {"frame_start": 1, "frame_end": 1800},      # Cycles by default: this scene is EEVEE.
                 {**self.ALL, "turntable": {}}]              # Its orbit camera exists only in the cloud.
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertIsNone(self.prepare(arguments))
        cycles = {"engine": "CYCLES", "width": 3840, "height": 2160, "samples": 128, "device": "CPU"}
        render_offer.record(KEY, cycles, 31.0)
        self.assertIsNone(self.prepare({"frame_start": 1, "frame_end": 1800}), "the scene itself renders EEVEE")
        self.assertIsNotNone(self.prepare({"frame_start": 1, "frame_end": 1800}, scene(engine="CYCLES", samples=4096)),
                             "the cloud's 128 default against a measured 128-sample frame")

    def test_a_failure_here_never_stops_the_paid_call(self):
        with patch.object(render_offer, "_prepare", side_effect=AttributeError("no scene")):
            self.assertIsNone(render_offer.prepare(self.ALL))


class QuoteModule:
    """cloud_jobs.run's quote step, in memory: the render quote, with or without estimated_seconds."""

    def __init__(self, credits=640, seconds=7680):
        self.quote = {"quote_id": "qt_render", "credits": credits, "expires_at": "2099-01-01T00:00:00Z"}
        if seconds is not None:
            self.quote["estimated_seconds"] = seconds

    def run(self, descriptor, arguments, files, *, quote=False, **_):
        raise cloud_http.CloudError("Approve the credit quote", "quote_required", extra=dict(self.quote))


class AuthorizeTest(unittest.TestCase):
    """The offer reaches the paid call's card details with the quote's credits and estimated_seconds."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = types.SimpleNamespace(lock=threading.RLock(), cache_dir=Path(self.temp.name), quotes={},
                                             quote_requests={}, handle_error=lambda error: None,
                                             manifest={"account": {"credits": {"balance": 1850}}})
        self.offer = {**UHD, "frames": [1, 1800, 1], "count": 1800, "fps": 24.0, "camera": "Camera",
                      "seconds_per_frame": 31.0}

    def authorize(self, module, offer):
        seen = []
        with cloud_account.approval_context(lambda details: seen.append(details) or True):
            arguments = cloud_account.authorize(module, RENDER, {"frame_start": 1, "frame_end": 1800}, {},
                                                project=PROJECT, cancel=threading.Event(), runtime=self.runtime,
                                                client=None, offer=offer)
        return arguments, seen[-1]

    def test_the_offer_and_the_quote_reach_the_card(self):
        arguments, details = self.authorize(QuoteModule(), self.offer)
        self.assertEqual(arguments["quote_id"], "qt_render")
        self.assertEqual(details["render_offer"], {**self.offer, "credits": 640, "cloud_seconds": 7680})
        self.assertEqual(details["balance"], 1850)
        self.assertEqual(self.runtime.quotes["qt_render"]["estimated_seconds"], 7680)

    def test_without_estimated_seconds_the_card_shows_credits_only(self):
        _, details = self.authorize(QuoteModule(seconds=None), self.offer)
        self.assertIsNone(details["render_offer"]["cloud_seconds"])

    def test_without_an_offer_the_card_is_todays(self):
        _, details = self.authorize(QuoteModule(), None)
        self.assertNotIn("render_offer", details)

    def test_an_estimate_is_kept_to_thirty_days(self):
        module = QuoteModule(seconds=10 ** 12)
        _, details = self.authorize(module, self.offer)
        self.assertEqual(self.runtime.quotes["qt_render"]["estimated_seconds"], 30 * 86400)
        self.assertEqual(details["render_offer"]["cloud_seconds"], 30 * 86400)

    def test_only_a_whole_estimated_seconds_of_at_least_one_is_kept(self):
        for bad in (0, -5, 7.5, "7680", True, None):
            with self.subTest(bad=bad):
                self.runtime.quotes.clear()
                module = QuoteModule()
                module.quote["estimated_seconds"] = bad
                arguments, details = self.authorize(module, self.offer)
                self.assertNotIn("estimated_seconds", self.runtime.quotes["qt_render"])
                self.assertEqual(arguments.quote, {"quote_id": "qt_render", "credits": 640,
                                                   "expires_at": "2099-01-01T00:00:00Z"})
                self.assertIsNone(details["render_offer"]["cloud_seconds"])



OFFER = {**UHD, "frames": [1, 1800, 1], "count": 1800, "fps": 24.0, "camera": "Camera", "seconds_per_frame": 31.0,
         "key": list(KEY)}
JOB_ID = "d" * 32


def render_details(credits=640, balance=1850, seconds=7680, offer=OFFER):
    """cloud_account.authorize's details for a quoted render, with the offer when there is one."""
    quote = {"quote_id": "qt_render", "credits": credits, "expires_at": "2099-01-01T00:00:00Z"}
    if seconds:
        quote["estimated_seconds"] = seconds
    details = {**cloud_account.approval_details(RENDER, quote), "balance": balance}
    if offer:
        details["render_offer"] = render_offer.choice(offer, quote)
    return details


def started(*args, **kwargs):
    """job_tools.start_render_job's result: the job's summary line and its note."""
    return tools.ToolResult(json.dumps({"id": JOB_ID, "state": "queued"}) + job_tools.STARTED_NOTE)


def open_scene(current=None, file=KEY[0]):
    """The scene Blender has open while a laptop pick starts its render (render_offer._changed)."""
    import contextlib
    stack = contextlib.ExitStack()

    class Open:
        def start(self):
            stack.enter_context(patch.object(bpy, "context", types.SimpleNamespace(scene=current or scene()), create=True))
            stack.enter_context(patch.object(bpy, "data", types.SimpleNamespace(filepath=file), create=True))

        def stop(self):
            stack.close()

        __enter__ = start

        def __exit__(self, *_):
            self.stop()
    return Open()


def on_main_now(fn):
    future = Future()
    future.set_result(fn())
    return future


class ChoiceConsentTest(unittest.TestCase):
    """agent._Consent: a long render's ask is the choice card; each answer does what its button says."""

    def setUp(self):
        self.session, self.turn = state.new_session(), agent.Turn()
        self.turn.spending = spending.Settings()
        self.card = state.item_tool("loopcut_cloud__render", "Render scene · Getting a credit quote…", "")
        self.session["items"].append(self.card)
        for target in (patch.object(agent, "_redraw"), patch.object(conversations, "save"),
                       patch.object(mainthread, "run_on_main", side_effect=on_main_now), open_scene()):
            target.start()
            self.addCleanup(target.stop)
        starting = patch.object(job_tools, "start_render_job", side_effect=started)
        self.start = starting.start()
        self.addCleanup(starting.stop)

    def ask(self, details, answer):
        """Run the consent; answer(card) is the user's click while the card waits. (result or error, card seen)."""
        seen = {}

        def wait(turn):
            seen.update(self.card)
            return answer(self.card)
        consent = agent._Consent(self.session, self.turn, self.card)
        with patch.object(agent, "_wait_for_approval", side_effect=wait):
            try:
                return consent(details), seen
            except cloud_http.CloudError as ex:
                return ex, seen

    def test_no_offer_is_todays_paid_card(self):
        _, seen = self.ask(render_details(offer=None), lambda card: True)
        self.assertEqual(seen["ask"], spending.COST)
        self.assertNotIn("render_choice", seen)

    def test_the_cards_title_says_its_quote_then_what_it_was_charged(self):
        """rel019-cloud2: "Render scene · Getting a credit quote…" stayed the card's title after its quote showed (29)
        and after the render ran (5 charged, 24 returned)."""
        def run(card, credits, charged, text):
            seen = {}
            consent = agent._Consent(self.session, agent.Turn(), card)
            consent.turn.spending = spending.Settings(ask_above=25)  # 29 credits asks.
            with patch.object(agent, "_wait_for_approval", side_effect=lambda turn: seen.update(card) or True):
                self.assertTrue(consent(render_details(credits=credits, offer=None)))
            quoted = card["summary"]
            with patch.object(state, "session", return_value=self.session):  # Its chat, on screen.
                consent({"charged": charged, "text": text})
            return seen.get("summary"), quoted, card["summary"]
        self.assertEqual(run(self.card, 29, 5, "Used 5 credits ($0.05)"),
                         ("Render scene · 29 credits", "Render scene · 29 credits", "Render scene · 5 credits"))
        self.assertEqual(self.card["cost"], "Used 5 credits ($0.05)")
        refunded = state.item_tool("loopcut_cloud__render", "Render scene · Getting a credit quote…", "")
        self.assertEqual(run(refunded, 640, 0, "")[1:], ("Render scene · 640 credits", "Render scene · no charge"))
        estimated = state.item_tool("loopcut_cloud__image_generate", "Generate image · About 6 credits ($0.06)", "")
        self.assertEqual(run(estimated, 6, 6, "Used 6 credits ($0.06)")[1:],
                         ("Generate image · About 6 credits ($0.06)",) * 2, "a priced title stays as it was")

    def test_an_offer_makes_the_ask_the_choice(self):
        _, seen = self.ask(render_details(), lambda card: True)
        self.assertEqual(seen["status"], "awaiting")
        self.assertEqual(seen["render_choice"], {**OFFER, "credits": 640, "cloud_seconds": 7680, "balance": 1850,
                                                 "plan": ""})
        self.assertTrue(seen["paid_approval"])

    def test_the_cloud_click_approves_the_quote_as_approve_does(self):
        plain = agent._Consent(state.new_session(), agent.Turn(), state.item_tool("loopcut_cloud__render", "", ""))
        with patch.object(agent, "_redraw"), patch.object(agent, "_wait_for_approval", return_value=True):
            self.assertTrue(plain(render_details(offer=None)))
        approved, _ = self.ask(render_details(), lambda card: True)
        self.assertIs(approved, True)
        self.assertEqual(self.session["spend"], plain.session["spend"])
        self.assertEqual(self.session["spend"], {"credits": 640, "auto": 0, "allowed": 0}, "held, approved")
        self.assertEqual(self.card["status"], "running")
        self.assertNotIn("render_choice", self.card)
        self.start.assert_not_called()

    def test_the_laptop_click_declines_the_call_and_starts_the_same_frames_here(self):
        def laptop(card):
            card["picked"] = "here"
            return False
        error, _ = self.ask(render_details(), laptop)
        self.assertEqual(error.code, render_offer.LOCAL)
        self.start.assert_called_once()
        args, kwargs = self.start.call_args
        self.assertEqual(kwargs["frames"], list(range(1, 1801)))
        self.assertEqual((kwargs["width"], kwargs["height"], kwargs["samples"], kwargs["camera"]),
                         (3840, 2160, 64, "Camera"))
        self.assertEqual(kwargs["output_dir"], render_offer.OUTPUT_DIR)
        self.assertEqual(args[0], math.ceil(31 * 1800 * 1.25) + 600, "a time budget past the measured estimate")
        self.assertEqual(self.session["spend"]["credits"], 0, "nothing held or charged")
        self.assertEqual(self.card["local_job"], JOB_ID)
        self.assertEqual(self.card["summary"], "Render 1,800 frames on this laptop")
        self.assertNotIn("render_choice", self.card)
        self.assertNotIn("picked", self.card)
        result = render_offer.result(error)
        self.assertTrue(result.ok)
        self.assertIn("chose to render these frames on this computer", result.text)
        self.assertIn(f"job_id {JOB_ID}", result.text)
        self.assertIn(job_tools.STARTED_NOTE.strip(), result.text)
        self.assertEqual((result.receipt, result.outcome), ("No charge", "Rendering on this laptop"))

    def test_the_laptop_denoises_as_the_cloud_would_and_the_live_scene_keeps_its_own(self):
        current = scene(engine="CYCLES", samples=128)
        current.cycles.use_denoising = False
        seen = []
        self.start.side_effect = lambda *a, **k: seen.append(current.cycles.use_denoising) or started()
        data = {**OFFER, "engine": "CYCLES", "samples": 128}
        for denoise in (True, False):
            with self.subTest(denoise=denoise), open_scene(current):
                self.assertTrue(render_offer._start({**data, "denoise": denoise})[2])
                self.assertFalse(current.cycles.use_denoising, "restored")
        self.assertEqual(seen, [True, False], "the saved copy has the cloud's setting")

    def test_a_local_render_that_cannot_start_says_why(self):
        def laptop(card):
            card["picked"] = "here"
            return False
        self.start.side_effect = tools.ToolError("Choose a camera in this scene before starting a render job.")
        error, _ = self.ask(render_details(), laptop)
        result = render_offer.result(error)
        self.assertFalse(result.ok)
        self.assertIn("could not start: Choose a camera", result.text)
        self.assertNotIn("local_job", self.card)

    def test_after_a_switch_to_another_scene_or_file_nothing_renders_and_the_model_hears_why(self):
        def laptop(card):
            card["picked"] = "here"
            return False
        other = scene()
        other.name = "Scene.001"
        for current, file, why in ((other, KEY[0], 'it was "Scene" in garden.blend'),
                                   (scene(), "/projects/other.blend", 'it was "Scene" in garden.blend'),
                                   (scene(engine="CYCLES"), KEY[0], "render engine changed")):
            with self.subTest(why=why, file=file), open_scene(current, file):
                self.card.pop("local_job", None)
                error, _ = self.ask(render_details(), laptop)
                result = render_offer.result(error)
                self.assertFalse(result.ok)
                self.assertIn(why, result.text)
                self.assertIn("could not start", result.text)
        self.start.assert_not_called()

    def test_esc_declines_both_and_the_model_asks_which(self):
        error, _ = self.ask(render_details(), lambda card: False)
        self.assertEqual((error.code, str(error)), ("declined", render_offer.SKIPPED))
        self.assertEqual(self.card["status"], "rejected")
        self.start.assert_not_called()

    def test_enter_does_nothing_unless_the_cloud_is_recommended_and_a_click_still_runs_it(self):
        self.card.update(status="awaiting", render_choice={**render_details(seconds=None)["render_offer"],
                                                          "balance": 1850})
        self.session["turn"] = self.turn
        with patch.object(state, "session", return_value=self.session), \
                patch.object(render_offer, "live_balance", return_value=None):
            self.assertFalse(agent.decide(True, index=0))
            self.assertFalse(self.turn.decided.is_set())
            render_offer.click(self.session, 0, "cloud")
        self.assertTrue(self.turn.approved)
        self.assertEqual(self.card["picked"], "cloud", "its consent pops it with the other ask fields")

    def test_enter_approves_the_choice_card_as_it_does_any_paid_card(self):
        self.card.update(status="awaiting", render_choice=render_details()["render_offer"])
        self.session["turn"] = self.turn
        with patch.object(state, "session", return_value=self.session):
            self.assertTrue(agent.decide(True))
        self.assertTrue(self.turn.approved)


class ExecuteTest(unittest.TestCase):
    """cloud.execute: a render call asks prepare() on Blender's thread, and a laptop pick is its tool result."""

    def setUp(self):
        from test_cloud import Reply, Transport, manifest, tool
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        schema = {"type": "object", "additionalProperties": False, "required": [],
                  "properties": {"frame_start": {"type": "integer"}, "frame_end": {"type": "integer"},
                                 "engine": {"type": "string", "enum": ["cycles", "eevee"]}}}
        descriptor = tool("render", kind="assist", input_schema=schema, backends=["managed"], byok=None,
                          cost={"credits_estimate": 5, "quote_required": True})
        client = cloud_http.Client(base_url="https://service.test", api_key="lc_demo",
                                   transport=Transport(Reply(manifest(descriptor))))
        runtime = cloud.Runtime(client, self.temp.name, signed_in=True)
        self.module = types.ModuleType("loopcut.cloud_jobs")
        self.runs = []

        def run(descriptor, arguments, files, *, quote=False, **_):
            if quote and not arguments.get("quote_id"):
                raise cloud_http.CloudError("Approve the quote", "quote_required", extra={
                    "quote_id": "qt_render", "credits": 640, "expires_at": "2099-01-01T00:00:00Z",
                    "estimated_seconds": 7680})
            self.runs.append(arguments)
        self.module.resolve_files, self.module.run = (lambda d, a, o: {}), run
        self.module.apply_result = lambda descriptor, job: "applied"
        for target in (patch.object(cloud, "_runtime", runtime), patch.dict(sys.modules, {"loopcut.cloud_jobs": self.module}),
                       patch.object(render_offer, "prepare", return_value=OFFER)):
            target.start()
            self.addCleanup(target.stop)
        runtime.refresh()

    def test_a_laptop_pick_is_the_render_calls_result_and_nothing_runs_in_the_cloud(self):
        seen = []

        def consent(details):
            seen.append(details)
            raise cloud_http.CloudError(render_offer.HERE_RESULT.format(id=JOB_ID, job="{}"), render_offer.LOCAL,
                                        extra={"ok": True, "job_id": JOB_ID})
        prepared = cloud.execute(cloud.PREFIX + "render", {"frame_start": 1, "frame_end": 1800, "engine": "eevee"})
        render_offer.prepare.assert_called_once_with({"frame_start": 1, "frame_end": 1800, "engine": "eevee"})
        with cloud_account.approval_context(consent):
            result = prepared.then(lambda: False, lambda fn: fn())
        self.assertEqual(seen[0]["render_offer"]["cloud_seconds"], 7680)
        self.assertTrue(result.ok)
        self.assertEqual(result.receipt, "No charge")
        self.assertIn(JOB_ID, result.text)
        self.assertEqual(self.runs, [], "no cloud job")


def measure(font, size, text):
    return len(text) * size * 0.6  # Wider than Inter: what fits here fits in Blender.


class ChoiceLayoutTest(unittest.TestCase):
    """The card at the panel's 420 px: the two options stacked, every line inside its box."""

    WIDTH = 420

    def build(self, item, **ui):
        session = state.new_session()
        session["items"].append(item)
        return layout.build(session, self.WIDTH, 2400, 1.0, measure, "fast", None,
                            {"selection": [], "context_budget": 24_000, "account": None, **ui})

    def card(self, **details):
        item = state.item_tool("loopcut_cloud__render", "Render scene", "")
        return state.awaiting(item, ask=spending.COST, paid_approval=True,
                              **render_offer.card_fields(render_details(**details)))

    @staticmethod
    def texts(display):
        """Each text by id, a paragraph's wrapped lines (id.l0, id.l1...) joined under its own id."""
        texts = {}
        for p in display["prims"]:
            if p.get("t") == "text":
                key = re.sub(r"\.l\d+$", "", p["id"])
                texts[key] = (texts[key] + " " + p["text"]) if key in texts else p["text"]
        return texts

    def assert_inside(self, display):
        boxes = {p["id"][:-len(".box")]: p for p in display["prims"] if p["id"].endswith(".box")}
        card = next(p for p in display["prims"] if p["id"] == "item0.choice.card")
        self.assertGreaterEqual(card["x"], T.PAD)
        self.assertLessEqual(card["x"] + card["w"], self.WIDTH - T.PAD)
        for p in display["prims"]:
            if not p["id"].startswith("item0"):
                continue
            box = next((b for k, b in boxes.items() if p["id"].startswith(k + ".")), card)
            right = box["x"] + box["w"] - (0 if p is box else 1)
            width = measure("ui", p["size"], p["text"]) if p.get("t") == "text" else p.get("w", 0)
            self.assertLessEqual(p["x"] + width, right, p["id"])
        for hit in display["hits"]:
            if hit["id"].startswith("item0"):
                self.assertLessEqual(hit["x"] + hit["w"], self.WIDTH - T.PAD, hit["id"])

    def survives_quit(self, value):
        """cloud_jobs.SURVIVES_QUIT as the case needs it, with or without the restart lane (R1) that sets it."""
        from loopcut import cloud_jobs
        return patch.object(cloud_jobs, "SURVIVES_QUIT", value, create=True)

    def test_the_choice_card_says_what_each_way_means_and_fits(self):
        with self.survives_quit(False):
            display = self.build(self.card())
        texts = self.texts(display)
        self.assertEqual(texts["item0.choice.title"], "Render 1,800 frames")
        self.assertEqual(texts["item0.choice.detail"], "4K · 75 s · EEVEE")
        self.assertEqual(texts["item0.choice.cloud.badge.label"], "Recommended")
        self.assertEqual([texts["item0.choice.cloud.fact0"], texts["item0.choice.cloud.fact1"]],
                         ["About 2 h", "640 credits · $6.40, quoted"])
        self.assertEqual([texts["item0.choice.here.fact0"], texts["item0.choice.here.fact1"]],
                         ["About 15½ h (31 s × 1,800)", "Free"])
        self.assertEqual([texts[f"item0.choice.cloud.bullet{k}"] for k in range(2)], ["Work on something else meanwhile",
                                                                                     "Blender stays open until it's done"])
        self.assertNotIn("item0.choice.cloud.bullet2", texts)
        self.assertEqual([texts[f"item0.choice.here.bullet{k}"] for k in range(2)], list(render_offer.HERE_BULLETS))
        self.assertEqual(texts["item0.choice.cloud.go.label"], "Render in the cloud · 640 credits  ⏎")
        self.assertEqual(texts["item0.choice.here.go.label"], "Render on my laptop")
        self.assertEqual(texts["item0.choice.footer"], "Balance 1,850 credits · after the cloud render 1,210")
        hits = {h["id"]: h["action"] for h in display["hits"]}
        self.assertEqual(hits["item0.choice.cloud.go"], ("render_choice", (0, "cloud")))
        self.assertEqual(hits["item0.choice.here.go"], ("render_choice", (0, "here")))
        self.assertEqual((texts["item0.choice.skip.label"], hits["item0.choice.skip"]), ("Not now · esc", ("reject", 0)))
        cloud_box = next(p for p in display["prims"] if p["id"] == "item0.choice.cloud.box")
        here_box = next(p for p in display["prims"] if p["id"] == "item0.choice.here.box")
        self.assertGreater(here_box["y"], cloud_box["y"] + cloud_box["h"] - 1, "stacked, the cloud first")
        self.assertEqual(cloud_box["w"], here_box["w"])
        self.assert_inside(display)
        self.assertEqual(display["max_scroll"], 0)

    def test_the_panel_can_answer_the_choice_card_it_drew(self):
        item = self.card()
        display = self.build(item)
        tag = (id(item), item["ask_serial"])
        hits = {h["id"]: h for h in display["hits"]}
        for name in ("item0.choice.cloud.go", "item0.choice.here.go", "item0.choice.skip"):
            self.assertEqual(hits[name]["card"], tag, name)
        self.assertEqual([(c["index"], c["card"], c["shown"]) for c in display["cards"]], [(0, tag, True)],
                         "its cloud button and Not now are its answer row: Enter and Esc reach it")
        session = state.new_session()
        session["items"].append(item)
        short = layout.build(session, self.WIDTH, 360, 1.0, measure, "fast", None,
                             {"selection": [], "context_budget": 24_000, "account": None})
        entry = short["cards"][0]
        self.assertFalse(entry["shown"], "a row cut by the panel's edge is not on screen")
        self.assertGreater(entry["scroll"], 0, "Enter scrolls to it")

    def test_the_cloud_is_recommended_only_when_it_is_clearly_faster(self):
        data = render_details()["render_offer"]  # The laptop: 31 s × 1,800 = 55,800 s.
        for seconds, expected in ((7680, True), (27900, True), (54900, True), (55000, False), (90000, False),
                                  (None, False)):
            with self.subTest(seconds=seconds):
                self.assertIs(render_offer.recommended({**data, "cloud_seconds": seconds}), expected)
        item = self.card(seconds=55000)
        texts = self.texts(self.build(item))
        self.assertNotIn("item0.choice.cloud.badge.label", texts)
        self.assertEqual(texts["item0.choice.cloud.go.label"], "Render in the cloud · 640 credits", "no ⏎: Enter skips it")

    def test_quitting_is_offered_only_once_cloud_jobs_survive_it(self):
        with self.survives_quit(False):
            texts = " ".join(self.texts(self.build(self.card())).values())
        self.assertNotIn("Quit Blender", texts)
        self.assertNotIn("wait in this chat", texts)
        with self.survives_quit(True):
            display = self.build(self.card())
        texts = self.texts(display)
        self.assertEqual([texts[f"item0.choice.cloud.bullet{k}"] for k in range(3)], list(render_offer.CLOUD_BULLETS))
        self.assert_inside(display)  # Three bullets fit at 420 px too.
        self.assertEqual(display["max_scroll"], 0)

    def test_without_estimated_seconds_the_cloud_shows_credits_only(self):
        texts = self.texts(self.build(self.card(seconds=None)))
        self.assertEqual(texts["item0.choice.cloud.fact0"], "640 credits · $6.40, quoted")
        self.assertNotIn("item0.choice.cloud.fact1", texts)

    def test_the_live_balance_replaces_the_one_the_card_showed(self):
        texts = self.texts(self.build(self.card(), credit_balance=2000))
        self.assertEqual(texts["item0.choice.footer"], "Balance 2,000 credits · after the cloud render 1,360")

    def test_after_the_laptop_pick_the_card_shows_the_local_jobs_frames(self):
        item = state.item_tool("loopcut_cloud__render", "Render 1,800 frames on this laptop", "")
        item.update(status="done", local_job=JOB_ID, outcome="Rendering on this laptop", cost="No charge")
        row = {"id": JOB_ID, "state": "running", "frames": list(range(1, 1801)),
               "progress": {"completed": 146, "total": 1800}}
        texts = self.texts(self.build(item, jobs=[row]))
        self.assertEqual(texts["item0.tool.local"], "On this laptop · 146 of 1,800 frames")
        self.assertNotIn("item0.tool.outcome", texts)
        row.update(state="complete", progress={"completed": 1800, "total": 1800})
        self.assertEqual(self.texts(self.build(item, jobs=[row]))["item0.tool.local"],
                         "Rendered 1,800 frames on this laptop")
        texts = self.texts(self.build(item, jobs=[]))
        self.assertEqual(texts["item0.tool.outcome"], "Rendering on this laptop", "a job no longer listed")

    def test_a_render_one_job_cannot_hold_shows_its_time_and_no_button(self):
        def here(count):
            data = {**render_details()["render_offer"], "count": count, "frames": [1, count, 1], "balance": 1850}
            return render_offer.options(data, None)[0][1]
        fits = here(2000)  # 62,000 s: a 78,100 s budget.
        self.assertEqual((fits["facts"][0], fits["button"]), ("About 17 h (31 s × 2,000)", "Render on my laptop"))
        near = here(2300)  # 71,300 s: its budget is over render_job's 86,400.
        self.assertEqual(near["facts"], ["About 20 h: too long for one run here", "(31 s × 2,300)"])
        self.assertNotIn("button", near)
        self.assertEqual(here(3000)["facts"][0], "Over a day on this computer")
        item = self.card()
        item["render_choice"].update(count=3000, frames=[1, 3000, 1])
        display = self.build(item)
        self.assertNotIn("item0.choice.here.go", {h["id"] for h in display["hits"]})
        self.assertIn("item0.choice.cloud.go", {h["id"] for h in display["hits"]})
        self.assert_inside(display)

    def test_copy_rounding(self):
        self.assertEqual([render_offer.about(s) for s in (30, 600, 3500, 3600, 7680, 55800, 5400)],
                         ["under a minute", "10 min", "58 min", "1 h", "2 h", "15½ h", "1½ h"])
        self.assertEqual([render_offer.clip(s) for s in (75, 119.6, 150, 600)], ["75 s", "2 min", "2 min 30 s", "10 min"])
        self.assertEqual(render_offer.detail({**OFFER, "width": 1000, "height": 1000, "engine": "CYCLES",
                                              "count": 1, "frames": [900, 900, 1]}), "1000×1000 · Cycles")


class ClickTest(unittest.TestCase):
    """render_offer.click (ui.host's render_choice action): a click answers only its own waiting card."""

    def setUp(self):
        self.session = state.new_session()
        self.card = state.item_tool("loopcut_cloud__render", "Render scene", "")
        self.card.update(status="awaiting", render_choice=render_details()["render_offer"])
        self.session["items"].append(self.card)

    def click(self, option, answered=True):
        with patch.object(agent, "decide", return_value=answered) as decide:
            render_offer.click(self.session, 0, option)
        return decide

    def test_cloud_approves_and_laptop_declines_with_the_pick(self):
        self.click("cloud").assert_called_once_with(True, index=0, drawn=None)
        self.assertEqual(self.card.pop("picked"), "cloud")
        self.click("here").assert_called_once_with(False, index=0, drawn=None)
        self.assertEqual(self.card["picked"], "here")

    def test_a_click_answers_only_the_card_and_ask_the_frame_drew(self):
        state.awaiting(self.card)
        with patch.object(agent, "decide", return_value=True) as decide:
            render_offer.click(self.session, 0, "here", drawn=(id(self.card), self.card["ask_serial"] - 1))
            decide.assert_not_called()
            self.assertNotIn("picked", self.card)
            drawn = (id(self.card), self.card["ask_serial"])
            render_offer.click(self.session, 0, "here", drawn=drawn)
            decide.assert_called_once_with(False, index=0, drawn=drawn)

    def test_enter_and_esc_through_the_drawn_card(self):
        turn = agent.Turn()
        self.session["turn"] = turn
        state.awaiting(self.card)
        drawn = (id(self.card), self.card["ask_serial"])
        with patch.object(state, "session", return_value=self.session), \
                patch.object(render_offer, "live_balance", return_value=None):
            self.assertTrue(agent.decide(True, index=0, drawn=drawn), "Enter: the cloud is Recommended")
            self.assertTrue(turn.approved)
            turn.decided.clear()
            self.assertTrue(agent.decide(False, index=0, drawn=drawn), "Esc declines it, not the turn")
            self.assertFalse(turn.approved)

    def test_a_click_that_answered_nothing_leaves_no_pick_for_a_later_enter(self):
        self.click("cloud", answered=False).assert_called_once()
        self.assertNotIn("picked", self.card)

    def test_a_render_too_long_for_one_job_is_never_started_from_a_click(self):
        self.card["render_choice"].update(count=3000, frames=[1, 3000, 1])
        self.click("here").assert_not_called()
        self.assertNotIn("picked", self.card)
        self.assertEqual(render_offer._start(self.card["render_choice"])[2], False)

    def test_a_card_no_longer_waiting_is_never_answered(self):
        self.card["status"] = "running"
        self.click("here").assert_not_called()
        self.assertNotIn("picked", self.card)
        render_offer.click(self.session, 5, "cloud")  # Gone: nothing to answer.


class ShortTest(unittest.TestCase):
    """Short of the quote: the plan that covers it, from the live balance and plan, on the credits pop-up's page."""

    def setUp(self):
        patcher = patch.object(config, "billing_open", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_free_plans_way_is_the_plan_that_covers_it(self):
        short = render_offer.shortfall(640, 100, "free", 7680 / 640, render=True)
        self.assertEqual(short["warn"], "You have 100 credits; this needs 640.")
        self.assertEqual(short["badge"], "With Pro")
        self.assertEqual(short["note"], "Pro is $20 a month with $22 of credits every month: up to 7 hours of cloud "
                                        "rendering.")
        self.assertEqual(short["label"], "Upgrade to Pro and render in the cloud")
        self.assertEqual(short["url"], config.pricing_url("insufficient_credits", "pro"))
        self.assertEqual(render_offer.shortfall(640, 100, "")["note"], "Pro is $20 a month with $22 of credits every "
                                                                        "month.", "no rate: no hours")
        self.assertEqual(render_offer.shortfall(640, 100, "")["label"], "Upgrade to Pro")
        big = render_offer.shortfall(3000, 100, "free", 12.0, render=True)
        self.assertEqual((big["badge"], big["plan"]), ("With Max", "max"))
        self.assertTrue(big["note"].startswith("Max is $60 a month with $66 of credits every month: up to 22 hours"))

    def test_no_upgrade_when_no_plans_month_covers_it(self):
        short = render_offer.shortfall(7000, 100, "free", 12.0, render=True)  # Max: 6,600 + 100 < 7,000.
        self.assertEqual(short["note"], "This needs more credits than a month of Max includes.")
        self.assertEqual((short["label"], short["url"], short["badge"]), ("", "", ""))
        data = {**render_details(credits=7000, balance=100)["render_offer"], "balance": 100, "plan": "free"}
        cloud, here = render_offer.options(data, None)[0]
        self.assertNotIn("button", cloud)
        self.assertEqual(here["button"], "Render on my laptop", "the laptop stays")

    def test_credits_read_as_whole_numbers(self):
        self.assertEqual(render_offer._n(1234567), "1,234,567")
        self.assertEqual(render_offer.shortfall(100, 99.6, "free")["warn"], "You have 99 credits; this needs 100.")

    def test_a_paid_plans_way_is_a_top_up(self):
        short = render_offer.shortfall(640, 100, "pro", 12.0, render=True)
        self.assertEqual((short["badge"], short["note"], short["plan"]), ("", "", ""))
        self.assertEqual(short["label"], "Add credits and render in the cloud")
        self.assertEqual(short["url"], config.credits_url("insufficient_credits"))

    def test_enough_credits_an_unknown_balance_or_closed_sales_offer_nothing(self):
        self.assertIsNone(render_offer.shortfall(640, 640, "free"))
        self.assertIsNone(render_offer.shortfall(640, None, "free"))
        with patch.object(config, "billing_open", return_value=False):
            self.assertIsNone(render_offer.shortfall(640, 100, "free"))

    def test_every_paid_ask_carries_its_price_and_a_choice_its_plan(self):
        with patch.object(render_offer, "account_plan", return_value="free"):
            fields = render_offer.card_fields(render_details(balance=100))
            plain = render_offer.card_fields({**cloud_account.approval_details(
                {**RENDER, "cost": {"credits_estimate": 80}}), "balance": 100})
        self.assertEqual(fields["price"], {"credits": 640, "balance": 100, "plan": "free"})
        self.assertEqual(fields["render_choice"]["plan"], "free")
        self.assertEqual(plain, {"price": {"credits": 80, "balance": 100, "plan": "free"}})
        self.assertEqual(render_offer.card_fields({"vendor": "OpenAI", "credits": None}), {})


class ShortLayoutTest(ChoiceLayoutTest):
    """The short-of-credits variant, and the Upgrade on a plain paid card."""

    def setUp(self):
        patcher = patch.object(config, "billing_open", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def free_card(self, **details):
        with patch.object(render_offer, "account_plan", return_value="free"):
            return self.card(balance=100, **details)

    def test_the_cloud_option_offers_pro_and_the_laptop_stays(self):
        display = self.build(self.free_card())
        texts = self.texts(display)
        self.assertEqual(texts["item0.choice.cloud.badge.label"], "With Pro")
        self.assertEqual(texts["item0.choice.cloud.warn"], "You have 100 credits; this needs 640.")
        warn = next(p for p in display["prims"] if p["id"].startswith("item0.choice.cloud.warn"))
        self.assertEqual(warn["color"], T.WARN)
        self.assertEqual(texts["item0.choice.cloud.note"], "Pro is $20 a month with $22 of credits every month: up to "
                                                            "7 hours of cloud rendering.")
        self.assertEqual(texts["item0.choice.cloud.go.label"], "Upgrade to Pro and render in the cloud")
        self.assertNotIn("item0.choice.footer", texts)
        hits = {h["id"]: h["action"] for h in display["hits"]}
        self.assertEqual(hits["item0.choice.cloud.go"], ("paid_upgrade", 0))
        self.assertEqual(hits["item0.choice.here.go"], ("render_choice", (0, "here")))
        self.assert_inside(display)

    def test_with_no_plan_to_offer_the_card_has_no_cloud_button_and_still_answers(self):
        display = self.build(self.free_card(credits=7000))
        texts = self.texts(display)
        self.assertEqual(texts["item0.choice.cloud.note"], "This needs more credits than a month of Max includes.")
        hits = {h["id"] for h in display["hits"]}
        self.assertNotIn("item0.choice.cloud.go", hits)
        self.assertIn("item0.choice.here.go", hits)
        self.assertTrue(display["cards"][0]["shown"], "Esc still declines it")
        self.assert_inside(display)
        plain = self.plain_card(50)
        plain["price"].update(credits=7000)
        display = self.build(plain)
        self.assertNotIn("item0.tool.plan_upgrade", {h["id"] for h in display["hits"]})
        self.assertEqual(self.texts(display)["item0.tool.plan_note"], "This needs more credits than a month of Max "
                                                                       "includes.")

    def test_once_the_balance_covers_it_the_card_is_the_recommended_one_again(self):
        texts = self.texts(self.build(self.free_card(), credit_balance=2300))
        self.assertEqual(texts["item0.choice.cloud.badge.label"], "Recommended")
        self.assertEqual(texts["item0.choice.footer"], "Balance 2,300 credits · after the cloud render 1,660")

    def plain_card(self, balance):
        item = state.item_tool("loopcut_cloud__image_to_3d", "Image to 3D", "")
        details = {**cloud_account.approval_details({**RENDER, "cost": {"credits_estimate": 80}}), "balance": balance}
        with patch.object(render_offer, "account_plan", return_value="free"):
            fields = render_offer.card_fields(details)
        item.update(status="awaiting", cost=details["text"], paid_approval=True, ask=spending.COST,
                    ask_label="Approve", ask_lines=spending.ask_lines(spending.COST, details, spending.new_ledger(),
                                                                      spending.Settings(), balance=balance), **fields)
        return item

    def test_a_plain_paid_card_short_of_its_price_gets_the_upgrade_first(self):
        for width in (420, 260):
            with self.subTest(width=width):
                self.WIDTH = width
                display = self.build(self.plain_card(50))
                hits = {h["id"]: h for h in display["hits"]}
                self.assertEqual(hits["item0.tool.plan_upgrade"]["action"], ("paid_upgrade", 0))
                self.assertEqual(hits["item0.tool.run"]["action"], ("approve", 0), "Approve stays")
                self.assertEqual(hits["item0.tool.reject"]["action"], ("reject", 0))
                fills = {p["id"]: p["color"] for p in display["prims"] if p.get("t") == "rect"}
                self.assertEqual((fills["item0.tool.plan_upgrade"], fills["item0.tool.run"]), (T.BUTTON, T.BUTTON_GHOST))
                texts = self.texts(display)
                self.assertEqual(texts["item0.tool.plan_upgrade.label"], "Upgrade to Pro")
                self.assertIn("You have 50 credits; this needs 80.", texts.values())
                self.assertEqual(texts["item0.tool.plan_note"], "Pro is $20 a month with $22 of credits every month.")
                for hit in display["hits"]:
                    if hit["id"].startswith("item0"):
                        self.assertLessEqual(hit["x"] + hit["w"], width - T.PAD, hit["id"])

    def test_a_plain_paid_card_with_enough_credits_is_todays(self):
        display = self.build(self.plain_card(500))
        hits = {h["id"] for h in display["hits"]}
        self.assertNotIn("item0.tool.plan_upgrade", hits)
        self.assertNotIn("item0.tool.plan_note", self.texts(display))


class ShortAnswerTest(unittest.TestCase):
    """Enter never upgrades; Upgrade opens the credits pop-up's page and the card waits for the new plan."""

    def setUp(self):
        for target in (patch.object(config, "billing_open", return_value=True),
                       patch.object(render_offer, "live_balance", return_value=None)):
            target.start()
            self.addCleanup(target.stop)
        self.session, self.turn = state.new_session(), agent.Turn()
        self.session["turn"] = self.turn
        self.card = state.item_tool("loopcut_cloud__render", "Render scene", "")
        with patch.object(render_offer, "account_plan", return_value="free"):
            self.card.update(status="awaiting", **render_offer.card_fields(render_details(balance=100)))
        self.session["items"].append(self.card)

    def test_enter_on_a_short_choice_does_nothing_and_esc_still_declines(self):
        with patch.object(state, "session", return_value=self.session):
            self.assertFalse(agent.decide(True))
            self.assertFalse(self.turn.decided.is_set())
            render_offer.live_balance.return_value = 2300  # Upgraded: the cloud is Recommended again.
            self.assertTrue(agent.decide(True))
            self.assertTrue(self.turn.approved)
            self.turn.decided.clear()
            render_offer.live_balance.return_value = 100
            self.assertTrue(agent.decide(False))
            self.assertFalse(self.turn.approved)

    @staticmethod
    def account():
        """loopcut.account without Blender (it imports bpy.utils.previews): its browser and plan watch, recorded."""
        import contextlib
        import loopcut
        from unittest.mock import Mock
        fake = types.SimpleNamespace(open_browser=Mock(), watch_plan=Mock())
        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(sys.modules, {"loopcut.account": fake}))
        stack.enter_context(patch.object(loopcut, "account", fake, create=True))

        @contextlib.contextmanager
        def entered():
            with stack:
                yield fake
        return entered()

    def test_upgrade_opens_the_plans_page_and_refreshes_on_a_new_plan(self):
        runtime = types.SimpleNamespace(refresh_async=lambda: runtime.refreshed.append(True), refreshed=[])
        with self.account() as account, patch.object(cloud, "_runtime", runtime):
            opened, watch = account.open_browser, account.watch_plan
            render_offer.upgrade(self.card)
            opened.assert_called_once_with(config.pricing_url("insufficient_credits", "pro"))
            previous, on_change = watch.call_args.args
            self.assertEqual(previous, "free")
            on_change({"plan": "pro"})
        self.assertEqual(runtime.refreshed, [True])
        self.assertEqual(self.card["status"], "awaiting", "the card waits for the click that renders")

    def test_a_paid_plan_tops_up_and_a_settled_card_opens_nothing(self):
        self.card["render_choice"]["plan"] = "pro"
        with self.account() as account:
            opened, watch = account.open_browser, account.watch_plan
            render_offer.upgrade(self.card)
            opened.assert_called_once_with(config.credits_url("insufficient_credits"))
            watch.assert_not_called()
            self.card["status"] = "running"
            render_offer.upgrade(self.card)
            self.assertEqual(opened.call_count, 1)



def frame_line(seconds, scene_name="Scene", **settings):
    row = {"scene": scene_name, "engine": "BLENDER_EEVEE", "width": 3840, "height": 2160, "samples": 64,
           "device": "GPU", **settings, "seconds": seconds}
    return render_offer.FRAME_PREFIX + json.dumps(row)


class WorkspaceRouteTest(Timings):
    """The ChatGPT-plan workspace (and Codex) route: run_blender's frames are timed, and the laptop's result never
    names render_job, which that route lacks."""

    def test_the_prelude_prints_each_frame_without_the_loopcut_package(self):
        import contextlib
        import io
        import runpy
        handlers = types.SimpleNamespace(render_pre=[], render_post=[], persistent=lambda fn: fn)
        addon = types.SimpleNamespace(preferences=types.SimpleNamespace(compute_device_type="NONE"))
        fake = types.SimpleNamespace(app=types.SimpleNamespace(handlers=handlers),
                                     context=types.SimpleNamespace(preferences=types.SimpleNamespace(
                                         addons={"cycles": addon})))
        prelude = Path(render_offer.__file__).with_name("render_timing.py")
        current = scene(engine="CYCLES", samples=128)
        current.cycles.device = "GPU"  # Asked for, but a factory Blender has no compute device: the CPU.
        output = io.StringIO()
        clock = iter([10.0, 41.5])
        with patch.dict(sys.modules, {"bpy": fake, "loopcut": None}), contextlib.redirect_stdout(output), \
                patch("time.monotonic", side_effect=lambda: next(clock)):
            runpy.run_path(str(prelude), run_name="__main__")
            handlers.render_pre[0](current)
            handlers.render_post[0](current)
        frames, kept = render_offer.log_frames(output.getvalue())
        self.assertEqual(kept, "")
        self.assertEqual(frames, [{"scene": "Scene", "engine": "CYCLES", "width": 3840, "height": 2160,
                                   "samples": 128, "device": "CPU", "seconds": 31.5}])

    def test_a_runs_frames_after_its_cold_first_one(self):
        log = "\n".join(["Blender 5.2", frame_line(12.0), "Saved: frame 1", frame_line(4.1), frame_line(3.9),
                         render_offer.FRAME_PREFIX + "{not json", frame_line(30.0, "Other")])
        frames, kept = render_offer.log_frames(log)
        self.assertEqual(kept, "Blender 5.2\nSaved: frame 1")
        timings = render_offer.run_seconds(frames)
        self.assertEqual([(name, round(seconds, 3)) for name, _, seconds in timings], [("Scene", 4.0), ("Other", 30.0)])

    def test_run_blender_on_source_blend_times_this_scene_and_says_so_on_its_card(self):
        from test_workspace import Folder
        from loopcut import workspace

        class Run(Folder):
            def runTest(self):
                pass
        run = Run()
        run.setUp()
        self.addCleanup(run.doCleanups)
        (run.ws / "test_frame.py").write_text("import bpy")
        (run.ws / "source.blend").write_bytes(b"fixture")
        log_text = "\n".join([frame_line(14.0), frame_line(6.2), frame_line(5.8), "Saved: 'frame.png'"])

        def process(command, ws, env, seconds, log, is_cancelled, label=""):
            self.assertIn(str(workspace.RENDER_TIMING), command)
            log.write_text(log_text, encoding="utf-8")
            return 0, 0.1, False
        for blend, recorded in (("source.blend", True), ("", False)):
            with self.subTest(blend=blend), patch.object(workspace, "run_process", side_effect=process), \
                    patch.object(render_offer, "live_path", return_value=KEY[0]), patch.object(render_offer, "_timings", []):
                result = run.then(workspace.run_blender("test_frame.py", blend=blend))
                self.assertEqual(result.outcome, "6 s a frame")
                self.assertNotIn(render_offer.FRAME_PREFIX, result.text)
                found = render_offer.seconds_per_frame(KEY, UHD)
                self.assertEqual(round(found, 3) if found else None, 6.0 if recorded else None,
                                 "only source.blend is the live scene")

    def test_the_laptop_result_names_no_tool_the_route_lacks(self):
        from loopcut import workspace
        data = {**OFFER}
        self.enterContext(open_scene())
        with patch.object(workspace, "current", return_value=Path("/ws")), \
                patch.object(job_tools, "start_render_job", return_value=tools.ToolResult(
                    json.dumps({"id": JOB_ID, "output": "/projects/renders/loopcut-d"}) + job_tools.STARTED_NOTE)):
            job_id, text, ok = render_offer._start(data)
        self.assertEqual((job_id, ok), (JOB_ID, True))
        self.assertNotIn("render_job", text)
        self.assertNotIn("status", text)
        self.assertIn("/projects/renders/loopcut-d", text)
        self.assertIn("1,800 frames", text)
        with patch.object(workspace, "current", return_value=Path("/ws")), \
                patch.object(job_tools, "start_render_job", side_effect=tools.ToolError("No camera.")):
            _, text, ok = render_offer._start(data)
        self.assertFalse(ok)
        self.assertNotIn("render_job", text)
        with patch.object(workspace, "current", return_value=None), \
                patch.object(job_tools, "start_render_job", side_effect=started):
            self.assertIn("render_job action status", render_offer._start(data)[1], "the single loop has it")


# --- 0.2.20: a long render_job's card is the same choice (rel019-cloud2: "in the cloud, I want to close my laptop",
# and the model called render_job, whose card offered no cloud) ------------------------------------------------------

JOB = {"action": "start", "output_dir": "//renders", "start": 1, "end": 120, "format": "MP4", "budget_seconds": 900}
JOB_INPUTS = {"frame_start": 1, "frame_end": 120, "engine": "eevee", "samples": 64, "resolution_pct": 100}
JOB_OFFER = {**UHD, "frames": [1, 120, 1], "count": 120, "fps": 24.0, "camera": "Camera", "seconds_per_frame": 31.0,
             "key": list(KEY), "denoise": True, "cloud": JOB_INPUTS}  # 120 × 31 s: about 1 h here.
CLOUD_JOB = json.dumps({"job_id": "job_1", "status": "queued", "next": "loopcut_cloud.job_status"})


class PrepareJobTest(Timings):
    """render_offer.prepare_job: a render_job's frames, pixels and samples, timed as prepare() times a cloud call's,
    and the cloud render's inputs for the same frames."""

    def prepare(self, arguments, current=None, rows=(), file=KEY[0]):
        current = current or scene()
        current.frame_start, current.frame_end = 1, 250
        with patch.object(bpy, "context", types.SimpleNamespace(scene=current), create=True), \
                patch.object(bpy, "data", types.SimpleNamespace(filepath=file), create=True), \
                patch.object(checkpoints, "data_root", return_value=Path("/nowhere")), \
                patch.object(jobs, "list_jobs", return_value=list(rows)):
            return render_offer.prepare_job(arguments)

    def test_no_timing_or_under_fifteen_minutes_means_no_offer(self):
        self.assertIsNone(self.prepare(JOB), "no guess")
        render_offer.record(KEY, UHD, 31.0)
        self.assertIsNone(self.prepare({**JOB, "end": 29}))  # 29 × 31 s = 899 s.
        self.assertIsNotNone(self.prepare({**JOB, "end": 30}))

    def test_an_unsaved_file_gets_no_offer(self):
        render_offer.record(("", "Scene"), UHD, 31.0)
        self.assertIsNone(self.prepare(JOB, file=""))

    def test_the_same_frames_pixels_and_samples_in_the_cloud(self):
        render_offer.record(KEY, UHD, 31.0)
        self.assertEqual(self.prepare(JOB), JOB_OFFER)
        self.assertEqual(self.prepare({**JOB, "camera": "Wide"})["cloud"], {**JOB_INPUTS, "camera": "Wide"})
        stepped = self.prepare({**JOB, "start": None, "end": None, "frames": list(range(1, 241, 2))})
        self.assertEqual((stepped["count"], stepped["frames"], stepped["cloud"]),
                         (120, [1, 239, 2], {**JOB_INPUTS, "frame_end": 239, "frame_step": 2}))
        default = self.prepare({k: v for k, v in JOB.items() if k not in ("start", "end")})
        self.assertEqual((default["count"], default["cloud"]["frame_end"]), (250, 250), "the scene's range")

    def test_pixels_are_a_resolution_pct_of_the_scenes_and_samples_the_jobs(self):
        half = {**UHD, "width": 1920, "height": 1080}
        render_offer.record(KEY, half, 31.0)
        self.assertEqual(self.prepare({**JOB, "width": 1920, "height": 1080})["cloud"]["resolution_pct"], 50)
        self.assertEqual(self.prepare(JOB, scene(pct=50))["cloud"]["resolution_pct"], 50, "the scene's own size")
        render_offer.record(KEY, {**UHD, "samples": 16}, 31.0)
        self.assertEqual(self.prepare({**JOB, "samples": 16})["cloud"]["samples"], 16)
        cycles = {"engine": "CYCLES", "width": 3840, "height": 2160, "samples": 512, "device": "CPU"}
        render_offer.record(KEY, cycles, 31.0)
        current = scene(engine="CYCLES", samples=512)
        current.cycles.use_denoising = False
        self.assertEqual(self.prepare(JOB, current)["cloud"], {**JOB_INPUTS, "engine": "cycles", "samples": 512,
                                                               "denoise": False})

    def test_what_the_cloud_render_cannot_name_means_no_offer(self):
        render_offer.record(KEY, UHD, 31.0)
        render_offer.record(KEY, {**UHD, "width": 1000, "height": 1000}, 31.0)
        cases = [{**JOB, "start": None, "end": None, "frames": [1, 2, 5] + list(range(6, 100))},  # Not one run.
                 {**JOB, "width": 1000, "height": 1000},                                         # No pct gives it.
                 {**JOB, "samples": 128}]                                                        # Not the measured.
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertIsNone(self.prepare(arguments))
        self.assertIsNone(self.prepare(JOB, scene(engine="BLENDER_WORKBENCH")), "the cloud renders no Workbench")
        render_offer.record(KEY, {**UHD, "samples": 8192}, 31.0)
        self.assertIsNone(self.prepare(JOB, scene(samples=8192)), "over the cloud render's 4096")
        self.assertIsNone(self.prepare({**JOB, "end": 0}), "a range render_job refuses")

    def test_the_cloud_render_must_be_there_for_this_account(self):
        managed = [{"name": "render", "backend": "managed"}]
        for runtime, expected in ((types.SimpleNamespace(signed_in=True, descriptors=lambda: managed),
                                   cloud.PREFIX + "render"),
                                  (types.SimpleNamespace(signed_in=False, descriptors=lambda: managed), ""),
                                  (types.SimpleNamespace(signed_in=True, descriptors=lambda: [
                                      {"name": "render", "backend": "byok"}]), ""),
                                  (types.SimpleNamespace(signed_in=True, descriptors=lambda: []), ""),
                                  (None, "")):
            with self.subTest(runtime=runtime), patch.object(cloud, "_runtime", runtime):
                self.assertEqual(render_offer.cloud_tool(), expected)


class JobChoiceLoopTest(unittest.TestCase):
    """agent.run_loop: the model's render_job start asks as the choice card, and each answer does what it says."""

    def run_job(self, answer, arguments=JOB, offer=JOB_OFFER, *, auto_heavy=False, tool="loopcut_cloud__render"):
        """One render_job call through the loop; answer(card) is the user's answer while it waits. (session, the card
        as it waited, tool runs, prepare_job's mock)."""
        from unittest import mock
        from loopcut import llm, nudges
        session = state.new_session()
        session["auto_heavy"] = auto_heavy
        session["items"].append(state.item_user("render the animation"))
        session["messages"].append({"role": "user", "content": "render the animation"})
        turn = agent.Turn()
        turn.config = config.Config("test-key", "https://example.invalid/v1", "fake", "low", False, False, 0, 24000)
        spec = agent.LoopSpec(role="single", system=lambda s: "", schemas=lambda: [
            {"type": "function", "function": {"name": "render_job"}}], save=lambda s: None, asks=True,
            checkpoints=False, changes_card=False)
        replies = iter([llm.Completion(tool_calls=[llm.ToolCall("c1", "render_job", json.dumps(arguments))]),
                        llm.Completion(text="Done.")])

        def reply(*_):
            completion = next(replies)
            item = state.item_assistant()
            item.update(text=completion.text, streaming=False)
            session["items"].append(item)
            return completion
        seen = {}

        def wait(_turn):
            card = next(i for i in session["items"] if i.get("status") == "awaiting")
            seen.update(json.loads(json.dumps(card)))
            return answer(card)
        run = mock.Mock(side_effect=lambda name, _: tools.ToolResult(
            CLOUD_JOB if name == tool else json.dumps({"id": JOB_ID, "state": "queued"}) + job_tools.STARTED_NOTE))
        with patch.object(agent, "_request", side_effect=reply), patch.object(agent, "_redraw"), \
                patch.object(nudges, "flush"), patch.object(agent, "_wait_for_approval", side_effect=wait), \
                patch.object(mainthread, "run_on_main", side_effect=on_main_now), \
                patch.object(render_offer, "cloud_tool", return_value=tool), \
                patch.object(render_offer, "prepare_job", return_value=offer) as prepared, \
                patch.object(render_offer, "live_balance", return_value=1850):
            session["busy"] = True
            agent.run_loop(session, turn, spec, run_tool=run, ensure_checkpoint=mock.Mock())
        return session, seen, run, prepared

    @staticmethod
    def result(session):
        return next(m["content"] for m in session["messages"] if m["role"] == "tool")

    @staticmethod
    def card(session):
        return next(i for i in session["items"] if i.get("kind") == "tool")

    def test_a_long_render_job_asks_as_the_choice(self):
        session, seen, run, prepared = self.run_job(lambda card: False)
        prepared.assert_called_once_with(JOB)
        self.assertEqual(seen["render_choice"], {**JOB_OFFER, "credits": None, "cloud_seconds": None,
                                                 "balance": 1850, "plan": ""})
        session, seen, *_ = self.run_job(lambda card: False, auto_heavy=True)
        self.assertIn("render_choice", seen, "renders always allowed: the long-render choice still asks")

    def test_the_owners_switch_lets_always_allowed_renders_skip_the_choice(self):
        with patch.object(render_offer, "ASK_EVEN_WHEN_ALWAYS_ALLOWED", False):
            session, seen, run, prepared = self.run_job(lambda card: self.fail("no card asks"), auto_heavy=True)
            prepared.assert_not_called()
            self.assertEqual((seen, run.call_args.args), ({}, ("render_job", json.dumps(JOB))), "as asked, unasked")
            self.assertEqual(self.result(session),
                             json.dumps({"id": JOB_ID, "state": "queued"}) + job_tools.STARTED_NOTE)
            session, seen, run, _ = self.run_job(lambda card: False)
            self.assertIn("render_choice", seen, "a card that asks anyway is still the choice")
            run.assert_not_called()

    def test_this_computer_runs_the_render_job_with_time_to_finish(self):
        def here(card):
            card["picked"] = "here"
            return False
        session, _, run, _ = self.run_job(here)
        name, arguments = run.call_args.args
        self.assertEqual((run.call_count, name), (1, "render_job"))
        budget = math.ceil(31 * 120 * 1.25) + 600
        self.assertEqual(json.loads(arguments), {**JOB, "budget_seconds": budget}, "the card said 1 h, not 15 min")
        result = self.result(session)
        self.assertTrue(result.startswith(render_offer.JOB_HERE + f" Its time limit is {budget} s, the measured time "
                                          f"of its 120 frames with a margin, not the 900 s asked.\n"), result)
        self.assertIn(JOB_ID, result)
        card = self.card(session)
        self.assertEqual((card["name"], card["status"]), ("render_job", "done"))
        self.assertIn("up to about 1½ h", card["summary"])
        self.assertNotIn("render_choice", card)
        session, _, run, _ = self.run_job(here, {**JOB, "budget_seconds": 86400})
        self.assertEqual(json.loads(run.call_args.args[1])["budget_seconds"], 86400, "as asked")
        self.assertTrue(self.result(session).startswith(render_offer.JOB_HERE + "\n"))
        session, _, run, _ = self.run_job(here, {k: v for k, v in JOB.items() if k != "budget_seconds"})
        self.assertEqual(json.loads(run.call_args.args[1])["budget_seconds"], budget)
        self.assertTrue(self.result(session).startswith(
            render_offer.JOB_HERE + f" Its time limit is {budget} s, the measured time of its 120 frames with a "
                                    "margin.\n"), self.result(session))

    def test_the_cloud_runs_the_cloud_render_for_the_same_frames_and_no_render_job(self):
        def cloud_pick(card):
            card["picked"] = "cloud"
            return True
        session, _, run, _ = self.run_job(cloud_pick)
        self.assertEqual(run.call_count, 1, "render_job never started")
        name, arguments = run.call_args.args
        self.assertEqual((name, json.loads(arguments)), ("loopcut_cloud__render", JOB_INPUTS))
        result = self.result(session)
        self.assertEqual(result, render_offer.JOB_CLOUD.format(inputs=json.dumps(JOB_INPUTS), mp4=render_offer.JOB_MP4,
                                                               result=CLOUD_JOB))
        self.assertIn("job_1", result)
        self.assertIn("follow it with loopcut_cloud.job_status", result)
        self.assertIn("the frames and, for an animation, an MP4 beside them", result, "a video comes back (0.2.19)")
        card = self.card(session)
        self.assertEqual((card["name"], json.loads(card["code"])), ("loopcut_cloud__render", JOB_INPUTS),
                         "its card is the cloud render's: its quote, progress and charge show there")
        session, _, run, _ = self.run_job(cloud_pick, {**JOB, "format": "PNG"})
        self.assertNotIn(render_offer.JOB_MP4.strip(), self.result(session))

    def test_not_now_runs_neither_and_the_model_asks_which(self):
        session, _, run, _ = self.run_job(lambda card: False)
        run.assert_not_called()
        self.assertEqual(self.result(session), render_offer.SKIPPED)
        self.assertEqual(self.card(session)["status"], "rejected")

    def test_without_an_offer_the_card_is_todays_render_job_card(self):
        session, seen, run, _ = self.run_job(lambda card: True, offer=None)
        self.assertNotIn("render_choice", seen)
        self.assertEqual(seen["summary"], "Render frames 1-120 as an MP4 on this computer, up to about 15 min. "
                                          + job_tools.STAYS_ON)
        self.assertEqual(run.call_args.args, ("render_job", json.dumps(JOB)), "as asked")
        self.assertEqual(self.result(session), json.dumps({"id": JOB_ID, "state": "queued"}) + job_tools.STARTED_NOTE)
        session, seen, run, prepared = self.run_job(lambda card: True, tool="")
        prepared.assert_not_called()  # No cloud render for this account: nothing to choose, nothing timed.
        self.assertNotIn("render_choice", seen)
        self.assertEqual(run.call_args.args[0], "render_job")

    def test_only_a_start_in_a_loop_that_asks_is_timed(self):
        spec = types.SimpleNamespace(asks=True)
        turn = agent.Turn()
        call = lambda arguments, name="render_job": types.SimpleNamespace(name=name, arguments=json.dumps(arguments))
        with patch.object(render_offer, "cloud_tool", return_value="loopcut_cloud__render"), \
                patch.object(render_offer, "prepare_job", return_value=JOB_OFFER) as prepared, \
                patch.object(mainthread, "run_on_main", side_effect=on_main_now):
            self.assertEqual(agent._job_offer(spec, call(JOB), turn, True), JOB_OFFER)
            self.assertEqual(agent._job_offer(spec, call(JOB), turn, False), JOB_OFFER, "renders always allowed")
            for asks, made in ((True, call({"action": "status", "job_id": "j"})), (True, call(JOB, "run_python")),
                               (False, call(JOB))):
                with self.subTest(asks=asks, made=made):
                    self.assertIsNone(agent._job_offer(types.SimpleNamespace(asks=asks), made, turn, True))
            with patch.object(render_offer, "ASK_EVEN_WHEN_ALWAYS_ALLOWED", False):
                self.assertIsNone(agent._job_offer(spec, call(JOB), turn, False), "the switch off: no choice")
                self.assertEqual(agent._job_offer(spec, call(JOB), turn, True), JOB_OFFER)
        self.assertEqual(prepared.call_count, 3)


class JobChoiceCardTest(ChoiceLayoutTest):
    """The render job's choice card: no quote before the click, so no price and no cloud time, ever guessed."""

    def job_card(self, balance=1850):
        item = state.item_tool("render_job", "Render frames 1-120 as an MP4 on this computer", "")
        with patch.object(render_offer, "live_balance", return_value=balance):
            return state.awaiting(item, render_choice=render_offer.job_choice(JOB_OFFER))

    def test_the_cloud_says_it_is_priced_before_it_starts(self):
        with self.survives_quit(True):
            display = self.build(self.job_card())
        texts = self.texts(display)
        self.assertEqual((texts["item0.choice.title"], texts["item0.choice.detail"]),
                         ("Render 120 frames", "4K · 5 s · EEVEE"))
        self.assertEqual(texts["item0.choice.cloud.fact0"], render_offer.UNPRICED)
        self.assertNotIn("item0.choice.cloud.fact1", texts, "no cloud time without a quote")
        self.assertNotIn("item0.choice.cloud.badge.label", texts, "never Recommended unpriced")
        self.assertEqual(texts["item0.choice.cloud.go.label"], "Render in the cloud", "no price, no ⏎")
        self.assertEqual([texts[f"item0.choice.cloud.bullet{k}"] for k in range(3)], list(render_offer.CLOUD_BULLETS))
        self.assertEqual(texts["item0.choice.cloud.bullet2"], "The frames and the MP4 wait in this chat",
                         "an animation's cloud render returns its MP4 beside the frames (0.2.19)")
        self.assertEqual([texts["item0.choice.here.fact0"], texts["item0.choice.here.fact1"]],
                         ["About 1 h (31 s × 120)", "Free"])
        self.assertEqual(texts["item0.choice.here.go.label"], "Render on my laptop")
        self.assertEqual(texts["item0.choice.footer"], "Balance 1,850 credits")
        cloud_texts = " ".join(v for k, v in texts.items() if k.startswith("item0.choice.cloud"))
        self.assertNotIn("credit", cloud_texts)
        self.assertNotIn("$", cloud_texts)
        hits = {h["id"]: h["action"] for h in display["hits"]}
        self.assertEqual((hits["item0.choice.cloud.go"], hits["item0.choice.here.go"], hits["item0.choice.skip"]),
                         (("render_choice", (0, "cloud")), ("render_choice", (0, "here")), ("reject", 0)))
        self.assert_inside(display)
        self.assertEqual(display["max_scroll"], 0)

    def test_one_frame_comes_back_as_an_image(self):
        one = {**JOB_OFFER, "frames": [40, 40, 1], "count": 1}
        with self.survives_quit(True):
            for data in ({**render_offer.job_choice(one), "balance": 1850},                  # A render job's card.
                         {**render_details()["render_offer"], "frames": [40, 40, 1], "count": 1}):  # The cloud call's.
                with self.subTest(credits=data["credits"]):
                    bullets = render_offer.options(data, None)[0][0]["bullets"]
                    self.assertEqual(bullets[2], "The image waits in this chat")
                    self.assertNotIn("MP4", " ".join(bullets))
            self.assertEqual(render_offer.options(render_offer.job_choice(JOB_OFFER), None)[0][0]["bullets"][2],
                             "The frames and the MP4 wait in this chat")
            item = self.job_card()
            item["render_choice"].update(frames=[40, 40, 1], count=1)
            display = self.build(item)
        self.assertEqual(self.texts(display)["item0.choice.cloud.bullet2"], "The image waits in this chat")
        self.assert_inside(display)
        with self.survives_quit(False):
            self.assertEqual(render_offer.cloud_bullets(1), list(render_offer.CLOUD_BULLETS_OPEN))

    def test_a_low_balance_gets_no_guessed_shortfall_and_no_balance_no_footer(self):
        data = render_offer.job_choice(JOB_OFFER)
        cloud_option, _ = render_offer.options({**data, "balance": 3}, None)[0]
        self.assertNotIn("warn", cloud_option)
        self.assertEqual(cloud_option["action"], "cloud")
        with patch.object(render_offer, "live_balance", return_value=None):
            self.assertEqual(render_offer.options(render_offer.job_choice(JOB_OFFER), None)[1], "")

    def test_enter_picks_neither_and_a_click_answers(self):
        session, turn = state.new_session(), agent.Turn()
        item = self.job_card()
        session["items"].append(item)
        session["turn"] = turn
        with patch.object(state, "session", return_value=session), \
                patch.object(render_offer, "live_balance", return_value=1850):
            self.assertFalse(agent.decide(True, index=0), "Enter: nothing is Recommended")
            self.assertFalse(turn.decided.is_set())
            render_offer.click(session, 0, "here")
        self.assertEqual((turn.approved, item["picked"]), (False, "here"))

    def test_the_quote_after_a_cloud_pick_asks_on_the_plain_paid_card(self):
        session, turn = state.new_session(), agent.Turn()
        turn.spending = spending.Settings()
        card = state.item_tool("loopcut_cloud__render", "Render scene · Getting a credit quote…", "")
        session["items"].append(card)
        seen = {}
        consent = agent._Consent(session, turn, card, chosen=True)
        with patch.object(agent, "_redraw"), patch.object(conversations, "save"), \
                patch.object(agent, "_wait_for_approval", side_effect=lambda t: seen.update(card) or True):
            self.assertTrue(consent(render_details()))
        self.assertEqual(seen["ask"], spending.COST)
        self.assertNotIn("render_choice", seen, "the user already chose the cloud: no second choice")
        self.assertEqual(seen["price"]["credits"], 640, "its price, and an Upgrade when short (price_shortfall)")


if __name__ == "__main__":
    unittest.main()
