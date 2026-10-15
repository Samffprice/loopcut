"""Workspace render paths with fake bpy and intercepted process launches only."""

import contextlib
import io
import os
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ADDONS = Path(__file__).resolve().parents[2] / "scripts" / "addons_core"
PRELUDE = ADDONS / "loopcut" / "render_path_guard.py"
sys.path.insert(0, str(ADDONS))
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import workspace  # noqa: E402


class RenderPaths(unittest.TestCase):
    def setUp(self):
        self.scene = types.SimpleNamespace(render=types.SimpleNamespace(filepath="image.png"))
        self.bpy = types.ModuleType("bpy")
        self.bpy.app = types.SimpleNamespace(background=True)
        self.bpy.context = types.SimpleNamespace(scene=self.scene)
        self.bpy.data = types.SimpleNamespace(scenes={})
        self.ops = types.ModuleType("bpy.ops")
        self.seen = []

        def render(*args, **kwargs):
            scene = self.bpy.data.scenes.get(kwargs.get("scene"), self.bpy.context.scene)
            self.seen.append(scene.render.filepath)
            return {"FINISHED"}

        self.operator = mock.Mock(side_effect=render)
        self.other = mock.Mock()
        self.factory = lambda module, name: (self.operator if (module, name) == ("render", "render")
                                             else self.other)
        self.ops._op_create_function = self.factory
        render_ops = types.ModuleType("bpy.ops.render")
        render_ops.__getattr__ = lambda name: self.ops._op_create_function("render", name)
        self.bpy.ops = types.SimpleNamespace(render=render_ops)
        self.enterContext(mock.patch.dict(sys.modules, {"bpy": self.bpy, "bpy.ops": self.ops}))
        self.cwd = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch("os.getcwd", return_value=str(self.cwd)))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def start(self):
        # --python must work without importing the Loopcut package.
        with mock.patch.dict(sys.modules, {"loopcut": None}):
            return runpy.run_path(str(PRELUDE), run_name="__main__")

    def test_bare_path_is_absolute_before_operator(self):
        self.start()
        result = self.bpy.ops.render.render("EXEC_DEFAULT", False, write_still=True)
        self.assertEqual(self.seen, [str(self.cwd / "image.png")])
        self.assertEqual(result, {"FINISHED"})
        self.operator.assert_called_once_with("EXEC_DEFAULT", False, write_still=True)
        self.assertTrue(self.output.getvalue().startswith("Loopcut note:"))

    def test_relative_subdirectory_uses_current_directory_at_call_time(self):
        self.start()
        self.scene.render.filepath = "images/image.png"
        with mock.patch("os.getcwd", return_value=str(self.cwd / "nested")):
            self.bpy.ops.render.render(write_still=True)
        self.assertEqual(self.seen, [str(self.cwd / "nested/images/image.png")])

    def test_absolute_blend_relative_and_empty_paths_are_unchanged(self):
        self.start()
        for path in (str(self.cwd / "image.png"), "//image.png", "//images/image.png", ""):
            with self.subTest(path=path):
                self.scene.render.filepath = path
                self.bpy.ops.render.render(write_still=True)
                self.assertEqual(self.seen[-1], path)
        self.assertEqual(self.output.getvalue(), "")

    def test_relative_directory_keeps_its_trailing_separator(self):
        self.start()
        self.scene.render.filepath = "images" + os.sep
        self.bpy.ops.render.render(write_still=True)
        self.assertEqual(self.seen, [str(self.cwd / "images") + os.sep])

    def test_only_still_writes_are_changed(self):
        self.start()
        for kwargs in ({}, {"write_still": False}, {"animation": True, "write_still": True},
                       {"use_sequencer_scene": True, "write_still": True}):
            with self.subTest(kwargs=kwargs):
                self.bpy.ops.render.render(**kwargs)
                self.assertEqual(self.seen[-1], "image.png")
        self.assertIs(self.ops._op_create_function("render", "opengl"), self.other)
        self.assertIs(self.ops._op_create_function("object", "delete"), self.other)
        self.assertEqual(self.output.getvalue(), "")

    def test_named_scene_does_not_change_context_scene(self):
        self.start()
        other_scene = types.SimpleNamespace(render=types.SimpleNamespace(filepath="other.png"))
        self.bpy.data.scenes["other"] = other_scene
        self.bpy.ops.render.render(write_still=True, scene="other")
        self.assertEqual(self.seen, [str(self.cwd / "other.png")])
        self.assertEqual(self.scene.render.filepath, "image.png")

    def test_current_scene_is_read_at_call_time(self):
        self.start()
        self.bpy.context.scene = types.SimpleNamespace(render=types.SimpleNamespace(filepath="new.png"))
        self.bpy.ops.render.render(write_still=True)
        self.assertEqual(self.seen, [str(self.cwd / "new.png")])
        self.assertEqual(self.scene.render.filepath, "image.png")

    def test_gui_does_not_install_or_change_paths(self):
        self.bpy.app.background = False
        self.start()
        self.assertIs(self.ops._op_create_function, self.factory)
        self.bpy.ops.render.render(write_still=True)
        self.assertEqual(self.seen, ["image.png"])
        self.assertEqual(self.output.getvalue(), "")

    def test_notice_is_once_even_if_prelude_is_run_again(self):
        self.start()
        for path in ("image.png", "other.png"):
            self.start()
            self.scene.render.filepath = path
            self.bpy.ops.render.render(write_still=True)
            self.assertEqual(self.seen[-1], str(self.cwd / path))
        self.assertEqual(len(self.output.getvalue().splitlines()), 1)

    def test_resolution_and_notice_failures_do_not_replace_operator_result(self):
        self.start()
        with mock.patch("os.getcwd", side_effect=OSError("unavailable directory")):
            self.assertEqual(self.bpy.ops.render.render(write_still=True), {"FINISHED"})
        self.assertEqual(self.seen, ["image.png"])
        with mock.patch("builtins.print", side_effect=OSError("closed stream")):
            self.assertEqual(self.bpy.ops.render.render(write_still=True), {"FINISHED"})
        self.assertEqual(self.seen[-1], str(self.cwd / "image.png"))

    def test_operator_error_and_attributes_are_preserved(self):
        self.start()
        self.assertIs(self.bpy.ops.render.render.poll, self.operator.poll)
        error = RuntimeError("render failed")
        self.operator.side_effect = error
        with self.assertRaises(RuntimeError) as raised:
            self.bpy.ops.render.render(write_still=True)
        self.assertIs(raised.exception, error)

    def test_missing_operator_factory_is_harmless(self):
        del self.ops._op_create_function
        self.start()
        self.assertFalse(hasattr(self.ops, "_op_create_function"))


