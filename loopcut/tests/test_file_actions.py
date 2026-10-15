"""Reply actions and image card layout, with thin host handlers tested without Blender."""

import ast
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ADDONS = Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'
sys.path.insert(0, str(ADDONS))
sys.modules.setdefault('bpy', ModuleType('bpy'))

from loopcut import state  # noqa: E402
from loopcut.ui import layout, markdown, theme as T  # noqa: E402


def frame(**ui):
    return layout.Frame(420, 700, 1.0, lambda font, size, text: len(text) * size * 0.6, ui=ui)


class FileActionsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.file = self.base / 'a.txt'
        self.file.touch()
        self.item = {'kind': 'assistant', 'streaming': False,
                     'text': f'**Saved** [file]({self.file}) [docs](https://example.com)'}
        markdown.finish_reply(self.item, self.base)

    def test_reply_emits_resolved_actions_and_readable_prose(self):
        f = frame()
        layout._item_assistant(f, 0, self.item, 10, 0, 400)
        self.assertEqual([h['action'] for h in f.hits], [('reference_open', str(self.file)),
                         ('reference_folder', str(self.file)), ('reference_url', 'https://example.com')])
        self.assertEqual(next(p['text'] for p in f.prims if p['id'] == 'item0.p0.l0'), 'Saved file docs')
        self.assertTrue(all(h['y'] > 0 for h in f.hits))

    def test_deleted_file_is_muted_and_has_no_buttons(self):
        self.file.unlink()
        f = frame(missing_paths={str(self.file)})
        layout._item_assistant(f, 0, self.item, 10, 0, 400)
        self.assertEqual([h['action'] for h in f.hits], [('reference_url', 'https://example.com')])
        self.assertEqual(next(p['color'] for p in f.prims if p['id'] == 'item0.ref0.name'), T.TEXT_FAINT)

    def test_cap_and_narrow_rows(self):
        self.item['file_references'] *= 5
        f = frame()
        layout._item_assistant(f, 0, self.item, 10, 0, 180)
        self.assertEqual(sum(h['action'][0] in {'reference_open', 'reference_url'} for h in f.hits),
                         markdown.REFERENCE_LIMIT)
        self.assertIn('+4 more', [p.get('text') for p in f.prims])
        for h in f.hits:
            self.assertGreaterEqual(h['x'], 10)
            self.assertLessEqual(h['x'] + h['w'], 190)
        for a, b in zip(f.hits, f.hits[1:]):
            self.assertTrue(a['y'] + a['h'] <= b['y'] or a['x'] + a['w'] <= b['x'])

    def test_streaming_has_no_actions(self):
        self.item['streaming'] = True
        f = frame()
        layout._item_assistant(f, 0, self.item, 0, 0, 420)
        self.assertEqual(f.hits, [])

    def test_expanded_image_has_box_and_open_above_original_output(self):
        card = state.item_tool('view_image', 'View image', '')
        card.update(status='done', expanded=True, image_path=str(self.file), output='Image attached: 800 x 400')
        f = frame()
        layout._item_tool(f, 0, card, 0, 0, 400)
        image = next(p for p in f.prims if p['t'] == 'image')
        output = next(p for p in f.prims if p['id'] == 'item0.tool.output.l0')
        self.assertEqual(image['path'], str(self.file))
        self.assertLessEqual(image['w'] + 2 * T.CARD_PAD, 400)
        self.assertLessEqual(image['h'], 220)
        self.assertLess(image['y'] + image['h'], output['y'])
        self.assertIn(('reference_open', str(self.file)), [h['action'] for h in f.hits])
        for expanded, missing in ((False, set()), (True, {str(self.file)})):
            card['expanded'] = expanded
            f = frame(missing_paths=missing)
            layout._item_tool(f, 0, card, 0, 0, 400)
            self.assertFalse(any(p['t'] == 'image' for p in f.prims))
            self.assertFalse(any(h['action'][0] == 'reference_open' for h in f.hits))

    def test_transient_notice_draws_above_input_and_expires(self):
        text = 'Opened a.png in your default app (outside Blender).'
        for width, scale in ((240, 1.0), (420, 1.0), (600, 1.5)):
            with self.subTest(width=width, scale=scale):
                session = state.new_session()
                ui = {'action_notice': {'text': text, 'until': 6.0}, 'now': 1.0}
                measure = lambda font, size, value: len(value) * size * 0.6
                display = layout.build(session, width, 700, scale, measure, ui=ui)
                lines = [p for p in display['prims'] if p['id'].startswith('input.notice.l')]
                self.assertEqual(' '.join(p['text'] for p in lines), text)
                card = next(p for p in display['prims'] if p['id'] == 'input.card')
                for line in lines:
                    self.assertGreaterEqual(line['y'], 0)
                    self.assertLessEqual(line['y'] + line['h'], card['y'])
                    self.assertLessEqual(line['x'] + measure('ui', line['size'], line['text']), width)
                expired = layout.build(session, width, 700, scale, measure, ui={**ui, 'now': 6.0})
                self.assertFalse(any(p['id'].startswith('input.notice.') for p in expired['prims']))
                expired_card = next(p for p in expired['prims'] if p['id'] == 'input.card')
                self.assertEqual(card, expired_card, 'the notice reserves space above the controls')


