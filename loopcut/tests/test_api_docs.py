"""The API notes reach the model where it lands after an error, not only on the one type they are
keyed by. harness/api_docs_check.py runs the notes' code on a real Blender; here RNA is faked."""

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import api_docs, tools  # noqa: E402


class Properties(dict):
    def __iter__(self):
        return iter(self.values())


def rna(identifier, base=None, properties=(), functions=()):
    return types.SimpleNamespace(identifier=identifier, base=base,
                                 functions=Properties({f: types.SimpleNamespace(identifier=f) for f in functions}),
                                 description="",
                                 properties=Properties({p: types.SimpleNamespace(identifier=p, type="STRING", fixed_type=None)
                                                        for p in properties}))


ID = rna("ID")
ACTION = rna("Action", ID, ("rna_type", "slots", "layers"))
STRIP = rna("ActionStrip")
NODE_TREE = rna("NodeTree", ID)
STRUCTS = {"ID": ID, "Action": ACTION, "ActionSlot": rna("ActionSlot"), "ActionStrip": STRIP,
           "ActionKeyframeStrip": rna("ActionKeyframeStrip", STRIP), "ActionChannelbag": rna("ActionChannelbag"),
           "AnimData": rna("AnimData"), "NodeTree": NODE_TREE, "GeometryNodeTree": rna("GeometryNodeTree", NODE_TREE),
           "Object": rna("Object", ID)}
FAKE_TYPES = types.SimpleNamespace(**{name: types.SimpleNamespace(bl_rna=struct) for name, struct in STRUCTS.items()})


def failed_run(message: str, error=AttributeError) -> str:
    """What run_python writes for model code that raised this."""
    try:
        exec(compile(f"raise {error.__name__}({message!r})", "<loopcut>", "exec"))
    except error as ex:
        with mock.patch.object(api_docs.bpy, "types", FAKE_TYPES, create=True):
            return tools._model_traceback(ex)
    raise AssertionError("did not raise")


class NotesTest(unittest.TestCase):
    def test_the_action_note_covers_the_types_on_the_way_to_fcurves(self):
        for name in ("Action", "ActionSlot", "ActionStrip", "ActionKeyframeStrip", "ActionChannelbag", "AnimData"):
            self.assertIn("channelbag(slot)", api_docs.note_for(STRUCTS[name]), name)
        self.assertIn("slot.identifier", api_docs.NOTES["Action"])  # 'ActionSlot' object has no attribute 'name'
        self.assertIn("interface.new_socket", api_docs.note_for(STRUCTS["GeometryNodeTree"]))
        self.assertEqual(api_docs.note_for(STRUCTS["Object"]), "")

    def test_a_missing_member_of_a_noted_type_says_the_note(self):
        with self.assertRaisesRegex(api_docs.ApiError, r"(?s)no property or function 'fcurves'\. NOTE: .*channelbag\(slot\)"):
            api_docs._walk_rna(types.SimpleNamespace(bl_rna=ACTION), ["fcurves"])
        with self.assertRaises(api_docs.ApiError) as caught:
            api_docs._walk_rna(types.SimpleNamespace(bl_rna=STRUCTS["Object"]), ["fcurves"])
        self.assertNotIn("NOTE", str(caught.exception))

    def test_run_python_errors_on_a_noted_type_carry_the_note(self):
        for message in ("'Action' object has no attribute 'fcurves'", "'ActionSlot' object has no attribute 'name'"):
            text = failed_run(message)
            self.assertIn(f"AttributeError: {message}\nNOTE: Actions are layered", text)
        self.assertIn("NOTE: Actions are layered", failed_run("'ActionChannelbag' object is not iterable", TypeError))
        for message in ("'NoneType' object has no attribute 'fcurves'", "'Object' object has no attribute 'nope'",
                        "'NotAType' object has no attribute 'x'", "no quote at all"):
            self.assertNotIn("NOTE", failed_run(message), message)
        self.assertNotIn("NOTE", failed_run("'Action' object", KeyError))

    def test_a_path_starting_with_double_slash_that_failed_says_how_to_write_it(self):
        # Exporters do not expand //: the glTF one opened '//dungeon.glb' as /dungeon.glb (eval, 2026-09-27).
        for message, error in (
                ("Error: Python: Traceback ...\nOSError: [Errno 30] Read-only file system: '//dungeon.glb'\n", RuntimeError),
                ("[Errno 13] Permission denied: '//renders/x.png'", OSError),
                ("Error: OBJ Export: Cannot open file '//z.obj'", RuntimeError),
                ("Error: USD Export: unable to find suitable USD plugin to write //z.usd", RuntimeError)):
            self.assertIn(f"NOTE: {api_docs.BLEND_RELATIVE}", failed_run(message, error), message)
        for message in ("Cannot open file '/tmp/z.obj'", "cannot fetch https://example.com/x.glb", "a // b"):
            self.assertNotIn("NOTE", failed_run(message, RuntimeError), message)


