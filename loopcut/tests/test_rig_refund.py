"""Owned-job refund receipts, uncertain outcomes and retry without another charge."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/addons_core'))
sys.path.insert(0, str(Path(__file__).parent))
from loopcut import cloud, cloud_http, cloud_jobs, rig_refund
from test_cloud_jobs import Reply, job, output


def rig_job():
    value = job()
    value.update(tool='auto_rig', outputs=[], credits={'reserved': 20, 'charged': 20, 'refunded': 0})
    value['provenance'].update(tool='auto_rig', kind='assist')
    return value


def receipt(**changes):
    return {'job_id': 'job_test', 'reason': 'validation_failed', 'status': 'refunded', 'review_reason': None,
            'credits_refunded': 20, 'reported_at': '2026-10-22T20:00:00Z', 'message': 'Server text is not displayed.',
            'support_url': rig_refund.SUPPORT, **changes}


class Service:
    def __init__(self, response=None):
        self.response = response or receipt()
        self.posts, self.calls, self.failures, self.fail_get = [], [], 0, False
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()

    def __call__(self, request, *, timeout):
        self.calls.append((threading.get_ident(), request.full_url, dict(request.header_items())))
        if request.method == 'POST':
            self.posts.append(json.loads(request.data))
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError('Test did not release the report request')
            if self.failures:
                self.failures -= 1
                raise urllib.error.URLError('lost reply')
            return Reply(self.response)
        if self.fail_get:
            raise urllib.error.URLError('balance refresh unavailable')
        value = rig_job()
        if self.response['status'] == 'refunded':
            value['credits'].update(charged=0, refunded=20)
        return Reply(value)

    def client(self):
        return cloud_http.Client(base_url='https://original.test', api_key='lc_test', transport=self,
            client_headers={'x-loopcut-contract': '0.8', 'x-loopcut-client': 'test'})


class RefundReceipts(unittest.TestCase):
    def make(self, service=None, value=None):
        service = service or Service()
        report = rig_refund.ApplyFailure(value or rig_job(), 'validation_failed', service.client(), lambda row: None)
        return service, report

    def test_closed_receipt_rejects_foreign_malformed_and_unsafe_values(self):
        changes = ({'job_id': 'job_other'}, {'reason': []}, {'status': {}}, {'extra': True},
                   {'credits_refunded': True}, {'credits_refunded': -1}, {'credits_refunded': 21},
                   {'support_url': 'https://attacker.test'}, {'reported_at': 'yesterday'},
                   {'review_reason': 'late'}, {'status': 'review', 'review_reason': None},
                   {'status': 'review', 'review_reason': []}, {'status': 'review', 'review_reason': 'limit'})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(cloud_http.CloudError):
                rig_refund.validate(receipt(**change), rig_job())
        self.assertEqual(rig_refund.validate(receipt(reason='other'), rig_job())['reason'], 'other')

    def test_original_account_frozen_request_private_and_network_off_main(self):
        service = Service()
        client = service.client()
        report = rig_refund.ApplyFailure(rig_job(), 'unsupported_hands', client, lambda row: None)
        client.base_url, client.api_key = 'https://different.test', 'lc_other'
        report.start()
        value = report.wait()
        self.assertEqual(value['status'], 'refunded')
        self.assertEqual(value['job']['credits']['charged'], 0)
        self.assertTrue(value['balance_refreshed'])
        self.assertEqual(service.posts, [{'reason': 'canonicalize_refused'}])
        for tid, url, headers in service.calls:
            self.assertNotEqual(tid, threading.get_ident())
            self.assertTrue(url.startswith('https://original.test/v1/jobs/job_test'))
            self.assertEqual({k.lower(): v for k, v in headers.items()}['authorization'], 'Bearer lc_test')
        self.assertNotIn('Server text', value['message'])
        self.assertNotIn('lc_test', repr(value))

    def test_one_inflight_report_and_one_confirmed_receipt(self):
        service, report = self.make()
        service.release.clear()
        report.start()
        self.assertTrue(service.entered.wait(5))
        for _ in range(10):
            report.start()
        service.release.set()
        self.assertEqual(report.wait()['status'], 'refunded')
        report.start()
        self.assertEqual(len(service.posts), 1)

    def test_lost_reply_retries_same_endpoint_once_without_double_refund(self):
        service, report = self.make()
        service.failures = 1
        report.start()
        value = report.wait()
        self.assertEqual(len(service.posts), 2)
        self.assertEqual(service.posts[0], service.posts[1])
        self.assertEqual(value['job']['credits']['refunded'], 20)

    def test_uncertain_report_can_retry_later_without_reapplying(self):
        service, report = self.make()
        service.failures = 2
        report.start()
        first = report.wait()
        self.assertEqual((first['status'], first['job']['credits']['charged']), ('unconfirmed', 20))
        self.assertEqual(len(service.posts), 2)
        self.assertEqual(report.wait(retry=True)['status'], 'refunded')
        self.assertEqual(len(service.posts), 3)

    def test_review_is_never_refund_approval(self):
        for review in ('late', 'limit'):
            service, report = self.make(Service(receipt(status='review', review_reason=review, credits_refunded=0)))
            report.start()
            value = report.wait()
            self.assertEqual(value['job']['credits']['charged'], 20)
            self.assertIn('contact support for review', value['message'])
            self.assertNotIn('credits returned', value['message'])

    def test_confirmed_receipt_survives_failed_balance_refresh_and_keeps_paths(self):
        value = rig_job()
        value['outputs'] = [{'name': 'rigged.glb', 'path': '/private/verified/rigged.glb'}]
        service, report = self.make(value=value)
        service.fail_get = True
        report.start()
        result = report.wait()
        self.assertEqual(result['status'], 'refunded')
        self.assertFalse(result['balance_refreshed'])
        self.assertEqual(result['job']['outputs'], value['outputs'])

    def test_reason_mapping_and_no_report_to_current_unrelated_account(self):
        for code, expected in [('correspondence_failed', 'correspondence_ambiguous'),
                ('ambiguous_front', 'canonicalize_refused'), ('invalid_bundle', 'validation_failed'),
                ('source_changed', 'other')]:
            self.assertEqual(rig_refund.reason(code), expected)
        with patch.dict(cloud_jobs._registry, {}, clear=True):
            self.assertFalse(cloud_jobs._report_rig_failure(rig_job(), 'source_changed'))

    def test_corrupt_delivered_rig_reports_failure_and_cleans_partial_files(self):
        service = Service()
        def transport(request, **kwargs):
            if request.full_url.startswith('https://storage.test'):
                return Reply(b'corrupt')
            return service(request, **kwargs)
        client = service.client()
        client.transport = transport
        value = rig_job()
        value['outputs'] = [{**output(), 'name': 'rigged.glb', 'type': 'rig', 'mime': 'model/gltf-binary'}]
        with tempfile.TemporaryDirectory() as root, patch.dict(cloud_jobs._registry, {}, clear=True), \
                patch.object(cloud_jobs, '_redraw'), patch.object(cloud, '_runtime', None), \
                patch.object(cloud_jobs.tempfile, 'mkdtemp', return_value=root):
            cloud_jobs._remember(value, 'Rig', threading.Event(), client=client)
            with self.assertRaises(cloud_http.CloudError) as caught:
                cloud_jobs._finish(client, value, threading.Event(), None)
            self.assertEqual(caught.exception.code, 'hash_mismatch')
            self.assertTrue(caught.exception.extra['apply_failure'])
            report = cloud_jobs._registry[value['id']]['rig_refund'].wait()
            self.assertEqual(report['job']['credits']['charged'], 0)
            self.assertFalse(Path(root).exists())
            self.assertEqual(service.posts, [{'reason': 'validation_failed'}])
            self.assertTrue(all('/v1/jobs/job_test' in url for tid, url, headers in service.calls))

    def test_registry_panel_and_status_do_not_expose_original_credentials(self):
        service = Service()
        with patch.dict(cloud_jobs._registry, {}, clear=True), patch.object(cloud_jobs, '_redraw'), \
                patch.object(cloud, '_runtime', None):
            cloud_jobs._remember(rig_job(), 'Rig', threading.Event(), client=service.client())
            self.assertTrue(cloud_jobs._report_rig_failure(rig_job(), 'source_changed'))
            row = cloud_jobs._registry['job_test']
            self.assertEqual(row['rig_refund'].wait()['status'], 'refunded')
            row['ready'] = True
            visible = cloud_jobs.progress_rows(cloud_jobs._registry)
            self.assertFalse(visible[0]['cancelable'])
            self.assertIn('20 credits returned', visible[0]['message'])
            self.assertNotIn('lc_test', repr(visible) + repr(cloud_jobs.job_status('job_test')))

    def test_replaying_a_hash_failure_keeps_its_original_report_and_account(self):
        service = Service()
        service.failures = 2
        client = service.client()
        with patch.dict(cloud_jobs._registry, {}, clear=True), patch.object(cloud_jobs, '_redraw'), \
                patch.object(cloud, '_runtime', None):
            cloud_jobs._remember(rig_job(), 'Rig', threading.Event(), client=client)
            cloud_jobs._report_rig_failure(rig_job(), 'validation_failed')
            row = cloud_jobs._registry['job_test']
            report = row['rig_refund']
            self.assertEqual(report.wait()['status'], 'unconfirmed')
            row.update(error=cloud_http.CloudError('Corrupt rig', 'hash_mismatch'), ready=True)
            self.assertFalse(cloud_jobs._remember(rig_job(), 'Replay', threading.Event(), client=Service().client()))
            self.assertIs(cloud_jobs._registry['job_test']['rig_refund'], report)
            self.assertIs(cloud_jobs._registry['job_test']['client'], client)
            self.assertEqual(report.wait(retry=True)['status'], 'refunded')
            with self.assertRaisesRegex(RuntimeError, 'off the main thread'):
                cloud_jobs.wait_rig_refund('job_test')


if __name__ == '__main__':
    unittest.main()
