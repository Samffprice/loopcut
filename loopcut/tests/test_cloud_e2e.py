"""The agent entry point with the real runtime, jobs, HTTP mock and result validation.

Only Blender's image/model creation and scene diff are replaced. Requests, file resolution,
uploads, SSE, downloads, cancellation, job_status and provenance stamping run unchanged.
"""

import hashlib
import io
import json
import os
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
from loopcut import (blobs, bundles, checkpoints, cloud, cloud_account, cloud_http, cloud_jobs, credentials, files,
                     mainthread, scene_diff, state, tools)
from loopcut.byok import runner
from mock_service import MockService


class Reply(io.BytesIO):
    def __init__(self, payload, status):
        super().__init__(json.dumps(payload).encode())
        self.status, self.headers = status, {}


DAILY_LIMIT = {'error': {'code': 'daily_limit_reached', 'type': 'limit_error', 'confirm_token': 'ct_test',
                         'message': "Today's cloud spending limit of 100 credits is reached."}}


class Blocks(list):
    def get(self, name):
        return next((block for block in self if block.name == name), None)


class Block(dict):
    def __init__(self, name):
        super().__init__()
        self.name = name


def uuid_hex(n):
    return f'{n:032x}'


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
        self.headers, self.refusals = [], []
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
        self.join_refreshes()
        self.assertTrue(self.runtime.refresh())
        self.addCleanup(self.cleanup_jobs)

    def join_refreshes(self):
        """A charged job's apply and a server limit refresh the catalog on a thread, which saves its cache into
        this test's folder. Each one is joined before that folder is removed, so nothing writes after the test."""
        threads = []
        def refresh_async():
            def refresh():
                self.runtime.refresh()
                mainthread.request_redraw()
            threads.append(threading.Thread(target=refresh, name='loopcut-manifest', daemon=True))
            threads[-1].start()
        def join():
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), 'a catalog refresh outlived its test')
        self.replace(self.runtime, 'refresh_async', refresh_async)
        self.addCleanup(join)  # After cleanup_jobs, before the patches stop and the folder goes.

    def replace(self, owner, name, value):
        context = patch.object(owner, name, value)
        context.start()
        self.addCleanup(context.stop)

    def transport(self, request, **options):
        if request.full_url.endswith('/v1/jobs') or request.full_url.endswith('/v1/quotes'):
            self.assertNotEqual(threading.get_ident(), self.main)
            self.requests.append((request.full_url, json.loads(request.data)))
            self.headers.append({k.lower(): v for k, v in request.header_items()})
            if request.full_url.endswith('/v1/jobs') and self.refusals:
                return self.refusals.pop(0)
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

    def pack(self, missing=(), unsupported=()):
        """Stand in for the scene snapshot and the child-Blender collect. Staging, manifest and upload are real;
        every collect writes new bytes, as a new snapshot of a live scene does."""
        steps, uploads = [], []
        def for_bundle(session):
            self.assertEqual(threading.get_ident(), self.main)
            steps.append(('snapshot', session['id']))
            return uuid_hex(len(steps))
        def bundle_from_checkpoint(session, checkpoint, staging):
            self.assertNotEqual(threading.get_ident(), self.main)
            steps.append(('collect', checkpoint))
            staging = Path(staging)
            staging.mkdir(parents=True)
            (staging / 'scene.blend').write_bytes(b'BLENDER-v502' + os.urandom(16))
            (staging / 'textures').mkdir()
            (staging / 'textures' / 'wood.png').write_bytes(b'png' * 100)
            info = {'blender': '5.2.0', 'created_at': '2026-10-17T12:00:00Z',
                    'scene': {'name': 'Scene', 'camera': 'Camera', 'frame_start': 1, 'frame_end': 3, 'fps': 24}}
            source = {'kind': 'checkpoint', 'session': session['id'], 'checkpoint': checkpoint}
            return {'manifest': bundles.manifest(staging, info, source), 'missing': list(missing),
                    'unsupported': list(unsupported), 'system': []}
        def discard(session, checkpoint):
            self.assertEqual(threading.get_ident(), self.main)
            steps.append(('discard', checkpoint))
        original = bundles.upload
        def upload(staging, manifest, *, client=None, **options):
            self.assertNotEqual(threading.get_ident(), self.main)
            self.assertEqual(set(options), {'cancel', 'progress', 'multipart'})
            self.assertEqual(options['multipart'], cloud.blob_multipart())
            uploads.append(original(staging, manifest, client=client, **options))
            return uploads[-1]
        self.replace(checkpoints, 'for_bundle', for_bundle)
        self.replace(checkpoints, 'bundle_from_checkpoint', bundle_from_checkpoint)
        self.replace(checkpoints, 'discard', discard)
        self.replace(bundles, 'upload', upload)
        return steps, uploads

    def sent(self, endpoint):
        return [payload for url, payload in self.requests if url.endswith('/v1/' + endpoint)]

    def prepared_inputs(self):
        folder = Path(self.directory.name) / 'jobs'
        return list(folder.iterdir()) if folder.is_dir() else []

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

    def test_failed_sound_effect_can_be_retried_even_with_its_key(self):
        # The contract keeps a key's job for good: resent with the same body it replays the failure, with
        # another body it is refused ("already used for a different request"). A retry is a new job.
        self.control(settings={'no_generation': False, 'private_mode': False}, fail_jobs=True)
        self.assertTrue(self.runtime.refresh())
        self.replace(cloud_jobs, '_add_audio', self.import_output)
        failed = self.execute('sfx', {'prompt': 'door creak'})
        self.assertFalse(failed.ok)
        self.assertIn('vendor_failed', failed.text)
        self.assertNotIn('idempotency_key', failed.text)  # Nothing to retry it with.
        key = self.requests[-1][1]['idempotency_key']
        self.control(fail_jobs=False)
        for arguments in ({'prompt': 'door creak', 'idempotency_key': key},
                          {'prompt': 'door creak, slower', 'idempotency_key': key}):
            result = self.execute('sfx', arguments)
            self.assertTrue(result.ok, result.text)
        keys = [body['idempotency_key'] for url, body in self.requests if url.endswith('/v1/jobs')]
        self.assertEqual(len(set(keys)), 3, keys)
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 3)
        self.assertEqual(len(self.applied), 2)

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
        self.assertNotIn('bundle', parameters['properties'])  # Loopcut packs the scene; the model never sends one.
        self.assertIn('quote_id', parameters['properties'])
        self.assertIn('idempotency_key', parameters['properties'])
        steps, uploads = self.pack()
        arguments = {'frame_start': 1, 'frame_end': 3}
        quoted = self.execute('render', arguments, consent=lambda details: False)
        self.assertFalse(quoted.ok)
        self.assertIn('approval_required', quoted.text)
        quote = next(iter(self.runtime.quotes.values()))
        self.assertIn('credits', quote)
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
        self.assertEqual([q['bundle'] for q in self.sent('quotes')], uploads)
        arguments.update(quote_id=quote['quote_id'], idempotency_key='test-render-retry')
        self.assertTrue(cloud.approval(cloud.PREFIX + 'render', arguments))
        self.assertIn(f"{quote['credits']} credits (${quote['credits'] / 100:.2f}), quoted",
                      cloud.describe(cloud.PREFIX + 'render', arguments)[0])
        def consent(details):
            self.assertEqual(details['credits'], quote['credits'])
            self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
            # A new call packs the scene as it is now: the old quote was for the old bundle, so it quotes again.
            self.assertEqual([q['bundle'] for q in self.sent('quotes')], uploads)
            return True
        # Keep result validation/stamping, but avoid sequencer/report Blender APIs for render.
        with patch.object(cloud_jobs, '_load_frames', self.import_output), patch.object(cloud_jobs, '_reports', return_value=[]):
            started = self.execute('render', arguments, consent=consent)
            self.assertTrue(started.ok, started.text)
            result = self.wait_status(json.loads(started.text)['job_id'])
        self.assertTrue(result.ok, result.text)
        payload = self.sent('jobs')[-1]
        self.assertEqual(payload['input'], {'frame_start': 1, 'frame_end': 3})
        self.assertEqual(payload['bundle'], uploads[-1])
        self.assertEqual(len(uploads), 2)
        self.assertNotEqual(payload['quote_id'], quote['quote_id'])  # The stale quote is never forwarded.
        self.assertIn(payload['quote_id'], self.runtime.quotes)
        self.assertEqual(payload['idempotency_key'], 'test-render-retry')

    def test_quote_approval_and_long_handoff_complete_in_one_call(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'render')
        descriptor['apply'] = 'none'  # Verify downloads without Blender's sequencer.
        self.pack()
        approvals = []
        def consent(details):
            self.assertEqual(len(self.runtime.quotes), 1)
            self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.requests], ['quotes'])
            approvals.append(details)
            return True
        stopped = threading.Event()
        started = self.execute('render', {'frame_start': 1, 'frame_end': 3}, stopped=stopped, consent=consent)
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

    def test_render_packs_the_live_scene_once_and_quotes_and_runs_with_its_hash(self):
        steps, uploads = self.pack()
        shown, original = [], cloud._progress
        def progress(name, value):
            shown.append(value)
            original(name, value)
        cards = []
        def consent(details):
            # The quote card: the bundle is uploaded and quoted, and nothing is packed again while it waits.
            self.assertEqual(len(uploads), 1)
            self.assertEqual([q['bundle'] for q in self.sent('quotes')], uploads)
            self.assertEqual(self.sent('jobs'), [])
            cards.append(details)
            return True
        # A bundle the model made up is never sent: Loopcut packs the open scene itself.
        arguments = {'frame_start': 1, 'frame_end': 3, 'bundle': 'f' * 64}
        with patch.object(cloud, '_progress', progress), patch.object(cloud_jobs, '_load_frames', self.import_output), \
                patch.object(cloud_jobs, '_reports', return_value=[]):
            started = self.execute('render', arguments, consent=consent)
            self.assertTrue(started.ok, started.text)
            handle = json.loads(started.text)['job_id']
            result = self.wait_status(handle)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.applied[-1]['id'], handle)  # Its frames came back through the render's apply.
        session = state.session()['id']
        self.assertEqual(steps, [('snapshot', session), ('collect', uuid_hex(1)), ('discard', uuid_hex(1))])
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(cards), 1)
        self.assertEqual(self.sent('jobs')[0]['bundle'], uploads[0])
        self.assertEqual(self.sent('jobs')[0]['input'], {'frame_start': 1, 'frame_end': 3})
        self.assertIn('Packing the scene…', shown)
        self.assertTrue(any(isinstance(v, str) and v.startswith('Uploading ') for v in shown), shown)
        self.assertEqual(self.prepared_inputs(), [])  # The staged bundle went with the call's inputs.
        image = self.execute()  # A tool without a bundle input never packs the scene.
        self.assertTrue(image.ok, image.text)
        self.assertEqual(len(steps), 3)
        self.assertNotIn('bundle', self.sent('jobs')[-1])

    def test_incomplete_scene_is_refused_before_any_upload_quote_or_job(self):
        steps, uploads = self.pack(
            missing=[{'type': 'textures', 'name': 'Wood', 'path': '//textures/wood.png'}],
            unsupported=[{'type': 'library', 'name': 'Set', 'path': '//set.blend',
                          'reason': 'Nested, missing or externally dependent linked library'}])
        consent = unittest.mock.Mock(return_value=True)
        result = self.execute('render', {'frame_start': 1}, consent=consent)
        self.assertFalse(result.ok)
        self.assertIn('bundle_incomplete', result.text)
        for name in ('Wood', '//textures/wood.png', 'Set', '//set.blend', 'externally dependent'):
            self.assertIn(name, result.text)
        consent.assert_not_called()
        self.assertEqual((uploads, self.requests), ([], []))
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
        self.assertEqual([step[0] for step in steps], ['snapshot', 'collect', 'discard'])
        self.assertEqual(self.prepared_inputs(), [])

    def test_python_drivers_are_named_with_what_to_do_and_counted_past_ten_before_any_upload(self):
        reason = bundles.PYTHON_DRIVER
        rows = [{'type': 'driver', 'name': f'Object "Rig.{n:03}"', 'path': 'location[2]', 'reason': reason}
                for n in range(12)]
        steps, uploads = self.pack(unsupported=rows)
        consent = unittest.mock.Mock(return_value=True)
        result = self.execute('render', {'frame_start': 1}, consent=consent)
        self.assertFalse(result.ok)
        first, _, extras = result.text.partition('\n')
        self.assertIn("can't pack this scene: 12 unsupported item(s)", first)
        self.assertIn('Nothing was uploaded', first)
        named = json.loads(extras)['error']['unsupported']
        self.assertEqual(named[0], f'driver Object "Rig.000" location[2]: {reason}')
        self.assertEqual((len(named), named[-1]), (11, 'and 2 more'))
        for text in ('simple expression', 'bake'):
            self.assertIn(text, named[0])
        consent.assert_not_called()
        self.assertEqual((uploads, self.requests), ([], []))
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)

    def test_limit_card_approve_resends_the_same_bundle_without_repacking(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'render')
        descriptor['apply'] = 'none'
        steps, uploads = self.pack()
        self.refusals.append(Reply(DAILY_LIMIT, 402))
        cards = []
        def consent(details):
            cards.append('limit' if details.get('limit') else 'cost')
            return True
        started = self.execute('render', {'frame_start': 1, 'frame_end': 3}, consent=consent)
        self.assertTrue(started.ok, started.text)
        self.assertEqual(cards, ['cost', 'limit'])
        jobs = self.sent('jobs')
        self.assertEqual([job['bundle'] for job in jobs], uploads * 2)
        self.assertEqual(jobs[0]['idempotency_key'], jobs[1]['idempotency_key'])
        self.assertEqual(self.headers[-1].get('x-loopcut-confirm'), 'ct_test')
        self.assertEqual(len(uploads), 1)
        self.assertEqual([step[0] for step in steps], ['snapshot', 'collect', 'discard'])
        self.assertTrue(self.wait_status(json.loads(started.text)['job_id']).ok)

    def test_skip_or_stop_while_packing_leaves_nothing_submitted(self):
        steps, uploads = self.pack()
        skipped = self.execute('render', {'frame_start': 1}, consent=lambda details: False)
        self.assertFalse(skipped.ok)
        self.assertIn('approval_required', skipped.text)
        self.assertEqual(len(self.sent('quotes')), 1)
        stopped, collect = threading.Event(), checkpoints.bundle_from_checkpoint
        def stop_while_collecting(*args):
            stopped.set()  # The panel's Stop, while the child Blender collects.
            return collect(*args)
        with patch.object(checkpoints, 'bundle_from_checkpoint', stop_while_collecting):
            canceled = self.execute('render', {'frame_start': 1}, stopped=stopped)
        self.assertFalse(canceled.ok)
        self.assertIn('canceled', canceled.text)
        self.assertEqual(len(uploads), 1)  # Stopped before its upload.
        self.assertEqual(len(self.sent('quotes')), 1)
        self.assertEqual(self.sent('jobs'), [])
        self.assertEqual(self.client.request('GET', '/mock/state')['jobs'], 0)
        self.assertEqual(cloud_account._pending(self.runtime), {})  # No retry key waits for a job that never was.
        self.assertEqual([step[0] for step in steps], ['snapshot', 'collect', 'discard'] * 2)
        self.assertEqual(self.prepared_inputs(), [])

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