class HostActionsTest(unittest.TestCase):
    def setUp(self):
        self.open = Mock(return_value={'FINISHED'})
        self.browser = Mock()
        self.retain = Mock()
        self.clock = Mock(return_value=0.0)
        self.session = state.new_session()
        self.area = Mock()
        self.windows = [SimpleNamespace(screen=SimpleNamespace(areas=[self.area]))]
        namespace = {'__package__': 'loopcut.ui', 'Path': Path, 'sys': sys, '_runtime': {}, 'PATH_TTL': 3.0,
                     'time': SimpleNamespace(monotonic=self.clock, time=self.clock),
                     'state': SimpleNamespace(session=lambda: self.session, item_error=state.item_error, ui={}),
                     'bpy': SimpleNamespace(ops=SimpleNamespace(wm=SimpleNamespace(path_open=self.open)),
                                            context=SimpleNamespace(window_manager=SimpleNamespace(windows=self.windows),
                                                                    area=self.area, region=SimpleNamespace(width=420, height=600),
                                                                    view_layer=None, scene=None,
                                                                    preferences=SimpleNamespace(system=SimpleNamespace(ui_scale=1.0)))),
                     'account': SimpleNamespace(open_browser=self.browser), 'layout': layout,
                     'draw': SimpleNamespace(retain_images=self.retain, render=Mock(),
                                             measure=lambda font, size, text: len(text) * size * 0.6), 'is_host': bool,
                     'settings': SimpleNamespace(preferences=lambda: SimpleNamespace(), model_label=str),
                     '_config_cached': lambda: ('model', False, 24000), '_checkpoint_statuses': lambda session: {},
                     '_cloud_panel_state': lambda session: {}, 'update': SimpleNamespace(banner=lambda: None),
                     '_displays': {}}
        namespace['account'].status = lambda: None
        source = ADDONS / 'loopcut/ui/host.py'
        names = {'_do_action', '_image_paths', '_missing_paths', '_refresh_missing_paths', '_prune_images',
                 'tag_redraw_all', 'draw_area'}
        nodes = [n for n in ast.parse(source.read_text()).body if getattr(n, 'name', None) in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
        self.host = namespace
        self.action, self.missing, self.prune = (namespace[n] for n in ('_do_action', '_missing_paths', '_prune_images'))
        self.refresh = namespace['_refresh_missing_paths']

    def notice_text(self):
        display = layout.build(self.session, 420, 700, 1.0, self.host['draw'].measure,
                               ui={**self.host['state'].ui, 'now': self.clock()})
        return ' '.join(p['text'] for p in display['prims'] if p['id'].startswith('input.notice.l'))

    def assert_open_confirmation(self, suffix):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / ('a' + suffix)
            file.touch()
            self.action(self.session, ('reference_open', str(file)))
            self.open.assert_called_once_with(filepath=str(file))
            self.assertEqual(self.notice_text(), f'Opened {file.name} in your default app (outside Blender).')

    def test_open_png_shows_default_app_confirmation(self):
        self.assert_open_confirmation('.png')

    def test_open_mp4_shows_default_app_confirmation(self):
        self.assert_open_confirmation('.mp4')

    def test_show_folder_confirms_opened_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            file = folder / 'a.png'
            file.touch()
            for platform, app in (('darwin', 'Finder'), ('win32', 'your file manager'),
                                  ('linux', 'your file manager')):
                for target in (file, folder):
                    with self.subTest(platform=platform, target=target):
                        self.host['sys'] = SimpleNamespace(platform=platform)
                        self.action(self.session, ('reference_folder', str(target)))
                        self.open.assert_called_with(filepath=str(folder))
                        self.assertEqual(self.notice_text(), f'Opened {folder.name} in {app} (outside Blender).')

    def test_cancelled_open_shows_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'a.png'
            file.touch()
            for action in ('reference_open', 'reference_folder'):
                for result in ({'CANCELLED'}, set()):
                    with self.subTest(action=action, result=result):
                        self.open.return_value = {'FINISHED'}
                        self.action(self.session, (action, str(file)))
                        self.open.return_value = result
                        self.session['scroll'] = 100.0
                        self.action(self.session, (action, str(file)))
                        error = self.session['items'][-1]
                        self.assertEqual(error['kind'], 'error')
                        target = file if action == 'reference_open' else file.parent
                        self.assertIn(target.name, error['text'])
                        self.assertIn('Blender could not hand it to another app', error['text'])
                        self.assertEqual(self.session['scroll'], 0.0)
                        self.assertEqual(self.notice_text(), '')

    def test_missing_file_shows_error_without_opening(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'a.png'
            for action in ('reference_open', 'reference_folder'):
                with self.subTest(action=action):
                    self.session['scroll'] = 100.0
                    self.action(self.session, (action, str(file)))
                    self.open.assert_not_called()
                    error = self.session['items'][-1]
                    self.assertEqual(error['kind'], 'error')
                    self.assertIn(file.name, error['text'])
                    self.assertIn(f'the file is no longer at {file}', error['text'])
                    self.assertEqual(self.session['scroll'], 0.0)
                    self.assertEqual(self.notice_text(), '')

    def test_open_exception_keeps_existing_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.open.side_effect = RuntimeError('Cannot open path')
            self.action(self.session, ('reference_open', tmp))
            self.assertEqual(self.session['items'], [state.item_error(f'Could not open {tmp}: Cannot open path')])
            self.assertEqual(self.notice_text(), '')

    def test_open_success_does_not_change_conversation(self):
        self.session['items'] = [state.item_user('Open the file')]
        self.session['messages'] = [{'role': 'user', 'content': 'Open the file'}]
        with tempfile.TemporaryDirectory() as tmp:
            for action in ('reference_open', 'reference_folder'):
                for suffix in ('.png', '.mp4'):
                    with self.subTest(action=action, suffix=suffix):
                        file = Path(tmp) / ('a' + suffix)
                        file.touch()
                        before = copy.deepcopy(self.session)
                        self.action(self.session, (action, str(file)))
                        self.assertEqual(self.session, before, 'success feedback stays out of saved and model messages')
                        self.assertTrue(self.notice_text())

    def test_open_show_folder_and_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            file = folder / 'a.txt'
            file.touch()
            for action, target, expected in [('reference_open', file, file), ('reference_folder', file, folder),
                                             ('reference_folder', folder, folder), ('reference_open', folder, folder)]:
                self.action(self.session, (action, str(target)))
                self.open.assert_called_with(filepath=str(expected))
            self.open.reset_mock()
            file.unlink()
            self.action(self.session, ('reference_open', str(file)))
            self.open.assert_not_called()
            self.action(self.session, ('reference_url', 'https://example.com'))
            self.browser.assert_called_once_with('https://example.com')

    def test_missing_paths_and_cleanup_when_editor_closes(self):
        self.session['items'] = [{'image_path': '/missing/image.png', 'file_references': [
            {'kind': 'file', 'target': '/missing/file.txt'}, {'kind': 'url', 'target': 'https://example.com'}]}]
        self.refresh(self.session)
        self.assertEqual(self.missing(self.session), {'/missing/image.png', '/missing/file.txt'})
        self.prune()
        self.retain.assert_called_with({'/missing/image.png'})
        self.windows.clear()
        self.prune()
        self.retain.assert_called_with(())

    def test_draws_only_read_cached_file_availability(self):
        self.session['items'] = [{'kind': 'assistant', 'text': '[file](/missing/file.txt)', 'streaming': False,
                                  'file_references': [{'kind': 'file', 'name': 'file.txt', 'target': '/missing/file.txt'}]},
                                 {**state.item_tool('view_image', 'View image', ''), 'expanded': True,
                                  'image_path': '/missing/image.png'}]
        dependencies = {
            'cloud': SimpleNamespace(panel_seen=lambda: None, _runtime=None),
            'cloud_account': SimpleNamespace(DEFAULTS={}, project_override=lambda scene: None),
            'cloud_jobs': SimpleNamespace(ui_rows=lambda: []),
            'chatgpt_ui': SimpleNamespace(status=lambda: None),
            'onboarding': SimpleNamespace(hints_dismissed=lambda: True),
        }
        with patch.object(Path, 'exists', return_value=False) as exists:
            self.host['tag_redraw_all']()
            self.assertEqual(exists.call_count, 2)
        with patch.multiple(sys.modules['loopcut'], create=True, **dependencies), \
                patch.object(Path, 'exists', side_effect=AssertionError('draw must not check files')):
            for _ in range(5):
                self.host['draw_area']()
        display = self.host['draw'].render.call_args.args[0]
        self.assertFalse(any(p['t'] == 'image' for p in display['prims']))
        self.assertFalse(any(h['action'][0].startswith('reference_') for h in display['hits']))

    def test_poll_refresh_is_throttled_and_detects_deletion_and_reappearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.txt'
            path.touch()
            self.session['items'] = [{'file_references': [{'kind': 'file', 'target': str(path)}],
                                      'image_path': str(path)}]
            self.host['tag_redraw_all']()
            self.assertEqual(self.missing(self.session), set())
            path.unlink()
            self.clock.return_value = 2.0
            with patch.object(Path, 'exists', side_effect=AssertionError('poll checked too soon')):
                self.host['tag_redraw_all']()
                self.assertEqual(self.missing(self.session), set())
            self.clock.return_value = 3.0
            self.host['tag_redraw_all']()
            self.assertEqual(self.missing(self.session), {str(path)})
            path.touch()
            self.clock.return_value = 6.0
            self.host['tag_redraw_all']()
            self.assertEqual(self.missing(self.session), set())

    def test_changed_references_conversation_and_click_refresh_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.txt'
            self.refresh(self.session)
            self.session['items'] = [{'image_path': str(path)}]
            self.host['tag_redraw_all']()
            self.assertEqual(self.missing(self.session), {str(path)})
            path.touch()
            self.session['id'] = 'another-conversation'
            self.assertEqual(self.missing(self.session), set())
            self.host['tag_redraw_all']()
            self.assertEqual(self.missing(self.session), set())
            path.unlink()
            self.action(self.session, ('reference_open', str(path)))
            self.open.assert_not_called()
            self.assertEqual(self.missing(self.session), {str(path)})


if __name__ == '__main__':
    unittest.main()
