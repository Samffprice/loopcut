"""The agent entry point with the real runtime, jobs, HTTP mock and result validation.

Only Blender's image/model creation and scene diff are replaced. Requests, file resolution,
uploads, SSE, downloads, cancellation, job_status and provenance stamping run unchanged.
"""

import hashlib
import json
import queue
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))
from loopcut import blobs, cloud, cloud_account, cloud_http, cloud_jobs, credentials, files, mainthread, scene_diff, state, tools
from loopcut.byok import runner
from mock_service import MockService


class Blocks(list):
    def get(self, name):
        return next((block for block in self if block.name == name), None)


class Block(dict):
    def __init__(self, name):
        super().__init__()
        self.name = name


class CloudEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = MockService('--live-tools', 'image_generate,image_to_3d,render,sfx', '--job-seconds', '.3')
        cls.service.__enter__()
        cls.addClassCleanup(cls.service.__exit__, None, None, None)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.main = threading.get_ident()
        self.applied, self.requests, self.progress_threads = [], [], []
        self.data = types.SimpleNamespace(images=Blocks(), objects=Blocks())
        self.fake = types.SimpleNamespace(data=self.data, path=types.SimpleNamespace(abspath=lambda value: value),
                                         app=types.SimpleNamespace(version=(5, 2, 0)))
        context = patch.dict(sys.modules, {'bpy': self.fake})
        context.start()
        self.addCleanup(context.stop)
        self.replace(scene_diff, 'snapshot', lambda: {})
        self.replace(files, 'project_roots', lambda: (Path(tempfile.gettempdir()).resolve(),))
        self.replace(cloud, '_jobs', {})
        self.replace(cloud_jobs, '_registry', {})
        self.replace(state, '_session', state.new_session())
        self.replace(cloud_jobs, '_add_images', self.import_output)
        self.replace(cloud_jobs, '_model_import', self.import_output)
        # A snapshot must not read credentials or Blender after dispatch leaves main.
        def vendor_key(name):
            self.assertEqual(threading.get_ident(), self.main)
            return 'test-vendor-key'
        self.replace(credentials, 'vendor_key', vendor_key)
        self.client = cloud_http.Client(base_url=self.service.url, api_key=self.service.key,
                                        transport=self.transport, client_headers=cloud_http.contract_headers())
        self.control(reset=True, flags={'premium_skills': False})
        self.runtime = cloud.Runtime(self.client, self.directory.name, signed_in=True)
        self.replace(cloud, '_runtime', self.runtime)
        self.assertTrue(self.runtime.refresh())
        self.addCleanup(self.cleanup_jobs)

    def replace(self, owner, name, value):
        context = patch.object(owner, name, value)
        context.start()
        self.addCleanup(context.stop)

    def transport(self, request, **options):
        if request.full_url.endswith('/v1/jobs') or request.full_url.endswith('/v1/quotes'):
            self.assertNotEqual(threading.get_ident(), self.main)
            self.requests.append((request.full_url, json.loads(request.data)))
        return cloud_http.urllib_transport(request, **options)

    def control(self, **options):
        return self.client.request('POST', '/mock/control', options)

    def cleanup_jobs(self):
        cloud_jobs.cancel_all()
        deadline = time.monotonic() + 5
        while cloud_jobs.ui_rows() and time.monotonic() < deadline:
            self.pump()
            time.sleep(.01)
        self.assertEqual(cloud_jobs.ui_rows(), [])
        for row in cloud_jobs._registry.values():
            for output in row['job']['outputs']:
                if 'path' in output:
                    shutil.rmtree(Path(output['path']).parent, ignore_errors=True)
        self.pump()

    def pump(self):
        while True:
            try:
                fn, future = mainthread._jobs.get_nowait()
            except queue.Empty:
                return
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except BaseException as ex:
                    future.set_exception(ex)

    def execute(self, name='image_generate', arguments=None, stopped=None, consent=lambda details: True):
        full_name = cloud.PREFIX + name
        prepared = tools.execute(full_name, json.dumps({'prompt': 'tiny prop'} if arguments is None else arguments))
        if prepared.then is None:
            return prepared
        state.session()['items'].append({'name': full_name, 'status': 'running'})
        results = []
        def work():
            try:
                with cloud_account.approval_context(consent):
                    results.append(prepared.then((stopped or threading.Event()).is_set,
                                   lambda fn: mainthread.run_on_main(fn).result(timeout=5)))
            except BaseException as ex:
                results.append(ex)
        worker = threading.Thread(target=work)
        worker.start()
        deadline = time.monotonic() + 8
        while worker.is_alive() and time.monotonic() < deadline:
            self.pump()
            time.sleep(.002)
        worker.join(timeout=.1)
        self.assertFalse(worker.is_alive(), 'agent continuation did not finish')
        self.pump()
        if isinstance(results[0], BaseException):
            raise results[0]
        return results[0]

    def import_output(self, job):
        self.assertEqual(threading.get_ident(), self.main)
        for output in job['outputs']:
            data = Path(output['path']).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), output['sha256'])
            self.assertEqual(len(data), output['size'])
            collection = self.data.images if output['type'] == 'image' else self.data.objects
            collection.append(Block(output['name']))
        self.applied.append(job)
        return [output['name'] for output in job['outputs']]

    def long_image(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'image_generate')
        descriptor['latency'] = 'long'

    def render_arguments(self):
        bundle = Path(self.directory.name) / 'bundle.json'
        bundle.write_text('{"schema":"loopcut.bundle/0.1"}')
        digest = blobs.sha256(bundle)
        blobs.upload_missing([{'path': bundle, 'sha256': digest, 'size': bundle.stat().st_size,
                               'mime': 'application/json'}], client=self.client)
        return {'bundle': digest, 'frame_start': 1, 'frame_end': 3}

    def wait_status(self, handle):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.pump()
            result = self.execute('job_status', {'job_id': handle})
            if not result.ok or not result.text.startswith('{'):
                return result
            time.sleep(.01)
        self.fail('long job never finished')

    def test_seconds_and_minutes_upload_apply_and_progress_on_correct_threads(self):
        original = cloud._progress
        def progress(name, value):
            self.progress_threads.append(threading.get_ident())
            self.assertEqual(set(value), {'pct', 'message'})
            original(name, value)
        with patch.object(cloud, '_progress', progress):
            image = self.execute()
        self.assertTrue(image.ok, image.text)
        self.assertIn('Added image references', image.text)
        self.assertTrue(self.progress_threads)
        self.assertTrue(all(thread != self.main for thread in self.progress_threads))
        self.assertIn('%', state.session()['items'][-1]['output'])
        image_job = self.applied[0]
        model = self.execute('image_to_3d', {'image': image_job['outputs'][0]['path']})
        self.assertTrue(model.ok, model.text)
        self.assertEqual(len(self.applied), 2)
        self.assertEqual(self.requests[-1][1]['input'], {})
        self.assertEqual(self.requests[-1][1]['files'], {'image': image_job['outputs'][0]['sha256']})
        for block, job in zip([self.data.images[0], self.data.objects[0]], self.applied):
            self.assertEqual(json.loads(block['loopcut_provenance']), job['provenance'])
        self.assertEqual(cloud_jobs.ui_rows(), [])

    def test_long_job_uses_one_id_and_applies_once_after_originating_turn(self):
        self.long_image()
        stopped = threading.Event()
        started = self.execute(stopped=stopped)
        self.assertTrue(started.ok, started.text)
        handle = json.loads(started.text)['job_id']
        self.assertEqual(list(cloud_jobs._registry), [handle])
        self.assertEqual([row['id'] for row in cloud_jobs.ui_rows()], [handle])
        stopped.set()  # A completed turn must not cancel its detached job.
        result = self.wait_status(handle)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.execute('job_status', {'job_id': handle}).text, result.text)
        self.assertEqual(len(self.applied), 1)

    def test_long_apply_failure_stays_failed_and_is_not_applied_twice(self):
        self.long_image()
        handle = json.loads(self.execute().text)['job_id']
        with patch.object(cloud_jobs, '_add_images', side_effect=ValueError('cannot import')) as apply:
            result = self.wait_status(handle)
            repeated = self.execute('job_status', {'job_id': handle})
        self.assertFalse(result.ok)
        self.assertFalse(repeated.ok)
        self.assertEqual(result.text, repeated.text)
        apply.assert_called_once()

    def test_long_replay_keeps_one_worker_cancel_event_and_application(self):
        self.long_image()
        arguments = {'prompt': 'same prop', 'idempotency_key': 'repeat-long-call'}
        handle = json.loads(self.execute(arguments=arguments).text)['job_id']
        cancel = cloud_jobs._registry[handle]['cancel']
        replay = self.execute(arguments=arguments)
        self.assertEqual(json.loads(replay.text)['job_id'], handle)
        self.assertIs(cloud_jobs._registry[handle]['cancel'], cancel)
        self.assertTrue(self.wait_status(handle).ok)
        self.execute(arguments=arguments)
        self.assertTrue(self.execute('job_status', {'job_id': handle}).ok)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 1)

    def test_long_status_waits_for_verified_download_and_can_cancel_it(self):
        self.long_image()
        started, release = threading.Event(), threading.Event()
        original = cloud_jobs._download
        def download(client, job, cancel):
            started.set()
            release.wait(timeout=5)
            return original(client, job, cancel)
        with patch.object(cloud_jobs, '_download', download):
            handle = json.loads(self.execute().text)['job_id']
            try:
                self.assertTrue(started.wait(timeout=3))
                status = json.loads(self.execute('job_status', {'job_id': handle}).text)
                self.assertEqual(status['status'], 'running')
                self.assertEqual(status['progress']['message'], 'Downloading outputs')
                self.assertEqual([row['id'] for row in cloud_jobs.ui_rows()], [handle])
                self.execute('job_status', {'job_id': handle, 'cancel': True})
            finally:
                release.set()
            result = self.wait_status(handle)
        self.assertFalse(result.ok)
        self.assertIn('canceled', result.text)
        self.assertEqual(self.applied, [])

    def test_server_errors_keep_codes_and_extras(self):
        for code, extra in [('insufficient_credits', 'needed'), ('generation_disabled', 'scope'),
                            ('private_mode', 'data_class'), ('vendor_failed', 'refunded')]:
            with self.subTest(code=code):
                self.control(force_error=code)
                result = self.execute()
                self.assertFalse(result.ok)
                self.assertIn(code, result.text)
                self.assertIn(extra, result.text)
        self.assertEqual(self.applied, [])

    def test_live_account_policy_changes_are_refused_with_stale_manifest(self):
        for setting, tool, code, extra in [('no_generation', 'image_generate', 'generation_disabled', 'account'),
                                           ('private_mode', 'sfx', 'private_mode', 'C')]:
            with self.subTest(setting=setting):
                self.control(settings={'no_generation': False, 'private_mode': False})
                self.assertTrue(self.runtime.refresh())
                self.control(settings={setting: True})
                result = self.execute(tool)
                self.assertFalse(result.ok)
                self.assertIn(code, result.text)
                self.assertIn(extra, result.text)

    def test_private_manifest_and_byok_catalog_cannot_restore_refused_tool(self):
        from loopcut.byok import catalog
        self.runtime.keys_set = {'ELEVENLABS_API_KEY': True}
        self.runtime.preferred = {'sfx': 'byok'}
        self.assertNotIn(cloud.PREFIX + 'sfx', [s['function']['name'] for s in cloud.tool_schemas()])
        self.assertIn('private_mode', self.execute('sfx').text)
        local = next(d for d in catalog.descriptors(no_generation=False, private_mode=True,
                     keys_set=self.runtime.keys_set) if d['name'] == 'sfx')
        self.assertEqual(local['unavailable_reason'], 'private_mode')

    def test_async_vendor_failure_has_refund_and_long_failure_is_retained(self):
        self.control(fail_jobs=True)
        for long in (False, True):
            if long:
                self.long_image()
            result = self.execute()
            if long:
                handle = json.loads(result.text)['job_id']
                result = self.wait_status(handle)
                self.assertFalse(self.execute('job_status', {'job_id': handle}).ok)
            self.assertFalse(result.ok)
            self.assertIn('vendor_failed', result.text)
            self.assertIn('refunded', result.text)
        self.assertEqual(self.applied, [])

    def test_cancel_from_panel_and_job_status_share_the_same_event(self):
        self.long_image()
        for source in ('panel', 'tool'):
            with self.subTest(source=source):
                handle = json.loads(self.execute().text)['job_id']
                if source == 'panel':
                    self.assertTrue(cloud_jobs.cancel_job(handle))
                else:
                    self.execute('job_status', {'job_id': handle, 'cancel': True})
                self.assertTrue(cloud_jobs._registry[handle]['cancel'].is_set())
                result = self.wait_status(handle)
                self.assertFalse(result.ok)
                self.assertIn('canceled', result.text)
                self.assertIn('refunded', result.text)
        self.assertEqual(self.applied, [])

    def test_agent_stop_cancels_blocking_job_without_apply(self):
        stopped = threading.Event()
        with patch.object(cloud, '_progress', side_effect=lambda name, value: stopped.set()):
            result = self.execute(stopped=stopped)
        self.assertFalse(result.ok)
        self.assertIn('canceled', result.text)
        self.assertEqual(self.applied, [])

    def test_quote_bundle_and_retry_metadata_round_trip_through_schema_and_dispatch(self):
        descriptor = next(d for d in self.runtime.descriptors() if d['name'] == 'render')
        parameters = cloud.schema(descriptor)['function']['parameters']
        self.assertIn('bundle', parameters['required'])
        self.assertIn('quote_id', parameters['properties'])
        self.assertIn('idempotency_key', parameters['properties'])
        missing = self.execute('render', {})
        self.assertFalse(missing.ok)
        self.assertIn('bundle', missing.text)
        arguments = self.render_arguments()
        quoted = self.execute('render', arguments, consent=lambda details: False)
        self.assertFalse(quoted.ok)
        self.assertIn('approval_required', quoted.text)
        quote = next(iter(self.runtime.quotes.values()))
        self.assertIn('credits', quote)
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
        arguments.update(quote_id=quote['quote_id'], idempotency_key='test-render-retry')
        self.assertTrue(cloud.approval(cloud.PREFIX + 'render', arguments))
        self.assertIn(f"{quote['credits']} credits (quoted)", cloud.describe(cloud.PREFIX + 'render', arguments)[0])
        def consent(details):
            self.assertEqual(details['credits'], quote['credits'])
            self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
            self.assertEqual(sum(url.endswith('/v1/quotes') for url, _ in self.requests), 1)
            return True
        # Keep result validation/stamping, but avoid sequencer/report Blender APIs for render.
        with patch.object(cloud_jobs, '_load_frames', self.import_output), patch.object(cloud_jobs, '_reports', return_value=[]):
            started = self.execute('render', arguments, consent=consent)
            self.assertTrue(started.ok, started.text)
            result = self.wait_status(json.loads(started.text)['job_id'])
        self.assertTrue(result.ok, result.text)
        payload = self.requests[-1][1]
        self.assertEqual(payload['input'], {'frame_start': 1, 'frame_end': 3})
        self.assertEqual(payload['bundle'], arguments['bundle'])
        self.assertEqual(payload['quote_id'], quote['quote_id'])
        self.assertEqual(payload['idempotency_key'], 'test-render-retry')

    def test_quote_approval_and_long_handoff_complete_in_one_call(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'render')
        descriptor['apply'] = 'none'  # Verify downloads without Blender's sequencer.
        approvals = []
        def consent(details):
            self.assertEqual(len(self.runtime.quotes), 1)
            self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes'])
            approvals.append(details)
            return True
        stopped = threading.Event()
        started = self.execute('render', self.render_arguments(), stopped=stopped, consent=consent)
        self.assertTrue(started.ok, started.text)
        handle = json.loads(started.text)['job_id']
        self.assertEqual(list(cloud_jobs._registry), [handle])
        self.assertEqual(list(cloud._jobs), [handle])
        self.assertEqual([row['id'] for row in cloud_jobs.ui_rows()], [handle])
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes', 'jobs'])
        quote = self.runtime.quotes[self.requests[-1][1]['quote_id']]
        self.assertEqual([d['credits'] for d in approvals], [quote['credits']])
        stopped.set()
        result = self.wait_status(handle)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.execute('job_status', {'job_id': handle}).text, result.text)

    def test_byok_real_runner_and_shared_apply_with_main_thread_key_snapshot(self):
        self.runtime.preferred = {'image_generate': 'byok'}
        self.runtime.keys_set = {'OPENAI_API_KEY': True}
        # Replace only the vendor boundary. The BYOK envelope and saved output are real.
        artifact = {'name': 'image', 'mime': 'image/png', 'data': b'unit-test-image', 'meta': {}}
        with patch.object(runner.openai, 'execute', return_value=[artifact]) as vendor:
            result = self.execute()
        self.assertTrue(result.ok, result.text)
        self.assertEqual(vendor.call_args.args[-1], 'test-vendor-key')
        provenance = json.loads(self.data.images[0]['loopcut_provenance'])
        self.assertEqual(provenance['backend'], 'byok')
        self.assertEqual(self.requests, [])

    def test_canceled_byok_result_is_never_applied(self):
        self.runtime.preferred = {'image_generate': 'byok'}
        self.runtime.keys_set = {'OPENAI_API_KEY': True}
        stopped = threading.Event()
        def vendor(*args):
            stopped.set()
            return []
        with patch.object(runner.openai, 'execute', side_effect=vendor):
            result = self.execute(stopped=stopped)
        self.assertFalse(result.ok)
        self.assertIn('canceled', result.text)
        self.assertEqual(self.applied, [])