class AttributeNotesTest(unittest.TestCase):
    def setUp(self):
        self.structs = {}
        self.types = types.SimpleNamespace()
        self.enterContext(mock.patch.object(api_docs.bpy, "types", self.types, create=True))
        self.enterContext(mock.patch.object(api_docs, "_error_index", None, create=True))

    def add(self, name, properties=(), functions=(), base=None, python=()):
        struct = rna(name, base, properties, functions)
        self.structs[name] = struct
        setattr(self.types, name, type(name, (), {"bl_rna": struct, **dict.fromkeys(python)}))
        return struct

    def link(self, owner, prop, target, kind="POINTER"):
        self.structs[owner].properties[prop] = types.SimpleNamespace(
            identifier=prop, type=kind, fixed_type=self.structs[target])

    def note(self, owner, name):
        return api_docs.error_note(AttributeError(f"'{owner}' object has no attribute '{name}'"))

    def bones(self):
        self.add("Bone", ("hide_select",))
        self.add("PoseBone", ("select",))
        self.add("EditBone", ("select",))
        self.add("MeshVertex", ("select",))  # Same attribute alone does not establish a relationship.
        self.add("BoneMeal", ("select",))  # Nor does a substring inside another word.
        self.add("ID")
        self.add("Object", base=self.structs["ID"])
        self.add("Armature", base=self.structs["ID"])
        self.add("Pose")
        self.link("Object", "pose", "Pose")
        self.link("Pose", "bones", "PoseBone", "COLLECTION")
        self.link("Armature", "edit_bones", "EditBone", "COLLECTION")
        self.link("PoseBone", "parent", "PoseBone")

    def test_bone_error_finds_sibling_structs_and_real_access_paths(self):
        self.bones()
        note = self.note("Bone", "select")
        self.assertIn("hide_select", note)
        self.assertIn("PoseBone.select via Object.pose.bones[...]", note)
        self.assertIn("EditBone.select via Armature.edit_bones[...]", note)
        self.assertNotIn("MeshVertex", note)
        self.assertNotIn("BoneMeal", note)
        self.assertLessEqual(len(note.splitlines()), 2)
        self.assertIn("NOTE: " + note, tools._model_traceback(AttributeError("'Bone' object has no attribute 'select'")))

    def test_close_names_include_rna_functions_and_python_members(self):
        self.add("Mesh", ("selection_mode",), ("selection_get",), python=("selection_set",))
        note = self.note("Mesh", "selection")
        for name in ("selection_mode", "selection_get", "selection_set"):
            self.assertIn(name, note)
        self.assertNotIn("bl_rna", note)
        self.assertEqual(self.note("Mesh", "zzzzzzzzzz"), "")

    def test_five_closest_names_and_short_output(self):
        names = tuple("selection_" + str(i) for i in range(12))
        self.add("Mesh", names)
        note = self.note("Mesh", "selection")
        self.assertEqual(sum(name in note.split(": ", 1)[1].rstrip(".").split(", ") for name in names), 5)
        self.assertLessEqual(len(note), 320)

    def test_real_rename_with_less_than_point_six_similarity(self):
        self.add("UvSculpt", ("curve_distance_falloff_preset", "size", "strength"))
        self.assertIn("Closest on UvSculpt: curve_distance_falloff_preset", self.note("UvSculpt", "curve_preset"))

    def test_base_chain_and_one_or_two_property_hops_but_not_three(self):
        base = self.add("Root", functions=("removed",))
        middle = self.add("Middle", base=base)
        self.add("Source", base=middle)
        self.add("Direct", ("removed",))
        self.add("Item", functions=("removed",))
        self.add("FarAway", ("removed",))
        self.add("Unrelated", ("removed",))
        self.link("Source", "direct", "Direct")
        self.link("Direct", "items", "Item", "COLLECTION")
        self.link("Item", "far", "FarAway")
        self.link("Item", "cycle", "Source")
        note = self.note("Source", "removed")
        self.assertIn("Root.removed", note)
        self.assertIn("Direct.removed via Source.direct", note)
        self.assertIn("Item.removed via Source.direct.items[...]", note)
        self.assertNotIn("FarAway", note)
        self.assertNotIn("Unrelated", note)

    def test_sibling_names_share_a_stem_even_when_source_has_a_prefix(self):
        self.add("PoseBone")
        self.add("EditBone", ("select",))
        self.assertIn("EditBone.select", self.note("PoseBone", "select"))

    def test_collection_wrapper_functions_have_no_item_subscript(self):
        self.add("Object")
        self.add("Modifier")
        wrapper = self.add("ObjectModifiers", functions=("new",))
        self.link("Object", "modifiers", "Modifier", "COLLECTION")
        self.structs["Object"].properties["modifiers"].srna = wrapper
        self.assertIn("ObjectModifiers.new via Object.modifiers.", self.note("Object", "new"))
        self.assertNotIn("[...]", self.note("Object", "new"))

    def test_many_related_types_are_truncated_without_partial_paths(self):
        self.add("Bone")
        for i in range(20):
            self.add(f"Variant{i}Bone", ("select",))
        note = self.note("Bone", "select")
        self.assertEqual(note.count(".select"), 5)
        self.assertTrue(note.endswith(", ..."), note)
        self.assertLessEqual(len(note), 320)

    def test_curated_notes_win_without_building_the_index(self):
        self.add("Action")
        with mock.patch.object(api_docs, "_build_error_index", side_effect=AssertionError("unnecessary scan")):
            self.assertEqual(self.note("Action", "fcurves"), api_docs.NOTES["Action"])

    def test_index_is_built_once_and_warm_errors_do_not_scan_rna(self):
        self.bones()
        with mock.patch.object(api_docs, "_build_error_index", wraps=api_docs._build_error_index) as build:
            first = self.note("Bone", "select")
            # Drop live RNA members after warming: the index holds names and paths, not RNA handles.
            for struct in self.structs.values():
                struct.properties = None
                struct.functions = None
            self.assertEqual(self.note("Bone", "select"), first)
            self.assertIn("PoseBone.select", self.note("Object", "select"))
            build.assert_called_once_with()

    def test_no_note_for_unknown_types_or_non_attribute_errors(self):
        self.add("Bone", ("select",))
        for ex in (AttributeError("'Unknown' object has no attribute 'select'"),
                   TypeError("'Bone' object has no attribute 'select'"), AttributeError("select"),
                   AttributeError("'Bone' object has no attribute ''")):
            self.assertEqual(api_docs.error_note(ex), "")

    def test_diagnostic_failures_never_replace_the_original_exception(self):
        class BadMessage(Exception):
            def __str__(self):
                raise RuntimeError("broken message")

        self.assertEqual(api_docs.error_note(BadMessage()), "")
        self.add("Bone")
        with mock.patch.object(api_docs, "_build_error_index", side_effect=ReferenceError("RNA removed")):
            self.assertEqual(self.note("Bone", "select"), "")
        with mock.patch.object(api_docs, "note_for", side_effect=RuntimeError("RNA unavailable")):
            self.assertIn("AttributeError: 'Bone' object has no attribute 'select'",
                          tools._model_traceback(AttributeError("'Bone' object has no attribute 'select'")))


