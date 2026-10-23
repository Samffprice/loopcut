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


class StrictObject:
    """RNA-like attributes: reading or writing an undeclared property is an error."""
    __slots__ = ('_fields',)

    def __init__(self, **fields):
        object.__setattr__(self, '_fields', fields)

    def __getattr__(self, name):
        if name not in self._fields:
            raise AttributeError(name)
        return self._fields[name]

    def __setattr__(self, name, value):
        if name not in self._fields:
            raise AttributeError(name)
        self._fields[name] = value


class ImageStore:
    __slots__ = ('images', 'loads')

    def __init__(self):
        self.images, self.loads = [], []

    def load(self, path, *, check_existing):
        assert not check_existing
        image = StrictObject(name=Path(path).name, filepath=path, users=0, size=(800, 400), alpha_mode='STRAIGHT')
        self.images.append(image)
        self.loads.append(path)
        return image

    def remove(self, image):
        self.images.remove(image)


class ImageSpace:
    __slots__ = ('_image', 'mode')  # Blender 5.2 has no show_region_header here.

    def __init__(self):
        self._image, self.mode = None, 'VIEW'

    @property
    def image(self):
        return self._image

    @image.setter
    def image(self, image):
        if self._image is not None:
            self._image.users -= 1  # The extra user remains even after replacement.
        image.users += 2
        self._image = image


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
        self.assertEqual(image['w'], 400 - 2 * T.CARD_PAD)
        self.assertIn('Open image', [p.get('text') for p in f.prims])
        self.assertIn(('reference_open', str(self.file)), [h['action'] for h in f.hits])
        self.assertFalse(any(h['action'][0] == 'preview_open' for h in f.hits))
        for expanded, missing in ((False, set()), (True, {str(self.file)})):
            card['expanded'] = expanded
            f = frame(missing_paths=missing)
            layout._item_tool(f, 0, card, 0, 0, 400)
            self.assertFalse(any(p['t'] == 'image' for p in f.prims))
            self.assertFalse(any(h['action'][0] in {'reference_open', 'preview_open'} for h in f.hits))

    def test_capture_uses_readable_summary_instead_of_hash_filename(self):
        card = {**state.item_tool('capture_viewport', 'Chest from the front', ''), 'status': 'done',
                'image_path': '/images/0123456789abcdef.png'}
        f = frame()
        layout._item_tool(f, 0, card, 0, 0, 400)
        previews = [h for h in f.hits if h['action'][0] == 'preview_open']
        self.assertEqual(len(previews), 2, 'both the image and its button open the preview')
        self.assertTrue(all(h['action'][1] == (card['image_path'], 'Chest from the front') for h in previews))

    def test_inline_previews_enlarge_at_narrow_widths_and_keep_titles_and_input(self):
        for width, scale in ((240, 1), (420, 1), (640, 1.5)):
            for helper in (False, True):
                with self.subTest(width=width, scale=scale, helper=helper):
                    session = state.new_session()
                    path, title = '/images/0123456789abcdef.png', 'Printing machine'
                    item = (state.item_wave('w1', 1, [{**state.wave_row('a', title, 'modeling'), 'image_path': path}])
                            if helper else {**state.item_tool('capture_viewport', title, ''), 'image_path': path})
                    session['items'] = [item]
                    measure = lambda font, size, text: len(text) * size * 0.6
                    collapsed = layout.build(session, width, 1200, scale, measure)
                    enlarged = layout.build(session, width, 1200, scale, measure,
                                            ui={'preview': {'path': path, 'title': title, 'conversation': session['id']}})
                    thumb = next(p for p in collapsed['prims'] if p['t'] == 'image')
                    image = next(p for p in enlarged['prims'] if p['t'] == 'image')
                    self.assertGreater(image['w'], thumb['w'])
                    self.assertGreater(image['h'], thumb['h'])
                    self.assertLessEqual(image['x'] + image['w'], width)
                    displayed_title = ' '.join(p['text'] for p in enlarged['prims']
                                               if p['id'].startswith(image['id'] + '.title.'))
                    self.assertEqual(displayed_title, title)
                    self.assertIn('Close preview', [p.get('text') for p in enlarged['prims']])
                    self.assertIsNotNone(enlarged['input'])
                    self.assertFalse(any('0123456789abcdef' in p.get('text', '') for p in enlarged['prims']))

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
    def test_helper_previews_are_retained_and_checked_for_missing_files(self):
        row = state.wave_row("a", "A", "modeling")
        row["image_path"] = "/missing/helper.png"
        self.session["items"] = [state.item_wave("w1", 1, [row])]
        self.refresh(self.session)
        self.assertEqual(self.missing(self.session), {"/missing/helper.png"})
        self.prune()
        self.retain.assert_called_with({"/missing/helper.png"})
        row["image_path"] = "/missing/new-preview.png"
        self.prune()
        self.retain.assert_called_with({"/missing/new-preview.png"})

    def setUp(self):
        self.open = Mock(return_value={'FINISHED'})
        self.browser = Mock()
        self.retain = Mock()
        self.clock = Mock(return_value=0.0)
        self.session = state.new_session()
        self.area = Mock()
        self.windows = [SimpleNamespace(screen=SimpleNamespace(areas=[self.area]), as_pointer=lambda: 1)]
        namespace = {'__package__': 'loopcut.ui', 'Path': Path, 'sys': sys, '_runtime': {}, 'PATH_TTL': 3.0,
                     'time': SimpleNamespace(monotonic=self.clock, time=self.clock),
                     'state': SimpleNamespace(session=lambda: self.session, item_error=state.item_error, ui={}),
                     'bpy': SimpleNamespace(ops=SimpleNamespace(wm=SimpleNamespace(path_open=self.open)),
                                            context=SimpleNamespace(window_manager=SimpleNamespace(windows=self.windows),
                                                                    area=self.area, region=SimpleNamespace(width=420, height=600),
                                                                    view_layer=None, scene=None,
                                                                    preferences=SimpleNamespace(system=SimpleNamespace(ui_scale=1.0)))),
                     'account': SimpleNamespace(open_browser=self.browser, refresh_expired_usage=Mock()), 'layout': layout,
                     'draw': SimpleNamespace(retain_images=self.retain, render=Mock(),
                                             measure=lambda font, size, text: len(text) * size * 0.6), 'is_host': bool,
                     'settings': SimpleNamespace(preferences=lambda: SimpleNamespace(), model_label=str),
                     '_config_cached': lambda: ('model', False, 24000), '_checkpoint_statuses': lambda session: {},
                     '_cloud_panel_state': lambda session: {}, '_connections_cached': lambda: {},
                     '_key_needed': lambda needs_setup: '',
                     'update': SimpleNamespace(banner=lambda: None),
                     '_displays': {}}
        namespace['account'].status = lambda: None
        source = ADDONS / 'loopcut/ui/host.py'
        names = {'_do_action', '_image_paths', '_missing_paths', '_refresh_missing_paths', '_prune_images',
                 'tag_redraw_all', 'draw_area'}
        tree = ast.parse(source.read_text())
        nodes = [n for n in tree.body if getattr(n, 'name', None) in names]
        # Inline expansion must preserve typing; Escape can close it.
        key = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_key')
        nodes.append(key)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
        self.host = namespace
        self.action, self.missing, self.prune = (namespace[n] for n in ('_do_action', '_missing_paths', '_prune_images'))
        self.refresh = namespace['_refresh_missing_paths']

    def preview_blender(self):
        images = ImageStore()
        self.area = StrictObject(type='TEXT_EDITOR', tag_redraw=Mock())
        window = StrictObject(screen=StrictObject(areas=[self.area]), as_pointer=lambda: 1)
        self.windows[:] = [window]
        context = StrictObject(window_manager=StrictObject(windows=self.windows), window=window, area=self.area)

        def duplicate(*args):
            area = StrictObject(type='TEXT_EDITOR', spaces=StrictObject(active=ImageSpace()),
                                regions=[StrictObject(type='WINDOW')], tag_redraw=Mock())
            new = StrictObject(screen=StrictObject(areas=[area]), as_pointer=lambda: len(self.windows))
            self.windows.append(new)
            context.window, context.area = new, area  # area_dupli changes the active modal context.
            return {'FINISHED'}

        duplicate = Mock(side_effect=duplicate)
        bpy = StrictObject(data=StrictObject(images=images), context=context,
                           ops=StrictObject(screen=StrictObject(area_dupli=duplicate),
                                            wm=StrictObject(path_open=self.open)))
        self.host['bpy'] = bpy
        # Use the real thumbnail cache for both collapsed and expanded display primitives.
        source = ADDONS / 'loopcut/ui/draw.py'
        tree = ast.parse(source.read_text())
        names = {'_release_image', 'retain_images', '_image_texture'}
        nodes = [n for n in tree.body if getattr(n, 'name', None) in names]
        cache = {'bpy': bpy, 'Path': Path, '_image_cache': {}, 'IMAGES_KEPT': 24,
                 'gpu': StrictObject(texture=StrictObject(from_image=lambda image: StrictObject(image=image)))}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), cache)
        return images, cache, duplicate

    def test_strict_preview_doubles_reproduce_rna_and_extra_user_semantics(self):
        images, _, duplicate = self.preview_blender()
        with self.assertRaises(AttributeError):
            self.area.unknown_property = True
        with self.assertRaises(AttributeError):
            self.area.unknown_property
        duplicate('INVOKE_DEFAULT')
        space = self.host['bpy'].context.area.spaces.active
        self.assertIsNot(self.host['bpy'].context.area, self.area)
        with self.assertRaises(AttributeError):
            space.show_region_header = True
        first = images.load('/first.png', check_existing=False)
        space.image = first
        space.image = images.load('/second.png', check_existing=False)
        self.assertEqual(first.users, 1)

    def test_helper_and_capture_toggle_inline_using_cached_images_without_context_changes(self):
        images, cache, duplicate = self.preview_blender()
        context = self.host['bpy'].context
        original_window, original_area = context.window, context.area
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / (name + '.png') for name in ('0123456789abcdef', 'fedcba9876543210')]
            for path in paths:
                path.touch()
            capture = {**state.item_tool('capture_viewport', 'Chest from the front', ''),
                       'status': 'done', 'image_path': str(paths[0])}
            row = {**state.wave_row('a', 'Printing machine', 'modeling'), 'image_path': str(paths[1])}
            self.session['items'] = [capture, state.item_wave('w1', 1, [row])]
            self.session['input_first'] = 0
            self.session['focused'] = True
            before = copy.deepcopy(self.session)

            def display():
                result = layout.build(self.session, 600, 1800, 1, self.host['draw'].measure,
                                      ui=self.host['state'].ui)
                for prim in result['prims']:
                    if prim['t'] == 'image':
                        self.assertIsNotNone(cache['_image_texture'](prim['path']))
                cache['retain_images'](self.host['_image_paths'](self.session))
                return result

            collapsed = display()
            thumbs = {p['path']: p for p in collapsed['prims'] if p['t'] == 'image'}
            self.assertEqual(len(images.images), 2)
            # Open/close the same image, then A -> B -> A, repeatedly.
            for index in (0, 0, 1, 0, 1, 1) * 4:
                path, title = str(paths[index]), ('Chest from the front', 'Printing machine')[index]
                action = ('preview_open', (path, title))
                was_open = self.host['state'].ui.get('preview', {}).get('path') == path
                self.action(self.session, action)
                result = display()
                image = next(p for p in result['prims'] if p['t'] == 'image' and p['path'] == path)
                self.assertEqual(image['w'] > thumbs[path]['w'], not was_open)
                self.assertEqual(image['h'] > thumbs[path]['h'], not was_open)
                if not was_open:
                    self.assertIn(title, [p.get('text') for p in result['prims'] if p['id'].endswith('.title.l0')])
                    button = next(h for h in result['hits'] if h['id'] == image['id'] + '.open')
                    self.assertEqual(button['action'], action)
                self.assertIsNotNone(result['input'])
                self.assertEqual(len(images.images), 2)
                self.assertEqual(images.loads, [str(p) for p in paths])
                self.assertIs(context.window, original_window)
                self.assertIs(context.area, original_area)
                self.assertEqual(len(self.windows), 1)
                self.assertEqual(self.session, before)
            duplicate.assert_not_called()
            self.open.assert_not_called()
            cache['retain_images'](())
            self.assertEqual(images.images, [])

    def test_helper_expands_inline_while_delivered_image_keeps_its_card_and_open_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '0123456789abcdef.png'
            path.touch()
            title = 'Printing machine'
            row = {**state.wave_row('a', title, 'modeling'), 'image_path': str(path)}
            # The same stored image can appear as a helper preview and a delivered file.
            delivered = {**state.item_tool('run_python', title, ''),
                         'status': 'done', 'image_path': str(path)}
            self.session['items'] = [delivered, state.item_wave('w1', 1, [row])]

            def display():
                return layout.build(self.session, 600, 1800, 1, self.host['draw'].measure,
                                    ui=self.host['state'].ui)

            before = display()
            delivery_image, helper_image = [p for p in before['prims'] if p['t'] == 'image']
            helper_action = next(h['action'] for h in before['hits'] if h['id'] == helper_image['id'])
            self.assertEqual(helper_action, ('preview_open', (str(path), title)))
            self.action(self.session, helper_action)
            after = display()
            delivery_after, helper_after = [p for p in after['prims'] if p['t'] == 'image']
            self.assertGreater(helper_after['w'], helper_image['w'])
            self.assertGreater(helper_after['h'], helper_image['h'])
            self.assertIn('Close preview', [p.get('text') for p in after['prims']])
            self.assertEqual(delivery_after, delivery_image)
            self.assertEqual([p for p in after['prims'] if p['id'].startswith('item0.')],
                             [p for p in before['prims'] if p['id'].startswith('item0.')])
            self.assertIn('Open image', [p.get('text') for p in after['prims']])
            delivery_action = next(h['action'] for h in after['hits']
                                   if h['id'] == delivery_image['id'] + '.open')
            self.assertEqual(delivery_action, ('reference_open', str(path)))
            self.open.assert_not_called()
            self.action(self.session, delivery_action)
            self.open.assert_called_once_with(filepath=str(path))
            self.assertIsNotNone(after['input'])

    def test_preview_does_not_expand_in_another_conversation(self):
        path, title = '/saved/a.png', 'Chest'
        self.session['items'] = [{**state.item_tool('capture_viewport', title, ''), 'image_path': path}]
        self.host['state'].ui['preview'] = {'path': path, 'title': title, 'conversation': 'another-chat'}
        display = layout.build(self.session, 600, 800, 1, self.host['draw'].measure, ui=self.host['state'].ui)
        image = next(p for p in display['prims'] if p['t'] == 'image')
        self.assertEqual(image['w'], 220)
        self.assertIsNotNone(display['input'])

    def test_missing_preview_is_readable_and_never_opens_an_app(self):
        images, _, duplicate = self.preview_blender()
        self.action(self.session, ('preview_open', ('/missing/hash.png', 'Printing machine')))
        self.assertEqual(self.notice_text(), 'Printing machine: this preview is no longer available.')
        self.assertEqual(self.session['items'], [])
        self.assertEqual(images.loads, [])
        self.assertNotIn('preview', self.host['state'].ui)
        duplicate.assert_not_called()
        self.open.assert_not_called()

    def test_inline_preview_allows_typing_and_escape_closes_without_stopping(self):
        from loopcut.ui import textedit
        self.host.update(textedit=textedit, _update_mentions=Mock(), agent=StrictObject(decide=Mock(), stop=Mock()))
        self.session['first_prompt_typed'] = True
        self.host['state'].ui['preview'] = {'path': '/a.png', 'title': 'Chest', 'conversation': self.session['id']}
        event = StrictObject(type='A', shift=False, oskey=False, ctrl=False, alt=False, unicode='a')
        self.assertTrue(self.host['_key'](None, None, self.session, event))
        self.assertEqual(self.session['input'], 'a')
        self.assertIn('preview', self.host['state'].ui)
        event.type = 'ESC'
        self.assertTrue(self.host['_key'](None, None, self.session, event))
        self.assertNotIn('preview', self.host['state'].ui)
        self.host['agent'].decide.assert_not_called()
        self.host['agent'].stop.assert_not_called()

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
