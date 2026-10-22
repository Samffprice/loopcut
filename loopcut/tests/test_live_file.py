"""C28: nothing the model's code runs moves the user's open file. Stand-in operators here follow Blender's
rules (a Save As without copy rebinds the open file; open_mainfile replaces it); the real thing is
loopcut/harness/guard_check.py's "live file" case."""

import contextlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import cost_guard, live_file, op_notes, tools  # noqa: E402


class Blender:
    """The open file and the files written, with the file operators' rules."""

    def __init__(self, filepath=""):
        self.filepath, self.written, self.calls = filepath, [], []

    def create(self, module, name):
        blender, idname = self, f"{module}.{name}"

        def op(*args, **kwargs):
            blender.calls.append((idname, args, kwargs))
            path = os.path.abspath(kwargs["filepath"]) if kwargs.get("filepath") else blender.filepath
            if idname in ("wm.save_as_mainfile", "wm.save_mainfile"):
                if not path:
                    raise RuntimeError("Unable to save an unsaved file with an empty filepath")
                blender.written.append(path)
                if not kwargs.get("copy"):
                    blender.filepath = path  # Blender's Save As: the open file is now the one written.
            elif idname in live_file.REPLACES:
                blender.filepath = path if idname == "wm.open_mainfile" else ""
            return {"FINISHED"}
        op.idname_py = lambda: idname
        return op


class _Submodule:
    """bpy.ops.<module>: every attribute is a lookup through _op_create_function, as in Blender."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, op):
        return sys.modules["bpy.ops"]._op_create_function(self.name, op)


def model_code(code: str) -> None:
    """Runs code the way run_python runs the model's: bpy.ops looked up while it runs."""
    exec(compile(code, "<loopcut>", "exec"), {"bpy": types.SimpleNamespace(ops=_ops())})


def _ops():
    return types.SimpleNamespace(wm=_Submodule("wm"), object=_Submodule("object"), ed=_Submodule("ed"))


class LiveFileTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.user_file = str(self.home / "Projects" / "mug.blend")
        self.workspace = self.home / "datafiles" / "loopcut" / "workspaces" / "91184bb6"
        self.blender = Blender(self.user_file)
        self.module = types.ModuleType("bpy.ops")
        self.module._op_create_function = self.real = self.blender.create
        self.enterContext(mock.patch.dict(sys.modules, {"bpy.ops": self.module}))
        self.enterContext(mock.patch.object(live_file, "_current", lambda: self.blender.filepath))
        self.enterContext(mock.patch.object(live_file, "_notices", []))

    def run_model(self, code: str) -> None:
        with live_file.watching_ops():
            model_code(code)

    def test_a_save_as_into_the_workspace_leaves_the_users_open_file_where_it_was(self):
        target = self.workspace / "coffee-table.blend"
        self.run_model(f"bpy.ops.wm.save_as_mainfile(filepath={str(target)!r})")
        self.assertEqual(self.blender.filepath, self.user_file, "Cmd+S must still write the user's file")
        self.assertEqual(self.blender.written, [str(target)], "the agent still gets its file")
        self.assertEqual(self.blender.calls[0][2], {"filepath": str(target), "copy": True})
        self.assertEqual(live_file.take(), [f"{live_file.NOTICE_PREFIX} saved coffee-table.blend as a copy; the "
                                            f"user's open file is still {self.user_file}."])

    def test_an_explicit_copy_false_and_a_relative_name_are_copies_too(self):
        cwd = os.getcwd()
        os.chdir(self.home)
        self.addCleanup(os.chdir, cwd)
        self.run_model("bpy.ops.wm.save_as_mainfile('EXEC_DEFAULT', filepath='stone_room.blend', copy=False, "
                       "compress=True)")
        self.assertEqual(self.blender.filepath, self.user_file)
        self.assertEqual(self.blender.calls[0][1:], (("EXEC_DEFAULT",),
                                                     {"filepath": "stone_room.blend", "copy": True, "compress": True}))

    def test_an_unsaved_scene_stays_unsaved(self):
        self.blender.filepath = ""
        self.run_model(f"bpy.ops.wm.save_as_mainfile(filepath={str(self.workspace / 'x.blend')!r})")
        self.assertEqual(self.blender.filepath, "")
        self.assertIn("still unsaved, as it was.", live_file.take()[0])

    def test_save_mainfile_to_another_path_is_a_save_as_copy_without_exit(self):
        target = str(self.workspace / "preview.blend")
        self.run_model(f"bpy.ops.wm.save_mainfile(filepath={target!r}, compress=True, exit=True)")
        self.assertEqual(self.blender.filepath, self.user_file)
        self.assertEqual(self.blender.calls, [("wm.save_as_mainfile", (), {"filepath": target, "compress": True,
                                                                           "copy": True})])
        self.assertEqual(len(live_file.take()), 1)

    def test_saving_the_users_own_file_where_it_is_runs_as_written(self):
        self.run_model(f"bpy.ops.wm.save_as_mainfile(filepath={self.user_file!r})\nbpy.ops.wm.save_mainfile()")
        self.assertEqual(self.blender.filepath, self.user_file)
        self.assertEqual([call[2] for call in self.blender.calls], [{"filepath": self.user_file}, {}])
        self.assertEqual(live_file.take(), [])

    def test_loading_another_file_in_the_users_place_is_refused_and_says_what_to_do(self):
        for op in ("open_mainfile(filepath='/tmp/other.blend')", "revert_mainfile()", "read_homefile()",
                   "read_factory_settings()", "recover_last_session()", "recover_auto_save(filepath='a.blend')",
                   "save_mainfile(incremental=True)"):
            with self.subTest(op=op), self.assertRaises(RuntimeError) as caught:
                self.run_model(f"bpy.ops.wm.{op}")
            self.assertIn("Nothing was changed", str(caught.exception))
            self.assertIn("bpy.data.libraries.load", str(caught.exception))
            self.assertIn("copy=True", str(caught.exception))
        self.assertEqual(self.blender.calls, [])
        self.assertEqual(self.blender.filepath, self.user_file)

    def test_the_users_own_save_as_is_untouched_and_the_hook_restores_even_on_error(self):
        with self.assertRaises(ValueError), live_file.watching_ops():
            raise ValueError("model bug")
        self.assertIs(self.module._op_create_function, self.real)
        mine = str(self.home / "Projects" / "mug-v2.blend")
        model_code(f"bpy.ops.wm.save_as_mainfile(filepath={mine!r})")  # The user's Save As, outside the run.
        self.assertEqual(self.blender.filepath, mine)

    def test_an_operator_the_code_keeps_stays_guarded_and_others_are_themselves(self):
        with live_file.watching_ops():
            kept = self.module._op_create_function("wm", "save_as_mainfile")
            other = self.module._op_create_function("object", "select_all")
        self.assertEqual(kept.idname_py(), "wm.save_as_mainfile")
        self.assertIsNot(type(other), live_file._KeptOp)
        kept(filepath=str(self.workspace / "later.blend"))  # A timer the model registered, say.
        self.assertEqual(self.blender.filepath, self.user_file)

    def test_run_python_keeps_the_open_file_and_tells_the_model(self):
        from loopcut import config, contact, scene_diff, shape_key_guard
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(tools, "_python_namespace", side_effect=lambda: {"bpy": tools.bpy}))
            for owner, name, value in (
                    (tools, "_editor_context", contextlib.nullcontext()),
                    (config, "run_timeout", 60), (cost_guard, "begin", None),
                    (cost_guard, "check", None), (cost_guard, "take", ("", [])),
                    (shape_key_guard, "install", None), (shape_key_guard, "check", None),
                    (shape_key_guard, "take", []), (scene_diff, "snapshot", {}),
                    (scene_diff, "diff", {"other": []}), (scene_diff, "is_empty", True),
                    (scene_diff, "for_model", "Scene changes: none"), (contact, "recheck", [])):
                stack.enter_context(mock.patch.object(owner, name, return_value=value))
            stack.enter_context(mock.patch.object(op_notes, "_notices", []))
            ops = _ops()
            ops.ed = types.SimpleNamespace(undo_push=lambda **kw: None)
            stack.enter_context(mock.patch.object(tools.bpy, "ops", ops, create=True))
            target = str(self.workspace / "stone_room.blend")
            result = tools.run_python(f"bpy.ops.wm.save_as_mainfile(filepath={target!r})")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.blender.filepath, self.user_file)
        self.assertEqual(self.blender.written, [target])
        self.assertIn("saved stone_room.blend as a copy; the user's open file is still", result.text)
        self.assertIs(self.module._op_create_function, self.real)


if __name__ == "__main__":
    unittest.main()
