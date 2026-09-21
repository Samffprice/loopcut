"""Support both geometry-node input APIs without treating Blender IDs as sequences."""

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


if __name__ == "__main__":
    unittest.main()
