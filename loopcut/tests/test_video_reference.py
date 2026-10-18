"""Video reference v0 without Blender: clip attachment checks and the 15 s trim, beats found on
synthetic frames (a still, a spike, a hold), the timed contact sheets' layout and timestamps,
beats.json and camera_motion.json, a poor camera solve dropped, and what the model is given within
the 6-image budget, including the clip tools offered only while a clip is attached.

What needs a real Blender (decoding frames through the sequencer, the tracker and solve, drawing the
sheets with imbuf and blf, the camera background) is loopcut/harness/video_reference_check.py.
All media here are made up in the test."""

import importlib.util
import json
import math
import os
import random
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import agent, attachments, conversations, state, tools, video_attach  # noqa: E402
from loopcut import videoref as V  # noqa: E402

TOOL_SCHEMAS = agent._tool_schemas  # Other tests stub it; keep the real one.
FPS = 30.0


def still_spike_hold(seed: int = 7, still: int = 15, hold: int = 20, side: int = 12) -> list[list[float]]:
    """Grey frames: `still` frames of one pose, then a sudden change held for `hold` frames, with a
    little sensor noise on every frame."""
    rng = random.Random(seed)
    pose_a = [rng.uniform(0.2, 0.4) for _ in range(side * side)]
    pose_b = [rng.uniform(0.6, 0.8) for _ in range(side * side)]
    return [[v + rng.uniform(-0.002, 0.002) for v in (pose_a if n < still else pose_b)] for n in range(still + hold)]


def swaying_clip(frames: int = 72, width: int = 64, height: int = 36, fps: float = 24.0):
    """Grey frames of a handheld shot: a smooth textured background swaying sub-pixel every frame
    (2.5 px across, 1 px up and down, at different rates), and a 14 px bright square that holds,
    moves 30 px across frames 24-28, and holds. Returns (frames, the true sway per frame)."""
    def sway(f):
        t = (f - 1) / fps
        return 2.5 * math.sin(2 * math.pi * t / 1.3), 1.0 * math.sin(2 * math.pi * t / 1.1)

    def overlap(a0, a1, b0, b1):
        return max(0.0, min(a1, b1) - max(a0, b0))
    out = []
    for f in range(1, frames + 1):
        ox, oy = sway(f)
        sx = (12.0 if f <= 24 else 42.0 if f >= 28 else 12.0 + 7.5 * (f - 24)) - ox
        sy = 12.0 - oy
        pixels = []
        for y in range(height):
            for x in range(width):
                u, v = x + ox, y + oy
                ground = 0.5 + 0.12 * math.sin(0.45 * u + 0.25 * v) + 0.1 * math.sin(0.17 * u - 0.5 * v + 1.0)
                cover = overlap(x, x + 1, sx, sx + 14) * overlap(y, y + 1, sy, sy + 14)
                pixels.append(ground * (1 - cover) + cover)
        out.append(pixels)
    return out, [sway(f) for f in range(1, frames + 1)]


def rz(degrees: float) -> list[list[float]]:
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def matrix(rotation, translation) -> list[list[float]]:
    return [rotation[0] + [translation[0]], rotation[1] + [translation[1]], rotation[2] + [translation[2]],
            [0.0, 0.0, 0.0, 1.0]]


SAMPLE_BEATS = [  # 61-285 at 30 fps: a start pose, a draw, a hold, a release, an end pose.
    {"kind": "hold", "frame": 68, "time": 2.233, "start_frame": 61, "end_frame": 75, "start_time": 2.0,
     "end_time": 2.467, "motion": 0.02, "label": "start pose"},
    {"kind": "peak", "frame": 94, "time": 3.1, "motion": 0.7, "label": "wind-up"},
    {"kind": "hold", "frame": 130, "time": 4.3, "start_frame": 120, "end_frame": 140, "start_time": 3.967,
     "end_time": 4.633, "motion": 0.05, "label": "anticipation"},
    {"kind": "peak", "frame": 160, "time": 5.3, "motion": 1.0, "label": "fastest motion"},
    {"kind": "hold", "frame": 278, "time": 9.233, "start_frame": 270, "end_frame": 285, "start_time": 8.967,
     "end_time": 9.467, "motion": 0.01, "label": "end pose"},
]


class Folder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"LOOPCUT_DATA_DIR": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = state.reset()
        self.addCleanup(state.reset)


