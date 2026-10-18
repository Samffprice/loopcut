"""006 request bytes, index lifecycle and mixed local/served selection, without Blender."""

import dataclasses
import io
import json
import os
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/addons_core'))
sys.modules.setdefault('bpy', types.SimpleNamespace(app=types.SimpleNamespace(driver_namespace={})))

from loopcut import agent, cloud, cloud_http, config, context, llm, orchestrator, prompt_index, router, skills, state, waves, workers
from prompt_index_fixture import built_index, expanded_chars, selected_ids, index_patch
from test_cloud import Reply, Transport, manifest
from test_skill_files import markdown


def response(version=None):
    result = io.BytesIO(b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    result.headers = {'x-loopcut-prompt-version': version} if version else {}
    return result


def cfg(url=config.SERVICE_BASE_URL):
    return config.Config('test', url, 'fast', 'low', True, False, 25, 40_000)


class RequestTest(unittest.TestCase):
    def setUp(self):
        patcher = index_patch()
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {'LOOPCUT_SERVICE_URLS': ''})
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, spec, *, url=config.SERVICE_BASE_URL, tail=''):
        session = state.new_session()
        session['addons_line'] = tail
        session['messages'] = [{'role': 'user', 'content': 'hello'}]
        spec = dataclasses.replace(spec, schemas=lambda: [])
        with mock.patch.object(agent, '_redraw'), mock.patch.object(llm.urllib.request, 'urlopen', return_value=response()) as opened:
            agent._request(session, agent.Turn(), cfg(url), spec)
        request = opened.call_args.args[0]
        return json.loads(request.data), {k.lower(): v for k, v in request.header_items()}

    def test_single_and_orchestrator_tail_and_no_tail(self):
        for spec in (agent.SINGLE_SPEC, agent.ORCH_SPEC):
            for tail in ('', 'Installed add-ons: test.\n'):
                with self.subTest(prompt=spec.prompt_id, tail=tail):
                    body, headers = self.request(spec, tail=tail)
                    self.assertEqual(headers['x-loopcut-prompt'], spec.prompt_id)
                    expected = ([{'role': 'system', 'content': '\n\n' + tail}] if tail else [])
                    self.assertEqual(body['messages'], expected + [{'role': 'user', 'content': 'hello'}])

    def test_single_off_service_keeps_exact_open_prompt(self):
        with mock.patch.object(cloud, 'hosted_index', side_effect=AssertionError('off-service index lookup')):
            body, headers = self.request(agent.SINGLE_SPEC, url='https://byok.example/v1', tail='Add-ons')
        self.assertEqual(body['messages'][0]['content'], agent._system_prompt({'addons_line': 'Add-ons'}))
        self.assertFalse(any(k.startswith('x-loopcut-') for k in headers))

    def test_hosted_single_without_index_keeps_open_prompt_and_budget(self):
        for tail in ('', 'Installed add-ons: test.\n'):
            for error in (None, prompt_index.IndexError('unavailable index')):
                with self.subTest(tail=tail, error=error), \
                        mock.patch.object(cloud, 'hosted_index', return_value=None, side_effect=error), \
                        mock.patch.object(context, 'prepare', wraps=context.prepare) as prepare:
                    body, headers = self.request(agent.SINGLE_SPEC, tail=tail)
                    system = agent._system_prompt({'addons_line': tail})
                    self.assertEqual(body['messages'], [{'role': 'system', 'content': system},
                                                       {'role': 'user', 'content': 'hello'}])
                    self.assertNotIn('x-loopcut-prompt', headers)
                    self.assertEqual(prepare.call_args.kwargs['fixed'], context.fixed_tokens(system, []))
                    self.assertIsNone(prepare.call_args.kwargs['skill_index'])

    def test_worker_no_system_and_off_service_refused(self):
        spec = workers.SPEC('modeling', save=lambda _: None)
        body, headers = self.request(spec)
        self.assertEqual(body['messages'], [{'role': 'user', 'content': 'hello'}])
        self.assertEqual(headers['x-loopcut-prompt'], 'worker')
        for spec in (spec, agent.ORCH_SPEC):
            with self.assertRaisesRegex(config.ConfigError, 'service'):
                self.request(spec, url='https://byok.example/v1')

    def test_router_header_and_no_system(self):
        with mock.patch.object(llm.urllib.request, 'urlopen', return_value=response()) as opened:
            router.ask(cfg(), {'role': 'user', 'content': 'hello'}, {'id': 'test', 'messages': []}, lambda: False)
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header('X-loopcut-prompt'), 'router')
        self.assertEqual([m['role'] for m in json.loads(request.data)['messages']], ['user'])

    def test_reference_header_and_open_prompt(self):
        message = {'role': 'user', 'content': [{'type': 'text', 'text': 'ref'},
                   {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}
        session = {'id': 'reference-test', 'references': [{'name': 'ref', 'ref': message['content'][1]['image_url']['url']}]}
        with mock.patch.object(agent.conversations, 'wire_messages', return_value=[message]), \
                mock.patch.object(llm.urllib.request, 'urlopen', return_value=response()) as opened:
            agent._reference_card(session, cfg(), message, lambda: False, 'orchestrator')
        request = opened.call_args.args[0]
        self.assertEqual(request.get_header('X-loopcut-role'), 'reference')
        self.assertIsNone(request.get_header('X-loopcut-prompt'))
        self.assertEqual(json.loads(request.data)['messages'][0]['content'], agent.REFERENCE_PROMPT)

    def test_workspace_specs_remain_open(self):
        self.assertIsNone(agent.WORKSPACE_SPEC.prompt_id)
        self.assertIsNone(agent.CODEX_SPEC.prompt_id)
        body, headers = self.request(agent.WORKSPACE_SPEC)
        self.assertEqual(body['messages'][0]['content'], agent.WORKSPACE_SPEC.system({}))
        self.assertNotIn('x-loopcut-prompt', headers)

    def test_transport_never_requires_index_even_on_retry(self):
        for url, identity in ((config.SERVICE_BASE_URL, 'single'), (config.SERVICE_BASE_URL, 'worker'),
                              (config.SERVICE_BASE_URL, None), ('https://byok.example/v1', 'single')):
            error = llm.urllib.error.HTTPError(url, 503, 'Unavailable',
                                               {'x-loopcut-prompt-version': 'later'}, io.BytesIO(b'{}'))
            self.addCleanup(error.close)
            with self.subTest(url=url, identity=identity), \
                    mock.patch.object(cloud, 'hosted_index', side_effect=prompt_index.IndexError('no index')) as index, \
                    mock.patch.object(llm, '_wait'), \
                    mock.patch.object(llm.urllib.request, 'urlopen', side_effect=[error, response()]) as opened:
                completion = llm.stream_chat(base_url=url, api_key='test', model='fast', messages=[], tools=[],
                                             reasoning_effort='low', on_text=lambda _: None,
                                             is_cancelled=lambda: False, prompt_id=identity)
                self.assertEqual(completion.text, 'ok')
                self.assertEqual(opened.call_count, 2)
                index.assert_not_called()


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for patcher in (mock.patch.object(cloud, '_runtime', None), mock.patch.object(prompt_index, '_cached', None),
                        mock.patch.dict(os.environ, {'LOOPCUT_PROMPT_INDEX': ''})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_override_cached_by_path_and_mtime_and_bad_file_is_loud(self):
        path = Path(self.folder.name) / 'index.json'
        index = deepcopy(built_index())
        path.write_text(json.dumps(index))
        read_text = Path.read_text
        with mock.patch.dict(os.environ, {'LOOPCUT_PROMPT_INDEX': str(path)}), \
                mock.patch.object(Path, 'read_text', autospec=True, wraps=Path.read_text) as read:
            read.side_effect = read_text
            self.assertEqual(cloud.hosted_index(), index)
            self.assertEqual(cloud.hosted_index(), index)
            self.assertEqual(read.call_count, 1)
            stamp = path.stat().st_mtime_ns
            index['prompts']['version'] = 'changed'
            path.write_text(json.dumps(index))
            os.utime(path, ns=(stamp + 1, stamp + 1))
            self.assertEqual(cloud.hosted_index()['prompts']['version'], 'changed')
            self.assertEqual(read.call_count, 2)
            path.write_text('{broken')
            for _ in range(2):
                with self.assertRaisesRegex(prompt_index.IndexError, 'LOOPCUT_PROMPT_INDEX'):
                    cloud.hosted_index()
            self.assertEqual(read.call_count, 3, 'bad files are also read once per mtime')
            second = path.with_name('second.json')
            index['prompts']['version'] = 'new-path'
            second.write_text(json.dumps(index))
            os.utime(second, ns=(path.stat().st_mtime_ns, path.stat().st_mtime_ns))
            os.environ['LOOPCUT_PROMPT_INDEX'] = str(second)
            self.assertEqual(cloud.hosted_index()['prompts']['version'], 'new-path')
            self.assertEqual(read.call_count, 4)
        with mock.patch.dict(os.environ, {'LOOPCUT_PROMPT_INDEX': str(path) + '.missing'}):
            with self.assertRaisesRegex(prompt_index.IndexError, 'LOOPCUT_PROMPT_INDEX'):
                cloud.hosted_index()

    def test_old_manifests_null_and_defensive_types(self):
        self.assertNotIn('prompts', cloud._manifest(manifest()))
        self.assertIsNone(cloud._manifest(manifest(prompts=None))['prompts'])
        for update in ({'chars': True}, {'chars': -1}, {'order': '1'}, {'everyday': 'glass'},
                       {'roles': [1]}, {'words': '['}, {'swapped_chars': {'swap': False}}):
            index = deepcopy(built_index())
            index['skills'][0].update(update)
            with self.subTest(update=update), self.assertRaises(prompt_index.IndexError):
                prompt_index.validate(index)

    def test_missing_index_is_optional_for_unloaded_and_old_manifests(self):
        runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test'), self.folder.name, signed_in=True)
        for value in (None, manifest(), manifest(prompts=None)):
            with self.subTest(manifest=value):
                cloud._runtime = runtime if value is not None else None
                if value is not None:
                    runtime.manifest = value
                self.assertIsNone(cloud.hosted_index())
                self.assertFalse(cloud.prompt_index_available())
                with self.assertRaisesRegex(prompt_index.IndexError, 'No Loopcut prompt index'):
                    cloud.hosted_index(required=True)

    def test_configure_uses_cached_index_without_refreshing(self):
        runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test'), self.folder.name, signed_in=True)
        cloud._runtime = runtime
        roles = config.Roles(cfg(), cfg(), orchestrate=True, router=cfg())
        for indexed in (False, True):
            runtime.manifest = manifest(**built_index()) if indexed else manifest()
            runtime.prompts_stale = True
            for workspace in (False, True):
                with self.subTest(indexed=indexed, workspace=workspace), \
                        mock.patch.object(config, 'codex_app', return_value=None), \
                        mock.patch.object(config, 'load_roles', return_value=dataclasses.replace(roles, workspace=workspace)), \
                        mock.patch.object(agent, '_keep_mode', side_effect=lambda value: value), \
                        mock.patch.object(runtime, 'refresh', side_effect=AssertionError('network on main')):
                    turn, session = agent.Turn(), state.new_session()
                    agent._configure(turn, session)
                    expected = agent.ORCH_SPEC if indexed else (agent.WORKSPACE_SPEC if workspace else agent.SINGLE_SPEC)
                    self.assertIs(turn.spec, expected)
                    if not indexed:
                        self.assertIsNone(turn.roles)
                        if not workspace:
                            self.assertIsNone(turn.spec.on_turn_start)

    def test_first_hosted_turn_without_runtime_uses_open_single_request(self):
        roles = config.Roles(cfg(), cfg(), orchestrate=True, router=cfg())
        session, turn = state.new_session(), agent.Turn()
        session['messages'] = [{'role': 'user', 'content': 'hello'}]
        with mock.patch.object(config, 'codex_app', return_value=None), \
                mock.patch.object(config, 'load_roles', return_value=roles), \
                mock.patch.object(agent, '_keep_mode', side_effect=lambda value: value), \
                mock.patch.object(agent, '_redraw'), \
                mock.patch.object(llm.urllib.request, 'urlopen', return_value=response()) as opened:
            agent._configure(turn, session)
            self.assertIs(turn.spec, agent.SINGLE_SPEC)
            self.assertIsNone(turn.roles)
            agent._request(session, turn, turn.config, turn.spec)
        request = opened.call_args.args[0]
        body = json.loads(request.data)
        self.assertIsNone(request.get_header('X-loopcut-prompt'))
        self.assertEqual(body['messages'][0]['content'], agent.SYSTEM_PROMPT)
        self.assertNotIn('delegate', {row['function']['name'] for row in body['tools']})

    def test_chatgpt_plan_completes_with_or_without_loopcut_account_or_index(self):
        from loopcut import chatgpt_responses
        settings = {'LOOPCUT_BASE_URL': config.CHATGPT_PLAN_BASE_URL, 'LOOPCUT_ORCHESTRATE': 'true'}
        for signed_in in (False, True):
            for indexed in (False, True):
                runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test' if signed_in else ''),
                                        self.folder.name, signed_in=signed_in)
                runtime.manifest = manifest(**built_index()) if indexed else manifest()
                cloud._runtime = runtime
                with self.subTest(signed_in=signed_in, indexed=indexed), \
                        mock.patch.object(config, '_getter', return_value=(None, lambda k, d='': settings.get(k, d))), \
                        mock.patch.object(agent, '_keep_mode', side_effect=lambda value: value), \
                        mock.patch.object(agent, '_workspace_turn'), mock.patch.object(agent, '_redraw'), \
                        mock.patch.object(agent.conversations, 'save'), \
                        mock.patch.object(cloud, 'hosted_index', side_effect=AssertionError('plan needs no index')), \
                        mock.patch.object(chatgpt_responses, 'stream', return_value=llm.Completion(text='ok')) as stream:
                    session, turn = state.new_session(), agent.Turn()
                    session['messages'] = [{'role': 'user', 'content': 'hello'}]
                    agent._configure(turn, session)
                    self.assertIs(turn.spec, agent.WORKSPACE_SPEC)
                    self.assertIsNone(turn.roles)
                    self.assertTrue(config.chatgpt_plan(turn.config.base_url))
                    agent.run_loop(session, turn, turn.spec)
                    self.assertEqual(turn.outcome, 'completed')
                    stream.assert_called_once()

    def test_available_from_override_and_disk_cache(self):
        runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test'), self.folder.name, signed_in=True)
        runtime.manifest = manifest(**built_index())
        runtime._save()
        cloud._runtime = cloud.Runtime(runtime.client, self.folder.name, signed_in=True)
        with mock.patch.object(cloud._runtime, 'refresh', side_effect=AssertionError('network on main')):
            self.assertTrue(cloud.prompt_index_available())
        cloud._runtime = None
        path = Path(self.folder.name) / 'override.json'
        path.write_text(json.dumps(built_index()))
        with mock.patch.dict(os.environ, {'LOOPCUT_PROMPT_INDEX': str(path)}):
            self.assertTrue(cloud.prompt_index_available())

    def test_response_version_refreshes_before_next_hosted_request(self):
        old = deepcopy(built_index())
        new = deepcopy(old)
        new['prompts']['version'] = 'new-version'
        transport = Transport(Reply(manifest(**new), headers={'etag': 'new'}))
        runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test', transport=transport), self.folder.name, signed_in=True)
        runtime.manifest = manifest(**old)
        # Worker-only metadata is never fetched, and this test is about the manifest refresh.
        runtime.manifest['flags']['premium_skills'] = False
        transport.replies[0] = Reply(manifest(**new, flags={'services': True, 'premium_skills': False}), headers={'etag': 'new'})
        cloud._runtime = runtime
        kwargs = dict(base_url=config.SERVICE_BASE_URL, api_key='test', model='fast', messages=[], tools=[],
                      reasoning_effort='low', on_text=lambda _: None, is_cancelled=lambda: False, prompt_id='single')
        with mock.patch.object(llm.urllib.request, 'urlopen', side_effect=[response('new-version'), response('new-version')]):
            completion = llm.stream_chat(**kwargs)
            self.assertEqual(completion.prompt_version, 'new-version')
            self.assertTrue(runtime.prompts_stale)
            self.assertEqual(transport.calls, [])
            session = state.new_session()
            session['messages'] = [{'role': 'user', 'content': 'hello'}]
            with mock.patch.object(agent, '_redraw'):
                agent._request(session, agent.Turn(), cfg(), dataclasses.replace(agent.SINGLE_SPEC, schemas=lambda: []))
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(transport.calls[0]['url'].endswith('/v1/manifest'))
        self.assertFalse(runtime.prompts_stale)
        cloud.hosted_index(required=True)
        self.assertEqual(len(transport.calls), 1)

    def test_version_notice_during_refresh_is_not_lost(self):
        def value(version):
            prompts = deepcopy(built_index()['prompts'])
            prompts['version'] = version
            return manifest(prompts=prompts, flags={'services': True, 'premium_skills': False})
        calls = []
        def transfer(request, *, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                cloud.note_prompt_version('latest')  # Another worker's response while GET is in flight.
                return Reply(value('middle'))
            return Reply(value('latest'))
        runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test', transport=transfer), self.folder.name, signed_in=True)
        runtime.manifest = value('old')
        cloud._runtime = runtime
        cloud.note_prompt_version('middle')
        self.assertEqual(cloud.hosted_index()['prompts']['version'], 'latest')
        self.assertEqual(len(calls), 2)
        self.assertFalse(runtime.prompts_stale)

    def test_stale_index_refresh_failure_keeps_cached_prompts(self):
        transport = Transport(llm.urllib.error.URLError('offline'))
        runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test', transport=transport), self.folder.name, signed_in=True)
        runtime.manifest = manifest(**built_index())
        cloud._runtime = runtime
        cloud.note_prompt_version('later')
        with mock.patch('builtins.print') as warning:
            index = cloud.hosted_index(required=True)
        self.assertEqual(index, built_index())
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(runtime.prompts_stale, 'a later request should try the refresh again')
        lines = [call.args[0] for call in warning.call_args_list]
        self.assertEqual(lines[0], 'Loopcut: GET request failed in transport: URLError')  # cloud_http's cause line
        self.assertEqual(len(lines), 2)
        self.assertIn('cached', lines[1])
        self.assertNotIn('\n', lines[1])

    def test_stale_index_refresh_failure_without_cache_is_optional_or_required(self):
        transport = Transport(llm.urllib.error.URLError('offline'), llm.urllib.error.URLError('offline'))
        runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test', transport=transport), self.folder.name, signed_in=True)
        cloud._runtime = runtime
        cloud.note_prompt_version('later')
        self.assertIsNone(cloud.hosted_index())
        with self.assertRaisesRegex(prompt_index.IndexError, 'No Loopcut prompt index'):
            cloud.hosted_index(required=True)

    def test_orchestrator_without_index_shows_clear_error(self):
        session = state.new_session()
        session['messages'] = [{'role': 'user', 'content': 'hello'}]
        turn = agent.Turn()
        turn.config = cfg()
        spec = dataclasses.replace(agent.ORCH_SPEC, save=lambda _: None, changes_card=False)
        with mock.patch.object(agent, '_redraw'), mock.patch.object(agent, '_route'), \
                mock.patch.object(llm.urllib.request, 'urlopen') as opened, mock.patch('traceback.print_exc') as traceback:
            agent.run_loop(session, turn, spec)
        self.assertEqual(session['turn_outcome'], 'error')
        self.assertEqual(session['items'], [state.item_error(
            'Loopcut could not load its service prompt index; try again in a moment')])
        opened.assert_not_called()
        traceback.assert_not_called()

    def test_missing_index_blocks_worker_and_router(self):
        session = state.new_session()
        session['messages'] = [{'role': 'user', 'content': 'hello'}]
        with mock.patch.object(llm.urllib.request, 'urlopen') as opened:
            with self.assertRaisesRegex(prompt_index.IndexError, 'No Loopcut prompt index'):
                agent._request(session, agent.Turn(), cfg(), workers.SPEC('modeling', save=lambda _: None))
            self.assertEqual(router.ask(cfg(), session['messages'][0], session, lambda: False), (None, {}))
        opened.assert_not_called()

    def test_missing_index_fails_spawn_before_any_scene_work_and_delegate_returns_error(self):
        turn = agent.Turn()
        turn.roles = config.Roles(orchestrator=cfg(), worker=cfg(), orchestrate=True)
        with mock.patch.object(waves, 'job_for') as job:
            with self.assertRaisesRegex(prompt_index.IndexError, 'No Loopcut prompt index'):
                waves.prepare({}, turn, {}, 0)
            job.assert_not_called()
        session = state.reset()
        self.addCleanup(state.reset)
        session['turn'] = turn
        with mock.patch.object(orchestrator, 'live_facts', return_value={}), \
                mock.patch.object(orchestrator, 'repair_wave', side_effect=lambda p, *args: (p, [])), \
                mock.patch.object(orchestrator, 'validate_wave', return_value=[]), \
                mock.patch.object(orchestrator, 'layout', return_value=([], [], [], [])), \
                mock.patch.object(orchestrator, '_snapshot', return_value=None):
            result = orchestrator.delegate({'shared': {}, 'workers': []})
        self.assertFalse(result.ok)
        self.assertIn('No Loopcut prompt index', result.text)
        self.assertIsNone(result.then)

    def test_delegate_refresh_runs_before_main_thread_dispatch(self):
        events = []
        result = types.SimpleNamespace(ok=True)
        with mock.patch.object(cloud, 'hosted_index', side_effect=lambda: events.append('index')), \
                mock.patch.object(agent, '_on_main', side_effect=lambda *args, **kwargs: events.append('main') or result):
            self.assertIs(agent._run_tool_on_main('delegate', '{}'), result)
        self.assertEqual(events, ['index', 'main'])
        with mock.patch.object(cloud, 'hosted_index', side_effect=prompt_index.IndexError('stale index')), \
                mock.patch.object(agent, '_on_main') as dispatch:
            result = agent._run_tool_on_main('delegate', '{}')
        self.assertFalse(result.ok)
        self.assertIn('stale index', result.text)
        dispatch.assert_not_called()

    def test_empty_skills_index_keeps_shot_plumbing_only(self):
        index = {'prompts': built_index()['prompts'], 'skills': []}
        brief = {'id': 'shot', 'role': 'lighting', 'deliverable': 'Shot', 'prefix': 'Shot', 'owns': [],
                 'requirements': [], 'steps': 1, 'aspects': ['camera']}
        with mock.patch.object(cloud, 'hosted_index', return_value=index):
            self.assertEqual(workers.role_notes('lighting', brief), workers.SHOT_NOTE)
            text = workers.opening(brief, {}, [], 'scene')
            self.assertEqual(text.split('<role_notes>', 1)[1].split('</role_notes>', 1)[0],
                             f'\n{workers.SHOT_NOTE}\n')
            self.assertNotIn('<loopcut-skills', text)
            brief['aspects'] = ['lighting']
            self.assertEqual(workers.role_notes('lighting', brief), '')
            self.assertNotIn('<role_notes>', workers.opening(brief, {}, [], 'scene'))


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.index = deepcopy(built_index()['skills'])
        patcher = mock.patch.object(config, 'skills_dir', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_selection_needs_no_builtin_bodies(self):
        with mock.patch.object(skills.loader, 'builtins', return_value=()):
            selected = skills.skills_for('modeling', {'style': 'painterly toon flat shaded'}, skill_index=self.index)
        self.assertIn('craft/modeling/painterly', selected_ids(selected))
        self.assertTrue(all(i in {e['id'] for e in self.index} for i in selected_ids(selected)))

    def test_local_override_splits_placeholder_and_keeps_position(self):
        core = next(e for e in self.index if e.get('part') == 'core' and e.get('craft') == 'camera')
        with tempfile.TemporaryDirectory() as directory:
            fields = {k: v for k, v in core.items() if k not in ('chars', 'sha256', 'swapped_chars')}
            fields['version'] = 2
            Path(directory, 'override.md').write_text(markdown('local camera', **fields))
            selected = skills.skills_for('camera', {}, extra_dir=directory, skill_index=self.index)
            self.assertTrue(selected.startswith('local camera\n<loopcut-skills'))
            self.assertNotIn('ids="' + core['id'], selected)
            self.assertIn('craft/camera/assets', selected_ids(selected))

    def test_local_topic_splits_between_hosted_ids(self):
        topic = next(e for e in self.index if e.get('part') == 'topic' and e.get('craft') == 'camera')
        with tempfile.TemporaryDirectory() as directory:
            fields = {k: v for k, v in topic.items() if k not in ('chars', 'sha256')}
            fields.update(words='localtopic', leads='localtopic', version=2)
            Path(directory, 'topic.md').write_text(markdown('inline topic', **fields))
            selected = skills.skills_for('camera', 'localtopic', extra_dir=directory, skill_index=self.index)
            self.assertIn('/>\ninline topic\n<loopcut-skills', selected)

    def test_swap_must_be_on_same_side_and_local_pair_replaces(self):
        core = next(e for e in self.index if e.get('part') == 'core' and e.get('craft') == 'modeling')
        swap = next(e for e in self.index if e.get('part') == 'swap' and e.get('craft') == 'modeling')
        with tempfile.TemporaryDirectory() as directory:
            fields = {**{k: v for k, v in swap.items() if k not in ('chars', 'sha256')}, 'words': 'xyzswap', 'replaces': 'before', 'version': 2}
            Path(directory, 'swap.md').write_text(markdown('after', **fields))
            with mock.patch('builtins.print') as warning:
                selected = skills.skills_for('modeling', 'xyzswap', extra_dir=directory, skill_index=self.index)
            warning.assert_called_once()
            self.assertNotIn(swap['id'], selected)
            Path(directory, 'core.md').write_text(markdown('before before', **{**{k: v for k, v in core.items() if k not in ('chars', 'sha256', 'swapped_chars')}, 'version': 2}))
            with mock.patch('builtins.print') as warning:
                selected = skills.skills_for('modeling', 'xyzswap', extra_dir=directory, skill_index=self.index)
            warning.assert_not_called()
            self.assertTrue(selected.startswith('after after\n<loopcut-skills'))
            Path(directory, 'swap.md').unlink()
            # The served swap matches, but cannot edit the locally overridden core.
            index = deepcopy(self.index)
            next(e for e in index if e['id'] == swap['id'])['words'] = 'xyzswap'
            with mock.patch('builtins.print') as warning:
                selected = skills.skills_for('modeling', 'xyzswap', extra_dir=directory, skill_index=index)
            warning.assert_called_once()
            self.assertTrue(selected.startswith('before before\n'))

    def test_served_extras_require_matching_words_or_available_tools(self):
        extra = dict(id='extras/test', chars=5, roles=['worker'], requires_tools=[], tier='paid', kind='assist')
        def selected(entry, **kwargs):
            return skills.skills_for('camera', 'needle', skill_index=[*self.index, entry], **kwargs)
        self.assertNotIn(extra['id'], selected(extra))
        self.assertTrue(selected({**extra, 'words': 'needle'}).endswith('\n\n<loopcut-skills ids="extras/test"/>'))
        self.assertNotIn(extra['id'], selected({**extra, 'words': 'absent'}))
        entry = {**extra, 'requires_tools': ['a', 'b']}
        self.assertNotIn(extra['id'], selected(entry, available_tools=['a']))
        self.assertIn(extra['id'], selected(entry, available_tools=['a', 'b']))
        self.assertNotIn(extra['id'], selected({**extra, 'words': 'needle', 'kind': 'gen'}, no_generation=True))
        self.assertEqual(skills.skills_for('unknown', 'needle', skill_index=[*self.index, {**extra, 'words': 'needle'}]), '')

    def test_budget_counts_expansion_with_swaps_and_unicode(self):
        index = built_index()
        selected = skills.skills_for('modeling', 'painterly toon flat shaded', skill_index=self.index)
        for content in (selected + ' 🌻', [{'type': 'text', 'text': selected + ' 🌻'}]):
            message = {'role': 'user', 'content': content}
            expected = expanded_chars(selected) + len(' 🌻')
            self.assertEqual(context._size(message, index), (expected, 0))
            self.assertEqual(context.estimate_tokens([message], fixed=0, skill_index=index), round(expected / context.CHARS_PER_TOKEN))
        self.assertEqual(context._size({'role': 'tool', 'content': selected}, index), (len(selected), 0))
        fixed = context.fixed_tokens('\n\ntail', [], prompt_id='worker', index=index)
        self.assertEqual(fixed, round((len('\n\ntail') + 2 + index['prompts']['items']['worker']['chars']) / context.CHARS_PER_TOKEN))

    def test_saved_placeholder_budget_survives_empty_index_and_is_not_sent(self):
        index = built_index()
        selected = skills.skills_for('modeling', 'painterly', skill_index=self.index)
        session = state.new_session()
        session['messages'] = [{'role': 'user', 'content': selected}]
        context.prepare(session, cfg(), lambda: False, fixed=0, skill_index=index)
        saved = json.loads(json.dumps(session['messages'][0]))
        self.assertIn(context.SKILL_CHARS, saved)
        empty = {'prompts': index['prompts'], 'skills': []}
        self.assertEqual(context._size(saved, empty), (expanded_chars(selected), 0))
        self.assertEqual(agent.conversations.wire_messages(session['id'], [saved]),
                         [{'role': 'user', 'content': selected}])
        self.assertIn('<loopcut-skills', saved['content'])

    def test_request_match_topic_uses_notes_and_stays_after_baseline_topics(self):
        # An extra craft skill is both a topic candidate and an appended request note, as before.
        raw = markdown('extra', id='extras/cloth', roles=['worker'], craft='modeling', words='cloth', order=0)
        record = skills.loader.parse_skill(raw)
        entry = {**dict(record.front_matter), 'chars': len(record.body)}
        actual = skills.skills_for('modeling', {'style': 'cloth'}, 'cloth', skill_index=[*self.index, entry])
        first, extra = actual.split('\n\n')
        self.assertEqual(selected_ids(first), ['craft/modeling/core', 'extras/cloth', 'craft/modeling/organic', 'craft/modeling/assets'])
        self.assertEqual(selected_ids(extra), ['extras/cloth'])
        notes_only = skills.skills_for('modeling', {}, 'cloth', skill_index=[*self.index, entry])
        self.assertIn('extras/cloth', notes_only.split('\n\n')[0])


class FetchTest(unittest.TestCase):
    def test_worker_only_and_paid_free_skipped_and_plan_required_is_nonfatal(self):
        records = []
        for identity, roles, tier in [('worker', ['worker'], 'free'), ('paid', ['workspace'], 'paid'),
                                      ('denied', ['single'], 'free'), ('allowed', ['single'], 'free')]:
            raw = markdown(identity, id='tests/' + identity, roles=roles, tier=tier, words='test')
            skill = skills.loader.parse_skill(raw)
            records.append(({**dict(skill.front_matter), 'sha256': skill.sha256, 'chars': len(skill.body)},
                            {'text': raw, 'id': skill.id, 'version': 1, 'sha256': skill.sha256}))
        value = manifest(skills=[e for e, _ in records])
        value['account']['plan'] = 'free'
        transport = Transport(Reply(value), cloud_http.CloudError('plan required', 'plan_required'), Reply(records[-1][1]))
        with tempfile.TemporaryDirectory() as directory, mock.patch('builtins.print') as warning:
            runtime = cloud.Runtime(cloud_http.Client(api_key='lc_test', transport=transport), directory, signed_in=True)
            self.assertTrue(runtime.refresh())
            self.assertEqual([s['front_matter']['id'] for s in runtime.server_skills()], ['tests/allowed'])
        warning.assert_not_called()
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(transport.calls[1]['url'].endswith('/tests/denied?version=1'))
        self.assertTrue(transport.calls[2]['url'].endswith('/tests/allowed?version=1'))


class ContractMockTest(unittest.TestCase):
    def test_hosted_requests_are_assembled_by_006_mock(self):
        from mock_service import MockTransport
        import platform_contract
        source = platform_contract.require('mock', 'server.py')
        contract = source.parents[1]
        if '/mock/upstream' not in source.read_text() or '/v1/chat/completions' not in source.read_text():
            self.skipTest('Contract mock does not yet implement proposal 006 chat/upstream routes')
        transport = MockTransport()
        client = cloud_http.Client(base_url='http://127.0.0.1:9999', api_key='lc_test', transport=transport)
        index = client.request('GET', '/v1/manifest')
        with mock.patch.object(cloud, 'hosted_index', return_value=index), \
                mock.patch.dict(os.environ, {'LOOPCUT_SERVICE_URLS': 'http://127.0.0.1:9999/v1'}), \
                mock.patch.object(llm.urllib.request, 'urlopen', side_effect=transport):
            for identity in ('single', 'worker', 'router'):
                user = {'role': 'user', 'content': 'test'}
                expected_user = deepcopy(user)
                tail = '\n\nmock tail' if identity == 'single' else ''
                if identity == 'worker':
                    ids = ['examples/craft/core', 'examples/craft/swap-joined', 'examples/craft/chairs', 'examples/craft/assets']
                    user['content'] = [{'type': 'text', 'text': '<role_notes>\n<loopcut-skills ids="' + ','.join(ids) + '"/>\n</role_notes>'}]
                    records = [skills.loader.read_skill(contract / 'mock/skills' / (i + '.md')) for i in ids]
                    core = records[0].body.replace(records[1].front_matter['replaces'], records[1].body)
                    expected_user['content'] = [{'type': 'text', 'text': '<role_notes>\n' + '\n'.join(
                        [core, *(r.body for r in records[2:])]) + '\n</role_notes>'}]
                completion = llm.stream_chat(base_url='http://127.0.0.1:9999/v1', api_key='lc_test', model='fast',
                                messages=([{'role': 'system', 'content': tail}] if tail else []) + [user],
                                tools=[], reasoning_effort='low', on_text=lambda _: None,
                                is_cancelled=lambda: False, prompt_id=identity)
                self.assertEqual(completion.prompt_version, index['prompts']['version'])
                upstream = client.request('GET', '/mock/upstream')
                expected = (contract / 'mock/prompts' / (('base' if identity == 'single' else identity) + '.md')).read_text().removesuffix('\n')
                self.assertEqual(upstream['messages'], [{'role': 'system', 'content': expected + tail}, expected_user])



if __name__ == '__main__':
    unittest.main()
