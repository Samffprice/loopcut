"""Runtime discovery, routing, provider schemas and thread boundaries, without Blender.

Fast transports remain useful when the shared contract checkout is absent. Loopback tests
exercise the mock's real ETag, flags, skills and error envelopes; no vendor/service traffic.
"""

import hashlib
import io
import json
import os
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
from loopcut import cloud, cloud_account, cloud_http, config, skills
from loopcut.byok import catalog
from mock_service import MockService


class Reply(io.BytesIO):
    def __init__(self, payload=None, status=200, headers=None):
        super().__init__(b'' if payload is None else json.dumps(payload).encode())
        self.status, self.headers = status, headers or {}


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append({'method': request.method, 'url': request.full_url,
                           'headers': {k.lower(): v for k, v in request.header_items()},
                           'body': json.loads(request.data) if request.data else None})
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def tool(name='image_generate', **changes):
    descriptor = catalog.descriptors(no_generation=False, private_mode=False,
                                     keys_set={'OPENAI_API_KEY': True})[0]
    descriptor.update(name=name, status='live', available=True)
    descriptor['cost'] = {'credits_estimate': 2, 'quote_required': False}
    descriptor.update(changes)
    return descriptor


def manifest(*tools, **changes):
    result = {'contract': {'version': '0.2', 'min_client': '0.1'},
              'account': {'plan': 'pro', 'credits': {'balance': 100}, 'settings': {'no_generation': False, 'private_mode': True}},
              'flags': {'services': True, 'premium_skills': True}, 'tools': list(tools), 'skills': []}
    result.update(changes)
    return result


def error(code, status, **extra):
    return Reply({'error': {'code': code, 'message': 'Service refused the call', 'type': 'service_error', **extra}}, status)


def ideas(*names, **changes):
    """A manifest of Coming soon tools, the ones people vote on."""
    return manifest(*(tool(name, status='coming_soon', available=False) for name in names), **changes)


def tally(mine=(), levels=None, badges=None, max_votes=3):
    """A /v1/votes picture (contract 0.7): our picks, and a level (0-1) and badge per tool. Never a count."""
    badges = badges or {}
    return Reply({'max_votes': max_votes, 'mine': list(mine),
                  'tools': [{'tool': name, 'level': level, 'badge': badges.get(name)} for name, level in (levels or {}).items()]})


def served(*, words=None, requires=('loopcut_cloud.asset_search',), craft=None):
    fields = {'id': 'examples/runtime', 'version': 1, 'title': 'Runtime integration', 'when': 'Use available tools',
              'roles': ['single', 'workspace', 'worker'], 'requires_tools': list(requires), 'tier': 'paid', 'kind': 'assist'}
    if words:
        fields['words'] = words
    if craft:
        fields['craft'] = craft
    document = {'id': fields['id'], 'version': 1, 'front_matter': fields, 'body': 'Test integration note.\n'}
    document['sha256'] = hashlib.sha256(cloud._skill_bytes(document)).hexdigest()
    return document, {**fields, 'sha256': document['sha256']}


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        fake = types.SimpleNamespace(app=types.SimpleNamespace(version=(5, 2, 0), driver_namespace={}))
        context = patch.dict(sys.modules, {'bpy': fake})
        context.start()
        self.addCleanup(context.stop)
        context = patch.object(cloud, '_runtime', None)
        context.start()
        self.addCleanup(context.stop)

    def runtime(self, *replies, signed_in=True, **options):
        self.transport = Transport(*replies)
        client = cloud_http.Client(base_url='https://service.test', api_key='lc_demo' if signed_in else '', transport=self.transport)
        runtime = cloud.Runtime(client, self.directory.name, signed_in=signed_in, **options)
        cloud._runtime = runtime
        return runtime