class AttachmentTest(Folder):
    def test_clips_are_mp4_and_mov_beside_the_images(self):
        self.assertTrue(attachments.is_video("Crossbow.MOV") and attachments.is_video("a.mp4"))
        self.assertFalse(attachments.is_video("a.png") or attachments.is_video("a.avi"))
        self.assertEqual(attachments.ACCEPTED[-2:], (".mp4", ".mov"))

    def test_check_clip_wants_a_real_file_of_bounded_size(self):
        folder = Path(self.tmp.name)
        clip = folder / "take.mp4"
        clip.write_bytes(b"\x00" * 32)
        attachments.check_clip(clip)
        for bad in (folder / "missing.mp4", folder / "notes.txt"):
            with self.assertRaises(V.ClipError):
                attachments.check_clip(bad)
        (folder / "empty.mov").write_bytes(b"")
        with self.assertRaises(V.ClipError):
            attachments.check_clip(folder / "empty.mov")
        with mock.patch.object(attachments, "MAX_CLIP_BYTES", 16), self.assertRaisesRegex(V.ClipError, "larger"):
            attachments.check_clip(clip)

    def test_add_points_a_clip_to_the_dialog_and_counts_sheets_being_prepared(self):
        problems = attachments.add(self.session, ["/somewhere/take.mp4"])
        self.assertEqual(self.session["attachments"], [])
        self.assertIn("is a clip", problems[0])
        self.session["video_reserved"] = 6
        self.assertEqual(attachments.room(self.session), 0)
        problems = attachments.add(self.session, ["/somewhere/photo.png"])
        self.assertIn("Only 6 images", problems[0])

    def test_trim_maps_seconds_to_clip_frames_and_caps_at_15_s(self):
        self.assertEqual(V.trim(900, FPS, 2.0, 9.5), (61, 285))
        self.assertEqual(V.trim(450, FPS), (1, 450))            # Exactly 15 s.
        self.assertEqual(V.trim(1800, FPS, 10.0), (301, 750))   # No out point: 15 s from the in point.
        self.assertEqual(V.trim(120, FPS, 1.0, 99.0), (31, 120))  # Out past the end: the end.
        first, last = V.trim(900, 29.97, 0.0, 15.0)
        self.assertEqual(first, 1)
        self.assertLessEqual((last - first + 1) / 29.97, V.MAX_SECONDS + 1 / 29.97)
        for args, words in (((900, FPS, 0.0, 20.0), "15 s or less"), ((900, FPS, 31.0, None), "outside"),
                            ((900, FPS, 1.0, 1.2), "at least"), ((0, FPS, 0.0, 1.0), "no frames"),
                            ((100, 0.0, 0.0, 1.0), "no frames")):
            with self.assertRaisesRegex(V.ClipError, words):
                V.trim(*args)

    def test_start_trims_before_launching_and_reserves_the_sheets(self):
        clip = Path(self.tmp.name) / "take.mp4"
        clip.write_bytes(b"\x00" * 32)
        info = {"fps": FPS, "frames": 900, "size": [1920, 1080]}
        launched = []
        fake = SimpleNamespace(app=SimpleNamespace(binary_path="/opt/blender", timers=SimpleNamespace(
            is_registered=lambda f: False, register=lambda f, first_interval: None)))
        with mock.patch.object(video_attach, "probe", return_value=info), \
                mock.patch.object(video_attach, "bpy", fake), \
                mock.patch.object(video_attach.subprocess, "Popen", side_effect=lambda *a, **k: launched.append(a[0])
                                  or SimpleNamespace(poll=lambda: None)):
            with self.assertRaisesRegex(V.ClipError, "15 s or less"):
                video_attach.start(self.session, clip, 0.0, 20.0)
            self.assertEqual(launched, [])
            self.session["attachments"] = [{"ref": "r", "full": "r", "name": f"{n}.png"} for n in range(4)]
            clip_id = video_attach.start(self.session, clip, 2.0, 9.5, "  the crossbow\nand my hands ", True)
        self.addCleanup(video_attach._running.clear)
        self.assertEqual(launched[0][:4], ["/opt/blender", "--background", "--factory-startup", "--disable-autoexec"])
        spec = json.loads((Path(launched[0][-1]) / "spec.json").read_text())
        self.assertEqual((spec["in_frame"], spec["out_frame"], spec["max_sheets"]), (61, 285, 2))
        self.assertEqual(spec["watch"], "the crossbow and my hands")
        self.assertEqual(spec["source"], str(clip.resolve()))
        self.assertEqual(self.session["video_reserved"], 2)
        self.assertEqual(attachments.room(self.session), 0)
        self.assertEqual(video_attach.preparing(self.session), ["take.mp4"])
        self.assertIn(clip_id, video_attach._running)
        self.assertIn("stays on this computer", self.session["items"][-1]["text"])


