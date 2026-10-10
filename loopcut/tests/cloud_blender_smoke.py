"""Offline smoke for Blender's real main-thread pump; no service or scene changes.

Run: blender --background --factory-startup --python loopcut/tests/cloud_blender_smoke.py
The C3 boundary is fake here. Its actual exporters/importers need C3's own Blender checks.
"""

import io
import json
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
from loopcut import agent, cloud, cloud_http, mainthread


def main():
    main_id, calls = threading.get_ident(), []
    descriptor = {'name': 'runtime_smoke', 'namespace': 'loopcut_cloud', 'version': 1,
                  'title': 'Runtime smoke', 'description': 'Exercise the cloud thread boundary.',
                  'kind': 'assist', 'status': 'live', 'available': True, 'unavailable_reason': None,
                  'input_schema': {'type': 'object', 'properties': {}, 'required': []}, 'file_inputs': [],
                  'output': {'type': 'report', 'formats': ['json']}, 'apply': 'none', 'latency': 'seconds',
                  'cost': {'credits_estimate': 0, 'quote_required': False}, 'backends': ['managed']}
    manifest = {'account': {'credits': {'balance': 1}, 'settings': {'no_generation': False}},
                'flags': {'services': True, 'premium_skills': False}, 'tools': [descriptor], 'skills': []}
    def transport(request, *, timeout):
        assert request.full_url == 'https://service.test/v1/manifest'
        response = io.BytesIO(json.dumps(manifest).encode())
        response.status, response.headers = 200, {'ETag': '"smoke"'}
        return response
    module = types.ModuleType('loopcut.cloud_jobs')
    def resolve(descriptor, arguments, out_dir):
        calls.append(('resolve', threading.get_ident()))
        return {}
    def run(descriptor, arguments, files, *, project, on_progress=None, cancel=None, client=None):
        calls.append(('run', threading.get_ident()))
        on_progress({'pct': 100, 'message': 'Offline smoke complete'})
        return {'id': 'smoke', 'status': 'succeeded', 'outputs': [], 'error': None}
    def apply(descriptor, job):
        calls.append(('apply', threading.get_ident()))
        return 'Offline cloud smoke complete; scene unchanged.'
    module.resolve_files, module.run, module.apply_result = resolve, run, apply
    original_runtime, original_module = cloud._runtime, sys.modules.get('loopcut.cloud_jobs')
    try:
        with tempfile.TemporaryDirectory(prefix='loopcut-cloud-smoke-') as directory:
            # Ephemeral identity only; the injected transport cannot contact a service.
            mock_identity = 'lc_' + uuid.uuid4().hex
            client = cloud_http.Client(base_url='https://service.test', api_key=mock_identity, transport=transport)
            cloud._runtime = cloud.Runtime(client, directory, signed_in=True)
            assert cloud._runtime.refresh()
            sys.modules['loopcut.cloud_jobs'] = module
            results = []
            def worker():
                try:
                    results.append(agent._run_tool_on_main('loopcut_cloud__runtime_smoke', '{}'))
                except BaseException as ex:
                    results.append(ex)
            thread = threading.Thread(target=worker)
            thread.start()
            deadline = time.monotonic() + 10
            while thread.is_alive() and time.monotonic() < deadline:
                mainthread._pump()
                time.sleep(.01)
            thread.join(timeout=1)
            assert not thread.is_alive(), 'Main-thread pump timed out'
            assert results and not isinstance(results[0], BaseException), results
            assert results[0].ok, results[0].text
            assert calls[0] == ('resolve', main_id) and calls[-1] == ('apply', main_id), calls
            assert calls[1][0] == 'run' and calls[1][1] != main_id, calls
            print('Loopcut C2 Blender smoke: PASSED (offline transport, fake C3, scene unchanged)')
    finally:
        cloud._runtime = original_runtime
        if original_module is None:
            sys.modules.pop('loopcut.cloud_jobs', None)
        else:
            sys.modules['loopcut.cloud_jobs'] = original_module


if __name__ == '__main__':
    main()
