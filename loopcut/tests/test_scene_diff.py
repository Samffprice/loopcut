"""scene_diff.diff and its formatters are pure; snapshot() is covered by harness/tools_check.py."""

import copy
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import scene_diff  # noqa: E402


def cube(**overrides) -> dict:
    entry = {"type": "MESH", "location": (0, 0, 0), "rotation_deg": (0, 0, 0), "scale": (1, 1, 1),
             "parent": None, "visible": True, "data": "Cube", "materials": ("Material",), "modifiers": {},
             "collections": ("Collection",), "animated": False, "verts": 8, "faces": 6,
             "bounds": ((-1, -1, -1), (1, 1, 1))}
    entry.update(overrides)
    return entry


def material(**overrides) -> dict:
    entry = {"node Principled BSDF": "BSDF_PRINCIPLED", "Principled BSDF.Base Color": (0.8, 0.8, 0.8, 1.0),
             "Principled BSDF.Roughness": 0.5, "node Material Output": "OUTPUT_MATERIAL",
             "links": ("Principled BSDF.BSDF -> Material Output.Surface",), "settings.surface_render_method": "DITHERED"}
    entry.update(overrides)
    return entry


def scene(objects=None, **overrides) -> dict:
    shot = {"too_many": False, "objects": objects if objects is not None else {"Cube": cube()},
            "materials": {"Material": material()},
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

    def test_vertices_moved_with_the_same_counts_and_bounds_is_a_change(self):
        """A lid pushed in: counts and bounds as before, the positions not (eval 20260929, analysis §5.7)."""
        before, after = scene({"Cube": cube(shape="a1")}), scene({"Cube": cube(shape="b2")})
        changes = scene_diff.diff(before, after)
        self.assertEqual(changes["changed"], ["Cube: mesh vertices moved (8v, same counts)"])
        self.assertNotIn("none", scene_diff.for_model(changes))
        self.assertEqual(scene_diff.touched(before, after), ["Cube"])
        self.assertTrue(scene_diff.is_empty(scene_diff.diff(before, scene({"Cube": cube(shape="a1")}))))

    def test_a_count_change_says_it_once_and_an_unhashed_mesh_says_nothing(self):
        (line,) = scene_diff.diff(scene({"Cube": cube(shape="a1")}), scene({"Cube": cube(verts=10, shape="c3")}))["changed"]
        self.assertEqual(line, "Cube: mesh 8v/6f -> 10v/6f")
        self.assertEqual(scene_diff.diff(scene({"Cube": cube(shape="a1")}), scene({"Cube": cube(shape=None)}))["changed"], [])

    def test_uv_edits_are_a_change_and_a_new_active_map_says_its_name(self):
        """uv_unwrap changes no count, bound or position: it was "Scene changes: none" (m4-uv)."""
        before = scene({"Cube": cube(shape="a1", uv=("UVMap", "u1"))})
        changes = scene_diff.diff(before, scene({"Cube": cube(shape="a1", uv=("UVMap", "u2"))}))
        self.assertEqual(changes["changed"], ["Cube: UVs edited (map UVMap)"])
        self.assertEqual(scene_diff.touched(before, scene({"Cube": cube(shape="a1", uv=("UVMap", "u2"))})), ["Cube"])
        (line,) = scene_diff.diff(scene({"Cube": cube(uv=("", ""))}), scene({"Cube": cube(uv=("UVMap", "u1"))}))["changed"]
        self.assertEqual(line, "Cube: active UV map none -> UVMap")
        (line,) = scene_diff.diff(before, scene({"Cube": cube(verts=10, shape="c3", uv=("UVMap", "u3"))}))["changed"]
        self.assertEqual(line, "Cube: mesh 8v/6f -> 10v/6f", "a count change moves every corner: said once")
        for unhashed in (None, ("UVMap", None)):
            self.assertTrue(scene_diff.is_empty(scene_diff.diff(before, scene({"Cube": cube(shape="a1", uv=unhashed)}))))

    def test_painted_pixels_are_a_change_of_the_image_and_frame_who_wears_it(self):
        image = {"size": (512, 512), "pixels": "p1", "dirty": False, "materials": ("Material",)}
        before = scene(images={"Label": image})
        after = scene(images={"Label": {**image, "pixels": "p2", "dirty": True}})
        changes = scene_diff.diff(before, after)
        self.assertEqual(changes["changed"], ["image Label: pixels edited (in Material)"])
        self.assertNotIn("none", scene_diff.for_model(changes))
        self.assertEqual(scene_diff.touched(before, after), ["Cube"])
        big = {**image, "pixels": None}
        self.assertEqual(scene_diff.diff(scene(images={"Label": big}), scene(images={"Label": {**big, "dirty": True}}))
                         ["changed"], ["image Label: pixels edited (unsaved) (in Material)"])
        self.assertEqual(scene_diff.diff(before, scene(images={"Label": {**image, "size": (1024, 1024), "pixels": "p3"}}))
                         ["changed"], ["image Label: 512x512 -> 1024x1024 px (in Material)"])
        for quiet in ({"Label": {**big, "dirty": False}}, {"Label": {**image, "size": (0, 0), "pixels": None}}, {}):
            self.assertTrue(scene_diff.is_empty(scene_diff.diff(before, scene(images=quiet))), quiet)
        self.assertTrue(scene_diff.is_empty(scene_diff.diff(scene(), after)), "newly read: its nodes say it")

    def test_material_and_collection_changes(self):
        after = scene()
        after["materials"] = {"Material": material(**{"Principled BSDF.Base Color": (0, 0, 1, 1.0)}), "Red": material()}
        after["collections"] = {"Collection": (), "Props": ("Cube",)}
        changes = scene_diff.diff(scene(), after)
        self.assertEqual(changes["added"], ["material Red", "collection Props"])
        self.assertIn("material Material: Principled BSDF.Base Color (0.8, 0.8, 0.8, 1.0) -> (0, 0, 1, 1.0)",
                      changes["changed"])
        self.assertIn("collection Collection: -Cube", changes["changed"])

    def test_node_edits_the_model_makes_are_all_visible(self):
        """Color ramps, volume density and render-method settings once went unreported, so the
        model re-rendered to find out whether its edit had landed."""
        before = scene()
        before["materials"]["Material"]["node Color Ramp"] = "VALTORGB"
        before["materials"]["Material"]["Color Ramp.ramp"] = ("LINEAR", (0.0, (0, 0, 0, 1)), (1.0, (1, 1, 1, 1)))
        after = copy.deepcopy(before)
        after["materials"]["Material"].update({
            "Color Ramp.ramp": ("LINEAR", (0.0, (0, 0, 0, 1)), (0.006, (1, 1, 1, 1))),
            "settings.surface_render_method": "BLENDED",
            "node Principled Volume": "PRINCIPLED_VOLUME", "Principled Volume.Density": 0.6,
            "links": ("Principled BSDF.BSDF -> Material Output.Surface",
                      "Principled Volume.Volume -> Material Output.Volume")})
        (line,) = scene_diff.diff(before, after)["changed"]
        self.assertIn("+node Principled Volume (PRINCIPLED_VOLUME)", line)
        self.assertIn("+link Principled Volume.Volume -> Material Output.Volume", line)
        self.assertIn("Color Ramp.ramp", line)
        self.assertIn("settings.surface_render_method 'DITHERED' -> 'BLENDED'", line)
        self.assertNotIn("Principled Volume.Density", line)  # Part of the new node, not a change of its own.

    def test_world_and_render_engine_settings(self):
        before = scene(worlds={"World": {"node Background": "BACKGROUND", "Background.Strength": 1.0}})
        before["scene"]["eevee.use_volumetric_shadows"] = False
        after = copy.deepcopy(before)
        after["worlds"]["World"]["Background.Strength"] = 0.2
        after["scene"]["eevee.use_volumetric_shadows"] = True
        changes = scene_diff.diff(before, after)
        self.assertIn("world World: Background.Strength 1.0 -> 0.2", changes["changed"])
        self.assertIn("scene eevee.use_volumetric_shadows False -> True", changes["other"])

    def test_hiding_from_the_render_is_a_change_of_its_own(self):
        after = scene({"Cube": cube(renders=False, hidden_from_rays=("camera",))})
        before = scene({"Cube": cube(renders=True, hidden_from_rays=())})
        (line,) = scene_diff.diff(before, after)["changed"]
        self.assertIn("renders True -> False", line)
        self.assertIn("hidden_from_rays () -> ('camera',)", line)

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

    def test_renamed_object_is_one_note_not_a_removal_and_an_addition(self):
        """A datablock kept under a new name (matched by session_uid) is not the same as an
        unrelated object replacing it: "don't change anything else" means don't delete Wall_N3."""
        before = scene({"Wall_N3": cube(uid=42)})
        after = scene({"Doorway_N3": cube(uid=42)})
        changes = scene_diff.diff(before, after)
        self.assertEqual(changes["changed"], ["Wall_N3 → Doorway_N3 (renamed)"])
        self.assertEqual(changes["added"], [])
        self.assertEqual(changes["removed"], [])

    def test_renamed_object_still_reports_other_field_changes(self):
        before = scene({"Wall_N3": cube(uid=42, location=(0, 0, 0))})
        after = scene({"Doorway_N3": cube(uid=42, location=(1, 0, 0))})
        (line,) = scene_diff.diff(before, after)["changed"]
        self.assertTrue(line.startswith("Wall_N3 → Doorway_N3 (renamed); "), line)
        self.assertIn("location (0, 0, 0) -> (1, 0, 0)", line)

    def test_same_name_reused_by_a_different_object_is_not_a_rename(self):
        """No uid in common: an unrelated object took the freed name, so it is add + remove."""
        before = scene({"Wall_N3": cube(uid=42)})
        after = scene({"Wall_N3": cube(uid=99)})
        changes = scene_diff.diff(before, after)
        self.assertEqual(changes["added"], [])  # Same name stays in "objects", compared as one.
        self.assertEqual(changes["removed"], [])
        self.assertEqual(changes["changed"], [])  # uid is identity, never shown as a changed field.

    def test_object_without_a_uid_falls_back_to_add_and_remove(self):
        """Older snapshots (or anything that never went through _object) have no "uid" key."""
        before = scene({"Wall_N3": cube()})
        after = scene({"Doorway_N3": cube()})
        changes = scene_diff.diff(before, after)
        self.assertEqual(changes["added"], ["Doorway_N3 (mesh) at [-1, -1, -1]..[1, 1, 1]"])
        self.assertEqual(changes["removed"], ["Wall_N3 (mesh)"])



# mathutils and IDProperty stand-ins, as far as rounding goes. Like the real ones they are sequences
# through __len__ and __getitem__ alone (no __iter__), define no __round__, and a Matrix's items are
# its row Vectors.
class TouchedTest(unittest.TestCase):
    """What an automatic look frames: what the step added or changed, not the scene's first objects."""

    def test_added_changed_and_repainted_objects_with_geometry_that_show(self):
        mesh = {"type": "MESH", "visible": True, "location": (0, 0, 0), "materials": ("Stone",)}
        before = {"too_many": False, "materials": {"Stone": {"base": 1}, "Moss": {"base": 2}},
                  "objects": {"Tile_1": dict(mesh), "Tile_2": dict(mesh), "Wall": {**mesh, "materials": ("Moss",)},
                              "Ghost": {**mesh, "visible": False}}}
        after = {"too_many": False, "materials": {"Stone": {"base": 1}, "Moss": {"base": 3}},
                 "objects": {**before["objects"], "Tile_2": {**mesh, "location": (0, 0, 1)}, "Torch": dict(mesh),
                             "Torch_Light": {"type": "LIGHT", "visible": True}, "Ghost": {**mesh, "visible": False, "x": 1}}}
        self.assertEqual(scene_diff.touched(before, after), ["Tile_2", "Wall", "Torch"])
        self.assertEqual(scene_diff.touched({**before, "too_many": True}, after), [], "too many to say: frame all")


class Vector:
    def __init__(self, *values):
        self.values = [float(v) for v in values]

    def __getitem__(self, index):
        return self.values[index]

    def __len__(self):
        return len(self.values)


class Euler(Vector):
    pass


class Quaternion(Vector):
    pass


class Color(Vector):
    pass


class Matrix:
    def __init__(self, *rows):
        self.rows = [Vector(*row) for row in rows]

    def __getitem__(self, index):
        return self.rows[index]

    def __len__(self):
        return len(self.rows)


class IDPropertyArray:
    def __init__(self, *values):
        self.values = list(values)

    def to_list(self):
        return list(self.values)

    def __len__(self):
        return len(self.values)

    def __getitem__(self, index):
        raise AssertionError("read through to_list(), a copy")


def prop(identifier, kind, array_length=0):
    return types.SimpleNamespace(identifier=identifier, type=kind, array_length=array_length, is_readonly=False,
                                 is_enum_flag=False, is_deprecated=False)


def hook_modifier(offset=0.0):
    """A HookModifier as a BlenderKit floor lamp's wire had it: matrix_inverse is a 4x4 FLOAT property."""
    return types.SimpleNamespace(
        name="Hook-Cylinder", type="HOOK",
        bl_rna=types.SimpleNamespace(properties=[
            prop("rna_type", "POINTER"), prop("matrix_inverse", "FLOAT", 16), prop("center", "FLOAT", 3),
            prop("strength", "FLOAT"), prop("vertex_indices_set", "INT", 0)]),
        matrix_inverse=Matrix((1, 0, 0, offset), (0, 1, 0, 0), (0, 0, 1, -1.3090001), (0, 0, 0, 1)),
        center=Vector(0.1234567, 0, 1.309), strength=1.0, vertex_indices_set=0)


class BlenderValuesTest(unittest.TestCase):
    """Vectors, matrices and property arrays are rounded all the way down, never round() on a row.
    Eval 20260927-043942 shared_support: after a BlenderKit floor lamp with a hook modifier came in,
    every run_python failed with "type Vector doesn't define __round__ method", even print("x"),
    because the snapshot before the model's code rounded matrix_inverse row by row."""

    def test_the_fakes_fail_as_blender_did_under_the_old_rounding(self):
        with self.assertRaisesRegex(TypeError, "type Vector doesn't define __round__ method"):
            tuple(round(v, 4) for v in hook_modifier().matrix_inverse)  # The old _rounded.

    def test_a_hook_modifier_reads_as_plain_nested_numbers(self):
        values = scene_diff.rna_values(hook_modifier())
        self.assertEqual(values["matrix_inverse"],
                         ((1.0, 0.0, 0.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 1.0, -1.309), (0.0, 0.0, 0.0, 1.0)))
        self.assertEqual(values["center"], (0.1235, 0.0, 1.309))
        self.assertEqual(values["strength"], 1.0)
        self.assertEqual(values["vertex_indices_set"], 0)
        self.assertNotIn("rna_type", values)

    def test_a_moved_hook_is_a_change_the_diff_reports(self):
        def shot(offset):
            return scene({"Wire": cube(type="CURVE", modifiers=scene_diff._modifiers(
                types.SimpleNamespace(modifiers=[hook_modifier(offset)])))})
        self.assertTrue(scene_diff.is_empty(scene_diff.diff(shot(0.0), shot(0.0))))
        (line,) = scene_diff.diff(shot(0.0), shot(0.5))["changed"]
        self.assertIn("modifier Hook-Cylinder: matrix_inverse ((1.0, 0.0, 0.0, 0.0)", line)
        self.assertIn("-> ((1.0, 0.0, 0.0, 0.5)", line)

    def test_every_blender_array_type_is_rounded(self):
        rounded = scene_diff.rounded_value
        self.assertEqual(rounded(Vector(1.234567, 2, 3)), (1.2346, 2.0, 3.0))
        self.assertEqual(rounded(Euler(0.123456, 0, 0), 2), (0.12, 0.0, 0.0))
        self.assertEqual(rounded(Quaternion(1, 0, 0, 0.000049)), (1.0, 0.0, 0.0, 0.0))
        self.assertEqual(rounded(Color(0.8, 0.8, 0.80001)), (0.8, 0.8, 0.8))
        self.assertEqual(rounded(Matrix((1.00004, 0), (0, 2))), ((1.0, 0.0), (0.0, 2.0)))
        self.assertEqual(rounded(IDPropertyArray(0.123456, 2, 3)), (0.1235, 2, 3))
        self.assertEqual(rounded([[Vector(1, 2)], (True, 3)]), (((1.0, 2.0),), (True, 3)))
        for plain in (None, "text", 3, True, {"a": 1.23456}):
            self.assertEqual(rounded(plain), plain)
        blender_id = types.SimpleNamespace(name="Cube")  # No __len__, like a real ID.
        self.assertIs(rounded(blender_id), blender_id)

    def test_node_inputs_that_hold_vectors_or_matrices_stay_comparable(self):
        self.assertEqual(scene_diff._plain(Matrix((1, 0), (0, 1))), ((1.0, 0.0), (0.0, 1.0)))
        self.assertEqual(scene_diff._plain(Euler(0.5, 0, 0)), (0.5, 0.0, 0.0))
        self.assertEqual(scene_diff._plain(0.123456), 0.1235)
        self.assertEqual(scene_diff._plain("x" * 50), "x" * 40)
        self.assertEqual(scene_diff._plain(types.SimpleNamespace(name="Cube")), "namespace(name='Cube')")


if __name__ == "__main__":
    unittest.main()
