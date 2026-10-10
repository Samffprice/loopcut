"""What a worker may create and change, and how the merge reports it, where no Blender is needed: the
materials aspect and a worker's own material copies; animation for the roles that animate; what a
brief's own words ask for, whatever the role, and the reason given for what is still held back. The
real append, rollback and checks are loopcut/harness/merge_check.py's materials and shot waves and
worker_host_check.py's rollback case."""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import merge, ownership  # noqa: E402


def made(id_type: str, kind: str = "", name: str = ""):
    return types.SimpleNamespace(id_type=id_type, type=kind, name=name)


class MaterialsTest(unittest.TestCase):
    def test_the_materials_aspect_lets_any_role_make_materials(self):
        self.assertIn("materials", ownership.ASPECTS)
        material = made("MATERIAL")
        self.assertFalse(ownership.allowed("camera", material))
        self.assertTrue(ownership.allowed("camera", material, ["materials"]))
        self.assertTrue(ownership.allowed("modeling", material), "a modeler's own copies need no aspect")
        self.assertFalse(ownership.allowed("modeling", made("OBJECT", "LIGHT"), ["materials"]), "materials only")


    def test_merge_lines_say_what_happened_to_materials(self):
        report = merge.MergeReport(
            created=[{"worker": "a", "name": "Chair_Wood", "id_type": "MATERIAL", "type": "", "parent": None,
                      "variant_of": "Pal_Wood"}],
            reused=[{"worker": "a", "name": "Pal_Wool", "made": "Chair_Wool", "id_type": "MATERIAL", "copy": True},
                    {"worker": "b", "name": "Pal_Glass", "made": "Pal_Glass.001", "id_type": "MATERIAL"}],
            dropped_edits=[{"worker": "a", "key": "MATERIAL:Pal_Wood", "name": "Pal_Wood"},
                           {"worker": "a", "key": "OBJECT:Rug", "name": "Rug"}])
        lines = report.lines()
        self.assertIn("a: material Chair_Wool is an unchanged copy of the live Pal_Wool: the live one is used, "
                      "no copy merged", lines)
        self.assertIn("b: material Pal_Glass.001 is the live Pal_Glass made again (same name and settings): the live "
                      "one is used, no copy merged", lines)
        self.assertIn("a: material Chair_Wood is its own variant of Pal_Wood (on its own parts)", lines)
        self.assertIn("a: edit to Pal_Wood dropped (not owned; a palette material changes only with aspect materials, "
                      "a worker's own copy merges)", lines)
        self.assertIn("a: edit to Rug dropped (not owned)", lines)


class AnimationTest(unittest.TestCase):
    def test_roles_that_animate_may_create_actions(self):
        action = made("ACTION")
        for role in ("lighting", "camera", "animation"):
            self.assertTrue(ownership.allowed(role, action), role)
        self.assertFalse(ownership.allowed("modeling", action), "a modeler's keys need the brief to ask for motion")
        self.assertTrue(ownership.allowed("modeling", action, ["lighting"]), "a shot's aspects bring actions")
        self.assertTrue(ownership.allowed("materials", action, ["camera"]))
        self.assertTrue(ownership.allowed("camera", made("OBJECT", "CURVE")), "a path to follow")
        self.assertTrue(ownership.allowed("animation", made("CURVE")))