class RuntimeTest(RuntimeFixture):

    def test_html_404_is_quiet_and_removes_cached_beta_capabilities(self):
        for cached in (False, True):
            with self.subTest(cached=cached), patch.dict(os.environ, {'LOOPCUT_PROMPT_INDEX': ''}):
                missing = urllib.error.HTTPError('https://service.test/v1/manifest', 404, 'Not Found',
                    {'content-type': 'text/html'}, io.BytesIO(b'<!DOCTYPE html><h1>404</h1>'))
                runtime = self.runtime(missing)
                if cached:
                    runtime.manifest = manifest(tool(status='coming_soon'),
                        prompts={'version': 'cached', 'items': {'single': {'chars': 10}}})
                    runtime.etag = 'cached'
                    runtime._save()
                self.assertFalse(runtime.refresh())
                self.assertTrue(runtime.supported)
                self.assertEqual(cloud.coming_soon(), [])
                self.assertFalse(cloud.prompt_index_available())
                self.assertEqual(cloud_account.panel_data(runtime), {'credits': '', 'today': '', 'message': ''})
                self.assertEqual(cloud.ui_message(), '')
                self.assertEqual(runtime.descriptors(), [])
                self.assertEqual(runtime.etag, '')
                restarted = cloud.Runtime(runtime.client, self.directory.name, signed_in=True)
                self.assertEqual(restarted.coming_soon(), [])
                self.assertNotIn('prompts', restarted.manifest)

    def test_html_404_is_a_structured_cloud_error(self):
        reply = io.BytesIO(b'<!DOCTYPE html><h1>404</h1>')
        reply.status, reply.headers = 404, {'content-type': 'text/html'}
        runtime = self.runtime(reply)
        with self.assertRaises(cloud_http.CloudError) as caught:
            runtime.client.request('GET', '/v1/manifest')
        self.assertEqual((caught.exception.status, caught.exception.code), (404, 'http_error'))
        self.assertNotIn('<html', str(caught.exception))

    def test_dismissed_contract_message_stays_dismissed_after_refresh_and_restart(self):
        runtime = self.runtime(error('contract_unsupported', 426), error('contract_unsupported', 426))
        self.assertFalse(runtime.refresh())
        self.assertEqual(cloud.ui_message(), cloud.UPDATE_MESSAGE)
        cloud.dismiss_ui_message('stale message')
        self.assertEqual(cloud.ui_message(), cloud.UPDATE_MESSAGE)
        cloud.dismiss_ui_message(cloud.UPDATE_MESSAGE)
        self.assertEqual(cloud.ui_message(), '')
        self.assertFalse(runtime.refresh())
        self.assertEqual(cloud.ui_message(), '')
        cloud._runtime = cloud.Runtime(runtime.client, self.directory.name, signed_in=True)
        self.assertEqual(cloud.ui_message(), '')
        self.assertEqual(cloud._runtime.message, cloud.UPDATE_MESSAGE, 'tool errors still explain the unsupported contract')

    def test_etag_304_and_cache_survives_restart_offline(self):
        runtime = self.runtime(Reply(manifest(tool()), headers={'ETag': '"v1"'}), Reply(status=304), OSError('offline'))
        self.assertTrue(runtime.refresh())
        self.assertTrue(runtime.refresh())
        self.assertEqual(self.transport.calls[1]['headers']['if-none-match'], '"v1"')
        self.assertFalse(runtime.refresh())
        self.assertEqual([d['name'] for d in runtime.descriptors()], ['image_generate'])
        restarted = cloud.Runtime(runtime.client, self.directory.name, signed_in=True)
        self.assertEqual(restarted.etag, '"v1"')
        self.assertEqual(restarted.descriptors(), runtime.descriptors())
        self.assertEqual(self.transport.calls[0]['headers']['x-loopcut-contract'], '0.7')

    def test_multipart_capability_comes_from_the_latest_manifest(self):
        offered = {'blob_multipart': {'min_size': 16 * 1024 * 1024}}
        runtime = self.runtime(Reply(manifest(tool(), capabilities=offered)), Reply(manifest(tool())),
                               Reply(manifest(tool(), capabilities={'blob_multipart': {'min_size': 0}})),
                               Reply(manifest(tool(), capabilities=['blob_multipart'])))
        self.assertIsNone(cloud.blob_multipart())
        self.assertTrue(runtime.refresh())
        self.assertEqual(cloud.blob_multipart(), {'min_size': 16 * 1024 * 1024})
        self.assertEqual(cloud.Runtime(runtime.client, self.directory.name, signed_in=True).blob_multipart(),
                         {'min_size': 16 * 1024 * 1024})  # From the cache after a restart.
        for _ in range(3):  # Withdrawn, then malformed: whole-file PUTs.
            self.assertTrue(runtime.refresh())
            self.assertIsNone(cloud.blob_multipart())
        cloud._runtime = None
        self.assertIsNone(cloud.blob_multipart())

    def test_signed_out_discovery_has_no_auth_or_managed_tools(self):
        runtime = self.runtime(Reply(manifest(tool(available=False, unavailable_reason='sign_in'), account=None)), signed_in=False)
        self.assertTrue(runtime.refresh())
        self.assertEqual(runtime.descriptors(), [])
        self.assertNotIn('authorization', self.transport.calls[0]['headers'])
        self.assertEqual(len(runtime.manifest['tools']), 1)

    def test_426_switches_off_cached_and_byok_tools(self):
        runtime = self.runtime(Reply(manifest(tool())), error('contract_unsupported', 426, min_client='1'),
                               keys_set={'OPENAI_API_KEY': True})
        runtime.refresh()
        self.assertFalse(runtime.refresh())
        self.assertEqual(runtime.descriptors(), [])
        self.assertEqual(runtime.message, cloud.UPDATE_MESSAGE)
        restarted = cloud.Runtime(runtime.client, self.directory.name, signed_in=True, keys_set={'OPENAI_API_KEY': True})
        self.assertEqual(restarted.descriptors(), [])

    def test_bad_manifest_preserves_last_good(self):
        runtime = self.runtime(Reply(manifest(tool())), Reply({'tools': 'bad'}))
        runtime.refresh()
        self.assertFalse(runtime.refresh())
        self.assertEqual(len(runtime.descriptors()), 1)

    def test_tools_change_on_refresh(self):
        runtime = self.runtime(Reply(manifest(tool())), Reply(manifest(tool(status='coming_soon', available=False))),
                               Reply(manifest(tool('new_tool', backends=['managed']))))
        runtime.refresh()
        self.assertEqual(len(cloud.tool_schemas()), 1)
        runtime.refresh()
        self.assertEqual(cloud.tool_schemas(), [])
        self.assertEqual(runtime.coming_soon()[0]['title'], 'Generate image')
        runtime.refresh()
        self.assertEqual(cloud.tool_schemas()[0]['function']['name'], 'loopcut_cloud__new_tool')

    def test_tool_disabled_requests_refresh(self):
        runtime = self.runtime()
        for status in (403, None):  # Async job failures have no failing HTTP status.
            with self.subTest(status=status), patch.object(runtime, 'refresh_async') as refresh:
                runtime.handle_error(cloud_http.CloudError('disabled', 'tool_disabled', status))
                refresh.assert_called_once()

    def test_services_off_hides_managed(self):
        runtime = self.runtime(Reply(manifest(tool(), flags={'services': False, 'premium_skills': True})))
        runtime.refresh()
        self.assertEqual(runtime.descriptors(), [])

    def test_interest_once_and_thank_you_data(self):
        runtime = self.runtime(Reply(manifest(tool(status='coming_soon', available=False))), Reply(status=204))
        runtime.refresh()
        self.assertTrue(runtime.interest('image_generate'))
        self.assertFalse(runtime.interest('image_generate'))
        self.assertEqual(runtime.interests, {'image_generate': 'thanks'})
        self.assertEqual(self.transport.calls[-1]['body'], {'type': 'interest', 'tool': 'image_generate', 'source': 'coming_soon_button',
                                                           'app_version': config.app_version()})
        self.assertFalse(runtime.interest('not_a_tool'))

    def test_interest_error_can_retry(self):
        runtime = self.runtime(Reply(manifest(tool(status='coming_soon'))), error('rate_limited', 429, retry_after=10), Reply(status=204))
        runtime.refresh()
        self.assertFalse(runtime.interest('image_generate'))
        self.assertEqual(runtime.interests['image_generate'], 'retry')
        self.assertTrue(runtime.interest('image_generate'))

    def test_signed_out_interest_posts_anonymously_and_the_vote_view_asks_to_sign_in(self):
        from loopcut import credentials, state
        from loopcut.ui import layout
        config_dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, config_dir, True)
        with patch.object(sys.modules['bpy'].app, 'background', False, create=True), \
                patch.object(credentials, 'folder', return_value=config_dir):
            runtime = self.runtime(Reply(manifest(tool(status='coming_soon'), account=None)),
                                   Reply(status=401), signed_in=False)
            self.assertTrue(runtime.refresh())
            self.assertFalse(runtime.interest('image_generate'))  # An older server refuses anonymous events.
            install_id = credentials.interest_install_id()
        self.assertEqual(runtime.interests['image_generate'], 'notify')
        self.assertEqual(len(self.transport.calls), 2)
        self.assertNotIn('authorization', self.transport.calls[1]['headers'])
        self.assertEqual(self.transport.calls[1]['body'], {'type': 'interest', 'tool': 'image_generate',
                                                           'source': 'coming_soon_button', 'install_id': install_id,
                                                           'app_version': config.app_version()})
        display = layout.build(state.new_session(), 420, 600, 1, lambda font, size, text: len(text) * size * .6, 'fast',
                               None, {'view': 'vote', 'coming_soon': runtime.coming_soon(), 'service_signed_in': runtime.signed_in})
        action = next(hit['action'] for hit in display['hits'] if hit['id'] == 'vote.image_generate')
        self.assertEqual(action, ('vote', 'image_generate'))
        reminder = next(hit for hit in display['hits'] if hit['id'] == 'vote.signin')
        self.assertEqual(reminder['action'], ('sign_in', 'loopcut'))

    def test_double_click_while_sending_posts_once(self):
        runtime = self.runtime(Reply(manifest(tool(status='coming_soon'))), Reply(status=204))
        runtime.refresh()
        runtime.client.client_headers = cloud_http.contract_headers()
        entered, release = threading.Event(), threading.Event()
        original = runtime.client.request
        def send(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('interest send not released')
            return original(*args, **kwargs)
        with patch.object(runtime.client, 'request', side_effect=send):
            worker = threading.Thread(target=lambda: runtime.interest('image_generate'))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(runtime.interest('image_generate'))
                self.assertEqual(runtime.interests['image_generate'], 'sending')
            finally:
                release.set()
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(runtime.interests['image_generate'], 'thanks')
        self.assertEqual(sum(c['method'] == 'POST' for c in self.transport.calls), 1)

    def test_skills_cached_verified_and_flags_drop(self):
        document, entry = served()
        runtime = self.runtime(Reply(manifest(skills=[entry])), Reply(document), Reply(status=304),
                               Reply(manifest(flags={'services': True, 'premium_skills': False})))
        runtime.refresh()
        self.assertEqual(len(runtime.server_skills()), 1)
        self.assertTrue((Path(self.directory.name) / 'skills' / (entry['sha256'] + '.json')).is_file())
        runtime.refresh()
        self.assertEqual(len(self.transport.calls), 3)  # No second skill download.
        restarted = cloud.Runtime(runtime.client, self.directory.name, signed_in=True)
        self.assertEqual(restarted.server_skills(), runtime.server_skills())
        runtime.refresh()
        self.assertEqual(runtime.server_skills(), [])

    def test_tampered_skill_is_never_cached_or_used(self):
        document, entry = served()
        document['body'] += 'tampered'
        runtime = self.runtime(Reply(manifest(skills=[entry])), Reply(document))
        runtime.refresh()
        self.assertEqual(runtime.server_skills(), [])
        self.assertFalse((Path(self.directory.name) / 'skills').exists())

    def test_cache_corruption_is_refetched(self):
        document, entry = served()
        runtime = self.runtime(Reply(manifest(skills=[entry])), Reply(document), Reply(status=304), Reply(document))
        runtime.refresh()
        path = Path(self.directory.name) / 'skills' / (entry['sha256'] + '.json')
        path.write_text('{}')
        runtime.refresh()
        self.assertEqual(len(runtime.server_skills()), 1)
        self.assertEqual(len(self.transport.calls), 4)

    def test_skills_off_never_fetches_even_with_index(self):
        _, entry = served()
        runtime = self.runtime(Reply(manifest(skills=[entry], flags={'services': True, 'premium_skills': False})))
        runtime.refresh()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(runtime.server_skills(), [])


class VoteTest(RuntimeFixture):
    IDEAS = ('render', 'sfx', 'lip_sync', 'auto_rig')

    def voting(self, *replies, signed_in=True):
        runtime = self.runtime(Reply(ideas(*self.IDEAS, **({} if signed_in else {'account': None}))), *replies,
                               signed_in=signed_in)
        self.assertTrue(runtime.refresh())
        return runtime

    def row(self, runtime, name, *keys):
        row = next(row for row in runtime.coming_soon() if row['name'] == name)
        return {key: row[key] for key in keys} if keys else row

    def test_a_cast_shows_at_once_then_takes_the_servers_picture(self):
        runtime = self.voting(tally(['render'], {'render': 1.0, 'sfx': 0.4}, {'render': 'most_wanted'}))
        seen, original = [], runtime.client.request
        def send(*args, **kwargs):
            seen.append(self.row(runtime, 'render', 'voted', 'state', 'level'))
            return original(*args, **kwargs)
        with patch.object(runtime.client, 'request', side_effect=send):
            self.assertTrue(runtime.vote('render'))
        self.assertEqual(seen, [{'voted': True, 'state': 'sending', 'level': None}])
        call = self.transport.calls[-1]
        self.assertEqual((call['method'], call['url']), ('PUT', 'https://service.test/v1/votes/render'))
        self.assertEqual(call['body'], {'app_version': config.app_version()})
        self.assertEqual(call['headers']['authorization'], 'Bearer lc_demo')
        keys = ('voted', 'level', 'badge', 'state')
        self.assertEqual(self.row(runtime, 'render', *keys), {'voted': True, 'level': 1.0, 'badge': 'most_wanted', 'state': 'ready'})
        self.assertEqual(self.row(runtime, 'sfx', *keys), {'voted': False, 'level': 0.4, 'badge': None, 'state': 'ready'})
        self.assertIsNone(self.row(runtime, 'auto_rig')['level'])
        self.assertEqual((runtime.vote_states, cloud.vote_notice()), ({}, ''))
        self.assertFalse(runtime.vote('not_coming_soon'))
        self.assertEqual(len(self.transport.calls), 2)

    def test_taking_a_vote_back_sends_a_delete(self):
        runtime = self.voting(tally(['render', 'sfx'], {'render': 1.0}), tally(['sfx'], {'render': 0.8}))
        runtime.refresh_votes()
        get = self.transport.calls[-1]
        self.assertEqual((get['method'], get['url']), ('GET', 'https://service.test/v1/votes'))
        self.assertTrue(self.row(runtime, 'render')['voted'])
        self.assertTrue(runtime.vote('render'))
        call = self.transport.calls[-1]
        self.assertEqual((call['method'], call['url'], call['body']), ('DELETE', 'https://service.test/v1/votes/render', None))
        self.assertEqual(runtime.votes['mine'], ['sfx'])
        self.assertEqual(self.row(runtime, 'render', 'voted', 'level'), {'voted': False, 'level': 0.8})

    def test_a_vote_past_the_limit_sets_the_notice_without_a_request(self):
        runtime = self.voting(tally(['render', 'sfx', 'lip_sync'], {'render': 1.0}), tally(['sfx', 'lip_sync']))
        runtime.refresh_votes()
        calls = len(self.transport.calls)
        self.assertFalse(runtime.vote('auto_rig'))
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(cloud.vote_notice(), cloud.VOTE_LIMIT_NOTICE.format(3))
        self.assertEqual(runtime.votes['mine'], ['render', 'sfx', 'lip_sync'])
        self.assertEqual((runtime.vote_states, self.row(runtime, 'auto_rig')['voted']), ({}, False))
        self.assertTrue(runtime.vote('render'), 'taking one back is always allowed')
        self.assertEqual(cloud.vote_notice(), '', 'and it clears the notice')

    def test_the_servers_limit_is_the_one_counted(self):
        runtime = self.voting(tally(['render', 'sfx'], max_votes=2))
        runtime.refresh_votes()
        self.assertFalse(runtime.vote('lip_sync'))
        self.assertEqual(cloud.vote_notice(), cloud.VOTE_LIMIT_NOTICE.format(2))
        self.assertEqual(len(self.transport.calls), 2)

    def test_a_409_takes_only_that_vote_back_with_the_limit_notice(self):
        runtime = self.voting(tally(['sfx'], {'sfx': 1.0}), error('vote_limit', 409))
        runtime.refresh_votes()  # The server already counts votes cast from another computer.
        self.assertFalse(runtime.vote('render'))
        self.assertEqual([c['method'] for c in self.transport.calls[1:]], ['GET', 'PUT'])
        self.assertEqual(runtime.votes['mine'], ['sfx'])
        self.assertEqual(self.row(runtime, 'render', 'voted', 'state'), {'voted': False, 'state': 'ready'})
        self.assertEqual(cloud.vote_notice(), cloud.VOTE_LIMIT_NOTICE.format(3))
        self.assertEqual(runtime.vote_states, {})

    def test_a_server_without_votes_turns_a_cast_into_one_interest_event(self):
        runtime = self.voting(error('not_found', 404), Reply(status=204))
        self.assertTrue(runtime.vote('render'))
        put, post = self.transport.calls[1:]
        self.assertEqual(put['method'], 'PUT')
        self.assertEqual((post['method'], post['url']), ('POST', 'https://service.test/v1/events'))
        self.assertEqual(post['body'], {'type': 'interest', 'tool': 'render', 'source': 'coming_soon_button',
                                        'app_version': config.app_version()})
        self.assertIs(runtime.votes['server'], False)
        self.assertEqual(runtime.votes['mine'], ['render'])
        self.assertEqual(runtime.interests, {'render': 'thanks'})
        self.assertEqual(self.row(runtime, 'render', 'voted', 'level'), {'voted': True, 'level': None})
        self.assertEqual(cloud.vote_notice(), '')
        # From now on nothing asks the vote endpoints: a take-back stays here, a second cast sends no second event.
        self.assertTrue(runtime.vote('render'))
        self.assertEqual(runtime.votes['mine'], [])
        self.assertTrue(runtime.vote('render'))
        self.assertEqual(runtime.votes['mine'], ['render'])
        self.assertEqual(len(self.transport.calls), 3)

    def test_a_404_when_refreshing_marks_a_server_without_votes(self):
        runtime = self.voting(error('not_found', 404), Reply(status=204))
        runtime.refresh_votes()
        self.assertIs(runtime.votes['server'], False)
        self.assertTrue(runtime.vote('sfx'))
        self.assertEqual([c['method'] for c in self.transport.calls[1:]], ['GET', 'POST'])
        self.assertEqual(runtime.votes['mine'], ['sfx'])

    def test_an_unknown_tool_is_a_failed_vote_not_a_server_without_votes(self):
        runtime = self.voting(error('unknown_tool', 404))
        self.assertFalse(runtime.vote('render'))
        self.assertEqual(len(self.transport.calls), 2, 'no interest event')
        self.assertEqual((runtime.votes['mine'], runtime.votes['server']), ([], None))
        self.assertEqual(cloud.vote_notice(), cloud.VOTE_FAILED_NOTICE)

    def test_a_failed_send_puts_the_pick_back_with_a_notice(self):
        runtime = self.voting(tally(['render'], {'render': 1.0}), OSError('offline'), error('internal', 500))
        runtime.refresh_votes()
        for name in ('sfx', 'render'):  # A cast, then a take-back.
            with self.subTest(name=name):
                self.assertFalse(runtime.vote(name))
                self.assertEqual(runtime.votes['mine'], ['render'])
                self.assertEqual(cloud.vote_notice(), cloud.VOTE_FAILED_NOTICE)
                self.assertEqual(self.row(runtime, name)['state'], 'ready')
                self.assertEqual(runtime.vote_states, {})
        self.assertIs(runtime.votes['server'], True)
        self.assertEqual(runtime.votes['levels'], {'render': 1.0}, 'the last picture stays')

    def test_a_refused_signed_out_vote_asks_for_a_sign_in(self):
        """Signed-out votes switched off on the server (401): reached, refused, so not 'couldn't reach'."""
        from loopcut import credentials
        config_dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__('shutil').rmtree, config_dir, True)
        with patch.object(sys.modules['bpy'].app, 'background', False, create=True), \
                patch.object(credentials, 'folder', return_value=config_dir):
            runtime = self.voting(tally([], {'render': 1.0}), error('invalid_api_key', 401), signed_in=False)
            runtime.refresh_votes()
            self.assertFalse(runtime.vote('sfx'))
        self.assertEqual(self.transport.calls[-1]['method'], 'PUT')
        self.assertEqual(runtime.votes['mine'], [])
        self.assertEqual(cloud.vote_notice(), cloud.VOTE_SIGN_IN_NOTICE)

    def test_a_second_click_while_a_vote_is_sending_is_ignored(self):
        runtime = self.voting(tally(['render'], {'render': 1.0}))
        runtime.client.client_headers = cloud_http.contract_headers()  # Off the main thread, as a worker has them.
        entered, release = threading.Event(), threading.Event()
        original = runtime.client.request
        def send(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError('vote send not released')
            return original(*args, **kwargs)
        with patch.object(runtime.client, 'request', side_effect=send):
            worker = threading.Thread(target=lambda: runtime.vote('render'))
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(runtime.vote('render'))
                self.assertEqual(self.row(runtime, 'render')['state'], 'sending')
            finally:
                release.set()
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(sum(c['method'] == 'PUT' for c in self.transport.calls), 1)
        self.assertEqual(runtime.votes['mine'], ['render'])

    def test_an_earlier_answer_never_drops_a_later_pending_vote(self):
        """Two quick clicks: the first answer, which has not seen the second click, applies while the second waits."""
        def until(condition):
            deadline = time.monotonic() + 2
            while not condition():
                self.assertLess(time.monotonic(), deadline, 'timed out')
                time.sleep(0.005)
        # (the second click, the picks before it, the server's answer to the first click, to the second, picks between)
        cases = (('sfx', [], tally(['render'], {'render': 1.0}), tally(['render', 'sfx'], {'render': 1.0, 'sfx': 0.5}),
                  ['render', 'sfx']),
                 ('lip_sync', ['lip_sync'], tally(['lip_sync', 'render'], {'render': 1.0, 'lip_sync': 0.6}),
                  tally(['render'], {'render': 1.0}),
                  ['render']))
        for second, before, first_answer, second_answer, between in cases:
            with self.subTest(second=second):
                runtime = self.voting(tally(before), first_answer, second_answer)
                runtime.refresh_votes()
                runtime.client.client_headers = cloud_http.contract_headers()  # Off the main thread, as a worker has them.
                entered = {name: threading.Event() for name in ('render', second)}
                release = {name: threading.Event() for name in ('render', second)}
                original = runtime.client.request
                def send(method, path, *args, **kwargs):
                    name = path.split('?')[0].rsplit('/', 1)[-1]
                    entered[name].set()
                    if not release[name].wait(2):
                        raise AssertionError('vote send not released')
                    return original(method, path, *args, **kwargs)
                with patch.object(runtime.client, 'request', side_effect=send):
                    first = threading.Thread(target=lambda: runtime.vote('render'))
                    first.start()
                    self.assertTrue(entered['render'].wait(2))
                    later = threading.Thread(target=lambda: runtime.vote(second))
                    later.start()
                    try:
                        until(lambda: second in runtime.vote_states)  # Clicked, and waiting its turn.
                        self.assertFalse(entered[second].is_set(), 'one vote request at a time')
                        release['render'].set()
                        first.join(timeout=2)
                        self.assertFalse(first.is_alive())
                        self.assertEqual(runtime.votes['mine'], between, 'the first answer kept the second click')
                        self.assertEqual(self.row(runtime, second, 'state', 'voted'),
                                         {'state': 'sending', 'voted': second not in before})
                        self.assertEqual(self.row(runtime, 'render')['level'], 1.0, "the first answer's levels apply")
                        self.assertTrue(entered[second].wait(2))
                    finally:
                        release['render'].set()
                        release[second].set()
                        first.join(timeout=2)
                        later.join(timeout=2)
                self.assertFalse(later.is_alive())
                self.assertEqual([c['url'].split('?')[0].rsplit('/', 1)[-1] for c in self.transport.calls[2:]],
                                 ['render', second], 'answers in click order')
                self.assertEqual(runtime.votes['mine'], between)
                self.assertEqual((runtime.vote_states, cloud.vote_notice()), ({}, ''))

    def test_the_servers_picture_is_validated_and_clamped(self):
        runtime = self.voting(Reply({'tools': 'bad'}))
        good = {'max_votes': 3, 'mine': ['render'], 'tools': [{'tool': 'render', 'level': 1.0, 'badge': 'most_wanted'}]}
        runtime._apply_votes(good)
        picture = deepcopy(runtime.votes)
        self.assertEqual(picture, {'mine': ['render'], 'levels': {'render': 1.0}, 'badges': {'render': 'most_wanted'},
                                   'max': 3, 'server': True})
        missing = {key: value for key, value in good.items() if key != 'max_votes'}
        for bad in ({**good, 'tools': {}}, {**good, 'mine': 'render'}, {**good, 'mine': [1]}, missing,
                    {**good, 'max_votes': True}, {**good, 'max_votes': 3.0}, {**good, 'max_votes': 0}, {**good, 'max_votes': 11}):
            with self.subTest(bad=bad), self.assertRaises(cloud_http.CloudError) as caught:
                runtime._apply_votes(bad)
            self.assertEqual(caught.exception.code, 'invalid_response')
        runtime.refresh_votes()  # An invalid reply is dropped quietly.
        self.assertEqual(runtime.votes, picture, 'a bad picture never replaces the last good one')
        runtime._apply_votes({'max_votes': 2, 'mine': ['render', 'sfx', 'lip_sync'], 'tools': [
            {'tool': 'render', 'level': 1.7, 'badge': 'legendary'}, {'tool': 'sfx', 'level': -0.2, 'badge': 'popular'},
            {'tool': 'lip_sync', 'level': float('nan')}, {'tool': 'auto_rig', 'level': '0.5'}, {'tool': 'x', 'level': True},
            {'tool': 'y', 'level': float('inf')}, {'tool': 'z', 'level': 1}, 'render', {'level': 0.5}, {'tool': 7, 'level': 0.5}]})
        self.assertEqual(runtime.votes, {'mine': ['render', 'sfx'], 'levels': {'render': 1.0, 'sfx': 0.0, 'z': 1.0},
                                         'badges': {'sfx': 'popular'}, 'max': 2, 'server': True})

    def test_counts_from_the_server_are_never_kept_shown_or_cached(self):
        runtime = self.voting(Reply({'max_votes': 3, 'mine': ['render'], 'total': 4711, 'tools': [
            {'tool': 'render', 'level': 1.0, 'badge': 'most_wanted', 'count': 4711, 'votes': 4711}]}))
        runtime.refresh_votes()
        self.assertNotIn('4711', json.dumps(runtime.votes))
        self.assertNotIn('4711', json.dumps(runtime.coming_soon()))
        self.assertEqual(set(self.row(runtime, 'render')),
                         {'name', 'title', 'summary', 'state', 'category', 'voted', 'level', 'badge'})
        cache = (Path(self.directory.name) / 'manifest.json').read_text()
        self.assertNotIn('4711', cache)
        self.assertEqual(json.loads(cache)['votes_mine'], ['render'])

    def test_picks_survive_a_restart_but_levels_wait_for_the_server(self):
        runtime = self.voting(tally(['render'], {'render': 1.0}, {'render': 'most_wanted'}))
        self.assertTrue(runtime.vote('render'))
        restarted = cloud.Runtime(runtime.client, self.directory.name, signed_in=True)
        self.assertEqual(restarted.votes['mine'], ['render'])
        self.assertIsNone(restarted.votes['server'])
        self.assertEqual(self.row(restarted, 'render', 'voted', 'level', 'badge'), {'voted': True, 'level': None, 'badge': None})
        path = Path(self.directory.name) / 'manifest.json'
        for cached, mine in ((['render', 'sfx', 'lip_sync', 'auto_rig'], ['render', 'sfx', 'lip_sync']), ('render', [])):
            with self.subTest(cached=cached):
                payload = json.loads(path.read_text())
                payload['votes_mine'] = cached
                path.write_text(json.dumps(payload))
                with patch('builtins.print'):
                    restarted = cloud.Runtime(runtime.client, self.directory.name, signed_in=True)
                self.assertEqual(restarted.votes['mine'], mine)
                self.assertEqual(len(restarted.coming_soon()), 4, 'a bad pick list never costs the catalog')

    def test_signed_in_votes_add_the_install_id_and_signed_out_votes_send_only_it(self):
        from loopcut import credentials
        for signed_in in (True, False):
            with self.subTest(signed_in=signed_in):
                config_dir = Path(tempfile.mkdtemp())
                self.addCleanup(__import__('shutil').rmtree, config_dir, True)
                with patch.object(sys.modules['bpy'].app, 'background', False, create=True), \
                        patch.object(credentials, 'folder', return_value=config_dir):
                    runtime = self.voting(tally(), tally(['render']), tally(), signed_in=signed_in)
                    runtime.refresh_votes()
                    self.assertTrue(runtime.vote('render'))
                    self.assertTrue(runtime.vote('render'))
                    install_id = credentials.interest_install_id()
                get, put, delete = self.transport.calls[1:]
                self.assertEqual(get['url'], 'https://service.test/v1/votes?install_id=' + install_id)
                self.assertEqual(put['url'], 'https://service.test/v1/votes/render')
                self.assertEqual(put['body'], {'app_version': config.app_version(), 'install_id': install_id})
                self.assertEqual(delete['url'], 'https://service.test/v1/votes/render?install_id=' + install_id)
                for call in (get, put, delete):
                    self.assertEqual('authorization' in call['headers'], signed_in)

    def test_signed_out_without_an_install_id_never_votes_anonymously(self):
        runtime = self.voting(signed_in=False)  # Background Blender: no install id.
        runtime.refresh_votes()
        self.assertFalse(runtime.vote('render'))
        self.assertEqual(len(self.transport.calls), 1, 'only the manifest')
        self.assertEqual((runtime.votes['mine'], cloud.vote_notice()), ([], cloud.VOTE_FAILED_NOTICE))

    def test_module_calls_run_off_the_ui_thread_then_redraw(self):
        from loopcut import mainthread
        runtime = self.runtime()
        done, calls = threading.Event(), []
        with patch.object(runtime, 'vote', side_effect=lambda name: calls.append((name, threading.current_thread().name))), \
                patch.object(runtime, 'refresh_votes', side_effect=lambda: calls.append(threading.current_thread().name)), \
                patch.object(mainthread, 'request_redraw', side_effect=done.set) as redraw:
            cloud.vote('render')
            self.assertTrue(done.wait(2))
            done.clear()
            cloud.refresh_votes()
            self.assertTrue(done.wait(2))
        self.assertEqual(calls, [('render', 'loopcut-vote'), 'loopcut-votes'])
        self.assertEqual(redraw.call_count, 2)
        runtime.vote_notice = 'Notice'
        self.assertEqual(cloud.vote_notice(), 'Notice')
        cloud._runtime = None
        cloud.vote('render')
        cloud.refresh_votes()
        self.assertEqual(cloud.vote_notice(), '')
        self.assertEqual(len(calls), 2)


class MergeTest(unittest.TestCase):
    def names(self, value, **options):
        return {d['name']: d['backend'] for d in cloud.merge_tools(value, **options)}

    def test_funded_managed_unless_byok_selected(self):
        options = {'signed_in': True, 'keys_set': {'OPENAI_API_KEY': True}}
        self.assertEqual(self.names(manifest(tool()), **options)['image_generate'], 'managed')
        self.assertEqual(self.names(manifest(tool()), preferred={'image_generate': 'byok'}, **options)['image_generate'], 'byok')

    def test_private_mode_routing_reads_the_own_key_class_never_the_managed_one(self):
        # manifest() has Private mode on. A managed class must not let a class-C own-key route through, nor
        # take away a class-A own-key route.
        own_key = {'provider': 'openai', 'key_name': 'OPENAI_API_KEY', 'data_class': 'C'}
        options = {'keys_set': {'OPENAI_API_KEY': True}, 'preferred': {'image_generate': 'byok'}}
        self.assertNotIn('image_generate', self.names(manifest(tool(data_class='A', byok=own_key)), **options))
        offered = self.names(manifest(tool(data_class='C')), **options)
        self.assertEqual(offered.get('image_generate'), 'byok')

    def test_unfunded_or_signed_out_byok_requires_key(self):
        value = manifest(tool(available=False, unavailable_reason='credits'), account={'credits': {'balance': 0}})
        self.assertEqual(self.names(value, signed_in=True), {})
        self.assertEqual(self.names(value, keys_set={'OPENAI_API_KEY': True})['image_generate'], 'byok')
        self.assertEqual(self.names(manifest(account=None), keys_set={'FAL_KEY': True})['retopology'], 'byok')

    def test_a_try_it_cards_tool_is_offered_without_credits_and_nothing_else_is_lifted(self):
        # The setup's free cards (try_samples): only a refusal for credits is lifted, only for the named tool.
        value = manifest(tool(available=False, unavailable_reason='credits'), tool('sfx', available=False, unavailable_reason='credits'),
                         account={'credits': {'balance': 25}})
        self.assertEqual(self.names(value, signed_in=True), {})
        self.assertEqual(self.names(value, signed_in=True, on_the_house={'image_generate'}), {'image_generate': 'managed'})
        self.assertEqual(self.names(value, on_the_house={'image_generate'}), {})  # Signed out: still nothing.
        for reason in ('plan', 'private_mode', 'no_generation', 'disabled'):
            refused = manifest(tool(available=False, unavailable_reason=reason), account={'credits': {'balance': 0}})
            self.assertEqual(self.names(refused, signed_in=True, on_the_house={'image_generate'}), {}, reason)
        off = manifest(tool(available=False, unavailable_reason='credits'), flags={'services': False})
        self.assertEqual(self.names(off, signed_in=True, on_the_house={'image_generate'}), {})

    def test_the_running_try_it_cards_tool_is_on_the_house(self):
        from loopcut import state
        turn = types.SimpleNamespace(try_it='photo_3d')
        with patch.object(state, '_session', {'busy': True, 'turn': turn}):
            self.assertEqual(cloud.on_the_house_tools(), {'image_to_3d'})
        with patch.object(state, '_session', {'busy': False, 'turn': turn}):
            self.assertEqual(cloud.on_the_house_tools(), set())  # Between turns.
        with patch.object(state, '_session', {'busy': True, 'turn': types.SimpleNamespace(try_it='')}):
            self.assertEqual(cloud.on_the_house_tools(), set())  # An edited prompt is the user's own request.
        with patch.object(state, '_session', None):
            self.assertEqual(cloud.on_the_house_tools(), set())

    def test_no_generation_account_and_explicit_project_override(self):
        value = manifest(tool(), tool('assist_tool', kind='assist', backends=['managed']),
                         account={'credits': {'balance': 100}, 'settings': {'no_generation': True}})
        self.assertEqual(self.names(value, signed_in=True), {'assist_tool': 'managed'})
        self.assertNotIn('image_generate', self.names(value, signed_in=True, policy={'no_generation': False}))
        self.assertNotIn('image_generate', self.names(manifest(tool()), signed_in=True, policy={'no_generation': True}))

    def test_coming_soon_never_exposed_even_with_key(self):
        self.assertNotIn('image_generate', self.names(manifest(tool(status='coming_soon')), signed_in=True,
                                    keys_set={'OPENAI_API_KEY': True}))

    def test_services_off_keeps_byok_but_plan_and_region_refusals_bind(self):
        value = manifest(tool(), flags={'services': False, 'premium_skills': False})
        self.assertEqual(self.names(value, signed_in=True, keys_set={'OPENAI_API_KEY': True})['image_generate'], 'byok')
        for reason in ('plan', 'region', 'no_generation', 'private_mode'):
            self.assertNotIn('image_generate', self.names(manifest(tool(available=False, unavailable_reason=reason)), signed_in=True,
                                        keys_set={'OPENAI_API_KEY': True}))

    def test_private_mode_filters_byok(self):
        self.assertNotIn('sfx', self.names(manifest(account=None), keys_set={'ELEVENLABS_API_KEY': True}))
        self.assertEqual(self.names(manifest(account=None), keys_set={'ELEVENLABS_API_KEY': True},
                                    policy={'private_mode': False})['sfx'], 'byok')

    def test_private_mode_blocks_c_and_d_on_both_routes_despite_stale_local_policy(self):
        for data_class in ('C', 'D'):
            descriptor = tool('sfx', byok={'provider': 'elevenlabs', 'data_class': data_class})
            for backend in ('managed', 'byok'):
                with self.subTest(data_class=data_class, backend=backend):
                    self.assertNotIn('sfx', self.names(manifest(descriptor), signed_in=True,
                        keys_set={'ELEVENLABS_API_KEY': True}, preferred={'sfx': backend},
                        policy={'private_mode': False}))

    def test_duplicate_descriptors_merge_by_name(self):
        descriptors = cloud.merge_tools(manifest(tool(), tool(description='Latest')), signed_in=True)
        self.assertEqual(len(descriptors), 1)
        self.assertEqual(descriptors[0]['description'], 'Latest')

    def test_new_server_tool_needs_no_local_entry(self):
        self.assertEqual(self.names(manifest(tool('new_tool', backends=['managed'])), signed_in=True), {'new_tool': 'managed'})

    def test_coming_soon_never_exposed_even_with_key_marked_instead_of_credits(self):
        self.assertNotIn('image_generate', self.names(manifest(tool(status='coming_soon')), signed_in=True,
                                    keys_set={'OPENAI_API_KEY': True}, preferred={'image_generate': 'byok'}))

    def test_byok_uses_local_file_contract(self):
        descriptor = tool('image_to_3d', file_inputs=[{'field': 'image', 'required': False,
                                                   'max_mb': 20, 'accept': ['image/png']}])
        result = cloud.merge_tools(manifest(descriptor), keys_set={'FAL_KEY': True})
        found = next(d for d in result if d['name'] == 'image_to_3d')
        self.assertEqual(found['file_inputs'][0]['field'], 'reference')
        self.assertEqual(found['description'], descriptor['description'])


class OwnKeyFirstTest(unittest.TestCase):
    """"Use this key instead of credits", per vendor key (owner, 2026-10-16): config.service_backends is
    the `preferred` that merge_tools reads, from the checkboxes and the keys that are set."""

    def routes(self, value, *, first=None, keys=(), signed_in=True, policy=None):
        settings = {config.OWN_KEY_FIRST + name: 'true' if on else 'false' for name, on in (first or {}).items()}
        keys_set = {name: True for name in keys}
        with patch.object(config, '_getter', return_value=(None, lambda key, default='': settings.get(key) or default)):
            preferred = config.service_backends(keys_set)
        return {d['name']: d['backend'] for d in cloud.merge_tools(value, signed_in=signed_in, keys_set=keys_set,
                                                                    preferred=preferred, policy=policy)}

    def test_on_by_default_for_every_vendor_key(self):
        from loopcut import credentials
        with patch.object(config, '_getter', return_value=(None, lambda key, default='': default)):
            self.assertEqual(config.own_key_first(), {name: True for name in credentials.VENDOR_KEY_NAMES})
            self.assertEqual(config.service_backends({'FAL_KEY': True}), {'image_to_3d': 'byok', 'retopology': 'byok'})
            self.assertEqual(config.service_backends({}), {})

    def test_checked_key_takes_the_own_key_route_with_credits(self):
        self.assertEqual(self.routes(manifest(tool()), keys=['OPENAI_API_KEY'])['image_generate'], 'byok')
        self.assertEqual(self.routes(manifest(tool()), first={'OPENAI_API_KEY': True},
                                     keys=['OPENAI_API_KEY'])['image_generate'], 'byok')

    def test_unchecked_key_takes_credits_first_then_the_key(self):
        first = {'OPENAI_API_KEY': False}
        self.assertEqual(self.routes(manifest(tool()), first=first, keys=['OPENAI_API_KEY'])['image_generate'], 'managed')
        unfunded = manifest(tool(available=False, unavailable_reason='credits'), account={'credits': {'balance': 0}})
        self.assertEqual(self.routes(unfunded, first=first, keys=['OPENAI_API_KEY'])['image_generate'], 'byok')
        self.assertEqual(self.routes(manifest(tool(), account=None), first=first, keys=['OPENAI_API_KEY'],
                                     signed_in=False)['image_generate'], 'byok')

    def test_no_key_is_unchanged(self):
        for first in (None, {'OPENAI_API_KEY': True}, {'OPENAI_API_KEY': False}):
            with self.subTest(first=first):
                self.assertEqual(self.routes(manifest(tool()), first=first)['image_generate'], 'managed')
                self.assertNotIn('image_generate', self.routes(manifest(tool(), account=None), first=first,
                                                               signed_in=False))

    def test_each_key_routes_only_its_own_vendors_tools(self):
        value = manifest(tool(), tool('image_to_3d'), tool('retopology', kind='assist'))
        routes = self.routes(value, first={'OPENAI_API_KEY': False, 'FAL_KEY': True}, keys=['OPENAI_API_KEY', 'FAL_KEY'])
        # image_edit has no managed route in this manifest, so its key serves it either way.
        self.assertEqual(routes, {'image_generate': 'managed', 'image_edit': 'byok', 'image_to_3d': 'byok',
                                  'retopology': 'byok'})

    def test_private_mode_no_generation_and_coming_soon_still_hide_a_checked_key(self):
        sfx = tool('sfx', byok={'provider': 'elevenlabs', 'key_name': 'ELEVENLABS_API_KEY', 'data_class': 'C'})
        self.assertNotIn('sfx', self.routes(manifest(sfx), keys=['ELEVENLABS_API_KEY']))  # manifest(): Private mode on.
        self.assertNotIn('image_generate', self.routes(manifest(tool()), keys=['OPENAI_API_KEY'],
                                                       policy={'no_generation': True}))
        self.assertNotIn('image_generate', self.routes(manifest(tool(status='coming_soon')), keys=['OPENAI_API_KEY']))
        for reason in ('plan', 'region'):
            self.assertNotIn('image_generate', self.routes(
                manifest(tool(available=False, unavailable_reason=reason)), keys=['OPENAI_API_KEY']))

    def test_checked_key_never_falls_back_to_credits(self):
        # The key went missing after the snapshot: the route is refused, never moved to credits.
        self.assertEqual(catalog.choose_backend(tool(), signed_in_with_credits=True, preferred_backend='byok',
                                                has_key=False), {'backend': None, 'unavailable_reason': 'disabled'})


class SchemaTest(RuntimeFixture):
    def test_eleven_coming_soon_tools_render_but_never_reach_any_model(self):
        from coming_soon_fixture import manifest as coming_soon_manifest
        from loopcut import agent, chatgpt_responses, codex_app, state
        from loopcut.ui import layout
        fixture = coming_soon_manifest()
        expected = {d['name'] for d in fixture['tools']}
        self.assertEqual(len(expected), 11)
        self.assertTrue(all(cloud._descriptor(d) for d in fixture['tools']))
        fixture['tools'].append(tool('live_extra', byok=None, backends=['managed']))
        fixture['account'] = manifest()['account']
        runtime = self.runtime(Reply(fixture), keys_set={'OPENAI_API_KEY': True})
        self.assertTrue(runtime.refresh())
        rows = runtime.coming_soon()
        self.assertEqual({r['name'] for r in rows}, expected)
        self.assertEqual(rows[-1]['category'], '')  # Invalid metadata does not hide a tool.
        local_names = {d['name'] for d in catalog.descriptors(no_generation=False, private_mode=False, keys_set={})}
        self.assertTrue(expected - local_names)
        flat = agent._tool_schemas()
        responses = chatgpt_responses.to_tools(flat)
        dynamic = codex_app.dynamic_tools()
        for names in ({s['function']['name'] for s in flat}, {s['name'] for s in dynamic}):
            self.assertIn('loopcut_cloud__live_extra', names)
            self.assertFalse(names & {cloud.PREFIX + n for n in expected})
        namespace = next(n for n in responses if n['name'] == cloud.NAMESPACE)
        self.assertIn('live_extra', {s['name'] for s in namespace['tools']})
        self.assertFalse({s['name'] for s in namespace['tools']} & expected)
        measure = lambda font, size, text: len(text) * size * .6
        for width in (240, 420, 900):
            with self.subTest(width=width):
                chat = layout.build(state.new_session(), width, 800, 1, measure, ui={'coming_soon': rows})
                self.assertEqual(next(h for h in chat['hits'] if h['id'] == 'vote.strip')['action'], ('votes_open', None))
                display = layout.build(state.new_session(), width, 800, 1, measure, ui={'view': 'vote', 'coming_soon': rows})
                votes = [h for h in display['hits'] if h['action'][0] == 'vote']
                self.assertEqual({h['action'][1] for h in votes}, expected)
                header = next(p for p in display['prims'] if p['id'] == 'header.bg')
                footer = next(p for p in display['prims'] if p['id'] == 'vote.footer.rule')
                for h in votes:
                    self.assertGreaterEqual(h['y'], header['y'] + header['h'])
                    self.assertLessEqual(h['y'] + h['h'], footer['y'])
                    self.assertLessEqual(h['x'] + h['w'], width)

    def test_category_is_optional_bounded_display_metadata(self):
        runtime = self.runtime()
        for category, expected in ((None, ''), (False, ''), (['images'], ''), ('3d_models', '3D models'),
                                   ('characters', 'Characters'), (' new_group\n ', 'New group'), ('x' * 100, 'X' + 'x' * 39)):
            with self.subTest(category=category):
                descriptor = tool(status='coming_soon', display={'title': 'Shown title', 'summary': 'One line.',
                                                                 'category': category})
                self.assertTrue(cloud._descriptor(descriptor))
                runtime.manifest = manifest(descriptor)
                row = runtime.coming_soon()[0]
                self.assertEqual(row['category'], expected)
                self.assertEqual((row['title'], row['summary']), ('Shown title', 'One line.'))
        runtime.manifest = manifest(tool(status='coming_soon'))  # No display: the descriptor's own title.
        self.assertEqual((runtime.coming_soon()[0]['category'], runtime.coming_soon()[0]['summary']), ('', ''))

    def test_invalid_bundle_metadata_does_not_reach_the_model(self):
        for bundle in (True, {}, {'required': 'yes', 'description': 'Scene'}, {'required': True}):
            with self.subTest(bundle=bundle):
                self.assertEqual(cloud.merge_tools(manifest(tool(bundle_input=bundle)), signed_in=True), [])

    def test_bundle_tools_never_ask_the_model_for_a_bundle(self):
        render = tool('render', byok=None, backends=['managed'], latency='long', apply='load_frames',
                      cost={'credits_estimate': 2, 'quote_required': True}, approval={'confirm_over_credits': 100},
                      bundle_input={'required': True, 'description': 'Self-contained Blender scene bundle.'},
                      input_schema={'type': 'object', 'properties': {'frame_start': {'type': 'integer'}},
                                    'required': [], 'additionalProperties': False})
        runtime = self.runtime(Reply(manifest(render, tool())))
        runtime.refresh()
        functions = {s['function']['name']: s['function'] for s in cloud.tool_schemas()}
        function = functions[cloud.PREFIX + 'render']
        self.assertNotIn('bundle', function['parameters']['properties'])
        self.assertNotIn('bundle', function['parameters']['required'])
        self.assertIn('quote_id', function['parameters']['properties'])
        self.assertTrue(function['description'].startswith(render['description']))
        self.assertIn('Loopcut packs and uploads the current scene', function['description'])
        descriptor = next(d for d in runtime.descriptors() if d['name'] == 'render')
        self.assertEqual(descriptor['bundle_input'], render['bundle_input'])  # The server contract is kept.
        image = functions[cloud.PREFIX + 'image_generate']  # A tool without a bundle is unchanged.
        self.assertEqual(image['description'], tool()['description'])
        self.assertNotIn('bundle', image['parameters']['properties'])

    def test_both_provider_paths_unique_and_namespace_roundtrip(self):
        from loopcut import chatgpt_responses as responses
        from loopcut import codex_app
        descriptor = tool(file_inputs=[{'field': 'reference', 'required': True, 'max_mb': 20, 'accept': ['image/png']}])
        runtime = self.runtime(Reply(manifest(descriptor, tool('later', status='coming_soon'))))
        runtime.refresh()
        flat = cloud.tool_schemas()
        self.assertNotIn('loopcut_cloud__later', {row['name'] for row in codex_app.dynamic_tools()})
        function = flat[0]['function']
        self.assertEqual(function['name'], 'loopcut_cloud__image_generate')
        self.assertEqual(function['description'], descriptor['description'])
        self.assertEqual(function['parameters']['properties']['reference']['type'], 'string')
        self.assertIn('reference', function['parameters']['required'])
        self.assertEqual(descriptor['input_schema']['required'], ['prompt'])  # Conversion did not mutate it.
        local = {'type': 'function', 'function': {'name': 'image_generate', 'description': 'Local', 'parameters': {}}}
        namespaces = responses.to_tools([local, *flat])
        self.assertEqual([n['name'] for n in namespaces], ['loopcut', 'loopcut_cloud'])
        self.assertEqual(namespaces[1]['tools'][0]['name'], 'image_generate')
        names = [f['function']['name'] for f in [local, *flat]]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn('later', json.dumps(namespaces))
        self.assertEqual(responses.bare_name('image_generate', 'loopcut_cloud'), function['name'])
        self.assertEqual(responses.bare_name('loopcut_cloud.image_generate'), function['name'])
        call = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'function': {'name': function['name'], 'arguments': '{}'}}]}
        replayed = responses._assistant_items(call, False)[0]
        self.assertEqual((replayed['namespace'], replayed['name']), ('loopcut_cloud', 'image_generate'))

    def test_long_tools_offer_job_status(self):
        runtime = self.runtime(Reply(manifest(tool(latency='long'))))
        runtime.refresh()
        self.assertEqual([s['function']['name'] for s in cloud.tool_schemas()], ['loopcut_cloud__image_generate', 'loopcut_cloud__job_status'])

    def test_codex_and_workspace_receive_dynamic_cloud_tools(self):
        from loopcut import agent, codex_app
        runtime = self.runtime(Reply(manifest(tool())))
        runtime.refresh()
        self.assertIn('loopcut_cloud__image_generate', [s['function']['name'] for s in agent._workspace_schemas()])
        self.assertIn('loopcut_cloud__image_generate', [s['name'] for s in codex_app.dynamic_tools()])


