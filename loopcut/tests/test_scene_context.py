"""The pure half of scene_context: finding @mentions and completing them while typing, and the
formatters for the compact camera/light/keyframe facts (they only duck-type against bpy attribute
access, so they are tested against fakes here; snapshot() and for_message() are covered by
harness/tools_check.py, which needs a real scene)."""

import math
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import scene_context as sc  # noqa: E402

NAMES = [("Table", "object"), ("Table Leg.001", "object"), ("Chair", "object"), ("Oak", "material"),
         ("Furniture", "collection"), ("Stable", "object")]


def obj(name="Cam", type="CAMERA", data=None, constraints=(), animation_data=None):
    return types.SimpleNamespace(name=name, type=type, data=data, constraints=list(constraints),
                                 animation_data=animation_data)


def constraint(kind="TRACK_TO", target=None, mute=False):
    return types.SimpleNamespace(type=kind, target=target, mute=mute)


def camera_data(lens=50.0, sensor_fit="AUTO", shift_x=0.0, shift_y=0.0, type="PERSP", ortho_scale=6.0):
    return types.SimpleNamespace(lens=lens, sensor_fit=sensor_fit, shift_x=shift_x, shift_y=shift_y,
                                 type=type, ortho_scale=ortho_scale)


def light_data(type="POINT", color=(1.0, 1.0, 1.0), energy=1000.0, spot_size=math.radians(45)):
    return types.SimpleNamespace(type=type, color=color, energy=energy, spot_size=spot_size)


def fcurve(data_path, frames):
    points = [types.SimpleNamespace(co=(f, 0.0)) for f in frames]
    return types.SimpleNamespace(data_path=data_path, keyframe_points=points)


def legacy_action(*fcurves):
    """An action with plain .fcurves, no .layers: the pre-4.4 shape _fcurves falls back to."""
    return types.SimpleNamespace(fcurves=list(fcurves))


def animated(*fcurves):
    return types.SimpleNamespace(action=legacy_action(*fcurves), action_slot=None)


class MentionsTest(unittest.TestCase):
    def test_parse_bare_quoted_and_repeated(self):
        text = 'put @Chair next to @"Table Leg.001", then copy @Chair. Mail me at a@b.c'
        self.assertEqual(sc.parse_mentions(text), ["Chair", "Table Leg.001"])

    def test_prefix_is_only_offered_while_typing_a_mention(self):
        self.assertEqual(sc.mention_prefix("move @Ta", 8), "Ta")
        self.assertEqual(sc.mention_prefix("move @", 6), "")
        self.assertEqual(sc.mention_prefix('move @"Table L', 14), "Table L")
        self.assertIsNone(sc.mention_prefix("move @Table there", 17), "the mention ended at the space")
        self.assertIsNone(sc.mention_prefix("mail a@b", 8), "an @ inside a word is not a mention")
        self.assertIsNone(sc.mention_prefix('see @"Table Leg" ', 17))
        self.assertIsNone(sc.mention_prefix("no mention", 5))

    def test_candidates_put_prefix_matches_first(self):
        self.assertEqual([n for n, _ in sc.candidates_from(NAMES, "ta")], ["Table", "Table Leg.001", "Stable"])
        self.assertEqual(sc.candidates_from(NAMES, "oak"), [("Oak", "material")])
        self.assertEqual(len(sc.candidates_from(NAMES, "", limit=4)), 4)

    def test_names_with_spaces_are_quoted_so_they_parse_back(self):
        for name in ("Chair", "Table Leg.001"):
            self.assertEqual(sc.parse_mentions("x " + sc.mention_text(name) + " y"), [name])


