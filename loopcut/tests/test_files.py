"""The file tools' rules and behaviour on a temp folder. No Blender needed: the image branch and
see_render need bpy and are covered by harness/tools_check.py."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))

fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import files, tools  # noqa: E402


class FileToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "notes.txt").write_text("one\ntwo\nthree\nfour\n")
        (self.project / "render.png").write_bytes(b"\x89PNG not really")
        (self.project / "textures").mkdir()
        (self.project / ".secret").write_text("key")
        os.environ["LOOPCUT_DATA_DIR"] = str(self.root / "data")

    # -- the gate

    def test_reads_inside_the_project_are_free_and_outside_ask(self):
        roots = (self.project,)
        self.assertFalse(files.needs_approval("read_file", {"path": str(self.project / "notes.txt")}, roots))
        self.assertFalse(files.needs_approval("list_files", {"path": str(self.project / "textures")}, roots))
        self.assertTrue(files.needs_approval("read_file", {"path": str(self.root / "other.txt")}, roots))
        self.assertTrue(files.needs_approval("list_files", {"path": "~"}, roots))
        self.assertTrue(files.needs_approval("read_file", {"path": ""}, roots), "unresolvable asks rather than runs")

    def test_writes_and_moves_always_ask(self):
        roots = (self.project,)
        self.assertTrue(files.needs_approval("write_file", {"path": str(self.project / "x.txt")}, roots))
        self.assertTrue(files.needs_approval("move_file", {"source": "a", "destination": "b"}, roots))
        self.assertFalse(files.needs_approval("see_render", {}, roots), "the last render is a read")
        self.assertFalse(files.needs_approval("capture_viewport", {}, roots))

    def test_renders_and_bakes_are_heavy(self):
        self.assertTrue(tools.is_heavy("see_render", {"render": True}))
        self.assertFalse(tools.is_heavy("see_render", {}))
        for code in ("bpy.ops.render.render(write_still=True)", "bpy.ops.object.bake(type='COMBINED')",
                     "bpy.ops.ptcache.bake_all()", "bpy.ops.fluid.bake_all()", "bpy.ops.object.voxel_remesh()"):
            self.assertTrue(tools.is_heavy("run_python", {"code": code}), code)
        for code in ("scene.render.resolution_x = 1920", "bpy.ops.mesh.primitive_cube_add()",
                     "scene.render.filepath = '//out/'", "bpy.ops.render.view_show()"):
            self.assertFalse(tools.is_heavy("run_python", {"code": code}), code)

    def test_hidden_and_loopcut_data_are_off_limits(self):
        with self.assertRaisesRegex(tools.ToolError, "hidden"):
            files.read_file(str(self.project / ".secret"))
        with self.assertRaisesRegex(tools.ToolError, "hidden"):
            files.list_files(str(self.root / ".ssh"))
        data = self.root / "data" / "conversations" / "abc"
        data.mkdir(parents=True)
        with self.assertRaisesRegex(tools.ToolError, "Loopcut's own data"):
            files.list_files(str(data))

    # -- the tools

    def test_list_files_shows_sizes_and_skips_hidden(self):
        text = files.list_files(str(self.project)).text
        self.assertIn("textures/", text)
        self.assertIn("notes.txt  19 B", text)
        self.assertNotIn(".secret", text)
        self.assertIn("(3 entries)", text)
        self.assertIn("(1 entries matching *.png)", files.list_files(str(self.project), "*.png").text)
        with self.assertRaisesRegex(tools.ToolError, "does not exist"):
            files.list_files(str(self.project / "nope"))

    def test_read_file_whole_and_by_lines(self):
        self.assertEqual(files.read_file(str(self.project / "notes.txt")).text,
                         f"{self.project / 'notes.txt'} (4 lines)\none\ntwo\nthree\nfour")
        self.assertEqual(files.read_file(str(self.project / "notes.txt"), [2, 3]).text,
                         f"{self.project / 'notes.txt'} (4 lines, showing 2-3)\ntwo\nthree")
        self.assertTrue(files.read_file(str(self.project / "notes.txt"), [3, 99]).text.endswith("showing 3-4)\nthree\nfour"))
        with self.assertRaisesRegex(tools.ToolError, "lines must be"):
            files.read_file(str(self.project / "notes.txt"), [3, 2])
        (self.project / "blob.bin").write_bytes(bytes(range(256)))
        with self.assertRaisesRegex(tools.ToolError, "not a text file"):
            files.read_file(str(self.project / "blob.bin"))
        with self.assertRaisesRegex(tools.ToolError, "use list_files"):
            files.read_file(str(self.project))

    def test_write_file_never_replaces_unless_told(self):
        target = self.project / "new.txt"
        self.assertIn("Wrote 5 B", files.write_file(str(target), "hello").text)
        with self.assertRaisesRegex(tools.ToolError, "already exists"):
            files.write_file(str(target), "again")
        files.write_file(str(target), "again", overwrite=True)
        self.assertEqual(target.read_text(), "again")
        with self.assertRaisesRegex(tools.ToolError, "does not exist"):
            files.write_file(str(self.project / "missing" / "x.txt"), "x")

    def test_move_and_copy_never_replace(self):
        source = self.project / "notes.txt"
        files.move_file(str(source), str(self.project / "textures"))
        self.assertTrue((self.project / "textures" / "notes.txt").is_file())
        self.assertFalse(source.exists())
        files.move_file(str(self.project / "textures" / "notes.txt"), str(source), copy=True)
        self.assertTrue(source.is_file() and (self.project / "textures" / "notes.txt").is_file())
        with self.assertRaisesRegex(tools.ToolError, "already exists"):
            files.move_file(str(source), str(self.project / "textures"))
        with self.assertRaisesRegex(tools.ToolError, "into itself"):
            files.move_file(str(self.project), str(self.project / "textures"))
        with self.assertRaisesRegex(tools.ToolError, "does not exist"):
            files.move_file(str(self.project / "gone.txt"), str(self.project / "a.txt"))

    def test_execute_dispatches_file_tools_and_describes_them(self):
        result = tools.execute("read_file", json.dumps({"path": str(self.project / "notes.txt")}))
        self.assertTrue(result.ok and result.text.endswith("four"))
        self.assertFalse(tools.execute("read_file", json.dumps({"path": str(self.project / "nope")})).ok)
        self.assertEqual(files.describe("move_file", {"source": "a", "destination": "b", "copy": True}), ("Copy a to b", ""))
        self.assertEqual(files.describe("write_file", {"path": "p", "content": "c"}), ("Write p", "c"))
        self.assertEqual(files.describe("see_render", {"render": True, "full": True}), ("Render the scene at full quality", ""))
        self.assertIsNone(files.describe("run_python", {}))


if __name__ == "__main__":
    unittest.main()