class SelectionTest(unittest.TestCase):
    def test_no_words_requires_nonempty_available_tools(self):
        document, _ = served()
        blank, _ = served(requires=())
        self.assertNotIn('Test integration note.', skills.single_notes('anything', server_skills=[document]))
        self.assertNotIn('Test integration note.', skills.single_notes('anything', server_skills=[blank]))
        self.assertIn('Test integration note.', skills.single_notes('anything', server_skills=[document],
                      available_tools=['loopcut_cloud.asset_search']))
        self.assertIn('Test integration note.', skills.skills_for('modeling', {}, server_skills=[document],
                      available_tools=['loopcut_cloud.asset_search']))

    def test_words_and_craft_pick_only_matching_request_and_worker(self):
        document, _ = served(words='bicycle', requires=(), craft='modeling')
        self.assertNotIn('Test integration note.', skills.single_notes('a boat', server_skills=[document]))
        self.assertIn('Test integration note.', skills.single_notes('a bicycle', server_skills=[document]))
        self.assertIn('Test integration note.', skills.skills_for('modeling', {'deliverable': 'bicycle'}, server_skills=[document]))
        self.assertNotIn('Test integration note.', skills.skills_for('materials', {'deliverable': 'bicycle'}, server_skills=[document]))

    def test_without_server_skills_output_is_unchanged(self):
        self.assertEqual(skills.single_notes('a boat'), skills.single_notes('a boat', server_skills=[], available_tools=['loopcut_cloud.asset_search']))
        self.assertEqual(skills.skills_for('modeling', {}), skills.skills_for('modeling', {}, server_skills=[]))


