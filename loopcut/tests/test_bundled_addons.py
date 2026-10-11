"""Session activation and startup repair without Blender or a real user config directory."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/addons_core"))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import addons, api_docs, bundled_addons as bundled  # noqa: E402


class BundledTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.system = self.root / "extensions/system"
        self.system.mkdir(parents=True)
        self.manifest = self.system.parent / "bundled.json"
        pins = json.loads((ROOT / "release/extensions/bundled.json").read_text())
        self.manifest.write_text(json.dumps(pins))
        for p in pins['packages']:
            directory = self.system / p['id']
            directory.mkdir()
            (directory / "blender_manifest.toml").write_text("")
        self.record = self.root / "config" / bundled.RECORD_FILE
        self.prefs = types.SimpleNamespace(addons={}, is_dirty=False, extensions=types.SimpleNamespace(repos=[
            types.SimpleNamespace(source='SYSTEM', module='system', directory=str(self.system))]))
        self.bpy = types.ModuleType("bpy")
        self.bpy.context = types.SimpleNamespace(preferences=self.prefs)
        self.bpy.app = types.SimpleNamespace(background=False)
        self.bpy.types = types.SimpleNamespace()
        self.bpy.ops = types.SimpleNamespace(mesh=types.SimpleNamespace(), uv=types.SimpleNamespace(),
                                             node=types.SimpleNamespace(), object=types.SimpleNamespace(),
                                             mpfb=types.SimpleNamespace())
        self.bpy.utils = types.SimpleNamespace(system_resource=lambda _: str(self.root / "core"))
        self.bl_ext = types.ModuleType("bl_ext")
        self.bl_ext.system = types.ModuleType("bl_ext.system")
        self.modules = {}
        for p in pins['packages']:
            module = types.ModuleType("bl_ext.system." + p['id'])
            module.__addon_enabled__ = False
            module.__file__ = str(self.system / p['id'] / "__init__.py")
            module.bl_info = {"name": p['name']}
            self.modules[module.__name__] = module
            setattr(self.bl_ext.system, p['id'], module)
        self.utils = types.SimpleNamespace(
            check=lambda name: (name in self.prefs.addons, self.modules[name].__addon_enabled__),
            enable=mock.Mock(side_effect=self.enable), disable=mock.Mock(side_effect=self.disable),
            modules=lambda **_: list(self.modules.values()), module_bl_info=lambda m: m.bl_info)
        for patcher in (
            mock.patch.dict(sys.modules, {"bpy": self.bpy, "addon_utils": self.utils,
                                          "bl_ext": self.bl_ext, "bl_ext.system": self.bl_ext.system, **self.modules}),
            mock.patch.object(api_docs, "bpy", self.bpy),
            mock.patch.object(bundled.credentials, "folder", return_value=self.record.parent),
            mock.patch.object(bundled, "_session_enabled", set()),
            mock.patch.object(addons, "_cache", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def enable(self, module, *, default_set=False, **kwargs):
        self.modules[module].__addon_enabled__ = True
        if default_set:
            self.prefs.addons[module] = object()
            self.prefs.is_dirty = True
        operator = types.SimpleNamespace(get_rna_type=lambda: types.SimpleNamespace(properties=[], description="Live operator"))
        self.bpy.ops.mesh.looptools_relax = operator
        return self.modules[module]

    def disable(self, module, *, default_set=False, **kwargs):
        self.modules[module].__addon_enabled__ = False
        if default_set:
            self.prefs.addons.pop(module, None)
            self.prefs.is_dirty = True

    def package(self, package_id="looptools"):
        return next(p for p in bundled.packages() if p['id'] == package_id)

    def journal(self, *ids):
        bundled._write_record(self.record, {"bl_ext.system." + p for p in ids})

    def test_catalog_only_uses_system_manifest_and_live_state(self):
        packages = bundled.packages()
        self.assertEqual(len(packages), 5)
        self.assertTrue(all(p['present'] and not p['enabled'] for p in packages))
        (self.system / "looptools/blender_manifest.toml").unlink()
        self.assertFalse(self.package()['present'])
        self.enable("bl_ext.system.mpfb", default_set=True)
        self.assertTrue(self.package('mpfb')['enabled'])
        self.manifest.unlink()
        self.assertEqual(bundled.packages(), [])

    def test_stock_or_user_repo_has_no_bundles(self):
        self.prefs.extensions.repos[0].source = 'USER'
        self.assertEqual(bundled.packages(), [])
        self.prefs.extensions.repos = []
        self.assertEqual(bundled.packages(), [])

    def test_invalid_catalog_rejected_without_enabling(self):
        for payload in ([], {"schema": "loopcut.bundled-extensions/1", "packages": [{"id": "../evil", "name": "Bad"}]}):
            self.manifest.write_text(json.dumps(payload))
            with self.assertRaises(bundled.BundledAddonError):
                bundled.packages()
        self.utils.enable.assert_not_called()

    def test_hints_are_short_open_capabilities(self):
        for package in bundled.packages():
            self.assertIn(package['id'], bundled.hint(package))
            self.assertNotIn('\n', bundled.hint(package))
            self.assertLess(len(bundled.hint(package)), 150)
        self.assertIn('interactive Blender only', bundled.hint(self.package('univ')))
        self.assertIn('HumanService', bundled.hint(self.package('mpfb')))

    def test_package_id_and_module_path_describe_enabled_module(self):
        for path in ('looptools', 'bl_ext.system.looptools'):
            with self.subTest(path=path):
                self.disable('bl_ext.system.looptools', default_set=True)
                self.prefs.is_dirty = False
                text = api_docs.inspect_api(path=path)
                self.assertTrue(text.startswith('Turned on the bundled LoopTools add-on for this session.'))
                self.assertIn('bl_ext.system.looptools', text)
                self.assertIn('mesh.looptools_*', text)
                self.assertIn('A background Blender (run_blender, blender -b) enables it separately', text)
                self.assertIn('addon_utils.enable("bl_ext.system.looptools", default_set=True)', text)
                self.assertIn('not saved on background exit', text)
                self.assertIn('explicit save_userpref() writes them', text)
                self.assertFalse(self.prefs.is_dirty)
                self.assertEqual(bundled._read_record(self.record), {'bl_ext.system.looptools'})
                self.assertTrue(self.utils.enable.call_args.kwargs['default_set'])

    def test_workspace_and_codex_inspection_use_the_live_dispatch_without_scene_changes(self):
        from loopcut import agent, codex_app, config, conversations, llm, state, tools
        arguments = json.dumps({'path': 'looptools'})
        cfg = config.Config('', '', 'test', '', False, True, 3, 200_000)
        for codex in (False, True):
            with self.subTest(codex=codex):
                self.disable('bl_ext.system.looptools', default_set=True)
                self.utils.enable.reset_mock()
                self.prefs.is_dirty = False
                session, turn = state.new_session(), agent.Turn()
                session['mode'] = 'workspace'
                session['messages'] = [{'role': 'user', 'content': 'Describe LoopTools.'}]
                turn.config = cfg
                checkpoint = mock.Mock()
                replies = [llm.Completion(tool_calls=[llm.ToolCall('api', 'inspect_api', arguments)]),
                           llm.Completion(text='Described.')]
                with mock.patch.object(agent, '_on_main', side_effect=lambda fn, *args, **kwargs: fn()) as main, \
                        mock.patch.object(agent, '_redraw'), mock.patch.object(conversations, 'save'), \
                        mock.patch.object(agent, '_workspace_turn'), \
                        mock.patch.object(agent, '_request', side_effect=replies), \
                        mock.patch.object(tools, 'execute', wraps=tools.execute) as execute:
                    if codex:
                        driver = codex_app.CodexTurn(session, turn, cfg, None, self.root, None,
                                                     agent._run_tool_on_main, checkpoint)
                        reply = driver.tool_call({'tool': 'inspect_api', 'arguments': json.loads(arguments),
                                                  'callId': 'api'})
                        self.assertTrue(reply['success'], reply)
                    else:
                        agent.run_loop(session, turn, agent.WORKSPACE_SPEC, ensure_checkpoint=checkpoint)
                        self.assertEqual(turn.outcome, 'completed')
                output = next(m['content'] for m in session['messages'] if m['role'] == 'tool')
                self.assertIn('Turned on the bundled LoopTools add-on for this session.', output)
                self.assertIn('bl_ext.system.looptools', output)
                self.utils.enable.assert_called_once()
                self.assertTrue(self.utils.enable.call_args.kwargs['default_set'])
                self.assertFalse(self.prefs.is_dirty)
                main.assert_called_once()
                execute.assert_called_once_with('inspect_api', arguments)
                checkpoint.assert_not_called()
                self.assertIsNone(turn.scene_before)
                self.assertIsNone(turn.scene_after)
                self.assertFalse(any(i['kind'] == 'changes' for i in session['items']))

    def test_univ_interactive_notice_names_only_the_live_blender(self):
        text = api_docs.inspect_api(path='univ')
        self.assertIn('Turned on the bundled UniV add-on for this session.', text)
        self.assertIn('UniV works only in the live Blender (run_python).', text)
        self.assertNotIn('addon_utils.enable', text)

    def test_operator_paths_enable_then_describe_real_rna(self):
        for path in ('mesh.looptools_relax', 'bpy.ops.mesh.looptools_relax'):
            self.disable('bl_ext.system.looptools', default_set=True)
            text = api_docs.inspect_api(path=path)
            self.assertIn('bpy.ops.mesh.looptools_relax()', text)
            self.assertIn('Live operator', text)
            self.assertTrue(self.prefs.is_dirty)  # Preserve an already-dirty state too.

    def test_unknown_and_similar_prefix_paths_do_not_enable(self):
        for path in ('other', 'looptools.evil', 'mesh.looptoolsx_relax', 'bl_ext.other.looptools',
                     'mesh.looptools_relax()', 'bpy.ops.mesh.looptools_relax.extra'):
            with self.subTest(path=path):
                self.assertIsNone(bundled.matching_path(path)[0])
        with mock.patch.object(api_docs, '_resolve', return_value=['ordinary API']):
            self.assertEqual(api_docs.inspect_api(path='Object'), 'ordinary API')
        self.utils.enable.assert_not_called()
        self.assertFalse(self.record.exists())

    def test_already_on_is_not_recorded_or_touched(self):
        self.enable('bl_ext.system.looptools', default_set=True)
        self.prefs.is_dirty = False
        text = api_docs.inspect_api(path='looptools')
        self.assertNotIn('Turned on', text)
        self.assertFalse(self.prefs.is_dirty)
        self.assertFalse(self.record.exists())
        self.utils.enable.assert_not_called()

    def test_user_preference_that_failed_to_load_is_never_recorded(self):
        self.prefs.addons['bl_ext.system.looptools'] = object()
        api_docs.inspect_api(path='looptools')
        self.assertFalse(self.record.exists())
        self.assertFalse(self.prefs.is_dirty)

    def test_background_enables_without_reading_or_writing_record(self):
        self.bpy.app.background = True
        with mock.patch.object(bundled.credentials, 'folder', side_effect=AssertionError('background config access')):
            text = api_docs.inspect_api(path='looptools')
            bundled.startup_cleanup()
        self.assertIn('Turned on the bundled LoopTools add-on for this session.', text)
        self.assertNotIn('A background Blender', text)  # A worker's child: it is on where it runs.
        self.assertFalse(self.record.exists())
        self.assertFalse(self.prefs.is_dirty)

    def test_univ_background_explains_without_enabling_or_resolving(self):
        self.bpy.app.background = True
        for path in ('univ', 'bl_ext.system.univ', 'bpy.ops.uv.univ_home', 'uv.univ_home'):
            self.assertEqual(api_docs.inspect_api(path=path),
                             'UniV registers only in an interactive Blender; it is not available here.')
        self.utils.enable.assert_not_called()
        self.assertFalse(self.record.exists())

    def test_failed_enable_rolls_back_prefs_and_restores_dirty(self):
        def failure(module, **kwargs):
            self.enable(module, default_set=True)
            kwargs['handle_error'](RuntimeError('upstream registration failed'))
        self.utils.enable.side_effect = failure
        with self.assertRaises(api_docs.ApiError):
            api_docs.inspect_api(path='looptools')
        self.assertFalse(self.prefs.is_dirty)
        self.assertNotIn('bl_ext.system.looptools', self.prefs.addons)
        self.assertFalse(self.package()['enabled'])
        self.assertIn('bl_ext.system.looptools', bundled._read_record(self.record))

    def test_unwritable_record_refuses_activation(self):
        with mock.patch.object(bundled, '_write_record', side_effect=OSError('cannot write journal')):
            with self.assertRaisesRegex(api_docs.ApiError, 'cannot write journal'):
                api_docs.inspect_api(path='looptools')
        self.utils.enable.assert_not_called()
        self.assertFalse(self.prefs.is_dirty)

    def test_missing_package_is_not_activated(self):
        (self.system / 'looptools/blender_manifest.toml').unlink()
        with self.assertRaisesRegex(api_docs.ApiError, 'missing from the System'):
            api_docs.inspect_api(path='looptools')
        self.utils.enable.assert_not_called()

    def test_search_miss_mentions_bundle_without_enabling(self):
        with mock.patch.object(api_docs, '_registered_key', return_value=()), \
                mock.patch.object(api_docs, '_index', []), mock.patch.object(api_docs, '_index_key', ()):
            text = api_docs.inspect_api(search='mesh cleanup')
            self.assertIn('LoopTools [looptools]', text)
            self.assertNotIn('Bool Tool', text)
        self.utils.enable.assert_not_called()
        self.assertFalse(self.record.exists())
        self.assertFalse(self.prefs.is_dirty)

    def test_search_hit_does_not_advertise_off_bundles(self):
        with mock.patch.object(api_docs, '_registered_key', return_value=()), \
                mock.patch.object(api_docs, '_index', [('bpy.types.Mesh', 'mesh cleanup')]), \
                mock.patch.object(api_docs, '_index_key', ()):
            self.assertNotIn('Bundled', api_docs.inspect_api(search='mesh cleanup'))

    def test_startup_disables_recorded_saved_entry_and_keeps_record(self):
        self.journal('looptools', 'mpfb')
        self.enable('bl_ext.system.looptools', default_set=True)
        self.enable('bl_ext.system.bool_tool', default_set=True)  # The user's own add-on.
        self.prefs.is_dirty = False
        bundled.startup_cleanup()
        self.assertFalse(self.package()['enabled'])
        self.assertNotIn('bl_ext.system.looptools', self.prefs.addons)
        self.assertTrue(self.prefs.is_dirty)
        self.assertEqual(bundled._read_record(self.record), {'bl_ext.system.looptools'})
        self.assertTrue(self.package('bool_tool')['enabled'])
        self.utils.disable.assert_called_once()
        self.prefs.is_dirty = False
        bundled.startup_cleanup()  # A later startup observes the saved repair.
        self.assertEqual(bundled._read_record(self.record), set())
        self.assertFalse(self.prefs.is_dirty)

    def test_startup_removes_recorded_pref_even_if_package_is_missing(self):
        self.journal('looptools')
        self.prefs.addons['bl_ext.system.looptools'] = object()
        self.manifest.unlink()
        bundled.startup_cleanup()
        self.assertNotIn('bl_ext.system.looptools', self.prefs.addons)
        self.assertTrue(self.prefs.is_dirty)
        self.assertIn('bl_ext.system.looptools', bundled._read_record(self.record))

    def test_same_session_callback_does_not_disable_its_own_activation(self):
        api_docs.inspect_api(path='looptools')
        bundled.startup_cleanup()
        self.assertTrue(self.package()['enabled'])
        self.utils.disable.assert_not_called()

    def test_cleanup_failure_keeps_journal_for_retry(self):
        self.journal('looptools')
        self.enable('bl_ext.system.looptools', default_set=True)
        self.utils.disable.side_effect = bundled.BundledAddonError('unregister failed')
        with self.assertRaises(bundled.BundledAddonError):
            bundled.startup_cleanup()
        self.assertIn('bl_ext.system.looptools', bundled._read_record(self.record))

    def test_invalid_record_never_disables_anything(self):
        self.record.parent.mkdir()
        for data in ('{bad json', '{"schema":1,"modules":["user.addon"]}'):
            self.record.write_text(data)
            with self.assertRaises((ValueError, bundled.BundledAddonError)):
                bundled.startup_cleanup()
            self.assertEqual(self.record.read_text(), data)
        self.utils.disable.assert_not_called()

    def test_prompt_lists_installed_then_off_bundles_with_shared_budget(self):
        installed = [{'name': 'User Tool', 'module': 'user_tool', 'operators': ['mesh.user_tool']}]
        packages = bundled.packages()
        with mock.patch.object(bundled, 'packages', side_effect=AssertionError('line must be pure')):
            text = addons.line(installed, packages)
        self.assertLess(text.index('Installed add-ons:'), text.index('Bundled, off until inspect_api names one:'))
        self.assertIn('mesh.looptools_*: mesh cleanup', text)
        self.assertIn('interactive Blender only', text)
        text = addons.line(installed * (addons.MAX_LISTED - 1), packages)
        self.assertIn('+4 more bundled', text)
        self.assertEqual(text.count('[bool_tool]'), 1)
        self.assertNotIn('[univ]', text)
        self.assertIn('+5 more bundled', addons.line(installed * addons.MAX_LISTED, packages))

    def test_enabled_system_bundle_remains_in_installed_addons(self):
        self.enable('bl_ext.system.looptools', default_set=True)
        self.bpy.types.Operator = type('Operator', (), {})
        self.bpy.types.Relax = type('Relax', (self.bpy.types.Operator,), {
            '__module__': 'bl_ext.system.looptools', 'bl_idname': 'mesh.looptools_relax'})
        with mock.patch('loopcut.settings', types.SimpleNamespace(PACKAGE='loopcut'), create=True):
            entries = addons.enabled()
        self.assertEqual([p['module'] for p in entries], ['bl_ext.system.looptools'])
        text = addons.line(entries, bundled.packages())
        self.assertIn('Installed add-ons: LoopTools [bl_ext.system.looptools]', text)
        self.assertIn('bpy.ops.mesh.looptools_relax', text)
        self.assertNotIn('LoopTools [looptools]', text)


if __name__ == '__main__':
    unittest.main()