class LookupTest(unittest.TestCase):
    def test_a_type_an_add_on_left_out_of_bpy_types_is_found_through_its_property(self):
        # inspect_api('bpy.types.CyclesRenderSettings') said "No type" (4 eval steps, 2026-09-26/27).
        cycles = rna("CyclesRenderSettings")
        scene = rna("Scene", ID)
        scene.properties["cycles"] = types.SimpleNamespace(identifier="cycles", type="POINTER", fixed_type=cycles)
        fake = types.SimpleNamespace(Scene=types.SimpleNamespace(bl_rna=scene), ID=types.SimpleNamespace(bl_rna=ID))
        with mock.patch.object(api_docs.bpy, "types", fake, create=True):
            for path in ("bpy.types.CyclesRenderSettings", "CyclesRenderSettings"):
                lines = api_docs._resolve(path)
                self.assertIn("reach it as Scene.cycles", lines[0], path)
                self.assertEqual(lines[1], "bpy.types.CyclesRenderSettings", path)
            with self.assertRaisesRegex(api_docs.ApiError, "No type bpy.types.CyclesNoSuchSettings"):
                api_docs._resolve("bpy.types.CyclesNoSuchSettings")

    def test_methods_and_properties_defined_in_python_are_described_not_denied(self):
        # inspect_api said bpy.types.Mesh has no from_pydata (eval, 2026-09-27): RNA does not list it.
        class IDClass:
            bl_rna = ID

            def rename(self, name):
                """Inherited: not repeated on every ID type."""

        class Mesh(IDClass):
            bl_rna = rna("Mesh", ID, ("vertices",))

            def from_pydata(self, vertices, edges, faces, shade_flat=True):
                """Make a mesh from a list of vertices/edges/faces
                Until we have a nicer way to make geometry, use this."""

            @property
            def edge_keys(self):
                return []

        self.assertEqual(api_docs._walk_rna(Mesh, ["from_pydata"]), [
            "bpy.types.Mesh.from_pydata (defined in Python)",
            "from_pydata(vertices, edges, faces, shade_flat=True)  # Make a mesh from a list of vertices/edges/faces"])
        self.assertEqual(api_docs._walk_rna(Mesh, ["edge_keys"])[1], "edge_keys (read-only)")
        with self.assertRaisesRegex(api_docs.ApiError, "It has: vertices, rename, from_pydata, edge_keys$"):
            api_docs._walk_rna(Mesh, ["nope"])
        Mesh.bl_rna.properties.clear()  # _describe_rna's property lines need real RNA.
        fake = types.SimpleNamespace(ID=IDClass, Node=type("Node", (), {}))
        with mock.patch.object(api_docs.bpy, "types", fake, create=True):
            lines = api_docs._describe_rna(Mesh.bl_rna, Mesh)
        self.assertEqual(lines[1:4], ["defined in Python:",
                                      "  from_pydata(vertices, edges, faces, shade_flat=True)  # Make a mesh from a "
                                      "list of vertices/edges/faces", "  edge_keys (read-only)"])
        self.assertFalse(any("rename" in line for line in lines[:4]), lines)