class AskedForTest(unittest.TestCase):
    LAMP = {"deliverable": "ReadingLamp", "requirements": [{"quote": "a floor lamp that lights the reading corner",
                                                            "do": "floor lamp"}]}

    def test_words_that_ask_for_a_light_or_motion(self):
        self.assertEqual(ownership.asked_for("LIGHT", self.LAMP), "a floor lamp that lights the reading corner")
        for words in ("light grey sofa", "a lightweight chair", "light wood shelves", "a floor-standing vase"):
            self.assertEqual(ownership.asked_for("LIGHT", {"requirements": [{"quote": words}]}), "", words)
        for words in ("glowing crystals", "a lantern", "candles on the table", "the lamp is on"):
            self.assertTrue(ownership.asked_for("LIGHT", {"requirements": [{"quote": words}]}), words)
        self.assertTrue(ownership.asked_for("ACTION", {"defaults": ["blades spin at 1 turn a second"]}))
        self.assertTrue(ownership.asked_for("ACTION", {"requirements": [{"do": "keyframe the lid opening"}]}))
        self.assertEqual(ownership.asked_for("ACTION", {"requirements": [{"quote": "a still life"}]}), "")
        self.assertEqual(ownership.asked_for("WORLD", self.LAMP), "", "a world is lighting's, never asked around it")
        long = {"requirements": [{"quote": "x" * 100 + " and a lamp that lights it " + "y" * 100}]}
        self.assertLessEqual(len(ownership.asked_for("LIGHT", long)), 90)

    def test_names_the_brief_gives(self):
        brief = {"deliverable": "Shot", "owns": ["Rim_Rig"], "checks": [{"kind": "in_frame", "names": ["Bottle"],
                                                                          "camera": "Cam_Hero"}],
                 "requirements": [{"quote": "aim everything at Cam_Target", "do": "an aim empty Cam_Target"}]}
        for name in ("Shot", "Rim_Rig", "Bottle", "Cam_Hero", "Cam_Target", "Cam_Target.001"):
            self.assertTrue(ownership.pinned(name, brief), name)
        for name in ("Cam", "Light", "Other_Thing", "Target"):
            self.assertFalse(ownership.pinned(name, brief), name)
        self.assertFalse(ownership.pinned("Shot", None))

    def test_a_refusal_says_what_the_role_makes_and_what_would_let_it_through(self):
        text = ownership.refusal("modeling", [], "LIGHT")
        self.assertTrue(text.startswith("outside its role: a modeling worker makes curves, empties, materials, meshes, text;"),
                        text)
        self.assertIn("lights need aspect lighting", text)
        self.assertIn("with aspect camera", ownership.refusal("lighting", ["camera"], "WORLD"))

    def test_merge_lines_give_the_reason_for_what_is_held_and_what_came_through(self):
        report = merge.MergeReport(
            quarantined=[{"worker": "t", "name": "Lamp_Test", "id_type": "OBJECT", "type": "LIGHT",
                          "reason": "outside its role: ..."}, {"worker": "u", "name": "Old", "id_type": "WORLD", "type": ""}],
            admitted=[{"worker": "t", "name": "Lamp_Bulb", "id_type": "OBJECT", "type": "LIGHT",
                       "reason": "part of Lamp, and the brief asks for it: \"a lamp that lights\""}])
        lines = report.lines()
        self.assertIn("t: Lamp_Test (light) quarantined: outside its role: ...", lines)
        self.assertIn("u: Old (world) quarantined: outside its role", lines)
        self.assertIn("t: Lamp_Bulb (light) merged though outside its role: part of Lamp, and the brief asks for it: "
                      "\"a lamp that lights\"", lines)
        self.assertEqual(report.log()["admitted"], ["Lamp_Bulb"])


class DriverReportTest(unittest.TestCase):
    """The words for drivers a merge re-aims or leaves without a target (merge._settle); the real cases
    are loopcut/harness/apply_links_check.py's applies 5 to 7."""

    def test_lost_and_re_aimed_drivers_are_counted_and_named(self):
        lost = {"worker": "build", "owner": "object Indicator", "path": "location[1]", "variable": "var",
                "target": "the shape keys of mesh Tongue",
                "why": "was aimed at the shape keys of mesh Tongue, which this apply removed: now unset"}
        moved = {"worker": "build", "owner": "object Panel", "path": 'pose.bones["Meter"].location[0]',
                 "variable": "var", "old": "the shape keys of mesh Face", "new": "the shape keys of mesh FaceNew"}
        report = merge.MergeReport(unlinked=[lost, dict(lost, path="location[2]")], retargeted=[moved])
        self.assertIn("2 drivers left without their target", report.headline())
        self.assertIn("1 driver left without its target", merge.MergeReport(unlinked=[lost]).headline())
        self.assertIn("build: driver object Indicator location[1] (variable var) was aimed at the shape keys of mesh "
                      "Tongue, which this apply removed: now unset", report.lines())
        self.assertIn('build: driver object Panel pose.bones["Meter"].location[0] (variable var) re-aimed from the '
                      "shape keys of mesh Face, removed, to the shape keys of mesh FaceNew, which have the same key "
                      "blocks", report.lines())
        self.assertEqual(report.log()["unlinked"][0]["target"], "the shape keys of mesh Tongue")

    def test_the_key_block_a_driver_reads_is_found_through_any_path(self):
        key = types.SimpleNamespace(id_type="KEY")
        face = types.SimpleNamespace(id_type="OBJECT", data=types.SimpleNamespace(shape_keys=key))
        mesh = types.SimpleNamespace(id_type="MESH", shape_keys=key)

        def read(idb, path):
            return merge._key_read(types.SimpleNamespace(id=idb, data_path=path))
        self.assertEqual(read(key, 'key_blocks["smile"].value'), (key, "smile"))
        self.assertEqual(read(face, 'data.shape_keys.key_blocks["mouth \\"open\\""].slider_max'), (key, 'mouth "open"'))
        self.assertEqual(read(mesh, 'shape_keys.key_blocks["smile"].value'), (key, "smile"))
        self.assertEqual(read(face, "location[0]"), (None, None))
        self.assertEqual(read(key, "eval_time"), (None, None))


if __name__ == "__main__":
    unittest.main()
