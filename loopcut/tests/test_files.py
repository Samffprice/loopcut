"""The file tools' rules and behaviour on a temp folder. Image I/O is mocked here;
harness/tools_check.py covers it with Blender."""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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
        for code in ("bpy.ops.render.opengl(write_still=True)", "bpy.ops.object.bake(type='COMBINED')",
                     "bpy.ops.ptcache.bake_all()", "bpy.ops.fluid.bake_all()", "bpy.ops.object.voxel_remesh()"):
            self.assertTrue(tools.is_heavy("run_python", {"code": code}), code)
        for code in ("scene.render.resolution_x = 1920", "bpy.ops.mesh.primitive_cube_add()",
                     "scene.render.filepath = '//out/'", "bpy.ops.render.view_show()",
                     "bpy.ops.render.render(write_still=True)"):  # Refused by the cost guard, so not asked about.
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

    def test_temp_folders_in_a_hidden_folder_are_open_but_hidden_names_in_them_are_not(self):
        # Evals set TMPDIR to out/evals/<run>/.<task>-xxxx: every inspect_scene failed on Blender's
        # temp folder being "hidden", and so did reading it (perfume_ad, 2026-09-27).
        system_temp = self.root / ".perfume_ad-kzzck2rf"
        blender_temp = system_temp / "blender_NSmG7j"
        custom_temp = self.root / ".config" / "blender-temp"  # Blender's temp folder set elsewhere.
        for folder in (blender_temp, custom_temp):
            folder.mkdir(parents=True)
            (folder / "check.txt").write_text("rendered")
            (folder / ".token").write_text("key")
        for temp in (blender_temp, custom_temp):
            bpy = types.SimpleNamespace(app=types.SimpleNamespace(tempdir=f"{temp}/"))
            with patch.object(files.tempfile, "gettempdir", return_value=str(system_temp)), \
                    patch.dict(sys.modules, {"bpy": bpy}):
                self.assertEqual(files.check(files.resolve(str(temp))), temp)  # What inspect_scene asks.
                self.assertIn("check.txt", files.list_files(str(temp)).text)
                self.assertIn("rendered", files.read_file(str(temp / "check.txt")).text)
                for path in (temp / ".token", self.project / ".secret", self.root / ".ssh", self.root / ".config"):
                    with self.assertRaisesRegex(tools.ToolError, "hidden", msg=path):
                        files.read_file(str(path))

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

    def test_image_file_and_render_results_declare_file_origin(self):
        from loopcut import attachments
        with patch.object(attachments, "_to_png", return_value=(640, 480)), \
                patch.object(tools.bpy, "context", types.SimpleNamespace(scene=object()), create=True), \
                patch.object(files, "_last_render", return_value="last render"), \
                patch.object(files, "_render_now", return_value="render"), \
                patch.object(files.cost_guard, "render_refusal", return_value=""):
            for result in (files.read_file(str(self.project / "render.png")),
                           files.see_render(), files.see_render(render=True)):
                self.assertIsNotNone(result.image_path)
                self.assertTrue(result.file_image)

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



class RelativePathTest(unittest.TestCase):
    """A relative path lands next to the .blend, never in Blender's working directory. During evals
    that is the harness folder in the git checkout, and render_job output_dir="product_reveal_final/"
    put a job folder in blender/loopcut/harness/evals/ (2026-09-27)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project, self.cwd, self.system_temp = (self.root / n for n in ("project", "checkout", "tmp"))
        for folder in (self.project, self.cwd, self.system_temp):
            folder.mkdir()
        previous = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)
        temp = patch.object(files.tempfile, "gettempdir", return_value=str(self.system_temp))
        temp.start()
        self.addCleanup(temp.stop)
        os.environ["LOOPCUT_DATA_DIR"] = str(self.root / "data")

    def blender(self, blend="", render_output="/tmp/"):
        scene = types.SimpleNamespace(render=types.SimpleNamespace(filepath=render_output))
        bpy = types.SimpleNamespace(data=types.SimpleNamespace(filepath=blend),
                                    context=types.SimpleNamespace(scene=scene),
                                    path=types.SimpleNamespace(abspath=lambda p: p), app=types.SimpleNamespace(tempdir=""))
        return patch.dict(sys.modules, {"bpy": bpy})

    def test_saved_relative_and_double_slash_paths_are_next_to_the_blend(self):
        with self.blender(blend=str(self.project / "shot.blend")):
            for raw in ("product_reveal_final/", "//product_reveal_final", "./product_reveal_final"):
                self.assertEqual(files.resolve(raw), self.project / "product_reveal_final", raw)
            self.assertEqual(files.resolve("//../elsewhere"), self.root / "elsewhere")
            self.assertEqual(files.relative_note("renders"), "", "the .blend's folder needs no note")
            self.assertEqual(files.project_roots()[0], self.project)
        self.assertNotIn(self.cwd, files.resolve("x", base=self.project).parents)

    def test_absolute_and_home_paths_are_as_written(self):
        with self.blender(blend=str(self.project / "shot.blend")):
            self.assertEqual(files.resolve(str(self.root / "out")), self.root / "out")
            self.assertEqual(files.resolve("~/renders"), Path.home().resolve() / "renders")
            self.assertEqual(files.relative_note(str(self.root / "out"), "~/renders"), "")

    def test_unsaved_goes_under_the_render_output_folder_and_says_so(self):
        output = self.root / "renders"
        output.mkdir()
        for render_output in (f"{output}/", str(output), f"{output}/shot_"):
            with self.blender(render_output=render_output):
                self.assertEqual(files.resolve("final/"), output / "final", render_output)
                self.assertEqual(files.resolve("//final"), output / "final")
                note = files.relative_note("final/")
                self.assertIn("not saved", note)
                self.assertIn(f"the render output folder, {output}", note)

    def test_unsaved_without_an_absolute_render_output_uses_a_loopcut_temp_folder(self):
        for render_output in ("", "//render/", "render/"):
            with self.blender(render_output=render_output):
                self.assertEqual(files.resolve("final"), self.system_temp / "Loopcut" / "final", render_output)
                self.assertIn(str(self.system_temp / "Loopcut"), files.relative_note("final"))
                self.assertEqual(files.project_roots()[0], self.system_temp / "Loopcut")

    def test_file_tools_write_relative_paths_under_the_base_and_note_it_when_unsaved(self):
        output = self.root / "renders"
        output.mkdir()
        with self.blender(render_output=f"{output}/"):
            result = files.write_file("notes.txt", "hello")
            self.assertTrue((output / "notes.txt").is_file())
            self.assertFalse((self.cwd / "notes.txt").exists())
            self.assertIn(f"to {output / 'notes.txt'}.", result.text)
            self.assertIn("not saved", result.text)
            self.assertIn("not saved", files.list_files(".").text)
        with self.blender(blend=str(self.project / "shot.blend")):
            self.assertEqual(files.write_file("notes.txt", "hi").text, f"Wrote 2 B to {self.project / 'notes.txt'}.")

    def test_the_gate_resolves_relative_reads_against_the_first_root_without_blender(self):
        with patch.dict(sys.modules, {"bpy": None}):  # Off the main thread: bpy must not be read.
            roots = (self.project,)
            self.assertFalse(files.needs_approval("read_file", {"path": "notes.txt"}, roots))
            self.assertFalse(files.needs_approval("list_files", {"path": "//textures"}, roots))
            self.assertTrue(files.needs_approval("read_file", {"path": "../other.txt"}, roots))
            self.assertTrue(files.needs_approval("read_file", {"path": "notes.txt"}, ()))


if __name__ == "__main__":
    unittest.main()