class DispatchTest(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.module = types.ModuleType('loopcut.cloud_jobs')
        self.calls = []
        def resolve(descriptor, arguments, out_dir):
            self.calls.append(('resolve', threading.get_ident(), arguments))
            return {'reference': Path(out_dir) / 'ref.png'} if arguments.get('reference') else {}
        def run(descriptor, arguments, files, *, project, on_progress=None, cancel=None, client=None, quote=False):
            if quote and not arguments.get('quote_id'):  # authorize prices every paid call first, as the server quotes.
                self.calls.append(('quote', threading.get_ident(), project))
                raise cloud_http.CloudError('Approve the quote', 'quote_required',
                                            extra={'quote_id': 'qt_1', 'credits': 6, 'expires_at': '2099-01-01T00:00:00Z'})
            self.calls.append(('run', threading.get_ident(), project))
            if on_progress:
                on_progress({'pct': 40, 'message': 'Working'})
            return {'id': 'job_1', 'status': 'succeeded', 'outputs': [], 'error': None}
        def apply(descriptor, job):
            self.calls.append(('apply', threading.get_ident(), job))
            return 'Imported result.'
        self.module.resolve_files, self.module.run, self.module.apply_result = resolve, run, apply
        context = patch.dict(sys.modules, {'loopcut.cloud_jobs': self.module})
        context.start()
        self.addCleanup(context.stop)
        context = patch.dict(cloud._jobs, {}, clear=True)
        context.start()
        self.addCleanup(context.stop)
        from loopcut import scene_diff
        context = patch.object(scene_diff, 'snapshot', return_value={})
        context.start()
        self.addCleanup(context.stop)

    def ready(self, **changes):
        runtime = self.runtime(Reply(manifest(tool(**changes))))
        runtime.refresh()
        return runtime

    def test_dismissed_panel_notice_still_explains_unavailable_tool(self):
        runtime = self.runtime(error('contract_unsupported', 426))
        runtime.refresh()
        cloud.dismiss_ui_message(cloud.UPDATE_MESSAGE)
        self.assertEqual(cloud.ui_message(), '')
        result = cloud.execute(cloud.PREFIX + 'unknown_tool', {})
        self.assertFalse(result.ok)
        self.assertEqual(result.text, cloud.UPDATE_MESSAGE)
        self.assertEqual(self.calls, [])

    @patch.object(config, 'billing_open', lambda: True)
    def test_refusal_reason_precedes_dismissed_runtime_notice(self):
        runtime = self.ready(available=False, unavailable_reason='credits')
        runtime.message = runtime.dismissed_message = cloud.UPDATE_MESSAGE
        result = cloud.execute(cloud.PREFIX + 'image_generate', {'prompt': 'prop'})
        self.assertFalse(result.ok)
        self.assertEqual(result.text, 'Not enough credits.')
        self.assertEqual(self.calls, [])

    def test_no_generation_refusal_survives_unsupported_contract(self):
        runtime = self.ready()
        runtime.supported = False
        runtime.policy['no_generation'] = True
        runtime.message = runtime.dismissed_message = cloud.UPDATE_MESSAGE
        result = cloud.execute(cloud.PREFIX + 'image_generate', {'prompt': 'prop'})
        self.assertFalse(result.ok)
        self.assertEqual(result.text, 'No generation is on for this account or project.')
        self.assertEqual(self.calls, [])

    @staticmethod
    def quoting(run):
        """A fake run that first answers authorize's quote as the server does (every paid call is quoted)."""
        def priced(descriptor, arguments, files, *, quote=False, **kwargs):
            if quote and not arguments.get('quote_id'):
                raise cloud_http.CloudError('Approve the quote', 'quote_required',
                                            extra={'quote_id': 'qt_1', 'credits': 6, 'expires_at': '2099-01-01T00:00:00Z'})
            return run(descriptor, arguments, files, **kwargs)
        return priced

    def run_step(self, prepared):
        from loopcut import mainthread
        result, pending = [], queue.Queue()
        def on_main(fn):
            from concurrent.futures import Future
            future = Future()
            pending.put((fn, future))
            return future.result(timeout=5)
        def worker():
            try:
                with cloud_account.approval_context(lambda details: True):
                    result.append(prepared.then(lambda: False, on_main))
            except BaseException as ex:
                result.append(ex)
        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 5
        while thread.is_alive() and time.monotonic() < deadline:
            try:
                fn, future = pending.get(timeout=.01)
                future.set_result(fn())
            except queue.Empty:
                pass
            while not mainthread._jobs.empty():
                fn, future = mainthread._jobs.get_nowait()
                future.set_result(fn())
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        if isinstance(result[0], BaseException):
            raise result[0]
        return result[0]

    def test_resolve_and_apply_main_run_worker(self):
        self.ready()
        main = threading.get_ident()
        from loopcut import tools
        result = self.run_step(tools.execute('loopcut_cloud__image_generate', '{"prompt":"test"}'))
        self.assertTrue(result.ok)
        self.assertEqual(result.text, 'Imported result.')
        self.assertEqual([c[0] for c in self.calls], ['resolve', 'quote', 'run', 'apply'])  # Priced, then run.
        self.assertEqual([self.calls[0][1], self.calls[-1][1]], [main, main])
        self.assertNotEqual(self.calls[1][1], main)

    def test_missing_or_incomplete_c3_fails_clearly(self):
        self.ready()
        with patch.object(cloud.importlib, 'import_module', side_effect=ModuleNotFoundError(name='loopcut.cloud_jobs')):
            self.assertIn('not available in this build', cloud.execute('loopcut_cloud__image_generate', {'prompt': 'test'}).text)
        del self.module.apply_result
        self.assertIn('not available in this build', cloud.execute('loopcut_cloud__image_generate', {'prompt': 'test'}).text)

    def test_errors_actionable_and_tool_disabled_refreshes(self):
        runtime = self.ready()
        for code, status, extra, expected in [('insufficient_credits', 402, {'balance': 1, 'needed': 2}, 'balance 1, needed 2'),
                ('generation_disabled', 403, {}, 'disabled'), ('vendor_failed', 502, {'refunded': True}, 'refunded'),
                ('rate_limited', 429, {'retry_after': 7}, '7 seconds'), ('tool_disabled', 403, {}, 'refreshing'),
                ('contract_unsupported', 426, {}, cloud.UPDATE_MESSAGE)]:
            with self.subTest(code=code), patch.object(runtime, 'refresh_async') as refresh:
                runtime.supported = True
                self.module.run = lambda *a, **k: (_ for _ in ()).throw(cloud_http.CloudError('Refused', code, status, extra))
                result = self.run_step(cloud.execute('loopcut_cloud__image_generate', {'prompt': 'test'}))
                self.assertFalse(result.ok)
                self.assertIn(expected, result.text)
                if code == 'tool_disabled':
                    refresh.assert_called_once()

    def test_refused_scene_bundle_says_what_the_render_service_refuses(self):
        # Live 2026-10-21: a rigged BlenderKit character's render failed on the worker (a scripted driver) and the
        # model saw only "the vendor failed", so the user heard "it failed to bundle" and no reason.
        message = ('The service rejected the input (input.bundle). Vendor detail: invalid_input. Your credits were '
                   'refunded. To try again, start a new job with a new idempotency_key.')
        error = cloud_http.CloudError(message, 'vendor_failed', extra={'field': 'input.bundle', 'refunded': True})
        first, _, extras = cloud.error_text(error).partition('\n')
        for part in ('refused the packed scene', 'credits refunded', 'Python driver', 'driver settings', 'camera',
                     'Tell the user', 'Vendor detail: invalid_input'):
            self.assertIn(part, first)
        self.assertNotIn('idempotency_key', first)  # The add-on picks new keys itself.
        self.assertEqual(json.loads(extras)['error']['field'], 'input.bundle')
        other = cloud_http.CloudError('Vendor failed', 'vendor_failed', extra={'refunded': True})
        self.assertTrue(cloud.error_text(other).startswith('vendor_failed: the vendor failed; credits refunded.'))

    def test_animated_driver_settings_are_named_with_what_to_do_before_upload(self):
        # The render service refuses driver_animated (render_job.py); the packer names each one the same way it names
        # a Python driver, with what to do, and nothing is uploaded.
        from loopcut import bundles
        rows = [{'type': 'keyframes', 'name': 'Action "Sneaky"', 'path': 'animation_data.drivers[0].mute',
                 'reason': bundles.DRIVER_ANIMATED}]
        first, _, extras = cloud.error_text(cloud._bundle_refusal({'missing': [], 'unsupported': rows})).partition('\n')
        self.assertIn("can't pack this scene: 1 unsupported item(s)", first)
        self.assertIn('Nothing was uploaded', first)
        error = json.loads(extras)['error']
        self.assertEqual((error['code'], error['unsupported']), ('bundle_incomplete', [
            f'keyframes Action "Sneaky" animation_data.drivers[0].mute: {bundles.DRIVER_ANIMATED}']))

    def test_long_returns_handle_and_applies_exactly_once(self):
        self.ready(latency='long')
        job = {'id': 'job_1', 'status': 'running', 'progress': {'pct': 0, 'message': 'Queued'}}
        self.module.run = self.quoting(lambda *args, **kwargs: deepcopy(job))
        self.module.job_status = lambda handle: deepcopy(job)
        result = self.run_step(cloud.execute('loopcut_cloud__image_generate', {'prompt': 'test'}))
        handle = json.loads(result.text)['job_id']
        self.assertEqual(handle, 'job_1')
        self.assertEqual(json.loads(cloud.execute('loopcut_cloud__job_status', {'job_id': handle}).text)['status'], 'running')
        job['status'] = 'succeeded'
        self.assertEqual(cloud.execute('loopcut_cloud__job_status', {'job_id': handle}).text, 'Imported result.')
        self.assertEqual(cloud.execute('loopcut_cloud__job_status', {'job_id': handle}).text, 'Imported result.')
        self.assertEqual([c[0] for c in self.calls].count('apply'), 1)

    @patch.object(config, 'billing_open', lambda: True)
    def test_long_error_keeps_extras_and_reports_to_originating_card_once(self):
        self.ready(latency='long')
        self.module.run = self.quoting(lambda *args, **kwargs: {'id': 'job_1', 'status': 'running'})
        notices = []
        with cloud_account.approval_context(lambda details: notices.append(details) or True):
            cloud.execute(cloud.PREFIX + 'image_generate', {'prompt': 'prop'}).then(lambda: False, lambda fn: fn())
        failure = cloud_http.CloudError('Refused', 'insufficient_credits', 402,
            {'balance': 1, 'needed': 20, 'upgrade': 'https://loopcut.org/pricing', 'job': {'id': 'job_1'}})
        self.module.job_status = unittest.mock.Mock(side_effect=failure)
        first = cloud.execute(cloud.PREFIX + 'job_status', {'job_id': 'job_1'})
        second = cloud.execute(cloud.PREFIX + 'job_status', {'job_id': 'job_1'})
        self.assertFalse(first.ok)
        self.assertEqual(first.text, second.text)
        extra = json.loads(first.text.split('\n', 1)[1])['error']
        self.assertEqual(extra['needed'], 20)
        self.assertNotIn('job', extra)
        self.assertEqual([d['error']['upgrade'] for d in notices if 'error' in d], ['https://loopcut.org/pricing'])
        self.module.job_status.assert_called_once_with('job_1')

    def test_precanceled_dispatch_neither_asks_nor_submits(self):
        self.ready(cost={'quote_required': True, 'credits_estimate': 100})
        consent = unittest.mock.Mock(return_value=True)
        with cloud_account.approval_context(consent):
            result = cloud.execute(cloud.PREFIX + 'image_generate', {'prompt': 'prop'}).then(lambda: True, lambda fn: fn())
        self.assertFalse(result.ok)
        self.assertIn('canceled', result.text)
        consent.assert_not_called()
        self.assertEqual([c[0] for c in self.calls], ['resolve'])

    def test_byok_runs_local_runner_then_shared_apply(self):
        from loopcut import credentials
        from loopcut.byok import runner
        runtime = self.ready()
        runtime.preferred, runtime.keys_set = {'image_generate': 'byok'}, {'OPENAI_API_KEY': True}
        def run(*args, **kwargs):
            self.calls.append(('byok', threading.get_ident(), (args, kwargs)))
            return {'id': 'byok_1', 'status': 'succeeded', 'outputs': [], 'error': None}
        with patch.object(runner, 'run', side_effect=run), patch.object(credentials, 'vendor_key', return_value='test-key'):
            result = self.run_step(cloud.execute('loopcut_cloud__image_generate', {'prompt': 'test'}))
        self.assertTrue(result.ok)
        self.assertEqual([c[0] for c in self.calls], ['resolve', 'byok', 'apply'])
        self.assertEqual(self.calls[1][2][0][1], {'prompt': 'test'})


class MockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = MockService('--live-tools', 'image_generate,asset_search', '--job-seconds', '0.01')
        cls.service.__enter__()
        cls.addClassCleanup(cls.service.__exit__, None, None, None)

    def setUp(self):
        fake = types.SimpleNamespace(app=types.SimpleNamespace(version=(5, 2, 0), driver_namespace={}))
        context = patch.dict(sys.modules, {'bpy': fake})
        context.start()
        self.addCleanup(context.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = cloud_http.Client(base_url=self.service.base_url, api_key=self.service.key)
        self.client.request('POST', '/mock/control', {'reset': True})
        self.runtime = cloud.Runtime(self.client, self.directory.name, signed_in=True)

    def control(self, **options):
        self.client.request('POST', '/mock/control', options)
        self.assertTrue(self.runtime.refresh())

    def test_live_flags_coming_soon_and_etag(self):
        self.assertTrue(self.runtime.refresh())
        self.assertEqual({d['name'] for d in self.runtime.descriptors()}, {'image_generate', 'asset_search'})
        etag = self.runtime.etag
        self.assertTrue(self.runtime.refresh())
        self.assertEqual(self.runtime.etag, etag)
        self.control(live_tools=['retopology'])
        self.assertEqual({d['name'] for d in self.runtime.descriptors()}, {'retopology'})
        self.control(flags={'services': False})
        self.assertEqual(self.runtime.descriptors(), [])

    def test_signed_out_and_skills_flag(self):
        runtime = cloud.Runtime(cloud_http.Client(base_url=self.service.base_url, api_key=''),
                                Path(self.directory.name) / 'signed-out', signed_in=False)
        self.assertTrue(runtime.refresh())
        self.assertEqual(runtime.descriptors(), [])
        self.assertTrue(all(not d['available'] and d['unavailable_reason'] == 'sign_in' for d in runtime.manifest['tools']))
        self.assertIsNone(runtime.manifest['account'])
        self.assertTrue(self.runtime.refresh())
        self.assertTrue(self.runtime.server_skills())
        self.control(flags={'premium_skills': False})
        self.assertEqual(self.runtime.server_skills(), [])

    def test_real_304_response(self):
        self.runtime.refresh()
        response = self.client.request_response('GET', '/v1/manifest', headers={'If-None-Match': self.runtime.etag})
        self.assertEqual(response.status, 304)
        self.assertIsNone(response.data)

    def test_interest_event_count(self):
        self.runtime.refresh()
        name = self.runtime.coming_soon()[0]['name']
        self.assertTrue(self.runtime.interest(name))
        self.assertFalse(self.runtime.interest(name))
        self.assertEqual(self.client.request('GET', '/mock/state')['events'], 1)

    def test_mock_error_envelopes_trigger_runtime_rules(self):
        self.runtime.refresh()
        for code in ('tool_disabled', 'contract_unsupported'):
            self.client.request('POST', '/mock/control', {'force_error': code})
            with self.subTest(code=code), self.assertRaises(cloud_http.CloudError) as caught:
                self.client.request('POST', '/v1/jobs', {})
            with patch.object(self.runtime, 'refresh_async') as refresh:
                self.runtime.handle_error(caught.exception)
                if code == 'tool_disabled':
                    refresh.assert_called_once()
                else:
                    self.assertEqual(self.runtime.descriptors(), [])
                    self.assertEqual(self.runtime.message, cloud.UPDATE_MESSAGE)


class LifecycleTest(RuntimeFixture):
    def test_panel_refresh_is_asynchronous_and_throttled(self):
        runtime = self.runtime()
        with patch.object(cloud, '_snapshot', return_value=runtime), patch.object(runtime, 'refresh_async') as refresh:
            cloud.panel_seen()
            cloud.panel_seen()
            refresh.assert_called_once()
            runtime.refreshed_at = time.monotonic() - cloud.REFRESH_SECONDS - 1
            cloud.panel_seen()
            self.assertEqual(refresh.call_count, 2)

    def test_sign_in_timer_hooks_and_unregister(self):
        import loopcut
        account = types.SimpleNamespace(on_signed_in=[])
        timers = types.SimpleNamespace(is_registered=lambda fn: False, register=unittest.mock.Mock(), unregister=unittest.mock.Mock())
        fake = types.SimpleNamespace(app=types.SimpleNamespace(timers=timers))
        old = list(account.on_signed_in)
        self.addCleanup(lambda: account.on_signed_in.__setitem__(slice(None), old))
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(loopcut, 'account', account, create=True), \
                patch.object(cloud, 'refresh_async') as refresh:
            cloud.register()
            self.assertIn(refresh, account.on_signed_in)
            timers.register.assert_called_once_with(cloud._timer, first_interval=10, persistent=True)
            refresh.assert_called_once()
            cloud.unregister()
            self.assertNotIn(refresh, account.on_signed_in)

    def test_account_identity_isolated_and_policy_snapshot_main(self):
        from loopcut import checkpoints, credentials
        with patch.object(checkpoints, 'data_root', return_value=Path(self.directory.name)), \
                patch.object(credentials, 'api_key', return_value='lc_demo') as key, \
                patch.object(credentials, 'vendor_keys_set', return_value={}), \
                patch.object(config, 'service_policy', return_value={'no_generation': True, 'private_mode': True}), \
                patch.object(config, 'service_backends', return_value={}):
            signed_in = cloud._snapshot()
            self.assertEqual(signed_in.client.client_headers['x-loopcut-contract'], '0.7')
            signed_in.manifest = manifest(tool())
            signed_in._save()
            key.return_value = ''
            signed_out = cloud._snapshot()
            self.assertNotEqual(signed_in.cache_dir, signed_out.cache_dir)
            self.assertEqual(signed_out.descriptors(), [])
            self.assertTrue(signed_out.policy['no_generation'])

    def test_project_policy_uses_account_default_only_without_override(self):
        with patch.object(config, '_getter', return_value=(None, lambda key, default='': default)):
            self.assertTrue(config.service_policy(no_generation_default=True)['no_generation'])
        with patch.object(config, '_getter', return_value=(None, lambda key, default='': 'false' if key == 'LOOPCUT_NO_GENERATION' else default)):
            self.assertFalse(config.service_policy(no_generation_default=True)['no_generation'])

    def test_interest_success_survives_restart(self):
        runtime = self.runtime(Reply(manifest(tool(status='coming_soon'))), Reply(status=204))
        runtime.refresh()
        runtime.interest('image_generate')
        restarted = cloud.Runtime(runtime.client, self.directory.name, signed_in=True)
        self.assertFalse(restarted.interest('image_generate'))
        self.assertEqual(restarted.interests, {'image_generate': 'thanks'})

    def test_quoted_regex_skill_verifies_original_bytes(self):
        document, entry = served(words=r'\b(?:cloud|asset)\b')
        document['sha256'] = entry['sha256'] = hashlib.sha256(cloud._skill_bytes(document, {'words': "'"})).hexdigest()
        verified = cloud._verified_skill(document, entry)
        self.assertEqual(verified['front_matter']['words'], r'\b(?:cloud|asset)\b')
        self.assertIn('Test integration note.', skills.single_notes('cloud', server_skills=[verified], available_tools=['loopcut_cloud.asset_search']))

    def test_coming_soon_rows_and_a_cast_vote_feed_the_vote_view(self):
        from loopcut import state
        from loopcut.ui import layout
        measure = lambda font, size, text: len(text) * size * .6
        names = ['soon_' + str(n) for n in range(8)]
        runtime = self.runtime(Reply(ideas(*names)), tally(['soon_1'], {'soon_1': 1.0, 'soon_0': 0.5}, {'soon_1': 'most_wanted'}))
        runtime.refresh()
        self.assertTrue(runtime.vote('soon_1'))
        display = layout.build(state.new_session(), 420, 600, 1, measure, 'fast', None,
                               {'view': 'vote', 'coming_soon': runtime.coming_soon()})
        actions = {hit['id']: hit['action'] for hit in display['hits'] if hit['action'][0] == 'vote'}
        self.assertEqual(actions, {'vote.' + name: ('vote', name) for name in names})
        self.assertEqual(list(actions)[:2], ['vote.soon_1', 'vote.soon_0'], 'ranked by level')
        text = {p['id']: p.get('text') for p in display['prims']}
        self.assertEqual((text['vote.soon_1.toggle.glyph'], text['vote.soon_0.toggle.glyph']), ('✓', '▲'))
        self.assertEqual(text['vote.soon_1.badge.label'], 'Most wanted')
        self.assertEqual(text['vote.footer.status.l0'], '2 votes left')
        for hit in display['hits']:
            self.assertLessEqual(hit['y'] + hit['h'], 600)

    def test_schema_file_validation_before_export(self):
        descriptor = tool(file_inputs=[{'field': 'reference', 'required': True, 'max_mb': 20, 'accept': ['image/png']}])
        for arguments in ({'prompt': 'test'}, {'prompt': 'test', 'reference': 42}, {'prompt': 'test', 'reference': ''}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                cloud._validate_arguments(descriptor, arguments)
        cloud._validate_arguments(descriptor, {'prompt': 'test', 'reference': 'Cube'})

    def test_cancel_flag_honours_event_wait_and_turn_stop(self):
        stopped = threading.Event()
        flag = cloud._Cancel(stopped.is_set)
        self.assertFalse(flag.wait(.001))
        stopped.set()
        self.assertTrue(flag.wait(1))
        self.assertTrue(flag.is_set())
