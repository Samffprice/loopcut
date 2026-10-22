"""Regressions for paid-call retries, file boundaries and untrusted cloud results.

Every transport is in memory; these checks neither start Blender nor open sockets.
"""

import copy
import hashlib
import io
import json
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.modules.setdefault('bpy', types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))
from loopcut import agent, cloud, cloud_account, cloud_http, cloud_jobs, codex_app, files, state, tools
from test_cloud import Reply, RuntimeFixture, manifest, served, tool
from test_cloud_jobs import DESCRIPTOR, PROJECT, event, job, output
from mock_service import MockTransport


class RuntimeReviewTest(RuntimeFixture):
    def test_incomplete_cost_cannot_become_an_unapproved_free_call(self):
        for cost in ({}, {'credits_estimate': 0}, {'quote_required': False},
                     {'credits_estimate': 1, 'quote_required': 'false'}):
            self.assertFalse(cloud._descriptor(tool(cost=cost)), cost)

    def test_free_managed_tool_is_available_at_zero_balance(self):
        data = manifest(tool('asset_search', kind='assist', backends=['managed'],
                             cost={'credits_estimate': 0, 'quote_required': False}))
        data['account']['credits']['balance'] = 0
        self.assertEqual([d['name'] for d in cloud.merge_tools(data, signed_in=True)], ['asset_search'])

    def test_raw_skill_text_is_authoritative_and_hash_checked(self):
        document, entry = served()
        raw = cloud._skill_bytes(document).decode().replace('title: Runtime integration', 'title: Actual title')
        document.update(text=raw, sha256=hashlib.sha256(raw.encode()).hexdigest())
        entry['sha256'] = document['sha256']
        verified = cloud._verified_skill(document, entry)
        self.assertEqual(verified['front_matter']['title'], 'Actual title')
        document['text'] += 'tampered'
        with self.assertRaises(ValueError):
            cloud._verified_skill(document, entry)

    def test_stale_skill_files_are_removed_when_index_changes(self):
        document, entry = served()
        runtime = self.runtime(Reply(manifest(skills=[entry])), Reply(document), Reply(manifest()))
        runtime.refresh()
        cached = list((runtime.cache_dir / 'skills').glob('*.json'))
        self.assertEqual(len(cached), 1)
        runtime.refresh()
        self.assertFalse(cached[0].exists())

    def test_server_error_urls_do_not_reach_tool_text(self):
        error = cloud_http.CloudError('Failed https://storage.test/a?signature=private', 'vendor_failed',
            extra={'debug': {'url': 'https://storage.test/a?signature=private'}, 'refunded': True})
        text = cloud.error_text(error)
        self.assertNotIn('signature=private', text)
        self.assertIn('refunded', text)

    def test_recheck_refuses_a_tool_switched_off_during_consent(self):
        runtime = self.runtime(Reply(manifest(tool())))
        runtime.refresh()
        descriptor = runtime.descriptors()[0]
        runtime.manifest['flags']['services'] = False
        with self.assertRaises(cloud_http.CloudError):
            cloud_account.recheck(runtime, descriptor, types.SimpleNamespace(policy={}))

    def test_pending_restrictions_apply_before_settings_patch_returns(self):
        runtime = self.runtime(Reply(manifest(tool())))
        runtime.refresh()
        runtime.account_pending = {'no_generation': True}
        self.assertTrue(runtime.no_generation())
        self.assertEqual(runtime.descriptors(), [])

    def test_signed_urls_and_account_key_are_not_saved_in_manifest_cache(self):
        data = manifest(tool(description='https://storage.test/a?signature=private'))
        data['debug'] = {'authorization': 'Bearer lc_demo'}
        runtime = self.runtime(Reply(data))
        runtime.refresh()
        text = (runtime.cache_dir / 'manifest.json').read_text()
        self.assertNotIn('signature=private', text)
        self.assertNotIn('Bearer lc_demo', text)

    def test_paid_tool_without_an_implemented_handler_is_not_exposed(self):
        for handler in ('import_material', 'apply_shape_keys'):
            runtime = self.runtime(Reply(manifest(tool(apply=handler))))
            runtime.refresh()
            self.assertEqual(runtime.descriptors(), [])

    def test_cloud_file_upload_requires_consent_even_without_agent_gate(self):
        runtime = self.runtime(Reply(manifest(tool(file_inputs=[{'field': 'reference', 'required': False,
                                             'max_mb': 20, 'accept': ['image/png']}]))))
        runtime.refresh()
        source = Path(self.directory.name) / 'outside.png'
        source.write_bytes(b'private image')
        fake = types.SimpleNamespace(data=types.SimpleNamespace(images={}, objects={}))
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(files, 'project_roots', return_value=()), \
                patch.object(cloud_jobs, 'run') as run:
            prepared = cloud.execute(cloud.PREFIX + 'image_generate', {'prompt': 'prop', 'reference': str(source)})
            result = prepared.then(lambda: False, lambda fn: fn())
        self.assertFalse(result.ok)
        self.assertIn('approval_required', result.text)
        run.assert_not_called()

    def test_malformed_file_cap_cannot_expose_a_tool(self):
        for limit in (None, '20', float('inf'), -1):
            descriptor = tool(file_inputs=[{'field': 'reference', 'required': True, 'max_mb': limit,
                                           'accept': ['image/png']}])
            self.assertFalse(cloud._descriptor(descriptor))

    def test_cancel_while_waiting_for_consent_removes_preparation_files(self):
        from loopcut import llm
        runtime = self.runtime(Reply(manifest(tool(cost={'credits_estimate': 100, 'quote_required': False}))),
                               Reply({'quote_id': 'qt_1', 'credits': 100, 'expires_at': '2099-01-01T00:00:00Z'}))
        runtime.refresh()  # The quote's reply: every paid call is priced before its consent card.
        with patch.object(cloud_jobs, 'resolve_files', return_value={}):
            prepared = cloud.execute(cloud.PREFIX + 'image_generate', {'prompt': 'prop'})
        with cloud_account.approval_context(Mock(side_effect=llm.Cancelled())), self.assertRaises(llm.Cancelled):
            prepared.then(lambda: False, lambda fn: fn())
        self.assertEqual(list((runtime.cache_dir / 'jobs').iterdir()), [])

    def test_vendor_key_rotation_during_consent_requires_a_new_call(self):
        from loopcut import credentials
        runtime = self.runtime(Reply(manifest(tool())), keys_set={'OPENAI_API_KEY': True},
                               preferred={'image_generate': 'byok'})
        runtime.refresh()
        descriptor = runtime.descriptors()[0]
        snapshot = types.SimpleNamespace(policy={}, vendor_key='original-test-credential')
        with patch.object(credentials, 'vendor_key', return_value='replacement-test-credential'), \
                self.assertRaises(cloud_http.CloudError) as caught:
            cloud_account.recheck(runtime, descriptor, snapshot)
        self.assertEqual(caught.exception.code, 'account_changed')