class WorkspaceLaunches(unittest.TestCase):
    def setUp(self):
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.ws / "logs").mkdir()
        (self.ws / ".loopcut/tmp").mkdir(parents=True)
        (self.ws / "script.py").write_text("# fixture", encoding="utf-8")
        self.enterContext(mock.patch.object(workspace, "_workspace", return_value=({}, self.ws)))
        self.enterContext(mock.patch.object(workspace, "_path", side_effect=lambda p: self.ws / p))
        self.enterContext(mock.patch.object(workspace, "shell_env", return_value={"BLENDER": "/fixture/Blender"}))
        self.enterContext(mock.patch.object(workspace, "_next_run", return_value=1))

    def test_run_blender_loads_prelude_before_script_with_workspace_cwd(self):
        def run(command, cwd, env, seconds, log, is_cancelled, label=""):
            self.assertEqual(label, "script.py")
            self.assertEqual(cwd, self.ws)
            self.assertIn(str(PRELUDE), command)
            index = command.index(str(PRELUDE))
            self.assertEqual(command[index - 1], "--python")
            self.assertLess(index, command.index(str(self.ws / "script.py")))
            log.write_text("Loopcut note: render path resolved.\n" + "output\n" * 80, encoding="utf-8")
            return 0, 0.1, False

        with mock.patch.object(workspace, "run_process", side_effect=run):
            result = workspace.run_blender("script.py", blend="").then(lambda: False, lambda fn: fn())
        self.assertTrue(result.ok)
        self.assertEqual(result.text.count("Loopcut note: render path resolved."), 1)

    def test_both_shell_shims_load_prelude_before_user_arguments(self):
        with mock.patch.object(workspace, "_binary", return_value="/fixture/Blender"), \
                mock.patch.object(workspace, "_python", return_value="/fixture/python"):
            for platform, filename, arguments in (("posix", "blender", '"$@"'), ("nt", "blender.cmd", "%*")):
                with self.subTest(platform=platform), mock.patch.object(workspace.os, "name", platform):
                    workspace._shims(self.ws)
                shim = (self.ws / ".loopcut/bin" / filename).read_text()
                self.assertIn(f'--python "{PRELUDE}"', shim)
                self.assertLess(shim.index(str(PRELUDE)), shim.index(arguments))

    def test_shell_starts_in_workspace(self):
        def run(command, cwd, env, seconds, log, is_cancelled, label=""):
            self.assertEqual(label, "blender -b")
            self.assertEqual(cwd, self.ws)
            log.write_text("", encoding="utf-8")
            return 0, 0.1, False

        with mock.patch.object(workspace, "run_process", side_effect=run):
            result = workspace.shell("blender -b").then(lambda: False, lambda fn: fn())
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
