"""The chat's audio card for a generated sound: layout, Play / Stop through a mocked aud, one sound at a time,
a missing file, persistence and the apply step that adds it. No Blender."""

import ast
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ADDONS = Path(__file__).resolve().parents[2] / "scripts" / "addons_core"
sys.path.insert(0, str(ADDONS))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))

from loopcut import cloud_jobs, conversations, mainthread, playback, state  # noqa: E402
from loopcut.ui import layout, theme as T  # noqa: E402

MEASURE = lambda font, size, text: len(text) * size * 0.6  # noqa: E731


class AudError(Exception):
    """aud.error: what aud raises for an unreadable file or a device that cannot open."""


class FakeHandle:
    def __init__(self, path):
        self.path, self.status, self.position = path, True, 0.0  # Blender 5.2: status is a bool.

    def stop(self):
        was, self.status = self.status, False
        return was


class FakeDevice:
    def __init__(self):
        self.played = []

    def play(self, sound):
        if sound.path.endswith("broken.wav"):
            raise AudError("The file couldn't be read with any installed file reader.")
        handle = FakeHandle(sound.path)
        self.played.append(handle)
        return handle


class FakeSound:
    def __init__(self, path):
        self.path = path


def fake_aud():
    aud = types.ModuleType("aud")
    aud.Device, aud.Sound, aud.error = Mock(side_effect=FakeDevice), FakeSound, AudError
    aud.STATUS_INVALID, aud.STATUS_PLAYING, aud.STATUS_PAUSED, aud.STATUS_STOPPED = 0, 1, 2, 3
    return aud


class AudioTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.wav, self.other = self.folder / "voice.wav", self.folder / "door.wav"
        self.wav.write_bytes(b"RIFF")
        self.other.write_bytes(b"RIFF")
        self.aud = fake_aud()
        self.enterContext(patch.dict(sys.modules, {"aud": self.aud}))
        self.enterContext(patch.multiple(playback, _device=None, _now=None))

    def card(self, path=None, seconds=3.2, peaks=(0.1, 0.5, 1.0, 0.5)):
        return state.item_audio("Loopcut voice", str(path or self.wav), seconds, list(peaks))

    def frame(self, **ui):
        return layout.Frame(420, 700, 1.0, MEASURE, ui=ui)