class QuotingModule:
    """cloud_jobs as authorize sees it: every paid call is priced first (cloud_account.quoted), and the server
    answers with a quote. Whatever run a test assigns sees only the submissions; a quote_required tool's own
    quote stays the test's."""
    def __init__(self, run):
        self.quotes = 0
        self.run = run

    def __setattr__(self, name, value):
        object.__setattr__(self, name, Priced(self, value) if name == "run" else value)


class Priced:
    """A test's run behind the server's quote; its own attributes (a Mock's call_args_list) stay readable."""
    def __init__(self, module, run):
        self.module, self.submit = module, run

    def __call__(self, descriptor, arguments, files, quote=False, **kwargs):
        if quote and not arguments.get("quote_id") and not descriptor["cost"].get("quote_required"):
            self.module.quotes += 1
            raise cloud_http.CloudError("Quote", "quote_required", extra={
                "quote_id": f"qt_{self.module.quotes}", "credits": descriptor["cost"]["credits_estimate"],
                "expires_at": "later"})
        return self.submit(descriptor, arguments, files, **kwargs)

    def __getattr__(self, name):
        return getattr(self.submit, name)


class ConsentReviewTest(unittest.TestCase):
    def test_existing_file_permission_never_waives_paid_consent(self):
        turn, card = agent.Turn(), {}
        file_details = {'required': True, 'text': 'Upload approved file', 'credits': None, 'file_access': True}
        cost_details = {'required': True, 'text': '100 credits', 'credits': 100}
        with patch.object(agent, '_redraw'), patch.object(agent, '_wait_for_approval', return_value=True) as approve:
            self.assertTrue(agent._cloud_consent(turn, card, True, file_details, files_allowed=True))
            approve.assert_not_called()
            self.assertTrue(agent._cloud_consent(turn, card, True, cost_details, files_allowed=True))
            approve.assert_called_once()

    def test_codex_dynamic_tool_binds_cloud_consent_even_with_auto_run(self):
        turn = types.SimpleNamespace(turn=agent.Turn(), session=state.new_session(),
            cfg=types.SimpleNamespace(auto_run=True), items=[], ensure_checkpoint=lambda session: None)
        turn.session['auto_run'] = True
        def run(name, arguments):
            cloud_account.confirm({'required': True, 'text': '100 credits quoted', 'credits': 100})
            return tools.ToolResult('done')
        turn.run_tool = run
        with patch.object(agent, '_redraw'), patch.object(agent, '_note_level'), \
                patch.object(agent, '_wait_for_approval', return_value=True) as approve:
            result = codex_app.CodexTurn.run_dynamic(turn, {}, cloud.PREFIX + 'render', '{}')
        self.assertTrue(result[1])
        approve.assert_called_once()

    def test_submit_without_authorize_cannot_charge(self):
        module = types.SimpleNamespace(run=Mock())
        runtime = types.SimpleNamespace(lock=threading.RLock(), quotes={})
        with self.assertRaises(cloud_http.CloudError):
            cloud_account.submit(module, {'backend': 'managed'}, {}, {}, project=PROJECT,
                cancel=threading.Event(), on_progress=None, runtime=runtime, client=None)
        module.run.assert_not_called()


class RetryReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = cloud_http.Client(base_url='https://service.test', api_key='lc_fixture',
                                        client_headers={}, transport=Mock())
        self.runtime = cloud.Runtime(self.client, self.temp.name, signed_in=True)
        self.descriptor = {**DESCRIPTOR, 'backend': 'managed', 'cost': {'credits_estimate': 2, 'quote_required': False}}
        self.cancel = threading.Event()
        self.posts = []
        self.lose_reply = True
        def run(descriptor, arguments, files, **kwargs):
            self.posts.append(dict(arguments))
            if self.lose_reply:
                raise cloud_http.CloudError('Lost acknowledgement', 'connection_failed')
            return job()
        self.module = QuotingModule(run)

    def call(self, arguments=None):
        with cloud_account.approval_context(lambda details: True):
            arguments = cloud_account.authorize(self.module, self.descriptor, arguments or {'prompt': 'prop'}, {},
                runtime=self.runtime, client=self.client, project=PROJECT, cancel=self.cancel)
            return cloud_account.submit(self.module, self.descriptor, arguments, {}, runtime=self.runtime,
                client=self.client, project=PROJECT, cancel=self.cancel, on_progress=None)

    def test_lost_acknowledgement_reuses_key_after_runtime_restart(self):
        with self.assertRaises(cloud_http.CloudError):
            self.call()
        self.runtime = cloud.Runtime(self.client, self.temp.name, signed_in=True)
        self.lose_reply = False
        self.call()
        self.assertTrue(self.posts[0].get('idempotency_key'))
        self.assertEqual(self.posts[0], self.posts[1])
        self.call()
        self.assertNotEqual(self.posts[1]['idempotency_key'], self.posts[2]['idempotency_key'])
        for path in Path(self.temp.name).rglob('*.json'):
            self.assertNotIn('"prompt"', path.read_text())

    def test_quote_and_key_survive_lost_acknowledgement_and_restart(self):
        self.descriptor['cost']['quote_required'] = True
        quotes = []
        def run(descriptor, arguments, files, **kwargs):
            if not arguments.get('quote_id'):
                quote = {'quote_id': f'qt_{len(quotes)}', 'credits': 10, 'expires_at': 'later'}
                quotes.append(quote)
                raise cloud_http.CloudError('Quote', 'quote_required', extra=quote)
            self.posts.append(dict(arguments))
            if self.lose_reply:
                raise cloud_http.CloudError('Lost acknowledgement', 'connection_failed')
            return job()
        self.module.run = run
        with self.assertRaises(cloud_http.CloudError):
            self.call()
        self.runtime = cloud.Runtime(self.client, self.temp.name, signed_in=True)
        self.lose_reply = False
        self.call()
        self.assertEqual(len(quotes), 1)
        self.assertEqual(self.posts[0], self.posts[1])

    def test_quote_for_different_inputs_is_not_presented_as_current_price(self):
        self.descriptor['cost']['quote_required'] = True
        old = {'quote_id': 'qt_old', 'credits': 1, 'expires_at': 'later'}
        self.runtime.quotes['qt_old'] = old
        new = {'quote_id': 'qt_new', 'credits': 90, 'expires_at': 'later'}
        self.module.run = Mock(side_effect=cloud_http.CloudError('Quote', 'quote_required', extra=new))
        seen = []
        with cloud_account.approval_context(lambda d: seen.append(d) or False), self.assertRaises(cloud_http.CloudError):
            cloud_account.authorize(self.module, self.descriptor, {'prompt': 'different', 'quote_id': 'qt_old'}, {},
                runtime=self.runtime, client=self.client, project=PROJECT, cancel=self.cancel)
        self.assertEqual(seen[0]['credits'], 90)

    def test_monitor_http_error_does_not_forget_accepted_submission(self):
        def run(descriptor, arguments, files, **kwargs):
            self.posts.append(dict(arguments))
            raise cloud_http.CloudError('Cannot poll', 'tool_disabled', 403, {'job': job('running')})
        self.module.run = run
        for _ in range(2):
            with self.assertRaises(cloud_http.CloudError):
                self.call()
        self.assertEqual(self.posts[0]['idempotency_key'], self.posts[1]['idempotency_key'])

    def test_retry_after_a_failed_job_gets_a_new_key_even_when_the_model_resends_it(self):
        # A key names one job (SPEC §3.4): resent, it replays the failure or is refused for other inputs.
        failed = cloud_http.CloudError('Vendor failed', 'vendor_failed', None, {'refunded': True, 'job': job('failed')})
        outcomes = [failed, failed, job(), job()]
        def run(descriptor, arguments, files, **kwargs):
            self.posts.append(dict(arguments))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self.module.run = run
        with self.assertRaises(cloud_http.CloudError):
            self.call()
        spent = self.posts[0]['idempotency_key']
        with self.assertRaises(cloud_http.CloudError):
            self.call({'prompt': 'prop', 'idempotency_key': spent})
        self.runtime = cloud.Runtime(self.client, self.temp.name, signed_in=True)  # Remembered across a restart.
        self.call({'prompt': 'prop, louder', 'idempotency_key': spent})
        self.call({'prompt': 'prop', 'idempotency_key': spent})
        keys = [post['idempotency_key'] for post in self.posts]
        self.assertEqual(len(set(keys)), 4, keys)
        self.assertEqual(json.loads((self.runtime.cache_dir / 'pending.json').read_text()), {})
        for path in Path(self.temp.name).rglob('*.json'):
            self.assertNotIn(spent, path.read_text())  # Kept as a hash only.

    def test_retry_of_a_lost_or_succeeded_request_keeps_its_key_and_other_inputs_never_borrow_it(self):
        with self.assertRaises(cloud_http.CloudError):
            self.call({'prompt': 'prop', 'idempotency_key': 'model-key'})
        self.lose_reply = False
        self.call()  # The pending key: the server replays the job the lost reply was for.
        self.call({'prompt': 'prop', 'idempotency_key': 'model-key'})  # Succeeded: replayed, not charged again.
        self.call({'prompt': 'other prop', 'idempotency_key': 'model-key'})
        keys = [post['idempotency_key'] for post in self.posts]
        self.assertEqual(keys[:3], ['model-key'] * 3)
        self.assertNotEqual(keys[3], 'model-key')
        # A key the service refused as another request's is spent too, even one this client never sent.
        refused = cloud_http.CloudError('This key was already used for a different request.', 'invalid_input', 422,
                                        {'field': 'idempotency_key'})
        self.module.run = Mock(side_effect=[refused, job()])
        with self.assertRaises(cloud_http.CloudError):
            self.call({'prompt': 'third prop', 'idempotency_key': 'elsewhere'})
        self.call({'prompt': 'third prop', 'idempotency_key': 'elsewhere'})
        sent = [call.args[1]['idempotency_key'] for call in self.module.run.call_args_list]
        self.assertEqual(sent[0], 'elsewhere')
        self.assertNotEqual(sent[1], 'elsewhere')

    def test_invalid_retry_key_is_rejected_before_persisting_or_submitting(self):
        with self.assertRaises(cloud_http.CloudError) as caught:
            self.call({'prompt': 'prop', 'idempotency_key': 'x' * 201})
        self.assertEqual(caught.exception.code, 'invalid_input')
        self.assertEqual(self.posts, [])
        self.assertFalse((self.runtime.cache_dir / 'pending.json').exists())

    def test_contract_replay_after_two_lost_replies_and_restart_charges_once(self):
        service = MockTransport(live=True)
        lose = True
        def send(request, **options):
            response = service(request, **options)
            if lose and request.full_url.endswith('/v1/jobs'):
                response.close()
                raise urllib.error.URLError('reply lost after acceptance')
            return response
        self.client = cloud_http.Client(base_url='http://127.0.0.1:3000', api_key='lc_demo', transport=send,
                                       client_headers={'x-loopcut-contract': '0.2', 'x-loopcut-client': 'test'})
        self.runtime = cloud.Runtime(self.client, self.temp.name, signed_in=True)
        self.runtime.refresh()
        self.descriptor = next(d for d in self.runtime.descriptors() if d['name'] == 'image_generate')
        self.module = cloud_jobs
        with patch.object(cloud_jobs, '_registry', {}), patch.object(cloud_jobs, '_redraw'), \
                patch.object(cloud_jobs, '_download', side_effect=lambda client, result, cancel: result):
            with self.assertRaises(cloud_http.CloudError):
                self.call()
            self.runtime = cloud.Runtime(self.client, self.temp.name, signed_in=True)
            lose = False
            result = self.call()
        posts = [c['body'] for c in service.calls if c['path'] == '/v1/jobs']
        self.assertEqual(len(posts), 3)
        self.assertTrue(all(body == posts[0] for body in posts))
        self.assertEqual(len(service.state.jobs), 1)
        self.assertEqual(service.state.balance, 1840 - result['credits']['charged'])


class FileReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fake = types.SimpleNamespace(data=types.SimpleNamespace(images={}, objects={}),
                                         path=types.SimpleNamespace(abspath=lambda path: path))
        self.descriptor = {'file_inputs': [{'field': 'reference'}]}

    def test_hidden_files_are_refused_even_if_the_path_exists(self):
        secret = self.root / '.private.png'
        secret.write_bytes(b'private')
        with patch.dict(sys.modules, {'bpy': self.fake}), self.assertRaises(tools.ToolError):
            cloud_jobs.resolve_files(self.descriptor, {'reference': str(secret)}, self.root / 'export')

    def test_symlink_to_loopcut_data_is_refused(self):
        private = self.root / 'private'
        private.mkdir()
        (private / 'account.png').write_bytes(b'private')
        link = self.root / 'photo.png'
        link.symlink_to(private / 'account.png')
        with patch.dict(sys.modules, {'bpy': self.fake}), patch.object(files, '_forbidden_roots', return_value=(private,)), \
                self.assertRaises(tools.ToolError):
            cloud_jobs.resolve_files(self.descriptor, {'reference': str(link)}, self.root / 'export')

    def test_relative_input_uses_the_same_base_as_file_approval(self):
        source = self.root / 'photo.png'
        source.write_bytes(b'photo')
        with patch.dict(sys.modules, {'bpy': self.fake}), patch.object(files, 'relative_base', return_value=(self.root, '')):
            resolved = cloud_jobs.resolve_files(self.descriptor, {'reference': source.name}, self.root / 'export')
        self.assertEqual(resolved['reference'], source.resolve())


class JobsReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for context in (patch.object(cloud_jobs, '_registry', {}), patch.object(cloud_jobs, '_redraw')):
            context.start()
            self.addCleanup(context.stop)

    def test_terminal_status_before_done_is_not_ready_for_application(self):
        result = job('failed')
        result['error'] = None  # A status event precedes the final error envelope.
        cloud_jobs._remember(result, 'Job', threading.Event())
        self.assertEqual(cloud_jobs.job_status(result['id'])['status'], 'running')

    def test_replayed_sse_output_is_deduplicated_by_event_id(self):
        final = job()
        one, replay = output(), output()
        replay['url'] += '&renewed=yes'
        seen = []
        client = types.SimpleNamespace(stream=lambda *a, **k: Stream(event('output', one, 1) +
            event('output', replay, 1) + event('done', final, 2)))
        initial = job('queued')
        with patch.object(cloud_jobs, '_update', side_effect=lambda value, callback, **kw: seen.append(copy.deepcopy(value))):
            cloud_jobs._follow(client, initial, threading.Event(), None)
        self.assertLessEqual(max(len(value['outputs']) for value in seen), 1)

    def test_job_status_does_not_return_signed_download_urls(self):
        result = job()
        cloud_jobs._remember(result, 'Job', threading.Event())
        cloud_jobs._registry[result['id']]['ready'] = True
        self.assertNotIn('signature=test', json.dumps(cloud_jobs.job_status(result['id'])))

    def test_provenance_drops_unknown_fields_with_credentials(self):
        block = {}
        provenance = {**job()['provenance'], 'download_url': output()['url']}
        cloud_jobs.stamp_provenance([block], provenance)
        self.assertNotIn('signature=test', block['loopcut_provenance'])

    def test_report_text_redacts_signed_urls_before_history(self):
        path = self.root / 'report.txt'
        path.write_text('See https://storage.test/a?signature=private')
        text = cloud_jobs._report_text({'name': 'Report', 'path': str(path)})
        self.assertNotIn('signature=private', text)

    def test_oversized_outputs_are_refused_before_download(self):
        result = job()
        result['outputs'][0]['size'] = 1 << 60
        client = types.SimpleNamespace(download=Mock())
        with self.assertRaises(cloud_http.CloudError):
            cloud_jobs._download(client, result, threading.Event())
        client.download.assert_not_called()

    def test_job_id_length_is_bounded_before_creating_a_folder(self):
        result = job()
        result['id'] = result['provenance']['job_id'] = 'job_' + 'x' * 300
        with self.assertRaises(cloud_http.CloudError):
            cloud_jobs._job(result)

    def test_cancel_acknowledgement_must_be_terminal(self):
        cancel = threading.Event()
        cancel.set()
        client = types.SimpleNamespace(request=Mock(side_effect=[job('running'), job('canceled')]))
        result = cloud_jobs._follow(client, job('queued'), cancel, None)
        self.assertEqual(result['status'], 'canceled')

    def test_retry_can_resume_a_job_whose_monitor_lost_connection(self):
        initial = job('running')
        cloud_jobs._remember(initial, 'Job', threading.Event())
        cloud_jobs._registry[initial['id']]['error'] = cloud_http.CloudError('Lost stream', 'connection_failed')
        self.assertTrue(cloud_jobs._remember(initial, 'Job', threading.Event()))
        self.assertEqual(cloud_jobs.job_status(initial['id'])['status'], 'running')

    def test_material_variants_cannot_clear_existing_scene_metadata(self):
        path = self.root / 'model.gltf'
        path.write_text(json.dumps({'extensions': {'KHR_materials_variants': {'variants': []}}}))
        with self.assertRaises(cloud_http.CloudError):
            cloud_jobs._check_gltf(path)

    def test_long_output_cannot_apply_to_a_different_scene(self):
        origin = object()
        module = types.SimpleNamespace(job_status=lambda id: job(), apply_result=Mock())
        record = {'module': module, 'descriptor': DESCRIPTOR, 'result': None, 'scene': origin}
        fake = types.SimpleNamespace(context=types.SimpleNamespace(scene=object()))
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(cloud, '_jobs', {'job_test': record}), \
                patch.object(cloud, '_apply', return_value=tools.ToolResult('applied')) as apply:
            result = cloud._job_status({'job_id': 'job_test'})
        self.assertFalse(result.ok)
        apply.assert_not_called()

    def test_long_cancellation_releases_pending_submission(self):
        error = cloud_http.CloudError('Canceled', 'canceled', extra={'job': job('canceled')})
        record = {'module': types.SimpleNamespace(job_status=Mock(side_effect=error)), 'result': None,
                  'handle_error': Mock(), 'report_error': Mock(), 'complete': Mock()}
        with patch.object(cloud, '_jobs', {'job_test': record}):
            result = cloud._job_status({'job_id': 'job_test'})
        self.assertFalse(result.ok)
        record['complete'].assert_called_once()

    def test_temporary_monitor_error_does_not_permanently_poison_long_handle(self):
        error = cloud_http.CloudError('Lost stream', 'connection_failed')
        record = {'module': types.SimpleNamespace(job_status=Mock(side_effect=error)), 'result': None,
                  'handle_error': Mock(), 'report_error': Mock()}
        with patch.object(cloud, '_jobs', {'job_test': record}):
            cloud._job_status({'job_id': 'job_test'})
        self.assertIsNone(record['result'])

    def test_result_names_and_progress_are_safe_before_blender_and_panel_use(self):
        value = job()
        value['outputs'][0]['name'] = 'https://storage.test/a?signature=private'
        value['progress']['message'] = 'Authorization: Bearer lc_example_test_value'
        checked = cloud_jobs._job(value)
        self.assertNotIn('signature=private', checked['outputs'][0]['name'])
        self.assertNotIn('lc_example_test_value', checked['progress']['message'])
        self.assertEqual(checked['outputs'][0]['url'], value['outputs'][0]['url'])

    def test_sse_output_metadata_has_an_aggregate_memory_limit(self):
        value = job()
        value['outputs'] = [{**output(), 'meta': {'trace': 'x' * 100}} for _ in range(2)]
        with patch.object(cloud_jobs, 'MAX_EVENT', 256), self.assertRaises(cloud_http.CloudError):
            cloud_jobs._output_limits(value)

    def test_empty_results_do_not_leave_unused_download_folders(self):
        value = job()
        value['outputs'] = []
        with patch.object(cloud_jobs.tempfile, 'mkdtemp') as mkdir:
            self.assertEqual(cloud_jobs._download(None, value, threading.Event()), value)
        mkdir.assert_not_called()


class Stream(io.BytesIO):
    headers = {'Content-Type': 'text/event-stream'}


if __name__ == '__main__':
    unittest.main()