class BeatsTest(unittest.TestCase):
    def test_a_still_then_a_spike_then_a_hold(self):
        frames = list(range(1, 36))
        energy = V.motion_energy(still_spike_hold())
        self.assertEqual(len(energy), 35)
        self.assertGreater(energy[15], 50 * max(energy[:15] + energy[16:]))
        beats = V.find_beats(energy, frames, FPS)
        peaks = [b for b in beats if b["kind"] == "peak"]
        holds = [b for b in beats if b["kind"] == "hold"]
        self.assertEqual([p["frame"] for p in peaks], [16])  # The first frame of the new pose.
        self.assertEqual(peaks[0]["label"], "fastest motion")
        self.assertAlmostEqual(peaks[0]["time"], 0.5)
        self.assertEqual(peaks[0]["motion"], 1.0)
        self.assertEqual([h["label"] for h in holds], ["start pose", "end pose"])
        self.assertEqual(holds[0]["start_frame"], 1)
        self.assertLess(holds[0]["end_frame"], 16)
        self.assertGreater(holds[1]["start_frame"], 16)
        self.assertEqual(holds[1]["end_frame"], 35)
        self.assertEqual([b["frame"] for b in beats], sorted(b["frame"] for b in beats))

    def test_two_moves_with_a_pause_between(self):
        rng = random.Random(3)
        poses = [[rng.random() for _ in range(64)] for _ in range(3)]
        frames = [poses[0]] * 15 + [poses[1]] * 15 + [poses[2]] * 15
        energy = V.motion_energy(frames)
        energy[30] *= 0.5  # The second move is the smaller one.
        beats = V.find_beats(energy, list(range(1, 46)), FPS)
        self.assertEqual([(b["kind"], b["frame"]) for b in beats if b["kind"] == "peak"], [("peak", 16), ("peak", 31)])
        self.assertEqual([b["label"] for b in beats], ["start pose", "fastest motion", "hold", "motion", "end pose"])

    def test_a_swaying_camera_still_has_holds(self):
        frames, sway = swaying_clip()
        numbers = list(range(1, 73))
        raw = V.find_beats(V.motion_energy(frames), numbers, 24.0)
        # Plain differencing: the sway is motion on every frame, so the holds are wrong (the harness, 2026-10-16).
        self.assertNotEqual([(b["start_frame"], b["end_frame"]) for b in raw if b["kind"] == "hold"][:1], [(1, 23)])
        motion = V.measure_motion(frames, 64)
        beats = V.find_beats(motion["energy"], numbers, 24.0)
        self.assertEqual([(b["kind"], b["label"]) for b in beats],
                         [("hold", "start pose"), ("peak", "fastest motion"), ("hold", "end pose")])
        hold_in, peak, hold_out = beats
        self.assertTrue(24 <= peak["frame"] <= 28, peak)
        self.assertEqual((hold_in["start_frame"], hold_out["end_frame"]), (1, 72))
        self.assertLessEqual(hold_in["end_frame"], 24)
        self.assertGreaterEqual(hold_out["start_frame"], 29)
        # The shift found is the sway: the picture moves against the camera, a fraction of a pixel a frame.
        found = [dx for dx, _ in motion["shift"][1:]]
        true = [-(b[0] - a[0]) for a, b in zip(sway, sway[1:])]
        steady = [i for i in range(len(true)) if not 22 <= i <= 28]  # Away from the subject's move.
        self.assertLess(max(abs(found[i] - true[i]) for i in steady), 0.2)
        found_y = [dy for _, dy in motion["shift"][1:]]
        true_y = [-(b[1] - a[1]) for a, b in zip(sway, sway[1:])]
        self.assertLess(max(abs(found_y[i] - true_y[i]) for i in steady), 0.2)

    def test_levels_rise_with_the_motion_handheld_footage_always_has(self):
        # What survives the shift removal in the harness clip (2026-10-16): an uneven residual from the sway
        # at about a quarter to a third of the one real move, dipping at its turnarounds.
        residual = [0.25 + 0.1 * math.sin(2 * math.pi * f / 15.6) + 0.04 * math.sin(f * 1.7) for f in range(60)]
        energy = [v + (0.7 if 12 <= f <= 15 else 0.0) for f, v in enumerate(residual)]
        beats = V.find_beats(energy, list(range(13, 73)), 24.0)
        self.assertEqual([(b["kind"], b["label"]) for b in beats],
                         [("hold", "start pose"), ("peak", "fastest motion"), ("hold", "end pose")])
        self.assertTrue(25 <= beats[1]["frame"] <= 28)
        self.assertEqual((beats[0]["start_frame"], beats[2]["end_frame"]), (13, 72))

    def test_shift_and_residual_on_a_known_move(self):
        width, height = 40, 20
        ground = [[math.sin(0.5 * x) + math.cos(0.4 * y) + 0.3 * math.sin(0.2 * x + 0.3 * y)
                   for x in range(-5, width + 5)]
                  for y in range(-5, height + 5)]
        before = [row[5:5 + width] for row in ground[5:5 + height]]
        now = [row[3:3 + width] for row in ground[4:4 + height]]  # The picture moved 2 px right, 1 px down the rows.
        dx, dy = V._frame_shift(before, now)
        self.assertAlmostEqual(dx, 2.0, delta=0.15)
        self.assertAlmostEqual(dy, 1.0, delta=0.15)
        self.assertLess(V._residual(before, now, 2.0, 1.0), 1e-9)
        self.assertGreater(V._residual(before, now, 0.0, 0.0), 0.1)

    def test_a_still_clip_is_one_hold(self):
        frames = [[0.5] * 16 for _ in range(20)]
        beats = V.find_beats(V.motion_energy(frames), list(range(5, 25)), FPS)
        self.assertEqual(len(beats), 1)
        self.assertEqual((beats[0]["kind"], beats[0]["label"], beats[0]["start_frame"], beats[0]["end_frame"]),
                         ("hold", "still", 5, 24))

    def test_motion_energy_wants_frames_of_one_size(self):
        self.assertEqual(V.motion_energy([]), [])
        with self.assertRaises(ValueError):
            V.motion_energy([[0.0, 1.0], [0.0]])

    def test_sampling_and_analysis_rates(self):
        samples = V.sample_frames(61, 285, FPS)
        self.assertEqual(samples[:4], [61, 64, 66, 69])
        self.assertEqual(samples[-1], 285)
        self.assertAlmostEqual(len(samples) / (225 / FPS), V.SAMPLE_FPS, delta=0.3)
        self.assertEqual(V.analysis_frames(1, 10, FPS), list(range(1, 11)))
        fast = V.analysis_frames(1, 240, 120.0)
        self.assertEqual(fast[:3], [1, 3, 5])
        self.assertEqual(fast[-1], 240)