class SearchTest(unittest.TestCase):
    INDEX = [("bpy.types.ActionLayers", "actionlayers collection of animation layers"),
             ("bpy.types.ActionChannelbag.fcurves", "actionchannelbag.fcurves f-curves the list of f-curves"),
             ("bpy.types.Action", "action a collection of f-curves for animation"),
             ("bpy.types.Mesh", "mesh mesh data-block")]

    def search(self, query: str) -> list[str]:
        with mock.patch.object(api_docs, "_registered_key", return_value="key"), \
                mock.patch.multiple(api_docs, _index=self.INDEX, _index_key="key"):
            return api_docs._search(query)

    def test_every_word_matching_is_unchanged(self):
        self.assertEqual(self.search("channelbag fcurves"), [
            "1 match(es) for 'channelbag fcurves'; pass one as `path` for details:",
            "  bpy.types.ActionChannelbag.fcurves"])

    def test_a_long_query_ranks_by_words_matched_instead_of_finding_nothing(self):
        # 'fcurves action layers animation' found nothing (eval, 2026-09-27).
        lines = self.search("fcurves action layers animation")
        self.assertIn("Nothing matches every word", lines[0])
        self.assertEqual(lines[1:], ["  bpy.types.ActionLayers (3 of 4 words)",  # Then words in the path.
                                     "  bpy.types.ActionChannelbag.fcurves (2 of 4 words)", "  bpy.types.Action (2 of 4 words)"])
        with mock.patch.object(api_docs, "MAX_PARTIAL_RESULTS", 1):
            self.assertEqual(len(self.search("fcurves action layers animation")), 2)
        self.assertIn("Nothing in bpy.types or bpy.ops matches", self.search("keyframe insert")[0])


if __name__ == "__main__":
    unittest.main()
