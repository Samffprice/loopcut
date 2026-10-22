"""Call-time operator facts with fake bpy; blender_op_notes.py measures real UVs."""

import contextlib
import importlib
import io
import runpy
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

from loopcut import api_docs, cost_guard, op_notes, tools, workspace  # noqa: E402

TEXT = op_notes.NOTES["uv.pack_islands"][0][1]
NOTICE = f"{op_notes.NOTICE_PREFIX} {TEXT}"


class Op:
    __doc__ = "Original operator documentation."
    bl_options = {"UNDO"}

    def __init__(self, name):
        self.name, self.calls, self.result, self.error = name, [], {"FINISHED"}, None

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.result

    def poll(self, *args):
        return args == ("EXEC_DEFAULT",)

    def get_rna_type(self):
        return types.SimpleNamespace(properties=[], description="Pack UV islands")

    def idname(self):
        module, name = self.name.split(".")
        return f"{module.upper()}_OT_{name}"

    def idname_py(self):
        return self.name


class Operators(unittest.TestCase):
    def setUp(self):
        self.module = types.ModuleType("bpy.ops")
        self.pack = Op("uv.pack_islands")
        self.other = Op("object.shade_smooth")
        self.real = lambda module, name: self.pack if (module, name) == ("uv", "pack_islands") else self.other
        self.module._op_create_function = self.real
        self.enterContext(mock.patch.dict(sys.modules, {"bpy.ops": self.module}))
        self.enterContext(mock.patch.object(op_notes, "_seen", set()))
        self.enterContext(mock.patch.object(op_notes, "_notices", []))

    def lookup(self, module="uv", name="pack_islands"):
        return self.module._op_create_function(module, name)

    def test_fraction_only_and_other_operators_are_the_original(self):
        with op_notes.watching_ops():
            for kwargs in ({}, {"margin_method": "SCALED"}, {"margin_method": "ADD"}):
                self.lookup()(**kwargs)
                self.assertEqual(op_notes.take(), [])
            self.assertIs(self.lookup("object", "shade_smooth"), self.other)
            self.lookup("object", "shade_smooth")(margin_method="FRACTION")
            self.assertEqual(op_notes.take(), [])
            self.lookup()(margin_method="FRACTION", margin=8 / 2048)
            self.assertEqual(op_notes.take(), [NOTICE])
        self.assertIs(self.module._op_create_function, self.real)

    def test_once_across_calls_contexts_take_and_module_reload(self):
        with op_notes.watching_ops():
            self.lookup()(margin_method="FRACTION")
            self.lookup()(margin_method="FRACTION")
        self.assertEqual(op_notes.take(), [NOTICE])
        self.assertEqual(op_notes.take(), [])
        importlib.reload(op_notes)
        with op_notes.watching_ops():
            self.lookup()(margin_method="FRACTION")
        self.assertEqual(op_notes.take(), [])

    def test_result_arguments_and_attributes_pass_through(self):
        with op_notes.watching_ops():
            proxy = self.lookup()
            result = proxy("EXEC_DEFAULT", False, margin_method="FRACTION", margin=0.01)
            self.assertIs(result, self.pack.result)
            self.assertEqual(self.pack.calls, [(("EXEC_DEFAULT", False), {"margin_method": "FRACTION", "margin": 0.01})])
            for attr in ("poll", "get_rna_type", "idname", "idname_py", "bl_options", "__doc__", "name"):
                self.assertEqual(getattr(proxy, attr), getattr(self.pack, attr), attr)
            self.assertTrue(proxy.poll("EXEC_DEFAULT"))
            self.assertEqual(proxy.get_rna_type(), self.pack.get_rna_type())
            self.assertEqual(proxy.idname(), "UV_OT_pack_islands")
            self.assertEqual(proxy.idname_py(), "uv.pack_islands")
            self.assertEqual(repr(proxy), repr(self.pack))
            self.assertEqual(str(proxy), str(self.pack))
            self.assertEqual(dir(proxy), dir(self.pack))

    def test_original_exception_is_preserved_and_hook_restores(self):
        error = self.pack.error = ValueError("operator error")
        with self.assertRaises(ValueError) as raised:
            with op_notes.watching_ops():
                self.lookup()(margin_method="FRACTION")
        self.assertIs(raised.exception, error)
        self.assertIs(self.module._op_create_function, self.real)
        self.assertEqual(op_notes.take(), [NOTICE])

    def test_raising_predicate_leaves_the_call_intact(self):
        predicate = mock.Mock(side_effect=RuntimeError("bad diagnostic"))
        with mock.patch.object(op_notes, "NOTES", {"uv.pack_islands": ((predicate, "broken note"),)}):
            with op_notes.watching_ops():
                self.assertIs(self.lookup()(margin_method="FRACTION"), self.pack.result)
        predicate.assert_called_once_with({"margin_method": "FRACTION"})
        self.assertEqual(op_notes.take(), [])

    def test_raising_output_stream_leaves_the_call_intact(self):
        restore = op_notes.install(echo=True)
        try:
            with mock.patch("builtins.print", side_effect=OSError("closed pipe")):
                self.assertIs(self.lookup()(margin_method="FRACTION"), self.pack.result)
            self.assertEqual(op_notes.take(), [NOTICE])
        finally:
            restore()

    def test_notes_are_once_per_fact_and_operator_table_is_generic(self):
        notes = {"object.shade_smooth": ((lambda kw: kw.get("first"), "first fact"),
                                         (lambda kw: kw.get("second"), "second fact"))}
        with mock.patch.object(op_notes, "NOTES", notes), op_notes.watching_ops():
            self.assertIs(self.lookup(), self.pack)
            for _ in range(2):
                self.lookup("object", "shade_smooth")(first=True, second=True)
        self.assertEqual(op_notes.take(), [f"{op_notes.NOTICE_PREFIX} first fact",
                                          f"{op_notes.NOTICE_PREFIX} second fact"])

    def test_saved_proxy_is_inert_after_live_scope(self):
        with op_notes.watching_ops():
            proxy = self.lookup()
        self.assertIs(proxy(margin_method="FRACTION"), self.pack.result)
        self.assertEqual(op_notes.take(), [])

    def test_cost_guard_composes_and_restores_in_both_orders_even_on_error(self):
        for notes_first in (False, True):
            for fail in (False, True):
                with self.subTest(notes_first=notes_first, fail=fail):
                    op_notes._seen.clear()
                    error = self.pack.error = RuntimeError("operator failed") if fail else None
                    outer, inner = ((op_notes.watching_ops, cost_guard.watching_ops) if notes_first else
                                    (cost_guard.watching_ops, op_notes.watching_ops))
                    with mock.patch.object(cost_guard, "check") as check:
                        with outer():
                            outer_factory = self.module._op_create_function
                            with contextlib.ExitStack() as stack:
                                if fail:
                                    raised = stack.enter_context(self.assertRaises(RuntimeError))
                                with inner():
                                    self.lookup()(margin_method="FRACTION")
                            if fail:
                                self.assertIs(raised.exception, error)
                            self.assertIs(self.module._op_create_function, outer_factory)
                        check.assert_called_once_with()
                    self.assertEqual(op_notes.take(), [NOTICE])
                    self.assertIs(self.module._op_create_function, self.real)
                    self.assertFalse(cost_guard._watching)

    def test_startup_file_echoes_immediately_once_without_package_imports(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            namespace = runpy.run_path(op_notes.__file__, run_name="__main__")
            self.lookup()(margin_method="FRACTION")
            self.assertEqual(output.getvalue().splitlines(), [NOTICE])
            self.lookup()(margin_method="FRACTION")
        self.assertEqual(output.getvalue().splitlines(), [NOTICE])
        self.assertEqual(namespace["take"](), [NOTICE])

    def test_inspect_api_shows_the_shared_text_without_recording_it(self):
        fake_ops = types.SimpleNamespace(uv=types.SimpleNamespace(pack_islands=self.pack),
                                         object=types.SimpleNamespace(shade_smooth=self.other))
        with mock.patch.object(api_docs.bpy, "ops", fake_ops, create=True):
            self.assertIn("NOTE: " + TEXT, api_docs.inspect_api("bpy.ops.uv.pack_islands"))
            self.assertNotIn(TEXT, api_docs.inspect_api("bpy.ops.object.shade_smooth"))
        self.assertEqual(op_notes.take(), [])

    def test_run_python_reports_once_after_clipping_and_restores_on_error(self):
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
                    (scene_diff, "for_model", "Scene changes: none"), (contact, "recheck", []),
                    (tools, "_model_traceback", "script error")):
                stack.enter_context(mock.patch.object(owner, name, return_value=value))
            fake_ops = types.SimpleNamespace(ed=types.SimpleNamespace(undo_push=lambda **kw: None),
                                             pack=lambda **kw: self.lookup()(**kw))
            stack.enter_context(mock.patch.object(tools.bpy, "ops", fake_ops, create=True))
            scaled = tools.run_python("bpy.ops.pack(margin_method='SCALED')")
            self.assertNotIn(NOTICE, scaled.text)
            first = tools.run_python("bpy.ops.pack(margin_method='FRACTION'); print('x' * 20000); raise ValueError()")
            self.assertFalse(first.ok)
            self.assertEqual(first.text.count(NOTICE), 1)
            self.assertIs(self.module._op_create_function, self.real)
            second = tools.run_python("bpy.ops.pack(margin_method='FRACTION')")
            self.assertTrue(second.ok)
            self.assertNotIn(NOTICE, second.text)
            self.assertIs(self.module._op_create_function, self.real)


