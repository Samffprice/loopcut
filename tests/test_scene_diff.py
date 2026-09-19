"""scene_diff.diff and its formatters are pure; snapshot() is covered by harness/tools_check.py."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "extension"))

from loopcut import scene_diff  # noqa: E402


def cube(**overrides) -> dict:
    entry = {"type": "MESH", "location": (0, 0, 0), "rotation_deg": (0, 0, 0), "scale": (1, 1, 1),
             "parent": None, "visible": True, "data": "Cube", "materials": ("Material",), "modifiers": {},
             "collections": ("Collection",), "animated": False, "verts": 8, "faces": 6,
             "bounds": ((-1, -1, -1), (1, 1, 1))}
    entry.update(overrides)
    return entry


def scene(objects=None, **overrides) -> dict:
    shot = {"too_many": False, "objects": objects if objects is not None else {"Cube": cube()},
            "materials": {"Material": {"nodes": 2, "links": 1, "Base Color": (0.8, 0.8, 0.8, 1.0)}},
            "collections": {"Collection": ("Cube",)},
            "scene": {"frame_start": 1, "frame_end": 250, "render.fps": 24}, "mode": "OBJECT"}
    shot["object_count"] = len(shot["objects"])
    shot.update(overrides)
    return shot


class SceneDiffTest(unittest.TestCase):
    def test_no_change_is_empty_and_tells_the_model_so(self):
        changes = scene_diff.diff(scene(), scene())
        self.assertTrue(scene_diff.is_empty(changes))
        self.assertIn("none", scene_diff.for_model(changes))
        self.assertEqual(scene_diff.headline(changes), "No changes")

    def test_added_object_reports_where_it_is_in_world_space(self):
        after = scene({"Cube": cube(), "Sphere": cube(data="Sphere", bounds=((-0.5, -0.5, 1.0), (0.5, 0.5, 2.0)))})
        changes = scene_diff.diff(scene(), after)
        self.assertEqual(changes["added"], ["Sphere (mesh) at [-0.5, -0.5, 1.0]..[0.5, 0.5, 2.0]"])
        self.assertEqual(scene_diff.headline(changes), "1 added")

    def test_removed_object(self):
        changes = scene_diff.diff(scene(), scene({}))
        self.assertEqual(changes["removed"], ["Cube (mesh)"])

    def test_moved_object_shows_old_and_new_and_the_bounds_it_ended_up_with(self):
        after = scene({"Cube": cube(location=(0, 0, 1), bounds=((-1, -1, 0), (1, 1, 2)))})
        (line,) = scene_diff.diff(scene(), after)["changed"]
        self.assertIn("location (0, 0, 0) -> (0, 0, 1)", line)
        self.assertIn("bounds now [-1, -1, 0]..[1, 1, 2]", line)

    def test_modifier_added_changed_and_removed(self):
        bevel = {"type": "BEVEL", "width": 0.1, "segments": 1}
        with_bevel = scene({"Cube": cube(modifiers={"Bevel": bevel})})
        self.assertIn("+modifier Bevel (BEVEL)", scene_diff.diff(scene(), with_bevel)["changed"][0])
        tuned = scene({"Cube": cube(modifiers={"Bevel": {**bevel, "segments": 3}})})
        self.assertIn("modifier Bevel: segments 1 -> 3", scene_diff.diff(with_bevel, tuned)["changed"][0])
        self.assertIn("-modifier Bevel", scene_diff.diff(with_bevel, scene())["changed"][0])

    def test_mesh_edit_is_one_note_not_two(self):
        after = scene({"Cube": cube(verts=8, faces=5)})
        (line,) = scene_diff.diff(scene(), after)["changed"]
        self.assertEqual(line, "Cube: mesh 8v/6f -> 8v/5f")

    def test_material_and_collection_changes(self):
        after = scene()
        after["materials"] = {"Material": {"nodes": 2, "links": 1, "Base Color": (0, 0, 1, 1.0)},
                              "Red": {"nodes": 2, "links": 1}}
        after["collections"] = {"Collection": (), "Props": ("Cube",)}
        changes = scene_diff.diff(scene(), after)
        self.assertEqual(changes["added"], ["material Red", "collection Props"])
        self.assertIn("material Material: Base Color (0.8, 0.8, 0.8, 1.0) -> (0, 0, 1, 1.0)", changes["changed"])
        self.assertIn("collection Collection: -Cube", changes["changed"])

    def test_scene_settings_and_mode(self):
        after = scene(mode="EDIT_MESH")
        after["scene"]["render.fps"] = 30
        self.assertEqual(scene_diff.diff(scene(), after)["other"],
                         ["scene render.fps 24 -> 30", "mode OBJECT -> EDIT_MESH"])

    def test_huge_scene_degrades_to_a_count(self):
        before = scene({}, too_many=True, object_count=6000)
        after = scene({}, too_many=True, object_count=6003)
        self.assertEqual(scene_diff.diff(before, after)["other"],
                         ["6003 objects (+3); too many to compare one by one"])

    def test_long_diffs_are_capped(self):
        after = scene({f"Cube.{i:03}": cube() for i in range(100)})
        text = scene_diff.for_model(scene_diff.diff(scene({}), after))
        self.assertIn("... and 60 more", text)

    def test_diff_does_not_mutate_its_inputs(self):
        before, after = scene(), scene({"Cube": cube(location=(1, 0, 0))})
        frozen = copy.deepcopy((before, after))
        scene_diff.diff(before, after)
        self.assertEqual((before, after), frozen)


if __name__ == "__main__":
    unittest.main()