class SheetTest(unittest.TestCase):
    def test_one_sheet_per_beat_range_with_every_beat_on_it(self):
        sheets = V.plan_sheets("take.mp4", FPS, 61, 285, SAMPLE_BEATS, (1920, 1080), 6)
        ranges = [tuple(s["range"]) for s in sheets]
        self.assertLessEqual(len(sheets), 6)
        self.assertEqual(ranges[0][0], 61)
        self.assertEqual(ranges[-1][1], 285)
        for (_, end), (start, _) in zip(ranges, ranges[1:]):
            self.assertEqual(start, end + 1)
        self.assertEqual(ranges[1][0], 130)  # Split at the middle hold.
        shown = {t["frame"]: t for s in sheets for t in s["tiles"]}
        for beat in SAMPLE_BEATS:
            self.assertIn(beat["frame"], shown)
            self.assertEqual(shown[beat["frame"]]["beat"], f"{beat['kind'].upper()} {beat['label']}")
        self.assertEqual(shown[94]["label"], "3.10 s  f94")
        self.assertEqual(shown[61]["label"], "2.00 s  f61")
        for sheet in sheets:
            self.assertLessEqual(len(sheet["tiles"]), V.TILES_PER_SHEET)
            width, height = sheet["size"]
            self.assertLessEqual((width, height), (V.SHEET_WIDTH, V.SHEET_MAX_HEIGHT))
            frames = [t["frame"] for t in sheet["tiles"]]
            self.assertEqual(frames, sorted(set(frames)))
            boxes = [t["box"] for t in sheet["tiles"]]
            for x, y, w, h in boxes:
                self.assertTrue(0 <= x and x + w <= width and V.HEADER <= y and y + h + V.LABEL <= height)
                self.assertAlmostEqual(w / h, 16 / 9, delta=0.02)
            for a in boxes:
                for b in boxes:
                    if a is not b:
                        self.assertTrue(a[0] + a[2] <= b[0] or b[0] + b[2] <= a[0] or a[1] + a[3] + V.LABEL <= b[1]
                                        or b[1] + b[3] + V.LABEL <= a[1], (a, b))
            a, b = sheet["range"]
            self.assertIn(f"{(a - 1) / FPS:.2f}-{(b - 1) / FPS:.2f} s", sheet["title"])

    def test_the_budget_caps_the_sheet_count(self):
        one = V.plan_sheets("take.mp4", FPS, 61, 285, SAMPLE_BEATS, (1920, 1080), 1)
        self.assertEqual([s["range"] for s in one], [[61, 285]])
        self.assertIn(160, [t["frame"] for t in one[0]["tiles"]])
        no_holds = V.plan_sheets("take.mp4", FPS, 1, 450, [], (1920, 1080), 6)
        self.assertEqual(len(no_holds), 4)  # 15 s split while a part is over 5 s.
        self.assertEqual(len(V.plan_sheets("take.mp4", FPS, 1, 450, [], (1920, 1080), 2)), 2)

    def test_portrait_clips_fit_the_sheet_height(self):
        for count in (1, 2, 7, 16):
            columns, width, height, total = V.sheet_grid(count, (1080, 1920))
            self.assertLessEqual(total, V.SHEET_MAX_HEIGHT)
            self.assertLessEqual(columns * width + (columns + 1) * V.GAP, V.SHEET_WIDTH)
            self.assertAlmostEqual(width / height, 1080 / 1920, delta=0.02)

    def test_the_worker_writes_sheets_as_jpeg(self):
        # Every request re-sends the sheets: six photographic PNGs (~1.3 MB each) broke ChatGPT uploads.
        sheet = V.plan_sheets("take.mp4", FPS, 61, 285, SAMPLE_BEATS, (1920, 1080), 6)[0]
        self.assertEqual(sheet["file"], "sheet_1.jpg")
        fakes = {name: mock.MagicMock() for name in ("blf", "imbuf", "numpy")}
        worker_py = Path(V.__file__).with_name("video_worker.py")
        with mock.patch.dict(sys.modules, fakes), mock.patch.object(sys, "path", list(sys.path)):
            spec = importlib.util.spec_from_file_location("video_worker", worker_py)
            worker = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(worker)
            worker.draw_sheet(dict(sheet, tiles=[]), Path("frames"), Path("clip") / sheet["file"])
        canvas = fakes["imbuf"].new.return_value
        fakes["imbuf"].write.assert_called_once_with(canvas, filepath=str(Path("clip") / "sheet_1.jpg"))
        self.assertEqual((canvas.file_type, canvas.quality), ("JPEG", V.SHEET_QUALITY))
        self.assertLessEqual(V.SHEET_QUALITY, 90)
        canvas.free.assert_called_once_with()

    def test_pick_tiles_keeps_beats_then_spreads(self):
        self.assertEqual(V.pick_tiles(list(range(10)), [7], 4), [0, 4, 7, 9])
        self.assertEqual(V.pick_tiles([1, 2], [5], 4), [1, 2, 5])