class AudioCardLayoutTest(AudioTest):
    def test_audio_result_draws_a_card_with_name_length_waveform_and_play(self):
        session = state.new_session()
        session["items"] = [state.item_tool("loopcut_cloud.voice_line", "Voice line", ""), self.card()]
        session["items"][0]["status"] = "done"
        display = layout.build(session, 420, 700, 1.0, MEASURE)
        texts = {p["id"]: p.get("text") for p in display["prims"]}
        self.assertIn("item1.audio.card", texts)
        self.assertEqual(texts["item1.audio.name"], "Loopcut voice")
        self.assertEqual(texts["item1.audio.length"], "0:03")
        self.assertEqual(texts["item1.audio.play.label"], "▶ Play")
        self.assertEqual(sum(p["id"].startswith("item1.audio.wave.b") for p in display["prims"]), 4)
        play = next(h for h in display["hits"] if h["id"] == "item1.audio.play")
        self.assertEqual(play["action"], ("audio_play", str(self.wav)))
        card = next(p for p in display["prims"] if p["id"] == "item1.audio.card")
        tool = next(p for p in display["prims"] if p["id"] == "item0.tool.card")
        self.assertGreater(card["y"], tool["y"], "the card sits under the call that made the sound")

    def test_playing_card_shows_stop_position_and_fills_the_waveform(self):
        f = self.frame(audio_playing={"path": str(self.wav), "position": 1.6})
        layout._item_notice(f, 0, self.card(), 0, 0, 400)
        self.assertEqual([h["action"] for h in f.hits], [("audio_stop", str(self.wav))])
        texts = {p["id"]: p.get("text") for p in f.prims}
        self.assertEqual(texts["item0.audio.length"], "0:02 / 0:03")
        bars = [p["color"] for p in f.prims if p["id"].startswith("item0.audio.wave.b")]
        self.assertEqual(bars, [T.ACCENT, T.ACCENT, T.TEXT_FAINT, T.TEXT_FAINT])

    def test_another_cards_sound_leaves_this_one_at_play(self):
        f = self.frame(audio_playing={"path": str(self.other), "position": 1.0})
        layout._item_notice(f, 0, self.card(), 0, 0, 400)
        self.assertEqual([h["action"] for h in f.hits], [("audio_play", str(self.wav))])

    def test_missing_file_disables_play_with_a_note(self):
        f = self.frame(missing_paths={str(self.wav)}, audio_playing={"path": str(self.wav), "position": 1.0})
        layout._item_notice(f, 0, self.card(), 0, 0, 400)
        self.assertEqual(f.hits, [])
        texts = {p["id"]: p for p in f.prims if p["t"] == "text"}
        self.assertEqual(texts["item0.audio.play.label"]["color"], T.TEXT_FAINT)
        self.assertEqual(texts["item0.audio.missing.l0"]["text"], "The sound file is no longer on disk.")

    def test_no_peaks_draws_a_progress_bar_and_narrow_panels_stay_inside(self):
        f = self.frame(audio_playing={"path": str(self.wav), "position": 0.8})
        height = layout._item_notice(f, 0, self.card(peaks=()), 10, 0, 160)
        played = next(p for p in f.prims if p["id"] == "item0.audio.wave.played")
        track = next(p for p in f.prims if p["id"] == "item0.audio.wave.track")
        self.assertAlmostEqual(played["w"] / track["w"], 0.25, delta=0.05)
        for prim in f.prims:
            self.assertGreaterEqual(prim["x"], 10)
            self.assertLessEqual(prim["y"] + prim["h"], height)
        for width in (160, 400):
            f = self.frame(audio_playing={"path": str(self.wav), "position": 0.8})
            layout._item_notice(f, 0, self.card(), 10, 0, width)
            name = next(p for p in f.prims if p["id"] == "item0.audio.name")
            length = next(p for p in f.prims if p["id"] == "item0.audio.length")
            right = name["x"] + MEASURE("ui", name["size"], name["text"])
            self.assertLessEqual(right, 10 + width - T.CARD_PAD)
            self.assertTrue(right <= length["x"] or name["y"] >= length["y"] + length["h"], "no overlap")
            self.assertEqual(name["text"], "Loopcut voice", "a narrow panel moves the name, not shortens it")

    def test_a_plain_notice_is_unchanged(self):
        f = self.frame()
        layout._item_notice(f, 0, state.item_notice("Saved."), 0, 0, 400)
        self.assertFalse(any(".audio." in p["id"] for p in f.prims))


class PlaybackTest(AudioTest):
    def test_play_hands_the_file_to_aud_and_reports_position(self):
        self.assertEqual(playback.play(str(self.wav)), "")
        device = playback._device
        self.assertEqual([h.path for h in device.played], [str(self.wav)])
        device.played[0].position = 1.25
        self.assertEqual(playback.playing(), {"path": str(self.wav), "position": 1.25})
        device.played[0].status = False  # It ended by itself.
        self.assertIsNone(playback.playing())

    def test_one_sound_at_a_time_and_stop(self):
        playback.play(str(self.wav))
        playback.play(str(self.other))
        first, second = playback._device.played
        self.assertFalse(first.status, "starting another sound stops the first")
        self.assertTrue(second.status)
        self.assertEqual(playback.playing()["path"], str(self.other))
        playback.stop()
        self.assertFalse(second.status)
        self.assertIsNone(playback.playing())
        playback.stop()  # Nothing playing: nothing to do.
        self.assertEqual(self.aud.Device.call_count, 1, "one device for every Play")

    def test_missing_or_unreadable_file_says_why_and_plays_nothing(self):
        playback.play(str(self.wav))
        self.wav.unlink()
        error = playback.play(str(self.wav))
        self.assertIn("voice.wav", error)
        self.assertIn("no longer at", error)
        self.assertIsNone(playback.playing(), "the sound before it was stopped")
        broken = self.folder / "broken.wav"
        broken.write_bytes(b"not audio")
        self.assertIn("couldn't be read", playback.play(str(broken)))
        self.assertIsNone(playback.playing())

    def test_the_panel_redraws_while_a_sound_plays(self):
        with patch.object(state, "session", return_value={"busy": False}), patch.object(state, "ui", {}):
            self.assertIsNone(mainthread._animating())
            playback.play(str(self.wav))
            self.assertEqual(mainthread._animating(), mainthread.ANIMATE_EVERY)
            playback.stop()
            self.assertIsNone(mainthread._animating())

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy ships with Blender, not with every Python")
    def test_peaks_are_the_loudest_sample_per_slice(self):
        import numpy
        samples = numpy.array([[0.0, 0.1], [0.2, -0.4], [0.05, 0.0], [-0.8, 0.0]], dtype=numpy.float32)
        self.aud.Sound = lambda path: SimpleNamespace(data=lambda: samples)
        self.assertEqual(playback.peaks("x.wav", 2), [0.5, 1.0])

    def test_peaks_of_an_unreadable_file_are_empty(self):
        def unreadable(path):
            raise AudError("The file couldn't be read with any installed file reader.")
        self.aud.Sound = lambda path: SimpleNamespace(data=lambda: unreadable(path))
        self.assertEqual(playback.peaks("x.wav"), [])


