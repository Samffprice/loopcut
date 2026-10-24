"""Exercise client boundaries with the private platform's own current fixtures."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from test_autorig import human, inputs
import platform_contract
from loopcut import autorig, cloud, cloud_http, rig_bundle, rig_experimental, rig_transport
from test_autorig_r10 import PLATFORM_NOTICE

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
import autorig_contract
import autorig_fake


class ConsentTask(unittest.TestCase):
    def test_configured_or_required_contract_never_silently_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            for root, required in ((str(Path(tmp) / 'missing'), '0'), ('', '1')):
                with self.subTest(root=root, required=required), \
                        patch.dict('os.environ', {'LOOPCUT_CONTRACT_DIR': root, 'LOOPCUT_REQUIRE_CONTRACT': required}), \
                        self.assertRaises(FileNotFoundError):
                    platform_contract.require()
            with patch.dict('os.environ', {'LOOPCUT_CONTRACT_DIR': tmp}), self.assertRaises(FileNotFoundError):
                platform_contract.require('missing-fixture.json')
        with patch.dict('os.environ', {'LOOPCUT_CONTRACT_DIR': '', 'LOOPCUT_REQUIRE_CONTRACT': '0'}), \
                self.assertRaises(unittest.SkipTest):
            platform_contract.require()

    def test_omitted_false_and_true_consent_are_written_into_the_hashed_task(self):
        for consent in ({}, {'accept_experimental': False}, {'accept_experimental': True}):
            with self.subTest(consent=consent), tempfile.TemporaryDirectory() as tmp:
                points, triangles = human()
                obj, back = Mock(), Mock()
                bpy = types.SimpleNamespace(context=types.SimpleNamespace(scene=Mock()))
                with patch.dict(sys.modules, {'bpy': bpy}), patch.object(autorig, '_main'), \
                        patch.dict(autorig._prepared, {}, clear=True), \
                        patch.object(autorig, 'inspect', return_value=([obj], [('body', points, triangles)], back, {})), \
                        patch.object(autorig, 'fingerprint', return_value='a' * 64):
                    files = autorig.prepare({**inputs(), **consent}, tmp)
                    raw = files['rig_task'].read_bytes()
                    task = json.loads(raw)
                    if root := platform_contract.contract_dir():
                        contract = autorig_contract.Contract(root)
                        contract.check({**inputs(), **consent}, 'autorig-input')
                        contract.check(task, 'rig-task')
                    self.assertIs(task['accept_experimental'], consent.get('accept_experimental', False))
                    self.assertEqual(task, autorig._prepared[task['task_id']]['task'])
                    altered = {**task, 'accept_experimental': not task['accept_experimental']}
                    self.assertNotEqual(rig_transport.digest(raw),
                                        rig_transport.digest(rig_transport.json_bytes(altered)))


class PlatformRigFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = platform_contract.require()
        cls.contract = autorig_contract.Contract(cls.root)

    def fixture(self, name):
        return json.loads((self.root / 'fixtures' / name).read_bytes())

    def report(self, rig):
        report = self.fixture('rig-validation/example.json')
        # The platform supplies one generic worker report; its v3 policy requires
        # the failed anatomy check when pairing it with an experimental sidecar.
        if rig.get('tier') == 'experimental':
            report['checks'].append({**report['checks'][0], 'id': 'anatomy', 'status': 'fail'})
        return report

    def test_all_valid_sidecars_and_legacy_apply(self):
        paths = sorted((self.root / 'fixtures/rig').glob('*.json'))
        self.assertTrue({'standard-v3.json', 'experimental.json', 'two-joint-experimental.json'} <= {p.name for p in paths})
        for path in paths:
            rig = json.loads(path.read_bytes())
            task = self.fixture('rig-task/' + ('experimental.json' if 'tier' in rig else 'example.json'))
            with self.subTest(fixture=path.name):
                rig_bundle.validate(rig, self.report(rig), task)

    def test_all_platform_invalid_sidecars_are_rejected_by_client(self):
        paths = sorted((self.root / 'fixtures/invalid/rig').glob('*.json'))
        self.assertTrue({'standard_without_plan.json', 'experimental_with_plan.json',
                         'experimental_unknown_reason.json', 'experimental_joint_count.json',
                         'experimental_unweighted.json', 'experimental_one_joint.json'} <= {p.name for p in paths})
        for path in paths:
            rig = json.loads(path.read_bytes())
            task = self.fixture('rig-task/' + ('experimental.json' if 'tier' in rig else 'example.json'))
            with self.subTest(fixture=path.name), self.assertRaises(autorig.RigError):
                rig_bundle.validate(rig, self.report(rig), task)

    def test_quote_and_descriptor_use_platform_names_and_disclosure(self):
        descriptor = self.fixture('tool-descriptor/auto_rig.json')
        request = self.fixture('quote-request/auto_rig-v3.json')
        task = self.fixture('rig-task/experimental.json')
        quote = self.fixture('quote/auto_rig-v3.json')
        self.assertEqual((cloud_http.CONTRACT, descriptor['version'], request['tool_version']), ('0.9', 3, 3))
        self.assertIs(request['input']['accept_experimental'], task['accept_experimental'])
        # The card shows rig_text.QUOTE, never this field; the platform's line may trail the card's (R10).
        self.assertIn(quote['experimental_notice'], (PLATFORM_NOTICE, rig_experimental.QUOTE))
        self.assertNotIn('accept_experimental', cloud.schema(descriptor)['function']['parameters']['properties'])
        info = self.contract.schemas['rig']['properties']['experimental']
        self.assertEqual(set(rig_experimental.REASONS), set(info['properties']['reasons']['items']['enum']))
        self.assertEqual(set(self.fixture('rig/experimental.json')['experimental']), set(info['required']))

    def test_platform_valid_and_invalid_fixtures_conform_to_the_replay_oracle(self):
        for stem in ('rig', 'rig-task', 'rig-result', 'rig-validation', 'quote', 'quote-request', 'job-create-request'):
            for invalid in (False, True):
                directory = self.root / 'fixtures' / ('invalid' if invalid else '') / stem
                paths = sorted(directory.glob('*.json'))
                self.assertTrue(paths, f'Missing platform fixtures: {directory}')
                for path in paths:
                    value = json.loads(path.read_bytes())
                    with self.subTest(fixture=str(path.relative_to(self.root))):
                        if invalid:
                            with self.assertRaises(AssertionError):
                                self.contract.check(value, stem)
                        else:
                            self.contract.check(value, stem)

    def test_invalid_worker_reports_are_rejected_by_client(self):
        paths = sorted((self.root / 'fixtures/invalid/rig-validation').glob('*.json'))
        self.assertTrue(paths)
        for path in paths:
            for filename in ('standard-v3.json', 'experimental.json'):
                report = json.loads(path.read_bytes())
                rig = self.fixture('rig/' + filename)
                with self.subTest(fixture=path.name, tier=rig['tier']), self.assertRaises(autorig.RigError):
                    rig_bundle.validate(rig, report, self.fixture('rig-task/experimental.json'))

    def test_actual_fake_manifest_quote_and_failed_job_conform(self):
        # Exercise the HTTP handler and emitted bytes, not copies of its literals.
        from urllib.request import urlopen, Request
        with patch.dict('os.environ', {'LOOPCUT_CONTRACT_DIR': str(self.root)}):
            service = autorig_fake.Server(lambda task: self.fail('No successful job is needed'), seconds=0)
        try:
            manifest = json.load(urlopen(service.url + '/v1/manifest'))
            self.contract.check(manifest, 'manifest')
            self.contract.validator.check_schema(service.descriptor['input_schema'], 'autorig-input', self.contract.schemas)
            request = self.fixture('quote-request/auto_rig-v3.json')
            task = self.fixture('rig-task/experimental.json')
            model = b'offline-model-hash-only'
            task['model_sha256'] = rig_transport.digest(model)
            raw = rig_transport.json_bytes(task)
            request['files'] = {'model': task['model_sha256'], 'rig_task': rig_transport.digest(raw)}
            service.blobs.update({request['files']['model']: model, request['files']['rig_task']: raw})
            response = json.load(urlopen(Request(service.url + '/v1/quotes', data=json.dumps(request).encode(),
                headers={'Content-Type': 'application/json', 'x-loopcut-contract': cloud_http.CONTRACT})))
            self.contract.check(response, 'quote')
            service.jobs['job_failure'] = {'start': 0, 'task': task, 'canceled': False,
                                           'failure': True, 'data': None, 'charged': False}
            self.contract.check(service.job('job_failure'), 'job')
        finally:
            service.close()

    def test_replay_rejects_either_consent_mismatch_before_a_quote(self):
        for consent in (False, True):
            task = self.fixture('rig-task/experimental.json')
            task['accept_experimental'] = consent
            model = b'offline-model-hash-only'
            task['model_sha256'] = rig_transport.digest(model)
            raw = rig_transport.json_bytes(task)
            request = self.fixture('quote-request/auto_rig-v3.json')
            request['input']['accept_experimental'] = not consent
            request['files'] = {'model': task['model_sha256'], 'rig_task': rig_transport.digest(raw)}
            blobs = {request['files']['model']: model, request['files']['rig_task']: raw}
            with self.subTest(consent=consent), self.assertRaises(AssertionError):
                self.contract.request(request, 'quote-request', blobs)

    def test_job_cannot_change_an_internally_consistent_task_after_quote(self):
        quote = self.fixture('quote-request/auto_rig-v3.json')
        job = self.fixture('job-create-request/auto_rig-v3.json')
        task = self.fixture('rig-task/experimental.json')
        model = b'offline-model-hash-only'
        task['model_sha256'] = rig_transport.digest(model)
        raw = rig_transport.json_bytes(task)
        quote['files'] = {'model': task['model_sha256'], 'rig_task': rig_transport.digest(raw)}
        blobs = {quote['files']['model']: model, quote['files']['rig_task']: raw}
        job['files'] = copy.deepcopy(quote['files'])
        self.contract.bind_quote(quote, job['quote_id'], create=True)
        self.contract.request(job, 'job-create-request', blobs)
        for change in ('consent', 'model'):
            new_task, new_job = copy.deepcopy(task), copy.deepcopy(job)
            if change == 'consent':
                new_task['accept_experimental'] = new_job['input']['accept_experimental'] = False
            else:
                new_task['model_sha256'] = rig_transport.digest(b'changed-model')
                new_job['files']['model'] = new_task['model_sha256']
                blobs[new_task['model_sha256']] = b'changed-model'
            raw = rig_transport.json_bytes(new_task)
            new_job['files']['rig_task'] = rig_transport.digest(raw)
            blobs[new_job['files']['rig_task']] = raw
            with self.subTest(change=change), self.assertRaisesRegex(AssertionError, 'approved quote'):
                self.contract.request(new_job, 'job-create-request', blobs)

    def test_result_fixture_metadata_matches_all_three_artifacts(self):
        for filename in ('standard-v3.json', 'experimental.json'):
            rig = self.fixture('rig/' + filename)
            result = self.fixture('rig-result/' + filename)
            meta = {k: result[k] for k in ('tier', 'reasons')}
            meta['canonicalization'] = 'not_applicable' if rig['tier'] == 'experimental' else 'local_pending'
            if rig['tier'] == 'experimental':
                meta['experimental'] = rig['experimental']
            outputs = [{**artifact, 'meta': copy.deepcopy(meta)} for artifact in result['artifacts']]
            with self.subTest(fixture=filename):
                rig_bundle.match_metadata(rig, outputs)
                outputs[1]['meta']['tier'] = 'experimental' if rig['tier'] == 'standard' else 'standard'
                with self.assertRaises(autorig.RigError):
                    rig_bundle.match_metadata(rig, outputs)
