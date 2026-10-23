"""Delivery metadata survives completion, scratch reuse and conversation reload."""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))

from loopcut import agent, codex_app, config, context, conversations, llm, state  # noqa: E402
from loopcut.ui import layout, markdown  # noqa: E402


class DeliveryPersistenceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        patch = mock.patch.dict(os.environ, {'LOOPCUT_DATA_DIR': str(self.base / 'data')})
        patch.start()
        self.addCleanup(patch.stop)
        self.session = state.new_session()
        self.turn = agent.Turn()
        self.turn.roots = (self.base,)
        self.cfg = config.Config('test', 'http://localhost/v1', 'test', 'low', True, False, 5, 24000)
        self.file = self.base / 'a.txt'
        self.file.write_text('content')
        self.text = '**Saved** [file](a.txt)'

    def bridge(self):
        return codex_app.CodexTurn(self.session, self.turn, self.cfg, None, self.base, mock.Mock(),
                                   mock.Mock(), mock.Mock())

    def test_regular_stream_completion_uses_tool_base_and_preserves_text(self):
        spec = agent.LoopSpec('single', lambda s: '', lambda: [], lambda s: None)

        def stream(**kwargs):
            kwargs['on_text'](self.text)
            self.assertNotIn('file_references', self.session['items'][-1])
            return llm.Completion(text=self.text, tool_calls=[], finish_reason='stop')

        with mock.patch.object(llm, 'stream_chat', side_effect=stream), \
                mock.patch.object(context, 'prepare', return_value=([], False)), \
                mock.patch.object(context, 'fixed_tokens', return_value=0), \
                mock.patch.object(context, 'estimate_tokens', return_value=0), \
                mock.patch.object(agent, '_redraw'):
            completion = agent._request(self.session, self.turn, self.cfg, spec)
        reply = self.session['items'][-1]
        self.assertFalse(reply['streaming'])
        self.assertEqual(reply['text'], completion.text)
        self.assertEqual(reply['file_references'][0]['target'], str(self.file))

    def test_codex_completion_persists_actions_and_leaves_wire_text_unchanged(self):
        bridge = self.bridge()
        bridge.started({'type': 'agentMessage', 'id': 'message'})
        with mock.patch.object(agent, '_redraw'):
            bridge.completed({'type': 'agentMessage', 'id': 'message', 'text': self.text})
        loaded = conversations.load(self.session['id'])
        self.assertEqual(loaded['items'][-1]['text'], self.text)
        self.assertEqual(loaded['messages'][-1]['content'], self.text)
        self.assertEqual(loaded['items'][-1]['file_references'][0]['target'], str(self.file))

    def test_stored_card_image_is_stable_after_scratch_reuse_and_reload(self):
        scratch = self.base / 'scratch.png'
        scratch.write_bytes(b'first image')
        card = state.item_tool('view_image', 'View image', '')
        ref = conversations.store_image(self.session, scratch, card)
        scratch.write_bytes(b'second image')
        other = state.item_tool('view_image', 'View image', '')
        conversations.store_image(self.session, scratch, other)
        reply = {'kind': 'assistant', 'text': self.text, 'streaming': False}
        markdown.finish_reply(reply, self.base)
        self.session['items'] = [card, other, reply]
        conversations.save(self.session)
        self.file.unlink()
        loaded = conversations.load(self.session['id'])
        first, second, reply = loaded['items']
        self.assertEqual(Path(first['image_path']), conversations.image_path(self.session['id'], ref))
        self.assertEqual(Path(first['image_path']).read_bytes(), b'first image')
        self.assertEqual(Path(second['image_path']).read_bytes(), b'second image')
        self.assertEqual(reply['file_references'][0]['target'], str(self.file))

    def test_new_image_card_is_visible(self):
        scratch = self.base / 'scratch.png'
        scratch.write_bytes(b'first image')
        card = state.item_tool('run_python', 'Run Python', 'x = 1')
        card['status'] = 'done'
        card['output'] = 'checked'
        conversations.store_image(self.session, scratch, card)

        f = layout.Frame(420, 700, 1.0, lambda font, size, text: len(text) * size * 0.6)
        layout._item_tool(f, 0, card, 0, 0, 400)
        self.assertEqual([p['path'] for p in f.prims if p['t'] == 'image'], [card['image_path']])
        self.assertIn('Open image', [p.get('text') for p in f.prims])
        self.assertIn(('reference_open', card['image_path']), [h['action'] for h in f.hits])
        # The code and output stay behind the card's toggle.
        self.assertFalse(any(str(p.get('id', '')).startswith(('item0.tool.code', 'item0.tool.output'))
                             for p in f.prims))

    def test_image_card_respects_explicit_collapse(self):
        scratch = self.base / 'scratch.png'
        scratch.write_bytes(b'first image')
        card = state.item_tool('view_image', 'View image', '')
        card['status'] = 'done'
        conversations.store_image(self.session, scratch, card)
        card['expanded'] = False  # The user's toggle stores an explicit boolean.

        for replace_image in (False, True):
            with self.subTest(replace_image=replace_image):
                if replace_image:
                    scratch.write_bytes(b'second image')
                    conversations.store_image(self.session, scratch, card)
                f = layout.Frame(420, 700, 1.0, lambda font, size, text: len(text) * size * 0.6)
                layout._item_tool(f, 0, card, 0, 0, 400)
                self.assertFalse(any(p['t'] == 'image' for p in f.prims))
                self.assertFalse(any(h['action'][0] == 'reference_open' for h in f.hits))

    def test_codex_native_image_view_keeps_a_conversation_copy(self):
        image = self.base / 'a.png'
        image.write_bytes(b'first image')
        bridge = self.bridge()
        with mock.patch.object(agent, '_redraw'):
            bridge.started({'type': 'imageView', 'id': 'image', 'path': 'a.png'})
        image.unlink()
        self.assertEqual(Path(bridge.cards['image']['image_path']).read_bytes(), b'first image')

    def test_codex_dynamic_image_keeps_the_stored_path_on_the_card(self):
        image = self.base / 'a.png'
        image.write_bytes(b'image')
        bridge = self.bridge()
        with mock.patch.object(bridge, 'run_dynamic', return_value=('Image attached', True, image, False)), \
                mock.patch.object(agent, '_describe', return_value=('View image', '')), \
                mock.patch.object(agent, '_redraw'):
            bridge.tool_call({'tool': 'capture_viewport', 'arguments': {}, 'callId': 'image'})
        image.unlink()
        card = conversations.load(self.session['id'])['items'][-1]
        self.assertEqual(Path(card['image_path']).read_bytes(), b'image')

    def test_malformed_preview_is_dropped_on_load(self):
        self.session['items'] = [{'kind': 'assistant', 'text': 'Saved', 'image_path': []}]
        conversations.save(self.session)
        with mock.patch('builtins.print') as log:
            loaded = conversations.load(self.session['id'])
        self.assertEqual(loaded['items'], [{'kind': 'assistant', 'text': 'Saved'}])
        log.assert_called_once()
        self.assertIn('preview', log.call_args.args[0].lower())

    def test_malformed_file_references_are_rejected_on_load(self):
        self.session['items'] = [{'kind': 'assistant', 'text': '', 'file_references': [{'kind': 'file'}]}]
        conversations.save(self.session)
        with self.assertRaises(conversations.ConversationError):
            conversations.load(self.session['id'])


if __name__ == '__main__':
    unittest.main()
