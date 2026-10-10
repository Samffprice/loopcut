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
                self.assertEqual(cloud_account.panel_data(runtime), {'credits': '', 'message': ''})
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
        self.assertEqual(self.transport.calls[0]['headers']['x-loopcut-contract'], '0.5')

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
        self.assertEqual(runtime.coming_soon()[0]['state'], 'thanks')
        self.assertEqual(self.transport.calls[-1]['body'], {'type': 'interest', 'tool': 'image_generate', 'source': 'coming_soon_button',
                                                           'app_version': config.app_version()})
        self.assertFalse(runtime.interest('not_a_tool'))

    def test_interest_error_can_retry(self):
        runtime = self.runtime(Reply(manifest(tool(status='coming_soon'))), error('rate_limited', 429, retry_after=10), Reply(status=204))
        runtime.refresh()
        self.assertFalse(runtime.interest('image_generate'))
        self.assertEqual(runtime.coming_soon()[0]['state'], 'retry')
        self.assertTrue(runtime.interest('image_generate'))

    def test_signed_out_interest_posts_anonymously_and_a_refusal_asks_to_sign_in(self):
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
        self.assertEqual(runtime.coming_soon()[0]['state'], 'notify')
        self.assertEqual(len(self.transport.calls), 2)
        self.assertNotIn('authorization', self.transport.calls[1]['headers'])
        self.assertEqual(self.transport.calls[1]['body'], {'type': 'interest', 'tool': 'image_generate',
                                                           'source': 'coming_soon_button', 'install_id': install_id,
                                                           'app_version': config.app_version()})
        display = layout.build(state.new_session(), 420, 600, 1,
            lambda font, size, text: len(text) * size * .6, 'fast', None, {'coming_soon': runtime.coming_soon()})
        action = next(hit['action'] for hit in display['hits'] if hit['id'] == 'cloud.image_generate')
        self.assertEqual(action, ('cloud_interest', 'image_generate'))
        reminder = next(hit for hit in display['hits'] if hit['id'] == 'cloud.image_generate.signin')
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
                self.assertEqual(runtime.coming_soon()[0]['state'], 'sending')
            finally:
                release.set()
                worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(runtime.coming_soon()[0]['state'], 'thanks')
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

    def test_byok_uses_local_file_contract(self):
        descriptor = tool('image_to_3d', file_inputs=[{'field': 'image', 'required': False,
                                                   'max_mb': 20, 'accept': ['image/png']}])
        result = cloud.merge_tools(manifest(descriptor), keys_set={'FAL_KEY': True})
        found = next(d for d in result if d['name'] == 'image_to_3d')
        self.assertEqual(found['file_inputs'][0]['field'], 'reference')
        self.assertEqual(found['description'], descriptor['description'])


class SchemaTest(RuntimeFixture):
    def test_eleven_coming_soon_tools_render_but_never_reach_any_model(self):
        import coming_soon_fixture
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
        labels = {r['name']: r['data_label'] for r in rows}
        self.assertEqual([labels[n] for n in ('render', 'lip_sync', 'asset_search')],
                         ['Private (A)', 'Private (A)', 'Longer retention (B)'], "0.6's managed class, no byok route")
        self.assertEqual(labels['auto_rig'], 'Data class unknown', 'no class at all: still said, never guessed')
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
                display = layout.build(state.new_session(), width, 800, 1, measure, ui={'coming_soon': rows})
                self.assertEqual({h['action'][1] for h in display['hits'] if h['action'][0] == 'cloud_interest'}, expected)
                labels = [p['text'] for p in display['prims'] if p['id'].startswith('cloud.category.')]
                self.assertEqual(labels, coming_soon_fixture.LABELS)
                box = next(p for p in display['prims'] if p['id'] == 'cloud.box')
                for h in display['hits']:
                    if h['action'][0] == 'cloud_interest':
                        self.assertGreaterEqual(h['y'], box['y'])
                        self.assertLessEqual(h['y'] + h['h'], box['y'] + box['h'])
                        self.assertLessEqual(h['x'] + h['w'], width)
                self.assertGreaterEqual(box['y'], 0)

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
        def run(descriptor, arguments, files, *, project, on_progress=None, cancel=None, client=None):
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

    @patch.object(config, 'BETA', False)
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
        self.assertEqual([c[0] for c in self.calls], ['resolve', 'run', 'apply'])
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

    def test_long_returns_handle_and_applies_exactly_once(self):
        self.ready(latency='long')
        job = {'id': 'job_1', 'status': 'running', 'progress': {'pct': 0, 'message': 'Queued'}}
        self.module.run = lambda *args, **kwargs: deepcopy(job)
        self.module.job_status = lambda handle: deepcopy(job)
        result = self.run_step(cloud.execute('loopcut_cloud__image_generate', {'prompt': 'test'}))
        handle = json.loads(result.text)['job_id']
        self.assertEqual(handle, 'job_1')
        self.assertEqual(json.loads(cloud.execute('loopcut_cloud__job_status', {'job_id': handle}).text)['status'], 'running')
        job['status'] = 'succeeded'
        self.assertEqual(cloud.execute('loopcut_cloud__job_status', {'job_id': handle}).text, 'Imported result.')
        self.assertEqual(cloud.execute('loopcut_cloud__job_status', {'job_id': handle}).text, 'Imported result.')
        self.assertEqual([c[0] for c in self.calls].count('apply'), 1)

    @patch.object(config, 'BETA', False)
    def test_long_error_keeps_extras_and_reports_to_originating_card_once(self):
        self.ready(latency='long')
        self.module.run = lambda *args, **kwargs: {'id': 'job_1', 'status': 'running'}
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
            self.assertEqual(signed_in.client.client_headers['x-loopcut-contract'], '0.5')
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
        self.assertEqual(restarted.coming_soon()[0]['state'], 'thanks')

    def test_quoted_regex_skill_verifies_original_bytes(self):
        document, entry = served(words=r'\b(?:cloud|asset)\b')
        document['sha256'] = entry['sha256'] = hashlib.sha256(cloud._skill_bytes(document, {'words': "'"})).hexdigest()
        verified = cloud._verified_skill(document, entry)
        self.assertEqual(verified['front_matter']['words'], r'\b(?:cloud|asset)\b')
        self.assertIn('Test integration note.', skills.single_notes('cloud', server_skills=[verified], available_tools=['loopcut_cloud.asset_search']))

    def test_coming_soon_layout_actions_and_thank_you(self):
        from loopcut import state
        from loopcut.ui import layout
        measure = lambda font, size, text: len(text) * size * .6
        items = [{'name': 'soon_' + str(n), 'title': 'Tool ' + str(n), 'state': 'ready'} for n in range(8)]
        items[0]['state'] = 'thanks'
        display = layout.build(state.new_session(), 420, 600, 1, measure, 'fast', None, {'coming_soon': items})
        actions = {hit['id']: hit['action'] for hit in display['hits']}
        self.assertEqual(actions['cloud.soon_0'], ('noop', None))
        self.assertEqual(actions['cloud.soon_1'], ('cloud_interest', 'soon_1'))
        self.assertEqual(len([name for name in actions if name.startswith('cloud.soon_')]), 8)
        text = [p.get('text') for p in display['prims']]
        self.assertIn('Tool 0 · Thank you', text)
        for hit in display['hits']:
            if hit['id'].startswith('cloud.'):
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