class SchemaTest(unittest.TestCase):
    def test_beats_json_round_trips_and_rejects_bad_shapes(self):
        frames = list(range(1, 36))
        motion = V.measure_motion(still_spike_hold(), 12)
        energy = motion["energy"]
        document = V.beats_document("take.mp4", FPS, 1, 35, frames, energy, V.find_beats(energy, frames, FPS),
                                    motion["shift"], (12, 12))
        checked = V.check_beats(json.loads(json.dumps(document)))
        self.assertEqual(set(checked), {"version", "clip", "fps", "in_frame", "out_frame", "in_time", "out_time",
                                        "about", "motion_curve", "beats"})
        self.assertEqual(checked["motion_curve"]["frames"][:3], [1, 4, 6])
        self.assertEqual(max(checked["motion_curve"]["values"]), 1.0)
        self.assertEqual(len(checked["motion_curve"]["image_shift"]), len(checked["motion_curve"]["frames"]))
        self.assertEqual(checked["motion_curve"]["image_shift"][0], [0.0, 0.0])
        for beat in checked["beats"]:
            self.assertTrue({"kind", "frame", "time", "motion", "label"} <= set(beat))
        for spoil in (lambda d: d["beats"][0].update(frame=99), lambda d: d["beats"][0].update(label=""),
                      lambda d: d["beats"].reverse(), lambda d: d.update(version=2),
                      lambda d: d["motion_curve"]["values"].append(0.5),
                      lambda d: d["motion_curve"]["image_shift"].pop()):
            broken = json.loads(json.dumps(document))
            spoil(broken)
            with self.assertRaises(ValueError):
                V.check_beats(broken)

    def test_camera_motion_is_relative_to_the_first_solved_frame(self):
        start = rz(90.0)
        cameras = [(61, matrix(start, [2.0, 3.0, 1.5])),
                   (62, matrix(start, [2.0, 4.0, 1.5])),                       # Moved along world +Y...
                   (63, matrix([[sum(start[i][k] * rz(10.0)[k][j] for k in range(3)) for j in range(3)]
                                for i in range(3)], [2.0, 3.0, 1.5]))]          # ...turned 10 degrees.
        document = V.camera_motion_document("take.mp4", FPS, 61, 63, 0.42, 24.5, 36.0, cameras)
        checked = V.check_camera_motion(json.loads(json.dumps(document)))
        first, moved, turned = checked["frames"]
        self.assertEqual((first["rotation_deg"], first["translation"]), ([0.0, 0.0, 0.0], [0.0, 0.0, 0.0]))
        self.assertEqual(moved["translation"], [1.0, 0.0, 0.0])  # ...is the camera's own +X.
        self.assertEqual([round(v, 3) for v in turned["rotation_deg"]], [0.0, 0.0, 10.0])
        self.assertAlmostEqual(moved["time"], 61 / FPS, places=3)
        self.assertEqual((checked["solve_error_px"], checked["focal_length_mm"]), (0.42, 24.5))
        self.assertEqual(checked["mode"], "full")
        tripod = V.camera_motion_document("take.mp4", FPS, 61, 63, 0.3, 35.0, 36.0, cameras, "tripod")
        self.assertEqual(V.check_camera_motion(tripod)["mode"], "tripod")
        with self.assertRaisesRegex(ValueError, "mode"):
            V.check_camera_motion(dict(tripod, mode="sideways"))
        broken = dict(checked, solve_error_px=2.5)
        with self.assertRaisesRegex(ValueError, "over the limit"):
            V.check_camera_motion(broken)

    def test_a_poor_solve_is_dropped_with_its_reason(self):
        self.assertEqual(V.accept_solve(True, 0.4, 220, 225), (True, ""))
        for args, words in (((True, 2.3, 225, 225), "2.30 px is over the 1 px limit"),
                            ((False, 0.0, 0, 225), "could not solve"),
                            ((True, 0.5, 100, 225), "only 100 of 225"),
                            ((True, float("nan"), 225, 225), "limit")):
            ok, reason = V.accept_solve(*args)
            self.assertFalse(ok)
            self.assertIn(words, reason)


def worker_output(folder: Path, camera: dict, sheets: int = 3, watch: str = "the crossbow and my hands") -> Path:
    """What video_worker.py leaves in a clip's folder, made up."""
    folder.mkdir(parents=True)
    spec = {"id": folder.name, "name": "take.mp4", "source": "/clips/take.mp4", "fps": FPS, "frames": 900,
            "size": [1920, 1080], "in_frame": 61, "out_frame": 285, "watch": watch, "solve": camera["asked"],
            "max_sheets": 4}
    (folder / "spec.json").write_text(json.dumps(spec))
    plan = V.plan_sheets("take.mp4", FPS, 61, 285, SAMPLE_BEATS, (1920, 1080), sheets)
    for n, sheet in enumerate(plan):
        (folder / sheet["file"]).write_bytes(f"sheet {n}".encode())
    beats = {"version": 1, "clip": "take.mp4", "fps": FPS, "in_frame": 61, "out_frame": 285, "in_time": 2.0,
             "out_time": 9.467, "about": V.BEATS_ABOUT, "motion_curve": {"frames": [61], "values": [0.0]},
             "beats": SAMPLE_BEATS}
    (folder / "beats.json").write_text(json.dumps(beats))
    if camera.get("solved"):
        (folder / "camera_motion.json").write_text(json.dumps(V.camera_motion_document(
            "take.mp4", FPS, 61, 62, 0.4, 26.0, 36.0,
            [(61, matrix(rz(0), [0, 0, 0])), (62, matrix(rz(1), [0, 0, 0]))])))
    (folder / "frames").mkdir()
    (folder / "frames" / V.frame_file(94)).write_bytes(b"frame 94")
    (folder / "result.json").write_text(json.dumps({"sheets": [{"file": s["file"], "range": s["range"],
                                                                "time": s["time"]} for s in plan],
                                                    "camera": camera, "beats": len(SAMPLE_BEATS)}))
    return folder


