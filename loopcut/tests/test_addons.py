"""The pure half of addons: the one line that names installed add-ons to the model."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

from loopcut import addons  # noqa: E402


def entry(name, module, ops=(), description=""):
    return {"name": name, "module": module, "operators": list(ops), "description": description}


class LineTest(unittest.TestCase):
    def test_nothing_installed_is_no_line(self):
        self.assertEqual(addons.line([]), "")

    def test_operators_are_grouped_by_module_with_a_shared_prefix(self):
        line = addons.line([entry("Node Wrangler", "bl_ext.blender_org.node_wrangler",
                                  ["node.nw_add_textures", "node.nw_merge", "node.nw_swap", "wm.nw_import"],
                                  "Various tools to enhance and speed up node-based workflow")])
        self.assertEqual(line, "Installed add-ons: Node Wrangler [bl_ext.blender_org.node_wrangler] "
                               "bpy.ops.node.nw_* (3), bpy.ops.wm.nw_import: "
                               "Various tools to enhance and speed up node-based workflow")

    def test_a_prefix_is_cut_at_an_underscore_or_dropped(self):
        self.assertIn("bpy.ops.probe.say_* (2)", addons.line([entry("P", "p", ["probe.say_hi", "probe.say_bye"])]))
        self.assertIn("bpy.ops.probe.* (2)", addons.line([entry("P", "p", ["probe.alpha", "probe.beta"])]))

    def test_long_descriptions_and_long_lists_are_cut(self):
        long = "x" * 200
        self.assertIn("…", addons.line([entry("A", "a", description=long)]))
        many = [entry(f"Add-on {i:02d}", f"a{i}") for i in range(addons.MAX_LISTED + 4)]
        line = addons.line(many)
        self.assertIn("+4 more", line)
        self.assertNotIn("Add-on 13", line)

    def test_only_the_largest_operator_groups_are_shown(self):
        ops = [f"{m}.op_{i}" for m in ("a", "b", "c", "d", "e") for i in range(2)] + ["a.op_2"]
        line = addons.line([entry("X", "x", ops)])
        self.assertIn("bpy.ops.a.op_* (3)", line)
        self.assertIn("+2 more modules", line)
        self.assertNotIn("bpy.ops.e.", line)


if __name__ == "__main__":
    unittest.main()