class SceneFactsTest(unittest.TestCase):
    """Camera lens/sensor fit/shift/aim, light colour/energy/spot size, and keyed channels: what
    a timing, colour or composition follow-up needs once the step that set them up has folded."""

    def test_camera_line_has_lens_fit_shift_and_no_track_target(self):
        cam = obj(data=camera_data(lens=35.0, sensor_fit="VERTICAL", shift_x=0.02, shift_y=-0.01))
        self.assertEqual(sc._camera_line(cam), "Cam: lens 35mm, sensor_fit VERTICAL, shift 0.02/-0.01")

    def test_camera_line_reports_the_track_to_target(self):
        target = types.SimpleNamespace(name="Hero")
        cam = obj(data=camera_data(), constraints=[constraint("TRACK_TO", target)])
        self.assertEqual(sc._camera_line(cam), "Cam: lens 50mm, sensor_fit AUTO, shift 0/0, tracks Hero")

    def test_camera_line_reports_ortho_scale_not_lens(self):
        cam = obj(data=camera_data(type="ORTHO", ortho_scale=8.5))
        self.assertIn("ortho scale 8.5", sc._camera_line(cam))
        self.assertNotIn("lens", sc._camera_line(cam))

    def test_track_target_ignores_muted_constraints_and_ones_without_a_target(self):
        cam = obj(constraints=[constraint("TRACK_TO", None), constraint("TRACK_TO", types.SimpleNamespace(name="X"), mute=True)])
        self.assertIsNone(sc._aim_target(cam))

    def test_track_target_accepts_damped_and_locked_track_too(self):
        target = types.SimpleNamespace(name="Anchor")
        self.assertEqual(sc._aim_target(obj(constraints=[constraint("DAMPED_TRACK", target)])), "Anchor")
        self.assertEqual(sc._aim_target(obj(constraints=[constraint("LOCKED_TRACK", target)])), "Anchor")
        self.assertIsNone(sc._aim_target(obj(constraints=[constraint("COPY_LOCATION", target)])))

    def test_light_line_has_type_hex_colour_and_energy(self):
        light = obj(name="Sun", type="LIGHT", data=light_data(type="SUN", color=(1.0, 0.867, 0.667), energy=3.0))
        self.assertEqual(sc._light_line(light), "Sun: SUN, color #ffddaa, energy 3")

    def test_spot_size_only_shown_for_spot_lights(self):
        spot = obj(data=light_data(type="SPOT", spot_size=math.radians(45)))
        self.assertIn("spot size 45.0°", sc._light_line(spot))
        point = obj(data=light_data(type="POINT"))
        self.assertNotIn("spot size", sc._light_line(point))

    def test_hex_color_clamps_out_of_range_channels(self):
        self.assertEqual(sc._hex_color((2.0, -1.0, 0.5)), "#ff0080")

    def test_keyed_channels_merge_array_indexed_curves_by_data_path(self):
        rig = obj(name="Cam", type="EMPTY", animation_data=animated(
            fcurve("location", [1, 64, 128, 192]), fcurve("location", [1, 64]), fcurve("location", [1])))
        self.assertEqual(sc._keyed_channels(rig), "Cam: location keys 1, 64, 128, 192")

    def test_keyed_channels_caps_frames_shown_per_channel(self):
        many_frames = [f * 4 for f in range(20)]
        rig = obj(animation_data=animated(fcurve("location", many_frames)))
        self.assertEqual(sc._keyed_channels(rig), "Cam: location keys 0, 4, 8, 12, 16, 20, 24, 28 (+12 more)")

    def test_keyed_channels_caps_channels_shown_per_object(self):
        curves = [fcurve(f"prop_{i}", [1]) for i in range(sc.MAX_CHANNELS_PER_OBJECT + 3)]
        rig = obj(animation_data=animated(*curves))
        line = sc._keyed_channels(rig)
        parts = line.split(": ", 1)[1].split("; ")
        self.assertEqual(len([p for p in parts if p.startswith("prop_")]), sc.MAX_CHANNELS_PER_OBJECT)
        self.assertIn("+3 more channels", line)

    def test_object_without_animation_has_no_keyed_channels_line(self):
        self.assertIsNone(sc._keyed_channels(obj(animation_data=None)))
        self.assertIsNone(sc._keyed_channels(obj(animation_data=types.SimpleNamespace(action=None, action_slot=None))))

    def test_scene_facts_groups_cameras_lights_and_animated_objects_with_caps(self):
        cams = [obj(name=f"Cam{i}", type="CAMERA", data=camera_data()) for i in range(sc.MAX_CAMERAS + 1)]
        lights = [obj(name=f"Light{i}", type="LIGHT", data=light_data()) for i in range(sc.MAX_LIGHTS + 1)]
        rig = obj(name="Rig", type="EMPTY", animation_data=animated(fcurve("location", [1, 2])))
        lines = sc._scene_facts(cams + lights + [rig])
        text = "\n".join(lines)
        self.assertIn("cameras:", text)
        self.assertIn(f"... and 1 more", text.split("lights:")[0])
        self.assertIn("lights:", text)
        self.assertIn("animated (keyed channels, frame numbers):\n  Rig: location keys 1, 2", text)

    def test_scene_facts_is_empty_for_a_plain_scene(self):
        cube = obj(name="Cube", type="MESH", data=None)
        self.assertEqual(sc._scene_facts([cube]), [])


