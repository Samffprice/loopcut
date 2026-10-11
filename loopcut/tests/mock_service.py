"""Canonical contract handlers through in-memory HTTP, including incremental SSE."""

import importlib.util
import io
import json
import argparse
import queue
import threading
from email.message import Message
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

import platform_contract


class MockTransport:
    """Run the canonical mock's HTTP handler in memory, with no sockets or vendor calls.

    Retains request bodies so a test can prove the quote id and project sent at the HTTP
    boundary. State is the real mock state, including settings, credits and quote expiry.
    """
    def __init__(self, **options):
        source = platform_contract.require('mock', 'server.py')  # Skips the test without it.
        contract = source.parents[1]
        spec = importlib.util.spec_from_file_location('loopcut_contract_mock', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.state = module.State(**{'job_seconds': .001, 'skills_dir': contract / 'mock' / 'skills', **options})
        self.calls = []
        self.before_request = None

        class Handler(module.Handler):
            def send_response(self, status, message=None):
                self.status = status

            def send_header(self, keyword, value):
                self.response_headers[keyword] = value

            def end_headers(self):
                if isinstance(self.wfile, StreamReply):
                    self.wfile.status, self.wfile.headers = self.status, self.response_headers
                    self.wfile.ready.set()

        self.handler = Handler

    def __call__(self, request, *, timeout):
        parts = urlsplit(request.full_url)
        body = request.data or b''
        if hasattr(body, 'read'):
            body = body.read()
        payload = json.loads(body) if body and request.method in ('POST', 'PATCH') else None
        call = {'method': request.method, 'path': parts.path, 'body': payload}
        self.calls.append(call)
        if self.before_request:
            self.before_request(call)
        handler = self.handler.__new__(self.handler)
        handler.server = SimpleNamespace(state=self.state, server_address=('127.0.0.1', 3000))
        handler.path = parts.path + ('?' + parts.query if parts.query else '')
        handler.headers = Message()
        for key, value in request.header_items():
            handler.headers[key] = value
        if 'Content-Length' not in handler.headers:
            handler.headers['Content-Length'] = str(len(body))
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.response_headers = {}
        if parts.path.endswith('/events') and request.method == 'GET':
            response = handler.wfile = StreamReply()
            def serve():
                try:
                    handler.dispatch(request.method)
                finally:
                    response.queue.put(None)
                    response.ready.set()
            response.worker = threading.Thread(target=serve, daemon=True)
            response.worker.start()
            if not response.ready.wait(timeout):
                raise TimeoutError('Mock headers timed out')
            return response
        handler.dispatch(request.method)
        response = io.BytesIO(handler.wfile.getvalue())
        response.status, response.headers = handler.status, handler.response_headers
        return response


class StreamReply:
    """A pipe preserves SSE timing so cancellation is exercised before a job completes."""
    def __init__(self):
        self.queue, self.ready = queue.Queue(), threading.Event()
        self.buffer, self.closed, self.eof = b'', False, False
        self.status, self.headers = 500, {}

    def write(self, data):
        if self.closed:
            raise BrokenPipeError()
        self.queue.put(data)

    def flush(self):
        pass

    def readline(self, size=-1):
        while b'\n' not in self.buffer and not self.eof and (size < 0 or len(self.buffer) < size):
            data = self.queue.get(timeout=5)
            if data is None:
                self.eof = True
            else:
                self.buffer += data
        end = self.buffer.find(b'\n') + 1 or len(self.buffer)
        if size >= 0:
            end = min(end, size)
        line, self.buffer = self.buffer[:end], self.buffer[end:]
        return line

    def read(self, size=-1):
        while not self.eof and (size < 0 or len(self.buffer) < size):
            data = self.queue.get(timeout=5)
            if data is None:
                self.eof = True
            else:
                self.buffer += data
        end = len(self.buffer) if size < 0 else size
        data, self.buffer = self.buffer[:end], self.buffer[end:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class MockService:
    """Compatibility wrapper for tests that previously started the loopback server."""
    def __init__(self, *switches):
        parser = argparse.ArgumentParser()
        parser.add_argument('--live', action='store_true')
        parser.add_argument('--live-tools', default='')
        parser.add_argument('--job-seconds', type=float, default=.01)
        args = parser.parse_args(switches)
        self.transport = MockTransport(live=args.live, live_tools=args.live_tools.split(',') if args.live_tools else [],
                                       job_seconds=args.job_seconds)
        self.base_url = self.url = 'http://127.0.0.1:3000'
        self.key = 'lc_demo'

    def __enter__(self):
        from loopcut import cloud_http
        self.patch = patch.object(cloud_http, 'urllib_transport', self.transport)
        self.patch.start()
        return self

    def __exit__(self, *args):
        self.patch.stop()
