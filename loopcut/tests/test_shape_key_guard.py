"""Driven key removals, without Blender; opt-in subprocess checks exercise real RNA.

LOOPCUT_BLENDER_TESTS=1 python3 -m unittest discover -s loopcut/tests -p test_shape_key_guard.py
Set LOOPCUT_BLENDER to override the bundled binary. Each real case runs in its own process.
"""

import contextlib
import importlib
import io
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
fake_bpy = types.ModuleType("bpy")
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault("bpy", fake_bpy)

from loopcut import shape_key_guard as guard, tools  # noqa: E402


def driver_path(name, prop="value"):
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'key_blocks["{escaped}"].{prop}'


class Drivers(list):
    def __init__(self, paths):
        super().__init__(types.SimpleNamespace(data_path=path) for path in paths)
        self.events = []

    def new(self, path):
        self.events.append(("new", path))
        curve = types.SimpleNamespace(data_path=path)
        self.append(curve)
        return curve

    def remove(self, curve):
        self.events.append(("remove", curve.data_path))
        super().remove(curve)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.keys = []
        self.handlers = types.SimpleNamespace(depsgraph_update_pre=[], frame_change_pre=[], save_pre=[],
                                              load_post=[])
        self.enterContext(mock.patch.object(guard.bpy, "app", types.SimpleNamespace(handlers=self.handlers)))
        self.enterContext(mock.patch.object(guard.bpy, "data", types.SimpleNamespace(shape_keys=self.keys), create=True))
        guard._state.clear()
        guard.take()
        self.addCleanup(guard._state.clear)
        self.addCleanup(guard.take)
        self.addCleanup(setattr, guard, "_echo", False)

    def key(self, names=("smile",), uid=1):
        key = types.SimpleNamespace(session_uid=uid, name="Key", user=types.SimpleNamespace(name="Face", id_type="MESH"),
                                    key_blocks=[types.SimpleNamespace(name=n) for n in ("Basis", *names)],
                                    animation_data=types.SimpleNamespace(drivers=Drivers(map(driver_path, names))))
        self.keys.append(key)
        return key

    def remove(self, key, name):
        key.key_blocks[:] = [block for block in key.key_blocks if block.name != name]
        # Blender silently drops the curves itself: this is not driver_remove.
        key.animation_data.drivers[:] = [curve for curve in key.animation_data.drivers
                                         if curve.data_path != driver_path(name)]

    def assert_rebuilt_once(self, key):
        self.assertEqual(key.animation_data.drivers.events,
                         [("new", guard._REFRESH_PATH), ("remove", guard._REFRESH_PATH)])
        self.assertFalse(any(c.data_path == guard._REFRESH_PATH for c in key.animation_data.drivers))

    def test_removal_rebuilds_once_and_names_the_block_mesh_and_driver(self):
        key = self.key(("smile", "mouth_open"))
        guard.install()
        self.remove(key, "smile")
        guard.check()
        guard.check()
        self.assert_rebuilt_once(key)
        notices = guard.take()
        self.assertEqual(len(notices), 1)
        for words in (guard.NOTICE_PREFIX, '"smile"', 'shape keys "Key"', 'mesh "Face"', driver_path("smile")):
            self.assertIn(words, notices[0])
        self.assertEqual([c.data_path for c in key.animation_data.drivers], [driver_path("mouth_open")])
        self.assertEqual(guard.take(), [])

    def test_multiple_removed_blocks_and_drivers_make_one_notice_and_rebuild_per_key(self):
        key = self.key(("smile", "blink"))
        key.animation_data.drivers.append(types.SimpleNamespace(data_path=driver_path("smile", "slider_max")))
        guard.install()
        key.key_blocks[:] = key.key_blocks[:1]
        key.animation_data.drivers.clear()
        guard.check()
        self.assert_rebuilt_once(key)
        notices = guard.take()
        self.assertEqual(len(notices), 1)
        for path in (driver_path("smile"), driver_path("blink"), driver_path("smile", "slider_max")):
            self.assertIn(path, notices[0])

    def test_rename_does_not_rebuild_or_report_a_removal(self):
        key = self.key()
        guard.install()
        key.key_blocks[1].name = "happy"
        key.animation_data.drivers[0].data_path = driver_path("happy")
        guard.check()
        self.assertEqual(key.animation_data.drivers.events, [])
        self.assertEqual(guard.take(), [])

    def test_driver_removed_first_then_block_removed_is_quiet(self):
        key = self.key()
        guard.install()
        key.animation_data.drivers.clear()
        guard.check()
        self.assertEqual(guard.take(), [])
        self.remove(key, "smile")
        guard.check()
        self.assertEqual(guard.take(), [])

    def test_remove_and_readd_same_name_rebuilds_without_notice(self):
        key = self.key()
        guard.install()
        self.remove(key, "smile")
        key.key_blocks.append(types.SimpleNamespace(name="smile"))
        guard.check()
        self.assert_rebuilt_once(key)
        self.assertEqual(guard.take(), [])

    def test_escaped_quotes_backslashes_unicode_and_literal_escape_sequences(self):
        for name in ('a "b"', 'a \\"b"', r'face\new', 'sourire é', "two\nlines"):
            with self.subTest(name=name):
                key = self.key((name,), uid=len(self.keys) + 1)
                guard.check()
                self.remove(key, name)
                guard.check()
                self.assert_rebuilt_once(key)
                notices = guard.take()
                self.assertEqual(len(notices), 1)
                self.assertIn(driver_path(name).replace("\n", "\\n"), notices[0])
                self.assertEqual(len(notices[0].splitlines()), 1)

    def test_removed_key_is_forgotten_without_touching_its_drivers(self):
        key = self.key()
        guard.install()
        self.keys.clear()
        guard.check()
        self.assertEqual(key.animation_data.drivers.events, [])
        self.assertEqual(guard._state, {})
        self.assertEqual(guard.take(), [])

    def test_unchanged_counts_do_not_parse_paths_or_read_block_names(self):
        key = self.key()
        guard.install()
        with mock.patch.object(guard, "_driven") as driven:
            key.key_blocks[:] = [None, None]
            guard.check()
        driven.assert_not_called()
        self.assertEqual(key.animation_data.drivers.events, [])

    def test_install_twice_and_after_reload_replaces_only_guard_handlers(self):
        key = self.key()
        unrelated = lambda *_: None
        self.handlers.save_pre.append(unrelated)
        guard.install()
        previous = guard._loopcut_shape_key_guard
        guard.install()
        importlib.reload(guard)
        self.assertIsNot(previous, guard._loopcut_shape_key_guard)
        guard.install()
        for name in guard._HANDLERS:
            handlers = getattr(self.handlers, name)
            self.assertEqual(handlers.count(guard._loopcut_shape_key_guard), 1)
            self.assertNotIn(previous, handlers)
            self.assertTrue(hasattr(guard._loopcut_shape_key_guard, "_bpy_persistent"))
            self.assertEqual(len(handlers), 2 if name == "save_pre" else 1)
        self.assertIn(unrelated, self.handlers.save_pre)
        self.remove(key, "smile")
        guard.check()
        self.assert_rebuilt_once(key)

    def test_each_handler_checks_and_echo_flushes_the_notice(self):
        for name in guard._HANDLERS:
            with self.subTest(handler=name):
                key = self.key(uid=len(self.keys) + 1)
                guard.install(echo=True)
                self.remove(key, "smile")
                with mock.patch("builtins.print") as printed:
                    getattr(self.handlers, name)[0](None, None)
                notice, = guard.take()
                printed.assert_called_once_with(notice, flush=True)
                self.assert_rebuilt_once(key)

    def test_handler_exceptions_are_reported_and_swallowed(self):
        guard.install()
        with mock.patch.object(guard, "_read", side_effect=RuntimeError("guard bug")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.handlers.depsgraph_update_pre[0](None)
        self.assertIn("guard bug", errors.getvalue())
        self.assertFalse(guard._checking)

    def test_startup_file_does_not_raise_even_when_its_first_check_fails(self):
        for keys in ([], None):  # Empty factory startup, then a broken diagnostic read.
            with self.subTest(keys=keys), \
                    mock.patch.object(guard.bpy, "data", types.SimpleNamespace(shape_keys=keys)), \
                    mock.patch.dict(sys.modules, {"bpy": guard.bpy}), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                namespace = runpy.run_path(guard.__file__, run_name="__main__")
                self.assertFalse(namespace["_checking"])
                self.assertEqual(bool(errors.getvalue()), keys is None)
                for name in guard._HANDLERS:
                    self.assertEqual(len(getattr(self.handlers, name)), 1)

    def test_notice_failure_does_not_leave_a_throwaway_driver(self):
        key = self.key()
        guard.install()
        self.remove(key, "smile")
        with mock.patch.object(guard.json, "dumps", side_effect=RuntimeError("notice bug")), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            guard.check()
        self.assertIn("notice bug", errors.getvalue())
        self.assert_rebuilt_once(key)

    def test_refresh_path_collision_preserves_the_existing_driver(self):
        key = self.key()
        key.animation_data.drivers.append(types.SimpleNamespace(data_path=guard._REFRESH_PATH))
        guard.install()
        self.remove(key, "smile")
        guard.check()
        self.assertEqual(key.animation_data.drivers.events,
                         [("new", guard._REFRESH_PATH + "_"), ("remove", guard._REFRESH_PATH + "_")])
        self.assertEqual([c.data_path for c in key.animation_data.drivers], [guard._REFRESH_PATH])


class RunPythonTest(unittest.TestCase):
    def test_guard_runs_before_exec_and_before_snapshot_even_on_script_error(self):
        from loopcut import config, contact, cost_guard, scene_diff
        for fail in (False, True):
            with self.subTest(fail=fail), contextlib.ExitStack() as stack:
                events = []
                notice = 'Loopcut: key block "smile" was removed (key_blocks["smile"].value)'
                stack.enter_context(mock.patch.object(guard, "install", side_effect=lambda: events.append("install")))
                stack.enter_context(mock.patch.object(guard, "check", side_effect=lambda: events.append("check")))
                stack.enter_context(mock.patch.object(guard, "take", side_effect=lambda: events.append("take") or [notice]))
                stack.enter_context(mock.patch.object(tools, "_editor_context", return_value=contextlib.nullcontext()))
                stack.enter_context(mock.patch.object(tools.bpy, "ops", mock.Mock(), create=True))
                stack.enter_context(mock.patch.object(tools.bpy, "guard_test_exec", lambda: events.append("exec"), create=True))
                stack.enter_context(mock.patch.object(config, "run_timeout", return_value=60))
                stack.enter_context(mock.patch.object(cost_guard, "begin"))
                stack.enter_context(mock.patch.object(cost_guard, "watching_ops", return_value=contextlib.nullcontext()))
                stack.enter_context(mock.patch.object(cost_guard, "take", return_value=("", [])))
                stack.enter_context(mock.patch.object(scene_diff, "snapshot", side_effect=lambda **kw: events.append("snapshot") or {}))
                stack.enter_context(mock.patch.object(scene_diff, "diff", return_value={"other": []}))
                stack.enter_context(mock.patch.object(scene_diff, "for_model", return_value="Scene changes: none"))
                stack.enter_context(mock.patch.object(scene_diff, "is_empty", return_value=True))
                stack.enter_context(mock.patch.object(contact, "recheck", return_value=[]))
                stack.enter_context(mock.patch.object(tools, "_model_traceback", return_value="script error"))
                code = "bpy.guard_test_exec(); print('output')" + ("; raise ValueError('failed')" if fail else "")
                result = tools.run_python(code)
                self.assertEqual(result.ok, not fail)
                self.assertEqual(events, ["install", "snapshot", "exec", "check", "take", "snapshot"])
                self.assertLess(result.text.index("output"), result.text.index(notice))
                self.assertLess(result.text.index(notice), result.text.index("Scene changes"))


BLENDER = Path(os.environ.get("LOOPCUT_BLENDER") or REPO.parent / "tools/Blender.app/Contents/MacOS/Blender")
_SETUP = '''\
import bpy
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
bpy.ops.mesh.primitive_cube_add()
face = bpy.context.active_object
face.name = "Face"
face.data.name = "Face"
face.shape_key_add(name="Basis")
smile = face.shape_key_add(name="smile")
mouth = face.shape_key_add(name="mouth_open")
blink = face.shape_key_add(name="blink")
bpy.ops.object.empty_add()
ctrl = bpy.context.active_object
ctrl.name = "Ctrl"
for block in (smile, mouth, blink):
    curve = block.driver_add("value")
    curve.driver.type = "AVERAGE"
    variable = curve.driver.variables.new()
    variable.targets[0].id = ctrl
    variable.targets[0].data_path = "location[0]"
bpy.context.view_layer.update()
bpy.ops.wm.save_as_mainfile(filepath=start, copy=True)
# The standalone handlers must survive a file opened by the agent's script.
bpy.ops.wm.open_mainfile(filepath=start)
face = bpy.data.objects["Face"]
print("C15: removing driven key", flush=True)
'''
_VERIFY = '''\
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
bpy.ops.wm.open_mainfile(filepath=out)
keys = bpy.data.objects["Face"].data.shape_keys
assert [block.name for block in keys.key_blocks] == ["Basis", "mouth_open", "blink"]
paths = sorted(curve.data_path for curve in keys.animation_data.drivers)
assert paths == ['key_blocks["blink"].value', 'key_blocks["mouth_open"].value'], paths
for curve in keys.animation_data.drivers:
    assert curve.driver.type == "AVERAGE"
    target = curve.driver.variables[0].targets[0]
    assert target.id == bpy.data.objects["Ctrl"] and target.data_path == "location[0]"
print("C15: saved drivers intact", flush=True)
'''


@unittest.skipUnless(os.environ.get("LOOPCUT_BLENDER_TESTS") == "1" and BLENDER.is_file(),
                     "set LOOPCUT_BLENDER_TESTS=1 and provide a Blender binary")
class BlenderSubprocessTest(unittest.TestCase):
    def run_case(self, mode, guarded=False, run_python=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "case.py"
            code = f"start = {str(root / 'start.blend')!r}\nout = {str(root / 'out.blend')!r}\n"
            if run_python:
                code += (f"import sys\nsys.path.insert(0, {str(REPO / 'loopcut/harness')!r})\n"
                         "import checkout\ncheckout.use()\nfrom loopcut import tools\n")
            code += _SETUP
            removal = 'face = bpy.data.objects["Face"]; face.shape_key_remove(face.data.shape_keys.key_blocks["smile"])'
            if run_python:
                code += (f"result = tools.run_python({removal!r})\nprint(result.text, flush=True)\n"
                         "assert result.ok, result.text\n"
                         f"assert {guard.NOTICE_PREFIX!r} in result.text, result.text\n")
            else:
                code += removal + "\n"
                code += {"save": "bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)",
                         "update": "bpy.context.view_layer.update()",
                         "depsgraph": "bpy.context.evaluated_depsgraph_get()",
                         "frame": "bpy.context.scene.frame_set(2)"}[mode] + "\n"
            code += _VERIFY
            script.write_text(code, encoding="utf-8")
            command = [str(BLENDER), "-b", "--factory-startup", "--python-exit-code", "1"]
            if guarded:
                command += ["--python", str(Path(guard.__file__).resolve())]
            command += ["--python", str(script)]
            return subprocess.run(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=90, env={**os.environ, "LOOPCUT_DATA_DIR": str(root / "data")})

    def test_control_reproduces_the_stale_driver(self):
        result = self.run_case("save")
        self.assertIn("C15: removing driven key", result.stdout, "Blender must finish the fixture before failing")
        if result.returncode == 0 and "BKE_animsys_eval_driver" not in result.stdout:
            self.skipTest("this Blender does not reproduce the stale-driver bug")

    def test_standalone_guard_covers_each_evaluation_path_and_saved_file(self):
        for mode in ("save", "update", "depsgraph", "frame"):
            with self.subTest(mode=mode):
                result = self.run_case(mode, guarded=True)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertNotIn("BKE_animsys_eval_driver", result.stdout)
                self.assertIn(guard.NOTICE_PREFIX, result.stdout)
                self.assertIn("C15: saved drivers intact", result.stdout)

    def test_guard_in_run_blender_and_shim_argument_orders(self):
        # run_blender: <blend> --python <guard> --python <script>; the shim: --python <guard> <blend> ...
        # The script removes the driven block first thing and saves: no evaluation in between.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start, out = root / "start.blend", root / "out.blend"
            build = root / "build.py"
            build.write_text(f"start = {str(start)!r}\n" + _SETUP.split("# The standalone")[0], encoding="utf-8")
            made = subprocess.run([str(BLENDER), "-b", "--factory-startup", "--python-exit-code", "1",
                                   "--python", str(build)], cwd=root, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, timeout=90)
            self.assertEqual(made.returncode, 0, made.stdout)
            script = root / "remove.py"
            script.write_text(f"import bpy\nout = {str(out)!r}\n"
                              'face = bpy.data.objects["Face"]\n'
                              'face.shape_key_remove(face.data.shape_keys.key_blocks["smile"])\n'
                              + _VERIFY, encoding="utf-8")
            guard_file = str(Path(guard.__file__).resolve())
            orders = {"run_blender": ["-b", "--factory-startup", str(start), "--python-exit-code", "1",
                                      "--python", guard_file, "--python", str(script)],
                      "shim": ["--factory-startup", "--python-exit-code", "1", "--python", guard_file,
                               "--python", str(Path(guard.__file__).with_name("op_notes.py")),
                               "-b", str(start), "--python", str(script)]}
            for name, args in orders.items():
                with self.subTest(order=name):
                    result = subprocess.run([str(BLENDER), *args], cwd=root, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, timeout=90)
                    self.assertEqual(result.returncode, 0, result.stdout)
                    self.assertNotIn("BKE_animsys_eval_driver", result.stdout)
                    self.assertIn(guard.NOTICE_PREFIX, result.stdout)
                    self.assertIn("C15: saved drivers intact", result.stdout)

    def test_run_python_without_register_reports_removal_before_saving(self):
        result = self.run_case("save", run_python=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("BKE_animsys_eval_driver", result.stdout)
        self.assertIn(guard.NOTICE_PREFIX, result.stdout)
        self.assertIn("C15: saved drivers intact", result.stdout)


if __name__ == "__main__":
    unittest.main()