class ModelInputTest(Folder):
    def clip_folder(self, name="c1") -> Path:
        return conversations._folder(self.session["id"]) / "video" / name

    def test_sheets_join_the_message_with_the_clip_described(self):
        folder = worker_output(self.clip_folder(), {"asked": True, "solved": True, "mode": "full", "error_px": 0.4,
                                                    "frames": 225})
        self.session["attachments"] = [{"ref": f"ref{n}", "full": f"ref{n}", "name": f"photo{n}.png"} for n in range(2)]
        line = video_attach.finish(self.session, folder)
        self.assertEqual(line, "take.mp4 is ready: 3 sheets, 5 beats; camera solved, error 0.40 px.")
        sheets = self.session["attachments"][2:]
        self.assertEqual([s["name"] for s in sheets],
                         ["take.mp4 2.00-4.27s", "take.mp4 4.30-6.87s", "take.mp4 6.90-9.47s"])
        self.assertEqual(sheets[0]["video"]["range"], [61, 129])
        self.assertTrue(all(s["ref"].startswith(conversations.IMAGE_SCHEME) for s in sheets))

        content = V.user_content("Animate the shot like this.", self.session["attachments"])
        images = [p for p in content if p["type"] == "image_url"]
        self.assertEqual(len(images), 5)
        self.assertLessEqual(len(images), attachments.MAX_PER_MESSAGE)
        text = content[0]["text"]
        self.assertTrue(text.startswith("Animate the shot like this.\n\n<video_reference clip=\"take.mp4\">"))
        self.assertEqual(text.count("<video_reference"), 1)
        for words in ("/clips/take.mp4", "on this computer only", "clip frames 61-285", "2.00-9.47 s",
                      "the crossbow and my hands", f"{folder}/beats.json", f"{folder}/camera_motion.json",
                      "error 0.40 px", f"{folder}/frames/000061.jpg", "3.10 s f94 peak 0.70 \"wind-up\"",
                      "take.mp4 2.00-4.27s, take.mp4 4.30-6.87s", "clip_background", "frame=<clip frame>",
                      "Tiles skip frames", "count clip frames (f61 is the first used one), not scene frames"):
            self.assertIn(words, text)

    def test_a_dropped_solve_is_said_and_its_file_never_read(self):
        reason = "solve error 2.30 px is over the 1 px limit"
        folder = worker_output(self.clip_folder(), {"asked": True, "solved": False, "error_px": 2.3,
                                                    "frames": 225, "reason": reason}, watch="")
        line = video_attach.finish(self.session, folder)
        self.assertIn("camera not used: " + reason, line)
        text = V.user_content("", self.session["attachments"])[0]["text"]
        self.assertIn("Camera motion: not used, " + reason, text)
        self.assertNotIn("camera_motion.json", text)
        self.assertNotIn("What to watch", text)

    def test_a_failed_or_oversized_result_is_refused(self):
        folder = worker_output(self.clip_folder(), {"asked": False})
        (folder / "result.json").write_text(json.dumps({"error": "RuntimeError: 3 clip frames did not decode"}))
        with self.assertRaisesRegex(V.ClipError, "did not decode"):
            video_attach.finish(self.session, folder)
        folder = worker_output(self.clip_folder("c2"), {"asked": False})
        spec = json.loads((folder / "spec.json").read_text())
        (folder / "spec.json").write_text(json.dumps(dict(spec, max_sheets=2)))
        with self.assertRaisesRegex(V.ClipError, "no usable sheets"):
            video_attach.finish(self.session, folder)  # 3 sheets where 2 were reserved.
        self.assertEqual(self.session["attachments"], [])

    def test_clip_tools_are_offered_only_while_a_clip_is_attached(self):
        plain = TOOL_SCHEMAS()
        names = [s["function"]["name"] for s in plain]
        self.assertNotIn("clip_background", names)
        compare = next(s for s in plain if s["function"]["name"] == "compare_with_reference")
        self.assertNotIn("frame", compare["function"]["parameters"]["properties"])
        folder = worker_output(self.clip_folder(), {"asked": False})
        video_attach.finish(self.session, folder)
        self.session["references"] = [dict(a, pinned=True) for a in self.session["attachments"]]
        offered = TOOL_SCHEMAS()
        self.assertEqual([s["function"]["name"] for s in offered], names + ["clip_background"])
        for name in ("compare_with_reference", "look_at_reference"):
            schema = next(s for s in offered if s["function"]["name"] == name)
            self.assertEqual(schema["function"]["parameters"]["properties"]["frame"]["type"], "integer")
        self.assertNotIn("frame", next(s for s in tools.SCHEMAS if s["function"]["name"] == "compare_with_reference")
                         ["function"]["parameters"]["properties"], "the shared schema was changed")
        self.assertIn("clip_background", tools.CHANGES_SCENE)
        with mock.patch.object(video_attach, "clip_background", return_value=tools.ToolResult("placed")) as placed:
            video_attach.DISPATCH["clip_background"] = video_attach.clip_background
            self.addCleanup(video_attach.DISPATCH.__setitem__, "clip_background", video_attach.clip_background)
            self.assertEqual(tools.execute("clip_background", json.dumps({"name": "take.mp4"})).text, "placed")
        placed.assert_called_once_with(name="take.mp4")

    def test_the_chatgpt_plan_route_offers_the_tools_its_clip_note_names(self):
        """F0297: the workspace agent (the ChatGPT plan's route) kept only workspace.LIVE_TOOLS, so the clip note
        told it to use clip_background and frame= on tools it did not have."""
        from loopcut import workspace
        with mock.patch.object(agent, "_tool_schemas", TOOL_SCHEMAS):
            plain = [s["function"]["name"] for s in agent._workspace_schemas()]
            self.assertFalse(set(video_attach.CLIP_TOOLS) & set(plain), "no clip: the workspace list is unchanged")
            folder = worker_output(self.clip_folder(), {"asked": False})
            video_attach.finish(self.session, folder)
            self.session["references"] = [dict(a, pinned=True) for a in self.session["attachments"]]
            offered = {s["function"]["name"]: s["function"] for s in agent._workspace_schemas()}
        note = V.model_text(self.session["references"][0]["video"], ["take.mp4 2.00-4.27s"])
        for name in video_attach.CLIP_TOOLS:
            self.assertIn(name, note)
            self.assertIn(name, offered)
        for name in ("compare_with_reference", "look_at_reference"):
            self.assertIn("frame", offered[name]["parameters"]["properties"])
        self.assertLessEqual(set(workspace.LIVE_TOOLS) | set(workspace.TOOLS), set(offered))

    def test_send_describes_the_clip_keeps_its_record_and_waits_while_preparing(self):
        folder = worker_output(self.clip_folder(), {"asked": False})
        video_attach.finish(self.session, folder)
        with mock.patch.object(agent, "_configure"), mock.patch.object(agent, "_launch"), \
                mock.patch.object(agent, "_project_roots", return_value=[]), \
                mock.patch.object(agent, "_scene_context", return_value="<scene/>"), \
                mock.patch.object(agent, "_scene_snapshot", return_value={}), \
                mock.patch.object(agent, "_addons_line", return_value=""), \
                mock.patch.object(agent.checkpoints, "begin_turn"), mock.patch.object(agent.conversations, "save"):
            video_attach._running["x"] = {"session": self.session, "name": "other.mov"}
            self.addCleanup(video_attach._running.clear)
            self.assertFalse(agent.send("go"))
            self.assertIn("Still preparing other.mov", self.session["items"][-1]["text"])
            video_attach._running.clear()
            self.assertTrue(agent.send("go"))
        message = self.session["messages"][-1]
        self.assertTrue(message[conversations.ATTACHED])
        self.assertIn("<video_reference clip=\"take.mp4\">", message["content"][0]["text"])
        self.assertEqual(sum(p["type"] == "image_url" for p in message["content"]), 3)
        self.assertEqual(self.session["attachments"], [])
        self.assertTrue(all(r["video"]["id"] == "c1" for r in self.session["references"]))


    def test_the_reference_card_describes_stills_not_sheets(self):
        video_attach.finish(self.session, worker_output(self.clip_folder(), {"asked": False}))
        photo = Path(self.tmp.name) / "photo.png"
        photo.write_bytes(b"a photo")
        sheets = list(self.session["attachments"])
        cfg = SimpleNamespace(base_url="http://model", api_key="k", model="m", max_output_tokens=100,
                              call_max_seconds=10)
        sent = []

        def stream_chat(**kwargs):
            sent.append(kwargs["messages"][1]["content"])
            return SimpleNamespace(text="a red mug")
        with mock.patch.object(agent.llm, "stream_chat", side_effect=stream_chat), \
                mock.patch.object(agent, "_correlation_headers", return_value={}):
            self.session["references"] = [dict(a, pinned=True) for a in sheets]
            only_sheets = {"role": "user", conversations.ATTACHED: True,
                           "content": V.user_content("go", sheets)}
            self.assertEqual(agent._reference_card(self.session, cfg, only_sheets, lambda: False), [])
            self.assertEqual(sent, [])
            self.assertTrue(only_sheets[agent.context.REFERENCE_CARD])
            ref = conversations.store_image(self.session, photo)
            still = {"ref": ref, "full": ref, "name": "photo.png"}
            self.session["references"].append(dict(still, pinned=True))
            mixed = {"role": "user", conversations.ATTACHED: True, "content": V.user_content("go", [still] + sheets)}
            self.assertEqual(agent._reference_card(self.session, cfg, mixed, lambda: False), ["photo.png"])
        self.assertEqual(sum(p.get("type") == "image_url" for p in sent[0]), 1)
        self.assertIn("<reference_card images=\"photo.png\">", mixed["content"][0]["text"])
        self.assertEqual(sum(p.get("type") == "image_url" for p in mixed["content"]), 4)