def shot(images: dict) -> dict:
    """A scene_diff snapshot of an empty scene but its images."""
    return {"too_many": False, "objects": {}, "object_count": 0, "materials": {}, "worlds": {}, "collections": {},
            "scene": {}, "mode": "OBJECT", "texts": {}, "images": images}


class SinceLastStepTest(unittest.TestCase):
    """The "by the user, not you" lines: a texture the agent's own look loaded moves scene_diff's pixel
    budget, and the image it took from is not one the user packed (test_scene_diff.LookTest)."""

    def test_a_look_is_not_a_user_change_and_an_image_the_user_opened_is(self):
        from loopcut import scene_diff
        rough = {"size": (2048, 2048), "pixels": "9f3a0c1d", "packed": "", "dirty": False, "materials": ("Rock",)}
        since = shot({"Rock_Rough": rough})
        looked = {"Rock_Rough": {**rough, "pixels": None, "packed": "5592405:1a2b3c4d"}}
        with mock.patch.object(scene_diff, "snapshot", return_value=shot(looked)):
            self.assertEqual(sc._since_last_step(since), [])
        photo = {"size": (1024, 768), "pixels": None, "packed": "", "dirty": False, "materials": ()}
        with mock.patch.object(scene_diff, "snapshot", return_value=shot({**looked, "photo.jpg": photo})):
            self.assertEqual(sc._since_last_step(since), ["changed since your last step (by the user, not you):",
                                                          "  + image photo.jpg (1024x768)"])



def view(translate=True, items=(("ja_JP", "Japanese - 日本語"), ("zh_HANS", "Chinese (Simplified) - 简体中文"),
                                ("sr_RS@latin", "Serbian Latin - srpski latinica"), ("en_US", "English - English"))):
    """preferences.view as interface_language reads it: the translate switch and the language enum."""
    language = types.SimpleNamespace(enum_items=[types.SimpleNamespace(identifier=i, name=n) for i, n in items])
    return types.SimpleNamespace(use_translate_interface=translate,
                                 bl_rna=types.SimpleNamespace(properties={"language": language} if items else {}))


class InterfaceLanguageTest(unittest.TestCase):
    def test_translated_interface_names_its_language(self):
        self.assertEqual(sc.interface_language(view(), "ja_JP"), ("ja_JP", "Japanese"))
        self.assertEqual(sc.interface_language(view(), "zh_HANS"), ("zh_HANS", "Chinese (Simplified)"))
        self.assertEqual(sc.interface_language(view(), "sr_RS@latin"), ("sr_RS", "Serbian Latin"))

    def test_english_or_untranslated_needs_no_line(self):
        self.assertIsNone(sc.interface_language(view(), "en_US"))
        self.assertIsNone(sc.interface_language(view(), "en_GB"))
        self.assertIsNone(sc.interface_language(view(translate=False), "ja_JP"))
        self.assertIsNone(sc.interface_language(view(), ""))  # A build without translations.

    def test_unlisted_locale_falls_back_to_its_code(self):
        self.assertEqual(sc.interface_language(view(), "ko_KR"), ("ko_KR", "ko_KR"))
        self.assertEqual(sc.interface_language(view(items=()), "ja_JP.UTF-8"), ("ja_JP", "ja_JP"))

    def test_line_says_which_language_to_answer_in(self):
        line = sc.language_line("Japanese", starter=False)
        self.assertTrue(line.startswith("interface language: Japanese, a fallback only"))
        self.assertIn("romaji", line)
        # Luna followed a closing "reply in Russian" for English writers (2026-10-25): the user's language is last.
        self.assertTrue(line.endswith("reply in the language the user writes in, English included."), line)
        self.assertNotIn("reply in Japanese", line)
        starter = sc.language_line("Japanese", starter=True)
        self.assertIn("starter Loopcut wrote", starter)
        self.assertTrue(starter.endswith("reply in Japanese."))

    def test_every_card_prompt_counts_as_a_starter(self):
        from loopcut import try_it
        self.assertEqual(try_it.PROMPTS, {card["prompt"] for card in try_it.CARDS})


if __name__ == "__main__":
    unittest.main()
