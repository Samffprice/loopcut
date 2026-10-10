"""Real local HTTP traffic through the stand-in; no provider or real prompt text."""

import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
import prompt_assembly as pa
import prompt_proxy
from test_prompt_assembly import HARNESS, placeholder, source, write_source

KEY = 'dummy-upstream-secret-do-not-record'


def start_server(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    return server, thread


def stop_server(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(5)


class ProxyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.received = []
        self.release = threading.Event()
        received, release = self.received, self.release

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                received.append((raw, {k.lower(): v for k, v in self.headers.items()}, self.path))
                if self.headers.get('Test-SSE'):
                    self.send_response(201)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.write(b'data: first\n\n')
                    self.wfile.flush()
                    release.wait(5)
                    self.wfile.write(b'data: second\n\ndata: [DONE]\n\n')
                    self.wfile.flush()
                else:
                    self.send_response(429 if self.headers.get('Test-Error') else 200)
                    data = b'{"upstream":true}'
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('X-Upstream', 'yes')
                    self.end_headers()
                    self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.upstream, upstream_thread = start_server(Upstream)
        self.addCleanup(stop_server, self.upstream, upstream_thread)
        self.addCleanup(self.release.set)
        self.source = source()
        self.proxy = prompt_proxy.Proxy(self.source, f'http://127.0.0.1:{self.upstream.server_port}/v1', KEY,
                                       self.root / 'record')
        self.server, thread = start_server(prompt_proxy.make_handler(self.proxy))
        self.addCleanup(stop_server, self.server, thread)

    def request(self, raw, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(conn.close)
        conn.request('POST', '/v1/chat/completions', body=raw, headers=headers or {})
        return conn.getresponse()

    def test_injected_body_headers_records_and_secret_hygiene(self):
        body = {'model': 'dummy', 'messages': [{'role': 'system', 'content': '\n\nTail'},
                                              {'role': 'user', 'content': placeholder('craft/core', 'craft/swap-a')}]}
        headers = {'x-loopcut-prompt': 'worker', 'X-Loopcut-Role': 'worker', 'X-LOOPCUT-Conversation': 'conv',
                   'x-loopcut-unknown': 'strip', 'Authorization': 'Bearer client', 'X-Useful': 'keep',
                   'Connection': 'keep-alive, X-Hop', 'X-Hop': 'drop'}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            response = self.request(json.dumps(body).encode(), headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'{"upstream":true}')
        raw, received, path = self.received[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(json.loads(raw), {**body, 'messages': [{'role': 'system', 'content': 'Dummy worker 🌍\n\nTail'},
                                                               {'role': 'user', 'content': 'bud bud'}]})
        self.assertFalse(any(key.startswith('x-loopcut-') for key in received))
        self.assertNotIn('x-hop', received)
        self.assertEqual(received['x-useful'], 'keep')
        self.assertEqual(received['authorization'], 'Bearer ' + KEY)
        self.assertEqual(response.getheader('x-loopcut-prompt-version'), pa.version(self.source))
        self.assertEqual((self.root / 'record/0001.json').read_bytes(), raw)
        self.assertEqual(json.loads((self.root / 'record/0001.meta.json').read_text()),
                         {'prompt_id': 'worker', 'x-loopcut-role': 'worker', 'x-loopcut-conversation': 'conv'})
        self.assertNotIn(KEY, output.getvalue())
        for file in (self.root / 'record').iterdir():
            self.assertNotIn(KEY.encode(), file.read_bytes())

    def test_plain_body_is_byte_identical_even_if_not_json(self):
        for raw in (b'{ "messages" : [ {"role":"user","content":"<loopcut-skills invalid"} ] }\r\n', b'not-json\x00'):
            response = self.request(raw, {'x-loopcut-role': 'single', 'x-loopcut-test': 'remove'})
            self.assertEqual(response.status, 200)
            response.read()
            self.assertIsNone(response.getheader('x-loopcut-prompt-version'))
            self.assertEqual(self.received[-1][0], raw)
            self.assertFalse(any(k.startswith('x-loopcut-') for k in self.received[-1][1]))
        self.assertEqual((self.root / 'record/0002.json').read_bytes(), b'not-json\x00')

    def test_all_assembly_errors_are_400_and_do_not_forward_or_record(self):
        cases = [('unknown_prompt', 'nope', [], {}),
                 ('bad_system_message', 'worker', [{'role': 'system', 'content': []}], {}),
                 ('bad_skill_placeholder', 'worker', [{'role': 'user', 'content': '<loopcut-skills'}], {}),
                 ('unknown_skill', 'worker', [{'role': 'user', 'content': placeholder('missing')}], {'id': 'missing'}),
                 ('skills_over_limit', 'worker', [{'role': 'user', 'content': placeholder('extra') * 65}], {})]
        for code, prompt, messages, extra in cases:
            with self.subTest(code=code):
                response = self.request(json.dumps({'messages': messages}), {'x-loopcut-prompt': prompt})
                self.assertEqual(response.status, 400)
                error = json.loads(response.read())['error']
                self.assertEqual(error['type'], code)
                self.assertEqual(error['code'], code)
                self.assertTrue(error['message'])
                self.assertEqual({k: v for k, v in error.items() if k not in ('code', 'type', 'message')}, extra)
                self.assertEqual(response.getheader('x-loopcut-prompt-version'),
                                 None if prompt == 'nope' else pa.version(self.source))
        self.assertEqual(self.received, [])
        self.assertEqual(list((self.root / 'record').iterdir()), [])

    def test_malformed_json_is_400_with_version_for_valid_prompt(self):
        for raw in ('broken', '[]'):
            response = self.request(raw, {'x-loopcut-prompt': 'worker'})
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())['error']['code'], 'invalid_request_error')
            self.assertEqual(response.getheader('x-loopcut-prompt-version'), pa.version(self.source))

    def test_upstream_status_and_body_are_preserved(self):
        response = self.request(b'{"messages":[]}', {'x-loopcut-prompt': 'worker', 'Test-Error': 'yes'})
        self.assertEqual(response.status, 429)
        self.assertEqual(response.getheader('X-Upstream'), 'yes')
        self.assertEqual(response.read(), b'{"upstream":true}')
        self.assertEqual(response.getheader('x-loopcut-prompt-version'), pa.version(self.source))

    def test_sse_first_event_arrives_before_upstream_finishes(self):
        response = self.request(b'{"messages":[]}', {'x-loopcut-prompt': 'worker', 'Test-SSE': 'yes'})
        self.assertEqual(response.status, 201)
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
        try:
            self.assertEqual(response.readline(), b'data: first\n')
            self.assertFalse(self.release.is_set())
        finally:
            self.release.set()
        self.assertEqual(response.read(), b'\ndata: second\n\ndata: [DONE]\n\n')

    def test_record_number_continues_without_overwriting(self):
        (self.root / 'record/0007.json').write_bytes(b'old')
        proxy = prompt_proxy.Proxy(self.source, 'http://localhost/v1', KEY, self.root / 'record')
        proxy.save(b'new', {})
        self.assertEqual((self.root / 'record/0007.json').read_bytes(), b'old')
        self.assertEqual((self.root / 'record/0008.json').read_bytes(), b'new')


class ProxyCLITest(unittest.TestCase):
    def test_invalid_key_error_does_not_echo_key(self):
        for key in (KEY + '\n', KEY + '🌍'):
            with self.assertRaises(ValueError) as caught:
                prompt_proxy.Proxy(source(), 'http://127.0.0.1:1/v1', key)
            self.assertNotIn(KEY, str(caught.exception))

    def test_startup_index_and_signal_shutdown(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prompts, skills = write_source(root)
                index_file = root / 'index.json'
                process = subprocess.Popen([sys.executable, str(HARNESS / 'prompt_proxy.py'), '--port', '0',
                                            '--prompts', str(prompts), '--skills', str(skills),
                                            '--upstream-base-url', 'http://127.0.0.1:1/v1',
                                            '--upstream-key-env', 'TEST_PROXY_KEY', '--write-index', str(index_file)],
                                           env={**os.environ, 'TEST_PROXY_KEY': KEY}, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    # Startup is short; use communicate's timeout below for shutdown.
                    line = process.stdout.readline()
                    self.assertRegex(line, r'^http://127\.0\.0\.1:\d+/v1\n$')
                    self.assertEqual(json.loads(index_file.read_text()), pa.index(pa.load(prompts, skills)))
                    process.send_signal(sig)
                    out, err = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, err)
                    self.assertNotIn(KEY, line + out + err)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