class ClipToolsTest(Folder):
    def setUp(self):
        super().setUp()
        folder = worker_output(conversations._folder(self.session["id"]) / "video" / "c1", {"asked": False})
        video_attach.finish(self.session, folder)
        self.session["references"] = [dict(a, pinned=True) for a in self.session["attachments"]]
        self.session["attachments"] = []
        self.found = self.session["references"][0]

    def fake_blender(self, clips=()):
        class Backgrounds(list):
            def new(self):
                self.append(SimpleNamespace(source="IMAGE", clip=None, alpha=1.0, display_depth="BACK",
                                            frame_method="STRETCH", use_camera_clip=True, show_background_image=False))
                return self[-1]

        class Clips(list):
            def load(self, path, check_existing=False):
                self.append(SimpleNamespace(filepath=path, frame_start=1))
                return self[-1]

        camera = SimpleNamespace(name="Camera", type="CAMERA",
                                 data=SimpleNamespace(background_images=Backgrounds(), show_background_images=False))
        scene = SimpleNamespace(camera=camera, objects={"Camera": camera}, frame_start=1,
                                render=SimpleNamespace(fps=24, fps_base=1.0, resolution_x=1080, resolution_y=1920))
        return SimpleNamespace(context=SimpleNamespace(scene=scene), data=SimpleNamespace(movieclips=Clips(clips)),
                               path=SimpleNamespace(abspath=lambda p: p))

    def test_clip_background_places_the_clip_at_its_frame_rate(self):
        blender = self.fake_blender()
        with mock.patch.object(video_attach, "bpy", blender), mock.patch.object(Path, "is_file", return_value=True):
            result = video_attach.clip_background("take.mp4", start_frame=10)
            camera, scene = blender.context.scene.camera, blender.context.scene
            background = camera.data.background_images[0]
            clip = blender.data.movieclips[0]
            self.assertEqual((background.source, background.clip, background.alpha, background.display_depth,
                              background.frame_method), ("MOVIE_CLIP", clip, 0.5, "FRONT", "FIT"))
            self.assertTrue(camera.data.show_background_images and not background.use_camera_clip)
            self.assertEqual((scene.render.fps, scene.render.fps_base), (30, 1.0))
            self.assertEqual(clip.frame_start, 10 - 61 + 1)
            self.assertIn("clip frames 61-285 (2.00-9.47 s) show at scene frames 10-234", result.text)
            self.assertIn("24.000 -> 30.000 fps", result.text)
            self.assertIn("render is 1080x1920 and the clip 1920x1080", result.text)
            self.assertEqual(video_attach.scene_frame(self.found["video"], 94), 43)
            again = video_attach.clip_background("take.mp4", opacity=2, depth="back")
            self.assertEqual(len(camera.data.background_images), 1)
            self.assertEqual((background.alpha, background.display_depth), (1.0, "BACK"))
            self.assertIn("scene frames 1-225", again.text)
            gone = video_attach.clip_background("take.mp4", remove=True)
            self.assertEqual(len(camera.data.background_images), 0)
            self.assertIn("Removed 1 background", gone.text)

    def test_clip_frames_for_compare_and_look(self):
        blender = self.fake_blender()
        with mock.patch.object(video_attach, "bpy", blender):
            path, at, what = video_attach.frame_for(self.found, 94)
            self.assertEqual(path.read_bytes(), b"frame 94")
            self.assertEqual(at, 1 + 94 - 61)  # No background yet: the scene's start shows the first used frame.
            self.assertEqual(what, "take.mp4 clip frame 94 (3.10 s)")
            for frame, words in ((300, "outside the part"), (95, "no longer on disk"), (9.5, "clip frame number"),
                                 (True, "clip frame number")):
                with self.assertRaisesRegex(tools.ToolError, words):
                    video_attach.frame_for(self.found, frame)
            with self.assertRaisesRegex(tools.ToolError, "is an image"):
                video_attach.frame_for({"name": "photo.png"}, 94)
            elsewhere = dict(self.found, video=dict(self.found["video"], folder="/tmp/elsewhere/c1"))
            with self.assertRaisesRegex(tools.ToolError, "not in this conversation"):
                video_attach.frame_for(elsewhere, 94)
        with mock.patch.object(tools, "_pixels", side_effect=lambda p, h=None: p), \
                mock.patch.object(tools, "crop", return_value=("pixels", "the whole image")), \
                mock.patch.object(tools, "_save_pixels", return_value=(640, 360)), \
                mock.patch.object(video_attach, "bpy", blender):
            with mock.patch.object(tools, "_pixels", side_effect=lambda p, h=None: SimpleNamespace(shape=(360, 640))):
                result = tools.look_at_reference("take.mp4", frame=94)
        self.assertTrue(result.text.startswith("Image attached: take.mp4 clip frame 94 (3.10 s), the whole image"))


if __name__ == "__main__":
    unittest.main()