class WorkspaceNotes(unittest.TestCase):
    def setUp(self):
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.ws / "logs").mkdir()
        (self.ws / "pack.py").write_text("# fixture", encoding="utf-8")
        self.enterContext(mock.patch.object(workspace, "_workspace", return_value=({}, self.ws)))
        self.enterContext(mock.patch.object(workspace, "_path", side_effect=lambda p: self.ws / p))
        self.enterContext(mock.patch.object(workspace, "shell_env", return_value={"BLENDER": "/fixture/Blender"}))
        self.enterContext(mock.patch.object(workspace, "_next_run", return_value=1))

    def test_run_blender_command_and_notices_outside_tail_or_clipped_tail(self):
        for output in (NOTICE + "\n" + "output\n" * 80, "output\n" + NOTICE + "\n",
                       "x" * 5000 + "\n" + NOTICE + "\n" + "x" * 5000):
            with self.subTest(length=len(output)):
                def run(command, ws, env, seconds, log, is_cancelled, label=""):
                    self.assertEqual(command, ["/fixture/Blender", "-b", "--factory-startup",
                                               str(self.ws / "source.blend"), "--python-exit-code", "1",
                                               "--python", str(workspace.SHAPE_KEY_GUARD),
                                               "--python", str(workspace.OP_NOTES),
                                               "--python", str(workspace.RENDER_PATH_GUARD),
                                               "--python", str(self.ws / "pack.py"), "--", "extra"])
                    log.write_text(output, encoding="utf-8")
                    return 0, 0.1, False

                with mock.patch.object(workspace, "run_process", side_effect=run):
                    result = workspace.run_blender("pack.py", args=["extra"]).then(lambda: False, lambda fn: fn())
                self.assertTrue(result.ok)
                self.assertEqual(result.text.count(NOTICE), 1)

    def test_posix_and_windows_shims_load_notes_before_user_arguments(self):
        with mock.patch.object(workspace, "_binary", return_value="/fixture/Blender"), \
                mock.patch.object(workspace, "_python", return_value="/fixture/python"):
            for platform, filename, arguments in (("posix", "blender", '"$@"'), ("nt", "blender.cmd", "%*")):
                with self.subTest(platform=platform), mock.patch.object(workspace.os, "name", platform):
                    workspace._shims(self.ws)
                text = (self.ws / ".loopcut" / "bin" / filename).read_text()
                self.assertIn(f'--python "{workspace.SHAPE_KEY_GUARD}" --python "{workspace.OP_NOTES}" '
                              f'--python "{workspace.RENDER_PATH_GUARD}" {arguments}', text)


if __name__ == "__main__":
    unittest.main()
