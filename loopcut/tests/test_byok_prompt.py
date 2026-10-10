"""V3: full turns to local third-party HTTP endpoints keep the open prompt and headers."""

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/addons_core'))
sys.modules.setdefault('bpy', types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import agent, cloud, config, conversations, router, state
from test_agent_loop import text_reply, tool_reply


class ByokPromptTest(unittest.TestCase):
    def run_provider(self, base_path, preferences):
        requests = []
        replies = [tool_reply('run_python', {'code': 'pass', 'summary': 'Probe'}), text_reply('Done.')]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, dict(self.headers),
                                 json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(replies.pop(0) if replies else text_reply('Unexpected request.'))

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}{base_path}'
        settings = dict(LOOPCUT_API_KEY=secrets.token_hex(24), LOOPCUT_BASE_URL=url, LOOPCUT_MODEL='byok-model',
                        LOOPCUT_ORCHESTRATE='true', LOOPCUT_ROUTER='on', LOOPCUT_AUTO_RUN='true',
                        LOOPCUT_AUTO_LOOK='false', LOOPCUT_WORKSPACE='false', LOOPCUT_CODEX_APP='false',
                        LOOPCUT_KEEP_CONTEXT='true', LOOPCUT_MAX_STEPS='4')
        env = {k: v for k, v in os.environ.items() if not k.startswith('LOOPCUT_')}
        env['LOOPCUT_SERVICE_URLS'] = ''
        if not preferences:
            env.update(settings)
        session = state.new_session()
        session['addons_line'] = 'Installed add-ons: BYOK test'
        session['messages'] = [{'role': 'user', 'content': 'Run the probe.'}]
        session['items'].append(state.item_user('Run the probe.'))
        turn = agent.Turn()
        ran = []
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(mock.patch.object(config, '_find_env_file', return_value=None))
            stack.enter_context(mock.patch.object(config, '_preference_values', return_value=settings if preferences else {}))
            stack.enter_context(mock.patch.object(cloud, 'sync_settings'))
            stack.enter_context(mock.patch.object(cloud, 'hosted_index', side_effect=AssertionError('off-service index')))
            ask = stack.enter_context(mock.patch.object(router, 'ask', side_effect=AssertionError('off-service router')))
            for name in ('_redraw', '_scene_snapshot'):
                stack.enter_context(mock.patch.object(agent, name, return_value=None))
            stack.enter_context(mock.patch.object(conversations, 'save'))
            stack.enter_context(mock.patch.object(agent, '_project_roots', return_value=()))
            agent._configure(turn, session)
            self.assertFalse(config.on_service(turn.config.base_url))
            self.assertIs(turn.spec, agent.SINGLE_SPEC)
            self.assertFalse(config.load_roles().orchestrate)
            self.assertIsNone(config.load_roles().router)
            session['busy'], session['turn'] = True, turn

            def execute(name, arguments):
                ran.append(name)
                return types.SimpleNamespace(text='Probe complete.', ok=True, changed=False, image_path=None)

            agent.run_loop(session, turn, turn.spec, run_tool=execute, ensure_checkpoint=lambda _: None)
            ask.assert_not_called()
        self.assertEqual(turn.outcome, 'completed')
        self.assertEqual(ran, ['run_python'])
        self.assertEqual(len(requests), 2)
        self.assertFalse(replies)
        for path, headers, body in requests:
            self.assertEqual(path, base_path + '/chat/completions')
            self.assertEqual(body['model'], 'byok-model')
            self.assertEqual(body['messages'][0], {'role': 'system', 'content':
                             agent.SYSTEM_PROMPT + '\n\n' + session['addons_line']})
            self.assertFalse(any(name.lower().startswith('x-loopcut-') for name in headers))
            self.assertNotIn('delegate', [tool['function']['name'] for tool in body['tools']])

    def test_ollama_like_url_with_environment_requesting_orchestration(self):
        self.run_provider('/v1', preferences=False)

    def test_custom_provider_url_with_preferences_requesting_orchestration(self):
        self.run_provider('/custom-provider/v1', preferences=True)


if __name__ == '__main__':
    unittest.main()