class HostAudioActionTest(AudioTest):
    def setUp(self):
        super().setUp()
        self.session = state.new_session()
        namespace = {"__package__": "loopcut.ui", "Path": Path, "_runtime": {}, "PATH_TTL": 3.0, "layout": layout,
                     "time": SimpleNamespace(monotonic=lambda: 0.0),
                     "state": SimpleNamespace(session=lambda: self.session, item_error=state.item_error, ui={})}
        source = ADDONS / "loopcut/ui/host.py"
        names = {"_do_action", "_image_paths", "_missing_paths", "_refresh_missing_paths"}
        nodes = [n for n in ast.parse(source.read_text()).body if getattr(n, "name", None) in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
        self.host = namespace

    def click(self, f, id):
        self.host["_do_action"](self.session, next(h["action"] for h in f.hits if h["id"] == id))

    def test_play_hit_plays_the_card_and_stop_hit_stops_it(self):
        self.session["items"] = [self.card(), self.card(self.other)]
        f = self.frame()
        layout._item_notice(f, 0, self.session["items"][0], 0, 0, 400)
        with patch.object(playback, "play", wraps=playback.play) as play:
            self.click(f, "item0.audio.play")
        play.assert_called_once_with(str(self.wav))
        self.assertEqual(playback.playing()["path"], str(self.wav))
        f = self.frame(audio_playing=playback.playing())
        layout._item_notice(f, 1, self.session["items"][1], 0, 0, 400)
        self.click(f, "item1.audio.play")
        self.assertEqual(playback.playing()["path"], str(self.other), "the second card's sound replaced the first")
        self.assertFalse(playback._device.played[0].status)
        f = self.frame(audio_playing=playback.playing())
        layout._item_notice(f, 1, self.session["items"][1], 0, 0, 400)
        self.click(f, "item1.audio.stop")
        self.assertIsNone(playback.playing())
        self.assertEqual(len(self.session["items"]), 2, "no error card")

    def test_a_file_deleted_since_the_last_draw_errors_and_disables_the_card(self):
        self.session["items"] = [self.card()]
        f = self.frame()
        layout._item_notice(f, 0, self.session["items"][0], 0, 0, 400)
        self.wav.unlink()
        self.click(f, "item0.audio.play")
        self.assertEqual(self.session["items"][-1]["kind"], "error")
        self.assertIn("no longer at", self.session["items"][-1]["text"])
        self.assertEqual(self.host["_missing_paths"](self.session), frozenset({str(self.wav)}))


class Strip(dict):
    def __init__(self, name, path, channel, frame_start, frames):
        super().__init__()
        self.name, self.filepath, self.channel = name, path, channel
        self.frame_final_start, self.frame_final_duration = frame_start, frames
        self.frame_final_end = frame_start + frames
        self.sound = SimpleNamespace(pack=Mock())


class Strips(list):
    frames = 60

    def new_sound(self, name, path, channel, frame_start):
        self.append(Strip(name, path, channel, frame_start, self.frames))
        return self[-1]


class AudioApplyTest(AudioTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"LOOPCUT_DATA_DIR": str(self.folder / "data")}))
        self.enterContext(patch.dict(cloud_jobs._registry, clear=True))
        self.strips = Strips()
        editor = SimpleNamespace(strips=self.strips, strips_all=self.strips)
        self.scene = SimpleNamespace(sequence_editor_create=lambda: editor, frame_current=12,
                                     render=SimpleNamespace(fps=24, fps_base=1.0))
        self.session = state.new_session()
        self.session["items"].append(state.item_tool("loopcut_cloud.voice_line", "Voice line", ""))

    def job(self, tool="voice_line", inputs=None, path=None):
        path = path or self.wav
        data = path.read_bytes()
        output = {"name": "audio", "type": "audio", "mime": "audio/wav", "sha256": hashlib.sha256(data).hexdigest(),
                  "size": len(data), "path": str(path)}
        job = {"id": "byok_voice", "tool": tool, "status": "succeeded", "error": None,
               "progress": {"pct": 100, "message": "Done"}, "credits": {"reserved": 0, "charged": 0, "refunded": 0},
               "outputs": [output], "provenance": {"job_id": "byok_voice", "tool": tool, "vendor": "gemini",
                                                   "model": "tts", "kind": "gen", "created_at": "2026-10-17T10:00:00Z",
                                                   "backend": "byok"}}
        if inputs is not None:
            job["input"] = inputs
        return job

    def apply(self, job):
        fake = SimpleNamespace(context=SimpleNamespace(scene=self.scene), data=SimpleNamespace())
        with patch.dict(sys.modules, {"bpy": fake}), patch.object(state, "session", return_value=self.session), \
                patch.object(playback, "peaks", return_value=[0.3, 1.0]) as peaks, \
                patch.object(conversations, "save") as save:
            summary = cloud_jobs.apply_result({"apply": "add_audio_strip"}, job)
        save.assert_called_once_with(self.session)
        return summary, peaks

    def test_apply_keeps_the_strip_packs_it_and_adds_a_card_under_the_call(self):
        summary, peaks = self.apply(self.job(inputs={"text": "Hello there", "voice": "Puck"}))
        strip = self.strips[0]
        self.assertEqual((strip.filepath, strip.frame_final_start), (str(self.wav), 12))
        strip.sound.pack.assert_called_once_with()  # Durable: the download is in a temp folder.
        self.assertEqual(json.loads(strip["loopcut_provenance"])["job_id"], "byok_voice")
        card = self.session["items"][-1]
        self.assertEqual(card["kind"], "notice")
        kept = Path(card["audio"]["path"])
        self.assertEqual(kept.parent, Path(os.environ["LOOPCUT_DATA_DIR"]) / "conversations" / self.session["id"] / "audio")
        self.assertEqual(kept.read_bytes(), self.wav.read_bytes())
        self.wav.unlink()
        self.assertTrue(kept.is_file(), "the card plays its own copy once the download is gone")
        self.assertEqual(card["audio"], {"name": "VO • Hello there", "path": str(kept), "seconds": 2.5,
                                         "peaks": [0.3, 1.0]})
        peaks.assert_called_once_with(str(kept))

    def test_strips_are_named_from_the_line_or_prompt_and_unique_in_the_scene(self):
        line = "Stand back,   everyone: the reactor is about to blow and nobody can stop it now"
        self.apply(self.job(inputs={"text": line}))
        self.apply(self.job(inputs={"text": line}))
        self.apply(self.job("sfx", {"prompt": "a heavy steel door slams shut in a long concrete corridor"}))
        self.apply(self.job("sfx", {"prompt": "bell"}))
        self.apply(self.job("other_audio", {"prompt": "x"}))
        names = [strip.name for strip in self.strips]
        self.assertEqual(names[0], "VO • Stand back, everyone: the reactor…")
        self.assertEqual(names[1], names[0] + ".001")
        self.assertEqual(names[2], "SFX • a heavy steel door slams shut in…")
        self.assertEqual(names[3:], ["SFX • bell", "audio"])
        for name in names:
            self.assertLessEqual(len(name.encode("utf-8")), 63, "Blender keeps 63 bytes of a strip name")
        self.assertEqual([c["audio"]["name"] for c in self.session["items"][1:]], names)

    def test_a_managed_job_is_named_from_the_input_kept_at_submit(self):
        job = self.job(inputs=None)
        cloud_jobs._registry[job["id"]] = {"job": job, "input": {"text": "Welcome aboard"}}
        self.apply(job)
        self.assertEqual(self.strips[0].name, "VO • Welcome aboard")
        self.assertEqual(cloud_jobs._first_words("ありがとうございます、またね"), "ありがとうございます、…")
        self.assertEqual(cloud_jobs._first_words("Supercalifragilisticexpialidociouslyextraordinary"),
                         "Supercalifragilisticexpialidociou…", "one long word is cut inside it")

    def test_result_tells_the_model_where_each_strip_sits(self):
        self.strips.append(Strip("music", "/m.wav", 2, 1, 100))
        self.scene.render = SimpleNamespace(fps=30000, fps_base=1001.0)
        Strips.frames = 75
        self.addCleanup(setattr, Strips, "frames", 60)
        summary, _ = self.apply(self.job(inputs={"text": "Go"}))
        self.assertIn('Added sound strips at the current frame: "VO • Go" (channel 3, frames 12–87 at 29.97 fps, '
                      'end exclusive, 2.50 s).', summary)
        self.assertEqual(self.session["items"][-1]["audio"]["seconds"], 75 / (30000 / 1001))


