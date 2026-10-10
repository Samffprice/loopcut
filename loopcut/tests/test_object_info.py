"""Support both geometry-node input APIs without treating Blender IDs as sequences; report ID properties safely."""

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import object_info

NS = types.SimpleNamespace


class ObjectInfoTest(unittest.TestCase):
    def setUp(self):
        class ID:
            name = "Asset"
            def __len__(self):
                raise AssertionError("an ID must be named, not iterated")
        self.ID = ID
        patcher = patch.object(object_info, "bpy", NS(types=NS(ID=ID)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ids_are_references(self):
        self.assertEqual(object_info._value(self.ID()), "ID 'Asset'")

    def test_new_geometry_node_inputs_include_an_editable_access_path(self):
        socket = NS(item_type="SOCKET", in_out="INPUT", identifier="Socket_2", name="Density")
        modifier = NS(name="GeometryNodes", type="NODES", bl_rna=NS(properties=[]),
                      node_group=NS(interface=NS(items_tree=[socket])),
                      properties=NS(inputs=NS(Socket_2=NS(value=0.4, type="ATTRIBUTE", attribute_name="density"))))
        with patch.object(object_info, "node_tree", return_value={}):
            result = object_info._modifier(modifier)
        self.assertEqual(result["inputs"]["Density"], {"identifier": "Socket_2", "value": 0.4,
                         "access": "properties.inputs.Socket_2.value", "type": "ATTRIBUTE", "attribute_name": "density"})

    def test_legacy_geometry_node_inputs_still_work(self):
        class Modifier(dict):
            name, type = "GeometryNodes", "NODES"
            bl_rna = NS(properties=[])
            node_group = NS(interface=NS(items_tree=[NS(item_type="SOCKET", in_out="INPUT",
                                                       identifier="Input_2", name="Size")]))
        with patch.object(object_info, "node_tree", return_value={}):
            result = object_info._modifier(Modifier(Input_2=2.0))
        self.assertEqual(result["inputs"]["Size"]["value"], 2.0)

    def test_a_modifier_without_id_properties_does_not_fail_the_call(self):
        # Blender 5 keeps a modifier's ID properties as system properties, so keys() raises. A node
        # group whose generated properties are not built has no properties.inputs either, and took
        # the old keys() path: "Bad arguments for get_object_info: bpy_struct.keys(): this type
        # doesn't support IDProperties" (evals 20260919-200031 scatter, 20260921-121938 perfume_ad).
        class Modifier:
            name, type = "GeometryNodes", "NODES"
            bl_rna = NS(properties=[])
            properties = NS()  # NodesModifierPropertiesEmpty: no inputs.
            node_group = NS(interface=NS(items_tree=[NS(item_type="SOCKET", in_out="INPUT",
                                                       identifier="Socket_2", name="Count")]))

            def keys(self):
                raise TypeError("bpy_struct.keys(): this type doesn't support IDProperties")
        with patch.object(object_info, "node_tree", return_value={}):
            result = object_info._modifier(Modifier())
        self.assertEqual(result["inputs"], {})

    def test_custom_properties_of_every_kind_are_reported_as_plain_short_data(self):
        class Group:  # IDPropertyGroup
            def to_dict(self):
                return {"size": 2.5, "tags": ["a", "b"], "nested": {"depth": 2}}

        class Array:  # IDPropertyArray
            def to_list(self):
                return [0.123456, 1.0, 2.0]

        class Odd:
            def __repr__(self):
                return "<odd value>"

        class Broken:
            def to_dict(self):
                raise RuntimeError("freed")

            def __repr__(self):
                return "<broken group>"

        class Object(dict):
            data, type, animation_data = None, "EMPTY", None
            users_collection, children, modifiers, constraints, material_slots = [], [], [], [], []
            bl_rna = NS(properties=[NS(identifier="cycles")])  # Registered by an add-on: not the user's.

        obj = Object(count=3, label="hero", scale=0.333333, flag=True, settings=Group(), weights=Array(),
                     target=self.ID(), odd=Odd(), broken=Broken(), notes="x" * 1000, _RNA_UI=Group(), cycles=Group())
        custom = object_info.describe(obj)["custom_properties"]
        self.assertEqual(custom["count"], 3)
        self.assertEqual(custom["label"], "hero")
        self.assertEqual(custom["scale"], 0.3333)
        self.assertIs(custom["flag"], True)
        self.assertEqual(custom["settings"], {"size": 2.5, "tags": ["a", "b"], "nested": {"depth": 2}})
        self.assertEqual(custom["weights"], [0.1235, 1.0, 2.0])
        self.assertEqual(custom["target"], "ID 'Asset'")
        self.assertEqual(custom["odd"], "<odd value>")
        self.assertEqual(custom["broken"], "<broken group>")
        self.assertTrue(custom["notes"].startswith('"xxx'))
        self.assertIn("(1002 chars; read obj['notes'] for all of it)", custom["notes"])
        self.assertLess(len(custom["notes"]), object_info.MAX_PROPERTY_CHARS + 60)
        self.assertNotIn("_RNA_UI", custom)
        self.assertNotIn("cycles", custom)


    def test_matrices_and_vectors_are_plain_json_numbers(self):
        # get_object_info on a BlenderKit lamp's wire (hook modifier) or anything with a Child Of
        # constraint: both hold a 4x4 matrix, which read row by row as Vectors that round() refuses.
        from test_scene_diff import Matrix, Vector, hook_modifier, prop

        child_of = NS(name="Child Of", type="CHILD_OF", target=None, influence=1.0,
                      bl_rna=NS(properties=[prop("inverse_matrix", "FLOAT", 16), prop("influence", "FLOAT")]),
                      inverse_matrix=Matrix((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 1, 0.123456), (0, 0, 0, 1)))

        class Object(dict):
            data, type, animation_data = None, "CURVE", None
            users_collection, children, material_slots = [], [], []
            modifiers, constraints = [hook_modifier()], [child_of]
            bl_rna = NS(properties=[])

        info = object_info.describe(Object())
        self.assertEqual(info["modifiers"][0]["matrix_inverse"][2], (0.0, 0.0, 1.0, -1.309))
        self.assertEqual(info["constraints"][0]["inverse_matrix"][2], (0.0, 0.0, 1.0, 0.1235))
        json.dumps(info)  # No default=: every value is plain data.
        self.assertEqual(object_info._value(Matrix((1.000004, 0), (0, 1))), [(1.0, 0.0), (0.0, 1.0)])
        self.assertEqual(object_info._value(Vector(0.123456, 1, 2)), [0.1235, 1.0, 2.0])

class ExecuteTest(unittest.TestCase):
    """A TypeError raised inside a tool is the tool's failure, not the model's arguments."""

    def test_arguments_that_do_not_fit_are_bad_arguments(self):
        from loopcut import tools
        result = tools.execute("get_object_info", json.dumps({"nmes": ["Cube"]}))
        self.assertFalse(result.ok)
        self.assertTrue(result.text.startswith("Bad arguments for get_object_info: "), result.text)

    def test_a_type_error_inside_the_tool_is_reported_as_the_tools(self):
        from loopcut import tools

        def get_object_info(names):
            raise TypeError("bpy_struct.keys(): this type doesn't support IDProperties")
        with patch.dict(tools._DISPATCH, {"get_object_info": get_object_info}):
            result = tools.execute("get_object_info", json.dumps({"names": ["ScatterPlane"]}))
        self.assertFalse(result.ok)
        self.assertEqual(result.text, "get_object_info failed inside the tool, not because of your arguments: "
                                      "TypeError: bpy_struct.keys(): this type doesn't support IDProperties")


class NodeReadoutTest(unittest.TestCase):
    """Two inputs of one name (Mix Shader's Shader, Shader) show their index, so a swap is visible."""
    def setUp(self):
        patcher = patch.object(object_info, "bpy", NS(types=NS(ID=type("ID", (), {}))))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def socket(name, identifier, is_output=False, **extra):
        return NS(name=name, identifier=identifier, is_output=is_output, enabled=True, is_linked=False, **extra)

    def tree(self, first="Diffuse", second="Glossy"):
        mix_inputs = [self.socket("Fac", "Fac", default_value=0.3), self.socket("Shader", "Shader"),
                      self.socket("Shader", "Shader_001")]
        node = lambda name, inputs=(), outputs=(): NS(name=name, bl_idname="ShaderNode", label="", mute=False,
                                                      inputs=list(inputs), outputs=list(outputs), node_tree=None)
        diffuse = node("Diffuse", outputs=[self.socket("BSDF", "BSDF", True)])
        glossy = node("Glossy", outputs=[self.socket("BSDF", "BSDF", True)])
        mix = node("Mix Shader", mix_inputs, [self.socket("Shader", "Shader", True)])
        found = {"Diffuse": diffuse, "Glossy": glossy}
        links = [NS(from_node=found[first], from_socket=found[first].outputs[0], to_node=mix, to_socket=mix_inputs[1]),
                 NS(from_node=found[second], from_socket=found[second].outputs[0], to_node=mix, to_socket=mix_inputs[2])]
        return NS(nodes=[diffuse, glossy, mix], links=links)

    def test_links_to_a_repeated_input_name_its_index(self):
        with patch.object(object_info.scene_diff, "rna_values", return_value={}):
            tree = object_info.node_tree(self.tree())
            swapped = object_info.node_tree(self.tree("Glossy", "Diffuse"))
        self.assertEqual(tree["links"], ["Diffuse.BSDF -> Mix Shader.Shader (inputs[1])",
                                         "Glossy.BSDF -> Mix Shader.Shader (inputs[2])"])
        self.assertNotEqual(sorted(tree["links"]), sorted(swapped["links"]), "a swap shows")
        self.assertEqual(tree["nodes"][2]["inputs"], {"Fac": 0.3}, "shader sockets hold no value")
        # A Mix node in colour mode: its enabled A is inputs[6], not the float A that inputs["A"] finds.
        inputs = [self.socket("A", f"A_{kind}", default_value=value) for kind, value in (("Float", 0.5), ("Color", (1, 0, 0, 1)))]
        inputs[0].enabled = False
        mix = NS(name="Mix", bl_idname="ShaderNodeMix", label="", mute=False, inputs=inputs, outputs=[], node_tree=None)
        with patch.object(object_info.scene_diff, "rna_values", return_value={}):
            read = object_info.node_tree(NS(nodes=[mix], links=[]))
        self.assertEqual(read["nodes"][0]["inputs"], {"A (inputs[1])": [1, 0, 0, 1]})

    def test_a_name_used_once_stays_plain(self):
        sockets = [self.socket("Color", "Color"), self.socket("Strength", "Strength")]
        self.assertEqual(object_info.scene_diff.socket_label(sockets[1], sockets), "Strength")
        outputs = [self.socket("Result", "Result_Float", True), self.socket("Result", "Result_Vector", True)]
        self.assertEqual(object_info.scene_diff.socket_label(outputs[1], outputs), "Result (outputs[1])")

    def test_a_world_reads_like_a_material(self):
        with patch.object(object_info.scene_diff, "rna_values", return_value={}):
            world = object_info.describe_world(NS(name="World", users=1, node_tree=self.tree()), True)
            plain = object_info.describe_world(NS(name="Flat", users=0, node_tree=None, color=(0.05, 0.05, 0.05)))
        self.assertEqual((world["type"], world["scene_world"]), ("WORLD", True))
        self.assertEqual(len(world["links"]), 2)
        self.assertEqual(plain, {"name": "Flat", "type": "WORLD", "users": 0, "color": [0.05, 0.05, 0.05]})


class FitTest(unittest.TestCase):
    """A result too long for the model loses whole sections, largest first and named, never half a value."""
    def setUp(self):
        patcher = patch.object(object_info, "bpy", NS(types=NS(ID=type("ID", (), {}))))
        patcher.start()
        self.addCleanup(patcher.stop)

    def entry(self, name="Bottle", materials=(5000, 3000, 200), animation=1500):
        return {"name": name, "type": "MESH", "location": [0, 0, 0], "bounds": {"min": [0, 0, 0], "max": [1, 1, 1]},
                "materials": [{"name": f"M{size}", "nodes": ["x" * size], "links": []} for size in materials],
                "animation": {"action": "Spin", "fcurves": ["y" * animation]}, "mesh": {"verts": 8}}

    def test_what_fits_is_left_whole(self):
        entries = [self.entry(materials=(10,), animation=10)]
        text, left_out = object_info.fit(entries, 8000)
        self.assertEqual(json.loads(text), entries)
        self.assertEqual(left_out, [])

    def test_the_largest_sections_go_first_and_are_named(self):
        text, left_out = object_info.fit([self.entry()], 5500)
        self.assertLessEqual(len(text), 5500)
        shown = json.loads(text)[0]
        self.assertEqual([m["name"] for m in shown["materials"]], ["M3000", "M200"])
        self.assertEqual(shown["materials"][0]["nodes"], ["x" * 3000], "what stays is whole")
        self.assertEqual(shown["animation"]["fcurves"], ["y" * 1500])
        self.assertEqual(shown["omitted"], ["material M5000 (5,040 chars)"])
        self.assertEqual(left_out, shown["omitted"])
        text, _ = object_info.fit([self.entry()], 1000)
        shown = json.loads(text)[0]
        self.assertEqual(set(shown), {"name", "type", "location", "bounds", "materials", "mesh", "omitted"})
        self.assertEqual(len(shown["omitted"]), 3, shown["omitted"])

    def test_whole_entries_go_from_the_end_when_their_cores_do_not_fit(self):
        entries = [{"name": f"Part{n}", "type": "MESH", "children": [f"c{k}" for k in range(80)]} for n in range(5)]
        text, left_out = object_info.fit(entries, 1500)
        shown = json.loads(text)
        self.assertLessEqual(len(text), 1500)
        self.assertEqual(shown[-1], {"omitted": f"{', '.join(e['name'] for e in entries[len(shown) - 1:])}: ask for them alone"})
        self.assertEqual([e["name"] for e in shown[:-1]], [f"Part{n}" for n in range(len(shown) - 1)])

    def test_brief_is_the_layout_and_the_slot_names(self):
        obj = NS(dimensions=(1.0, 2.0, 0.5), children=[NS(name="Cap")], type="MESH",
                 material_slots=[NS(material=NS(name="Glass")), NS(material=None)],
                 data=NS(vertices=[0] * 8, edges=[0] * 12, polygons=[0] * 6))
        self.assertEqual(object_info.brief(obj), {"dimensions": [1.0, 2.0, 0.5], "children": ["Cap"],
                                                  "material_slots": ["Glass", ""],
                                                  "mesh": {"verts": 8, "edges": 12, "faces": 6}})

    def test_the_tool_takes_detail_and_says_what_it_left_out(self):
        from loopcut import tools
        obj = NS(dimensions=(1.0, 1.0, 1.0), children=[], type="EMPTY", material_slots=[])
        summary = {"name": "Big", "type": "EMPTY", "location": [0, 0, 0], "materials": ["Glass"], "mesh": {}}
        with patch.object(tools, "bpy", NS(data=NS(objects={"Big": obj}, worlds={}), context=NS(scene=NS(world=None)))), \
                patch.object(tools, "object_summary", return_value=summary), \
                patch.object(object_info, "describe", return_value={"materials": [{"name": "Glass", "nodes": ["n" * 9000]}]}):
            brief = tools.get_object_info(["Big"], detail="transform")
            full = tools.get_object_info(["Big"])
            with self.assertRaises(tools.ToolError):
                tools.get_object_info(["Big"], detail="nodes")
        self.assertEqual(json.loads(brief.text), [{"name": "Big", "type": "EMPTY", "location": [0, 0, 0],
                                                   "dimensions": [1.0, 1.0, 1.0], "children": []}])
        body, _, note = full.text.partition("\nLeft out")
        self.assertEqual(json.loads(body)[0]["omitted"], ["material Glass (9,029 chars)"])
        self.assertIn("run_python", note)
        properties = tools.SCHEMAS[2]["function"]["parameters"]["properties"]
        self.assertEqual(properties["detail"]["enum"], ["full", "transform"])


if __name__ == "__main__":
    unittest.main()
