"""Local gateway stand-in: inject hosted prompts, then stream an OpenAI-compatible upstream.

    python3 prompt_proxy.py --upstream-base-url https://example.test/v1 \
        --upstream-key-env UPSTREAM_API_KEY --write-index /tmp/prompt-index.json

Point LOOPCUT_SERVICE_URLS and the eval endpoint at the printed base URL. Records
contain exact upstream request bytes and correlation metadata, never auth headers.
"""

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sys
import threading
from urllib.parse import urlsplit

import prompt_assembly

HOP_HEADERS = frozenset(('connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
                         'te', 'trailer', 'transfer-encoding', 'upgrade'))


def send_error(handler, status, error, version=None):
    data = json.dumps(error.envelope()).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Connection', 'close')
    if version:
        handler.send_header('x-loopcut-prompt-version', version)
    handler.end_headers()
    handler.close_connection = True
    handler.wfile.write(data)


def request_meta(headers):
    return {'prompt_id': headers.get('x-loopcut-prompt'),
            'x-loopcut-role': headers.get('x-loopcut-role'),
            'x-loopcut-conversation': headers.get('x-loopcut-conversation')}


class Proxy:
    def __init__(self, source, upstream_base_url, key, record=None):
        self.source, self.version = source, prompt_assembly.version(source)
        url = urlsplit(upstream_base_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.query or url.fragment):
            raise ValueError('upstream base URL must be an http(s) URL without credentials, query or fragment')
        self.host, self.port, self.tls = url.hostname, url.port, url.scheme == 'https'
        self.path = url.path.rstrip('/') + '/chat/completions'
        if not isinstance(key, str) or not key or any(not 33 <= ord(c) <= 126 for c in key):
            raise ValueError('upstream API key must be nonempty printable ASCII without whitespace')
        self.key = key
        self.record = Path(record) if record is not None else None
        self.lock = threading.Lock()
        self.count = 0
        if self.record is not None:
            self.record.mkdir(parents=True, exist_ok=True)
            self.count = max((int(p.stem) for p in self.record.glob('*.json') if p.stem.isdigit()), default=0)

    def save(self, data, headers):
        if self.record is None:
            return
        with self.lock:
            self.count += 1
            stem = f'{self.count:04d}'
            (self.record / f'{stem}.json').write_bytes(data)
            (self.record / f'{stem}.meta.json').write_text(json.dumps(request_meta(headers)) + '\n', encoding='utf-8')


def make_handler(proxy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            if self.path != '/v1/chat/completions':
                self.send_error(404)
                return
            # Absent or empty means pass-through (contract fixture empty-header).
            prompt_id = self.headers.get('x-loopcut-prompt') or None
            version = proxy.version if prompt_id in prompt_assembly.PROMPT_IDS and prompt_id in proxy.source.prompts else None
            try:
                length = int(self.headers.get('Content-Length', ''))
                if length < 0 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Unsupported request framing')
                data = self.rfile.read(length)
                if prompt_id is not None:
                    if prompt_id not in proxy.source.prompts or prompt_id not in prompt_assembly.PROMPT_IDS:
                        raise prompt_assembly.AssemblyError('unknown_prompt', 'Unknown prompt id.')
                    body = json.loads(data)
                    if not isinstance(body, dict):
                        raise ValueError('Expected an object')
                    body['messages'] = prompt_assembly.assemble(body.get('messages'), prompt_id, proxy.source)
                    data = json.dumps(body).encode('utf-8')  # The add-on's own serialisation (llm.stream_chat), for byte parity.
            except prompt_assembly.AssemblyError as ex:
                send_error(self, 400, ex, version)
                return
            except (ValueError, UnicodeError):
                send_error(self, 400, prompt_assembly.AssemblyError('invalid_request_error', 'Invalid request body.'), version)
                return
            connection_headers = {s.strip().lower() for s in self.headers.get('Connection', '').split(',')}
            blocked = HOP_HEADERS | connection_headers | {'host', 'authorization', 'content-length'}
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in blocked and not k.lower().startswith('x-loopcut-')}
            headers.update({'Authorization': 'Bearer ' + proxy.key, 'Content-Length': str(len(data))})
            connection = (http.client.HTTPSConnection if proxy.tls else http.client.HTTPConnection)(
                proxy.host, proxy.port, timeout=300)
            started = False
            try:
                proxy.save(data, self.headers)
                connection.request('POST', proxy.path, body=data, headers=headers)
                response = connection.getresponse()
                self.send_response(response.status)
                excluded = HOP_HEADERS | {s.strip().lower() for s in response.getheader('Connection', '').split(',')}
                for key, value in response.getheaders():
                    if key.lower() not in excluded and key.lower() != 'x-loopcut-prompt-version':
                        self.send_header(key, value)
                self.send_header('Connection', 'close')
                if version:
                    self.send_header('x-loopcut-prompt-version', version)
                self.end_headers()
                started = True
                self.close_connection = True
                # read1 returns available bytes, unlike read(n), which can wait for n bytes or EOF.
                while chunk := response.read1(64 * 1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, http.client.HTTPException):
                # Do not print upstream exceptions: they can contain URLs or credentials.
                if not started:
                    send_error(self, 502, prompt_assembly.AssemblyError('server_error', 'Upstream request failed.'), version)
            finally:
                connection.close()

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--upstream-base-url', required=True)
    parser.add_argument('--upstream-key-env', required=True)
    parser.add_argument('--prompts', type=Path)
    parser.add_argument('--skills', type=Path)
    parser.add_argument('--record', type=Path)
    parser.add_argument('--write-index', type=Path)
    args = parser.parse_args()
    key = os.environ.get(args.upstream_key_env)
    if not key:
        parser.error(f'set {args.upstream_key_env} to the upstream API key')
    try:
        source = prompt_assembly.load(args.prompts, args.skills)
        proxy = Proxy(source, args.upstream_base_url, key, args.record)
        if args.write_index:
            args.write_index.write_text(json.dumps(prompt_assembly.index(source), indent=2) + '\n', encoding='utf-8')
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(proxy))
    except (OSError, ValueError, KeyError) as ex:
        parser.exit(1, f'prompt_proxy: {ex}\n')

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    print(f'http://127.0.0.1:{server.server_port}/v1', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