class AudioPersistenceTest(AudioTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"LOOPCUT_DATA_DIR": str(self.folder / "data")}))

    def test_card_survives_a_reload_and_malformed_cards_are_refused(self):
        session = state.new_session()
        kept = conversations.store_audio(session, self.wav)
        session["items"] = [state.item_user("voice it"), self.card(kept)]
        conversations.save(session)
        loaded = conversations.load(session["id"])
        self.assertEqual(loaded["items"][1]["audio"], session["items"][1]["audio"])
        for bad in ({"seconds": float("nan")}, {"seconds": "3"}, {"path": ""}, {"path": None},
                    {"path": str(self.wav)}, {"path": "/etc/" + "a" * 64 + ".wav"},
                    {"path": f"/x/audio/../{'a' * 64}.wav"}, {"path": str(Path(kept).with_suffix(".py"))},
                    {"peaks": [2.0]}, {"peaks": ["x"]}, {"peaks": [0.5] * 300}):
            with self.subTest(bad=bad):
                session["items"][1]["audio"] = {**self.card(kept)["audio"], **bad}
                conversations.save(session)
                with self.assertRaises(conversations.ConversationError):
                    conversations.load(session["id"])
        session["items"][1] = {**state.item_tool("x", "x", ""), "audio": self.card(kept)["audio"]}
        conversations.save(session)
        with self.assertRaises(conversations.ConversationError):
            conversations.load(session["id"])

    def test_store_audio_keeps_one_copy_per_sound(self):
        session = state.new_session()
        first, again = conversations.store_audio(session, self.wav), conversations.store_audio(session, self.wav)
        self.assertEqual(first, again)
        odd = self.folder / "clip.EXE"
        odd.write_bytes(b"x")
        self.assertTrue(conversations.store_audio(session, odd).endswith(".bin"))

    def test_older_add_ons_read_the_card_as_a_notice(self):
        card = self.card()
        self.assertEqual({k: card[k] for k in ("kind", "text", "action_label", "checkpoint", "url")},
                         {"kind": "notice", "text": "Sound strip added: Loopcut voice.", "action_label": "",
                          "checkpoint": "", "url": ""})


class SubmitInputTest(unittest.TestCase):
    def test_byok_jobs_carry_their_input(self):
        from loopcut.byok import runner
        with tempfile.TemporaryDirectory() as tmp:
            job = runner.run("voice_line", {"text": "Hi"}, {}, Path(tmp), no_generation=True, private_mode=False)
        self.assertEqual(job["input"], {"text": "Hi"})

    def test_managed_registry_keeps_the_submitted_input(self):
        job = {"id": "job_x", "status": "queued"}
        with patch.dict(cloud_jobs._registry, clear=True), patch.object(cloud_jobs, "_redraw"):
            cloud_jobs._remember(job, "Voice line", None, {"text": "Hi"})
            self.assertEqual(cloud_jobs._tool_input(job), {"text": "Hi"})



if __name__ == "__main__":
    unittest.main()
