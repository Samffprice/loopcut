"""An owned rig job's idempotent apply-failure receipt, without Blender access."""
from copy import deepcopy
from datetime import datetime
import re
import threading

from . import cloud_http

SUPPORT = 'https://loopcut.org/beta/report-a-bug'
REASONS = {'correspondence_ambiguous', 'canonicalize_refused', 'validation_failed', 'other'}


def reason(code):
    if code == 'correspondence_failed':
        return 'correspondence_ambiguous'
    if code in {'unsupported_skeleton', 'unsupported_hands', 'ambiguous_front'}:
        return 'canonicalize_refused'
    return 'validation_failed' if code in {'validation_failed', 'invalid_bundle'} else 'other'


def validate(value, job):
    keys = {'job_id', 'reason', 'status', 'review_reason', 'credits_refunded', 'reported_at', 'message', 'support_url'}
    valid = (isinstance(value, dict) and set(value) == keys and value['job_id'] == job['id']
        and isinstance(value['reason'], str) and value['reason'] in REASONS
        and isinstance(value['status'], str) and value['status'] in {'refunded', 'review'}
        and type(value['credits_refunded']) is int and 0 <= value['credits_refunded']
        <= sum(job['credits'][k] for k in ('charged', 'refunded'))
        and isinstance(value['message'], str) and 1 <= len(value['message']) <= 400
        and value['support_url'] == SUPPORT and isinstance(value['reported_at'], str)
        and len(value['reported_at']) <= 64)
    if valid:
        try:
            valid = datetime.fromisoformat(value['reported_at'].replace('Z', '+00:00')).tzinfo is not None
        except ValueError:
            valid = False
    if valid:
        valid = (value['review_reason'] is None if value['status'] == 'refunded' else
                 isinstance(value['review_reason'], str) and value['review_reason'] in {'late', 'limit'}
                 and value['credits_refunded'] == 0)
    if not valid:
        raise cloud_http.CloudError('Invalid rig apply-failure receipt from Loopcut', 'invalid_response')
    return deepcopy(value)


class ApplyFailure:
    """One private report per job. Only start() retries an unconfirmed outcome."""
    def __init__(self, job, code, client, publish):
        if not re.fullmatch(r'job_[A-Za-z0-9_-]{1,124}', job['id']) or job['tool'] != 'auto_rig':
            raise ValueError('An apply-failure report needs an owned managed rig job')
        self.job, self.reason, self.publish = deepcopy(job), reason(code), publish
        # Freeze the original account and service, not whichever account is
        # selected when the asynchronous report or a later retry runs.
        self.client = cloud_http.Client(base_url=client.base_url, api_key=client.api_key, transport=client.transport,
            timeout=min(client.timeout, 10), client_headers=client.version_headers())
        self.lock, self.done = threading.Lock(), threading.Event()
        self.state = {'status': 'unconfirmed', 'message': '', 'job': deepcopy(job), 'receipt': None}

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state)

    def start(self):
        with self.lock:
            if self.state['status'] != 'unconfirmed':
                return
            self.done.clear()
            self.state.update(status='pending', message='Rig not added; requesting a refund.')
        self.publish(self.snapshot())
        threading.Thread(target=self._run, name='Loopcut rig refund', daemon=True).start()

    def wait(self, *, retry=False):
        if retry:
            self.start()
        self.done.wait()
        return self.snapshot()

    def _run(self):
        from . import cloud_jobs
        path = '/v1/jobs/' + self.job['id']
        receipt, updated = None, deepcopy(self.job)
        status, message, refreshed = 'unconfirmed', 'Refund unconfirmed; retry this job or contact support.', False
        try:
            for attempt in range(2):
                try:
                    receipt = validate(self.client.request('POST', path + '/apply-failed', {'reason': self.reason}), self.job)
                    break
                except cloud_http.CloudError as ex:
                    if ex.code != 'connection_failed' or attempt:
                        raise
            if receipt['status'] == 'refunded':
                amount = receipt['credits_refunded']
                updated['credits']['charged'] = max(0, self.job['credits']['charged'] - amount)
                updated['credits']['refunded'] = self.job['credits']['refunded'] + amount
                message = f'Rig not added; {amount} credits returned, so retry when ready.'
            else:
                why = 'window passed' if receipt['review_reason'] == 'late' else 'limit reached'
                message = f'Rig not added; refund {why}, so contact support for review.'
            # The receipt confirms its outcome even if this balance refresh fails.
            # Preserve every verified local artifact path while refreshing money.
            try:
                fresh = cloud_jobs._job(self.client.request('GET', path), expected=self.job)
                if (fresh['status'] != 'succeeded' or fresh['credits']['charged'] > updated['credits']['charged']
                        or fresh['credits']['refunded'] < updated['credits']['refunded']):
                    raise cloud_http.CloudError('Job accounting disagrees with its refund receipt', 'invalid_response')
                updated['credits'] = fresh['credits']
                refreshed = True
            except cloud_http.CloudError:
                refreshed = False  # Receipt remains authoritative; no claim of a refreshed account balance.
            status = receipt['status']
        except cloud_http.CloudError as ex:
            print('Loopcut rig refund report:', ex.code)
            status = 'unconfirmed'
            message = 'Rig not added; refund unconfirmed, so retry this job or contact support.'
        finally:
            with self.lock:
                self.state = {'status': status, 'message': message, 'job': updated,
                              'receipt': receipt, 'balance_refreshed': refreshed}
            try:
                self.publish(self.snapshot())
            finally:
                self.done.set()
