"""C12: local/index search and import through the canonical mock, no network or Blender."""

import copy
import contextlib
import hashlib
import io
import json
import queue
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "addons_core"))
sys.path.insert(0, str(Path(__file__).parent))
sys.modules.setdefault("bpy", types.ModuleType("bpy"))
from loopcut import asset_common, asset_libraries as libraries, asset_report, asset_search, assets  # noqa: E402
from loopcut import cloud, cloud_http, cloud_jobs, indexed_assets, polyhaven, tools  # noqa: E402
from mock_service import MockTransport  # noqa: E402
import platform_contract  # noqa: E402


def fixture(name):
    return json.loads(platform_contract.require('fixtures', 'asset-search-report', name).read_text())


def inputs_for(report):
    return {"query": report["query"], **report["filters"]}


def zip_bytes(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


class IdentityTest(unittest.TestCase):
    def test_proposal_ambientcg_key_and_published_cache_identity(self):
        row = fixture('remote-libraries.json')['results'][0]
        remote = row['remote_library']
        for i, entry in enumerate(remote['entries'], 1):
            self.assertEqual(libraries.remote_key(remote['url'], entry['file'], entry['id_type'], entry['name']),
                             row['dedupe_keys'][i])
        self.assertEqual(libraries.remote_cache_dirname(remote['url']), '6f18014bec4cad69')
        self.assertNotEqual(libraries.remote_cache_dirname(remote['url'].rstrip('/')), '6f18014bec4cad69')

    def test_only_scheme_host_and_trailing_slash_normalize(self):
        expected = 'remote:https://assets.test:443/Lib/%2F/./A|Map/F.blend|MATERIAL|Oak%2f'
        self.assertEqual(libraries.remote_key('HTTPS://ASSETS.Test:443/Lib/%2F/./A/', 'Map/F.blend', 'MATERIAL', 'Oak%2f'), expected)
        self.assertNotEqual(libraries.remote_key('https://assets.test:443/lib/%2F/./A/', 'Map/F.blend', 'MATERIAL', 'Oak%2f'), expected)
        self.assertNotEqual(libraries.remote_key('https://assets.test:443/Lib/%2f/./A/', 'Map/F.blend', 'MATERIAL', 'Oak%2f'), expected)
        for args in [('https://x.test/?q=x', 'x.blend', 'MATERIAL', 'X'),
                     ('https://x.test/#f', 'x.blend', 'MATERIAL', 'X'),
                     ('https://x.test/', '../x.blend', 'MATERIAL', 'X'),
                     ('https://x.test/', 'x\\y.blend', 'MATERIAL', 'X'),
                     ('https://x.test/', 'x.blend', 'MATERIAL', 'X|Y'),
                     ('https://x.test/', 'x.blend', 'MATERIAL', 'X\nY')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                libraries.remote_key(*args)

    def test_chained_matches_union_all_keys_and_count_each_list_once(self):
        local = [{'entry': 'mine', 'library': 'L', 'server': None, 'keys': ['a'], 'cached': False},
                 {'entry': 'variant', 'library': 'L', 'server': None, 'keys': ['c'], 'cached': True}]
        remote = [{'entry': None, 'library': None, 'server': 'first', 'keys': ['a', 'b'], 'cached': False},
                  {'entry': None, 'library': None, 'server': 'bridge', 'keys': ['b', 'c'], 'cached': False}]
        rows = asset_search.fuse(local, remote)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['keys'], {'a', 'b', 'c'})
        self.assertEqual(rows[0]['entry'], 'mine')
        self.assertAlmostEqual(rows[0]['score'], 2 / 61)

    def test_equal_rank_prefers_ownership_and_cache_before_input_order(self):
        local = [{'entry': 'mine', 'library': 'L', 'server': None, 'keys': [], 'cached': False}]
        remote = [{'entry': None, 'library': None, 'server': 'theirs', 'keys': ['x'], 'cached': True}]
        self.assertEqual(asset_search.fuse(local, remote)[0]['entry'], 'mine')
        base = {'score': 1 / 61, 'entry': None, 'cached': False, 'order': 0}
        cached = {**base, 'cached': True, 'order': 1}
        self.assertEqual(sorted([base, cached], key=asset_search.sort_key), [cached, base])


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = MockTransport(live_tools=['asset_search'])
        self.client = cloud_http.Client(base_url='http://127.0.0.1:3000', api_key='lc_demo',
            transport=self.transport, client_headers={'x-loopcut-contract': '0.5', 'x-loopcut-client': 'test'})
        self.runtime = cloud.Runtime(self.client, self.root / 'runtime', signed_in=True)
        self.runtime.manifest = self.transport.state.manifest(True)
        self.rows = fixture('remote-libraries.json')['results']
        self.own = libraries.Library('Secret purchased kit', self.root / 'private-assets')
        self.own.path.mkdir()
        (self.own.path / 'private.blend').write_bytes(b'not a real blend')
        self.entry = libraries.Entry(self.own.name, 'private.blend', 'MATERIAL', 'My Foam Aluminum',
            tags=['private-tag'], description='confidential-description', license='my licence')
        remote = self.rows[0]['remote_library']
        self.remote = libraries.Library('My registered foam', self.root / 'registered-cache', remote['url'], 'LINK')
        self.remote.path.mkdir()
        (self.remote.path / '_asset-library-meta.json').write_text(json.dumps({'api_versions': {'v1': {'url': 'index.json'}}}))
        (self.remote.path / 'index.json').write_text(json.dumps({'pages': [{'url': 'page.json'}]}))
        listing = {'assets': [{'name': e['name'], 'id_type': e['id_type'], 'files': [e['file']],
                              'meta': {'tags': ['foam'], 'license': 'registered licence'}} for e in remote['entries']],
                   'files': [{'path': f['path'], 'url': f['url'], 'hash': f['hash'], 'size_in_bytes': f['size']}
                             for f in remote['entries'][0]['files']]}
        (self.remote.path / 'page.json').write_text(json.dumps(listing))
        self.patch(cloud, '_runtime', self.runtime)
        self.patch(cloud, 'sync_settings')
        self.patch(cloud_jobs, '_redraw')
        self.patch(libraries, 'libraries', return_value=[self.own, self.remote])
        self.patch(libraries, '_scan_blend', return_value=[self.entry])
        self.patch(libraries.bpy, 'app', types.SimpleNamespace(cachedir=str(self.root / 'blender-cache'),
            version=(5, 2, 0), version_string='5.2'), create=True)
        self.patch(asset_common, 'cache_root', return_value=self.root / 'assets')
        self.patch(asset_common, 'thumbnail', return_value=None)
        self.sheet = self.patch(asset_common, 'sheet', return_value=None)
        self.patch(asset_common.urllib.request, 'urlopen', side_effect=AssertionError('Unexpected network'))
        original_mkdtemp = tempfile.mkdtemp
        self.patch(cloud_jobs.tempfile, 'mkdtemp', side_effect=lambda **kw: original_mkdtemp(**{**kw, 'dir': self.root}))
        libraries._remote_lists.clear()
        libraries._local_scans.clear()
        indexed_assets._rows.clear()
        cloud_jobs._registry.clear()
        self.addCleanup(libraries._remote_lists.clear)
        self.addCleanup(libraries._local_scans.clear)
        self.addCleanup(indexed_assets._rows.clear)
        self.addCleanup(cloud_jobs._registry.clear)

    def patch(self, *args, **kwargs):
        p = patch.object(*args, **kwargs)
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def search(self, query='foam aluminum', **kw):
        result = assets.search_assets(query=query, type='material', **kw)
        return result.then(lambda: False, lambda fn: fn())

class MergeTest(FixtureTest):
    def test_canonical_job_merges_variants_uses_own_registration_and_never_sends_library_data(self):
        result = self.search()
        self.assertTrue(result.ok, result.text)
        lines = [line for line in result.text.splitlines() if line[:1].isdigit() and '. ' in line]
        self.assertEqual(len(lines), 3, result.text)  # own folder, merged foam, aluminum
        self.assertEqual(sum('+ ambientcg' in line for line in lines), 1)
        merged = next(line for line in lines if '+ ambientcg' in line)
        self.assertIn('source="library"', merged)
        self.assertIn('My registered foam::', merged)
        self.assertIn('CC0-1.0', merged)
        self.assertIn('keyword matching only', result.text)
        self.sheet.assert_called_once()
        self.assertEqual(len(self.sheet.call_args.args[0]), 3)
        self.assertTrue(self.sheet.call_args.kwargs['fit'], 'each thumbnail whole in its tile')
        jobs = [c['body'] for c in self.transport.calls if c['path'] == '/v1/jobs']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['input'], {'query': 'foam aluminum', 'kind': 'material', 'limit': 8})
        self.assertEqual(jobs[0]['files'], {})
        self.assertNotIn('bundle', jobs[0])
        for secret in [self.own.name, self.entry.file, self.entry.name, self.entry.tags[0], self.entry.description,
                       str(self.own.path), self.remote.name, self.remote.remote_url]:
            self.assertNotIn(secret, json.dumps(self.transport.calls))
        self.assertEqual(next(iter(self.transport.state.jobs.values()))['job']['credits']['charged'], 0)

    def test_registered_and_api_row_imports_through_users_entry(self):
        result = self.search()
        ref = libraries.remote_entries(self.remote)[0].ref
        self.assertIn(ref, result.text)
        # Remote contents are mocked, but the real lookup/fetch path retains the user's LINK method/cache.
        blend = self.remote.path / self.rows[0]['remote_library']['entries'][0]['file']
        blend.parent.mkdir(parents=True)
        blend.write_bytes(b'blend')
        with patch.object(libraries, 'download') as download, patch.object(libraries, 'load_asset', return_value=tools.ToolResult('loaded')) as load:
            result = assets.import_asset(source='library', id=ref)
        self.assertEqual(result.text, 'loaded')
        self.assertTrue(load.call_args.args[0]['link'])
        self.assertEqual(load.call_args.args[0]['blend'], str(blend))
        self.assertEqual(download.call_args.args[1], blend)

    def test_local_folder_with_same_name_never_merges(self):
        self.entry.name = self.rows[0]['remote_library']['entries'][0]['name']
        self.entry.tags = ['foam']
        result = self.search('foam')
        self.assertEqual(sum('source="library"' in line for line in result.text.splitlines()), 2)

    def test_polyhaven_credit_once_and_native_import_route(self):
        result = assets.search_assets(query='chair', type='model').then(lambda: False, lambda fn: fn())
        self.assertEqual(result.text.count('Powered by Poly Haven'), 1)
        self.assertIn('source="polyhaven", id="ArmChair_01"', result.text)
        with patch.object(polyhaven, 'import_polyhaven', autospec=True, return_value=tools.ToolResult('ok')) as imported:
            assets.import_asset(source='polyhaven', id='ArmChair_01', resolution='2k')
        imported.assert_called_once_with(asset_id='ArmChair_01', resolution='2k')

    def test_index_missing_signed_out_off_coming_soon_unavailable_private(self):
        original = copy.deepcopy(self.runtime.manifest)
        for mode, reason in [('signedout', 'needs a Loopcut sign-in'), ('missing', ''), ('off', 'services are off'),
                             ('coming', 'coming soon'), ('unavailable', 'disabled'), ('private', 'private mode'),
                             ('signin', 'needs a Loopcut sign-in')]:
            with self.subTest(mode=mode):
                self.runtime.manifest = copy.deepcopy(original)
                self.runtime.signed_in = mode != 'signedout'
                descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'asset_search')
                if mode == 'missing':
                    self.runtime.manifest['tools'] = []
                elif mode == 'off':
                    self.runtime.manifest['flags']['services'] = False
                elif mode == 'coming':
                    descriptor['status'] = 'coming_soon'
                elif mode in ('unavailable', 'private', 'signin'):
                    descriptor.update(available=False, unavailable_reason={'private': 'private_mode', 'signin': 'sign_in'}
                                      .get(mode, 'disabled'))
                before = len(self.transport.calls)
                result = self.search()
                self.assertTrue(result.ok)
                self.assertIn(self.entry.name, result.text)
                self.assertIn(reason, result.text)
                self.assertEqual(result.text.count('Loopcut index not searched:'), int(bool(reason)))
                if mode == 'missing':
                    self.assertNotIn('asset_search is missing', result.text)
                self.assertEqual(len(self.transport.calls), before)

    def test_signed_out_note_is_one_line_naming_what_still_works(self):
        self.runtime.signed_in = False
        result = self.search()
        notes = [line for line in result.text.splitlines() if 'sign-in' in line]
        self.assertEqual(notes, [asset_search.SIGN_IN_NOTE])
        for words in ('blenderkit', 'polyhaven', "user's libraries"):
            self.assertIn(words, notes[0])
        self.assertIn(self.entry.name, result.text, "local libraries are still searched")

    def test_free_search_survives_no_generation_and_zero_credits_without_approval(self):
        self.transport.state.balance = 0
        self.transport.state.settings['no_generation'] = True
        self.runtime.manifest = self.transport.state.manifest(True)
        self.runtime.policy['no_generation'] = True
        with patch('loopcut.cloud_account.confirm', side_effect=AssertionError('Search must be free')):
            result = self.search()
        self.assertNotIn('not searched', result.text)
        self.assertIn('physicallybased', result.text)

    def test_job_failure_and_timeout_fall_back_once(self):
        self.transport.state.fail_jobs = True
        result = self.search()
        self.assertIn(self.entry.name, result.text)
        self.assertIn('vendor failed', result.text)
        self.assertEqual(result.text.count('Loopcut index not searched:'), 1)
        self.transport.state.fail_jobs = False
        with patch.object(asset_search, 'SEARCH_SECONDS', 0):
            result = self.search()
        self.assertIn('timed out', result.text)
        self.assertIn(self.entry.name, result.text)

    def test_bad_rows_dropped_and_bad_envelope_does_not_break_local(self):
        original = self.transport.state.asset_search_report
        def report(inputs):
            value = original(inputs)
            value['results'][1]['import']['id'] = 'wrong'
            return value
        self.patch(self.transport.state, 'asset_search_report', side_effect=report)
        result = self.search()
        self.assertIn('Dropped 1 invalid index result', result.text)
        self.assertNotIn('source="physicallybased"', result.text)
        with patch.object(self.transport.state, 'asset_search_report', return_value={'schema': 'no'}):
            result = self.search()
        self.assertIn(self.entry.name, result.text)
        self.assertIn('invalid or unreadable report', result.text)

    def test_empty_index_and_no_libraries_are_successful(self):
        with patch.object(libraries, 'libraries', return_value=[]):
            result = self.search('unmatchable')
        self.assertTrue(result.ok)
        self.assertIn('0 asset(s)', result.text)
        self.assertIn('Powered by Poly Haven', result.text)

    def test_native_ids_survive_other_searches_and_repeat_ids(self):
        self.search()
        self.search('foam')
        self.search('aluminum')
        self.assertIn(('ambientcg', 'AcousticFoam001'), indexed_assets._rows)
        self.assertIn(('physicallybased', 'Aluminum'), indexed_assets._rows)
        self.assertEqual(assets.describe('import_asset', {'source': 'physicallybased', 'id': 'Aluminum'})[0],
                         'Import physicallybased asset Aluminum')
        with self.assertRaisesRegex(tools.ToolError, 'Unknown physicallybased asset id'):
            assets.fetch_asset(source='physicallybased', id='invented')

    def test_network_is_deferred_and_sheet_runs_on_main(self):
        requests, sheets = [], []
        self.sheet.side_effect = lambda *a, **k: sheets.append(threading.current_thread())
        self.transport.before_request = lambda call: requests.append(threading.current_thread())
        prepared = assets.search_assets(query='foam', type='material')
        self.assertEqual(requests, [])
        work = queue.Queue()
        result = Future()
        def on_main(fn):
            future = Future()
            work.put((fn, future))
            return future.result(timeout=5)
        def run():
            try:
                result.set_result(prepared.then(lambda: False, on_main))
            except Exception as ex:
                result.set_exception(ex)
            finally:
                work.put(None)
        worker = threading.Thread(target=run)
        worker.start()
        while True:
            call = work.get(timeout=5)
            if call is None:
                break
            fn, future = call
            try:
                future.set_result(fn())
            except Exception as ex:
                future.set_exception(ex)
        worker.join(timeout=5)
        self.assertTrue(result.result().ok)
        self.assertTrue(requests)
        self.assertTrue(all(t is worker for t in requests))
        self.assertEqual(sheets, [threading.main_thread()])

    def test_omitted_source_and_type_search_all_kinds_and_label_results(self):
        schema = assets.SCHEMAS[0]['function']['parameters']
        self.assertEqual(schema['required'], ['query'])
        self.assertEqual(schema['properties']['type']['type'], 'string')
        result = assets.search_assets(query='chair wood studio foam').then(lambda: False, lambda fn: fn())
        self.assertIn('ArmChair_01', result.text)
        self.assertEqual(next(c['body']['input']['kind'] for c in self.transport.calls if c['path'] == '/v1/jobs'), 'any')
        self.assertNotIn('Loopcut index searched for models', result.text)
        for kind in ('model', 'material', 'hdri'):
            self.assertIn(f'; kind: {kind};', result.text)
        local_line = next(line for line in result.text.splitlines() if self.entry.name in line)
        self.assertIn('; kind: material;', local_line)
        self.assertEqual(result.text.count('Powered by Poly Haven'), 1)
        for args in ({'query': ''}, {'query': 'x', 'type': []}, {'query': 'x', 'limit': 'bad'}):
            self.assertFalse(tools.execute('search_assets', json.dumps(args)).ok)

    def test_typed_search_sends_one_kind_including_blender_id_types(self):
        for value, kind in [('model', 'model'), ('material', 'material'), ('hdri', 'hdri'),
                            *asset_search.INDEX_TYPES.items()]:
            with self.subTest(type=value):
                result = assets.search_assets(query='chair wood studio', type=value).then(lambda: False, lambda fn: fn())
                jobs = [c['body'] for c in self.transport.calls if c['path'] == '/v1/jobs']
                self.assertEqual(jobs[-1]['input']['kind'], kind)
                self.assertIn(f'; kind: {kind};', result.text)
                self.assertNotIn('not searched', result.text)

    def test_index_list_round_trip_and_model_only_polycount_filter(self):
        for kind, expected in [('any', {'material', 'hdri'}),
                               (['hdri', 'material', 'model'], {'material', 'hdri'}),
                               (['hdri', 'material'], {'material', 'hdri'}), ('material', {'material'})]:
            with self.subTest(kind=kind):
                inputs = {'query': 'chair wood studio', 'kind': kind, 'max_polycount': 1}
                rows, _, notes = asset_search._index(inputs, lambda: False, lambda fn: fn())
                self.assertEqual({r['kind'] for r in rows}, expected, notes)
                self.assertFalse(any('Dropped' in n or 'not searched' in n for n in notes), notes)
                jobs = [c['body'] for c in self.transport.calls if c['path'] == '/v1/jobs']
                self.assertEqual(jobs[-1]['input'], inputs)

    def test_semantic_index_match_still_uses_owned_remote_registration(self):
        result = self.search('sponge')  # Server tag matches; the local listing has no such keyword.
        self.assertIn('My registered foam + ambientcg', result.text)
        self.assertIn('source="library"', result.text)
        self.assertNotIn('source="ambientcg"', result.text)

    def test_corrupt_downloaded_report_keeps_local_results(self):
        def corrupt(call):
            if call['path'].startswith('/mock-files/'):
                _, mime = self.transport.state.output_files[call['path']]
                self.transport.state.output_files[call['path']] = (b'corrupt report', mime)
        self.transport.before_request = corrupt
        result = self.search()
        self.assertIn(self.entry.name, result.text)
        self.assertIn('hash mismatch', result.text)
        self.assertEqual(result.text.count('Loopcut index not searched:'), 1)

    def test_manifest_with_paid_or_file_inputs_is_not_run(self):
        descriptor = next(d for d in self.runtime.manifest['tools'] if d['name'] == 'asset_search')
        descriptor['cost']['credits_estimate'] = 1
        result = self.search()
        self.assertIn('free index search is unavailable', result.text)
        self.assertFalse(self.transport.calls)

    def test_explicit_search_sources_keep_their_dispatch(self):
        for source, (module, search, _, _) in assets.SOURCES.items():
            with self.subTest(source=source), patch.object(module, search, autospec=True, return_value=tools.ToolResult('native')) as call:
                args = {'query': 'wood'}
                if source in ('blenderkit', 'polyhaven'):
                    args['type'] = 'model'
                result = assets.search_assets(source=source, **args)
                self.assertEqual(result.text, 'native')
                expected = {**args, 'type': 'models'} if source == 'polyhaven' else args
                call.assert_called_once_with(**expected)


class ReportTest(unittest.TestCase):
    def test_supported_contract_fixtures(self):
        for name in ['empty.json', 'remote-libraries.json', 'remote-camera.json', 'polyhaven-models.json',
                     'mixed-materials.json', 'fifty-results.json', 'keyword-fallback.json',
                     'any-kind.json', 'kind-list.json']:
            report = fixture(name)
            with self.subTest(name=name):
                rows, _, notes = asset_report.validate_report(report, inputs_for(report))
                self.assertEqual(len(rows), len(report['results']), notes)

    def test_kind_list_is_canonical_and_request_form_is_preserved(self):
        report = fixture('kind-list.json')
        inputs = inputs_for(report)
        inputs['kind'] = ['material', 'model']
        rows, _, _ = asset_report.validate_report(report, inputs)
        self.assertEqual(rows, report['results'])
        for value in (['material', 'model'], ['model', 'material', 'material'], [], ['any'], 'any', 'model'):
            with self.subTest(kind=value), self.assertRaises(tools.ToolError):
                asset_report.validate_report({**report, 'filters': {**report['filters'], 'kind': value}}, inputs)

    def test_invalid_request_kinds_cannot_validate_matching_report_filters(self):
        report = fixture('empty.json')
        for value in ([], ['any'], ['model', 'model'], ['material', 'unknown'], [None], 'unknown', None):
            with self.subTest(kind=value), self.assertRaises(tools.ToolError):
                report['filters']['kind'] = value
                asset_report.validate_report(report, inputs_for(report))

    def test_unrequested_or_unknown_row_kinds_are_dropped(self):
        for requested, wrong in [('model', 'material'), (['model', 'hdri'], 'material'), ('any', 'unknown')]:
            with self.subTest(kind=requested):
                report = fixture('polyhaven-models.json')
                report['filters']['kind'] = requested
                report['results'][0]['kind'] = wrong
                rows, _, notes = asset_report.validate_report(report, inputs_for(report))
                self.assertEqual(len(rows), len(report['results']) - 1)
                self.assertIn('Dropped 1 invalid index result(s).', notes)

    def test_default_model_and_single_kind_reports_remain_compatible(self):
        report = fixture('polyhaven-models.json')
        inputs = inputs_for(report)
        inputs.pop('kind')
        self.assertEqual(asset_report.validate_report(report, inputs)[0], report['results'])
        for kind in asset_report.KINDS:
            with self.subTest(kind=kind):
                report = fixture('empty.json')
                report['filters']['kind'] = kind
                self.assertEqual(asset_report.validate_report(report, inputs_for(report))[0], [])

    def test_polycount_resolution_keeps_limit_only_when_model_is_requested(self):
        for kind in ('material', ['hdri', 'texture'], 'model', ['model', 'material'], 'any'):
            with self.subTest(kind=kind):
                report = fixture('empty.json')
                expected = 100 if kind == 'any' or 'model' in kind else None
                report['filters'].update(kind=kind, max_polycount=expected)
                inputs = {**inputs_for(report), 'max_polycount': 100}
                self.assertEqual(asset_report.validate_report(report, inputs)[0], [])
                report['filters']['max_polycount'] = None if expected else 100
                with self.assertRaises(tools.ToolError):
                    asset_report.validate_report(report, inputs)

    def test_row_validation_checks_id_keys_license_https_and_file_hash(self):
        original = fixture('remote-libraries.json')
        changes = [lambda r: r.update(id='ambientcg:wrong'), lambda r: r['dedupe_keys'].__setitem__(0, 'ambientcg:wrong'),
                   lambda r: r.update(page_url='http://unsafe.test/'), lambda r: r.update(preview_url='https://u:p@x.test/a.jpg'),
                   lambda r: r['license'].update(id='unknown'), lambda r: r.pop('attribution'),
                   lambda r: r['remote_library']['entries'][0]['files'][0].update(hash='MD5:' + '0' * 32),
                   lambda r: r['remote_library']['entries'][0]['files'][0].update(url='http://unsafe.test/file'),
                   lambda r: r['remote_library']['entries'][0]['files'][0].update(path='../escape'),
                   lambda r: r['dedupe_keys'].pop(1), lambda r: r.update(score=float('nan')),
                   lambda r: r['remote_library'].update(entries=[None])]
        for change in changes:
            report = copy.deepcopy(original)
            change(report['results'][0])
            rows, _, notes = asset_report.validate_report(report, inputs_for(report))
            self.assertEqual([r['source'] for r in rows], ['physicallybased'])
            self.assertIn('Dropped 1 invalid index result(s).', notes)

    def test_cc_by_requires_and_preserves_attribution_in_merged_row(self):
        report = fixture('remote-libraries.json')
        report['filters']['license'] = 'any'
        row = report['results'][0]
        row['license']['id'] = 'CC-BY-4.0'
        row['attribution'].update(required=True, text='Required author and licence link')
        rows, _, _ = asset_report.validate_report(report, inputs_for(report))
        local = libraries.Entry('Mine', 'm.blend', 'MATERIAL', 'Foam')
        line = asset_search._line(1, {'entry': local, 'server': rows[0]})
        self.assertIn('Required author and licence link', line)
        row['attribution']['required'] = False
        rows, _, notes = asset_report.validate_report(report, inputs_for(report))
        self.assertEqual(len(rows), 1)
        self.assertTrue(notes)


class ImportTest(FixtureTest):
    def asset_jobs(self):
        return [c['body'] for c in self.transport.calls
                if c['path'] == '/v1/jobs' and c['body']['tool'] == 'asset_search']

    def budget_catalog(self, tail_kind='texture'):
        # Exercise the canonical mock's actual 60 KB trim with twelve matching rows.
        # Large valid URLs force lower downloads out while the first ten still fit.
        payloads = {'material': ('zip', zip_bytes({'Budget_1K_Color.jpg': b'color'})),
                    'texture': ('png', b'texture'), 'hdri': ('exr', b'EXR image'),
                    'model': ('gltf', b'{"asset":{"version":"2.0"},"buffers":[]}')}
        self.transport.state.asset_catalog.clear()
        for i in range(12):
            row = copy.deepcopy(self.rows[0])
            row.pop('remote_library')
            kind = asset_report.KINDS[i % 4] if i < 10 else tail_kind
            fmt, data = payloads[kind]
            identity = f'Budget{i:02d}'
            row.update(id=f'ambientcg:{identity}', source_id=identity, title=identity, kind=kind,
                       dedupe_keys=[f'ambientcg:{identity}'], tags=['budget'], description='',
                       **{'import': {'source': 'ambientcg', 'id': identity}})
            row['downloads'] = [{'label': '1K', 'format': fmt, 'size': len(data),
                                 'url': f'https://assets.test/{identity}.{fmt}?download=' + 'x' * 4500}]
            self.transport.state.asset_catalog[row['id']] = row
        result = assets.search_assets(query='budget', limit=12).then(lambda: False, lambda fn: fn())
        self.assertNotIn('not searched', result.text)
        self.assertIn('trimmed', result.text)
        rows = list(indexed_assets._rows.values())
        self.assertEqual(len(rows), 12, result.text)
        self.assertTrue(all(r.get('downloads') for r in rows[:10]))
        self.assertFalse(rows[-1].get('downloads'))
        self.assertEqual(self.asset_jobs()[0]['input']['kind'], 'any')
        return rows, payloads

    def test_top_ten_mixed_ambient_downloads_import_without_second_search(self):
        rows, payloads = self.budget_catalog()
        loaders = {'model': '_import_model', 'material': '_import_texture',
                   'texture': '_import_texture', 'hdri': '_import_hdri'}
        for row in rows[:10]:
            with self.subTest(kind=row['kind'], id=row['id']):
                with patch.object(asset_common, 'open_url', return_value=io.BytesIO(payloads[row['kind']][1])) as download, \
                        patch.object(polyhaven, loaders[row['kind']], return_value=tools.ToolResult('loaded')) as load:
                    result = assets.import_asset(source='ambientcg', id=row['source_id'])
                self.assertEqual(result.text, 'loaded')
                download.assert_called_once()
                self.assertEqual(download.call_args.args[0], row['downloads'][0]['url'])
                load.assert_called_once()
                self.assertEqual(len(self.asset_jobs()), 1)

    def test_rank_eleven_plus_researches_with_its_own_kind(self):
        for kind in asset_report.KINDS:
            with self.subTest(kind=kind):
                self.transport.calls.clear()
                indexed_assets._rows.clear()
                rows, payloads = self.budget_catalog(tail_kind=kind)
                row = rows[-1]
                self.assertEqual(row['kind'], kind)
                with patch.object(asset_common, 'open_url', return_value=io.BytesIO(payloads[kind][1])):
                    fetched = assets.fetch_asset(source='ambientcg', id=row['source_id'])
                self.assertEqual(fetched['loader'], 'polyhaven')
                jobs = self.asset_jobs()
                self.assertEqual(len(jobs), 2)
                self.assertEqual(jobs[-1]['input'], {'query': row['source_id'], 'kind': kind,
                                                   'sources': ['ambientcg'], 'limit': 1})

    def test_typeless_physically_based_material_import_has_no_second_search(self):
        row, data = self.remote_asset()
        self.transport.state.asset_catalog[row['id']] = row
        indexed_assets._rows.clear()
        result = assets.search_assets(query='aluminum').then(lambda: False, lambda fn: fn())
        self.assertIn('source="physicallybased", id="Aluminum"', result.text)
        self.assertEqual(len(self.asset_jobs()), 1)
        self.assertEqual(self.asset_jobs()[0]['input']['kind'], 'any')
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)), \
                patch.object(libraries, 'load_asset', return_value=tools.ToolResult('loaded')) as load:
            self.assertEqual(assets.import_asset(source='physicallybased', id='Aluminum').text, 'loaded')
        self.assertEqual(load.call_args.args[0]['name'], 'Aluminum')
        self.assertEqual(len(self.asset_jobs()), 1)

    def remote_asset(self, source='physicallybased'):
        row = copy.deepcopy(next(r for r in self.rows if r['source'] == source))
        data = b'verified blender asset'
        for entry in row['remote_library']['entries']:
            for item in entry['files']:
                item.update(hash='SHA256:' + hashlib.sha256(data).hexdigest(), size=len(data))
        indexed_assets.remember(row)
        return row, data

    def test_remote_fetch_verifies_hash_and_load_split_is_json_only_without_registry(self):
        row, data = self.remote_asset()
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)):
            fetched = assets.fetch_asset(source='physicallybased', id='Aluminum')
        expected = self.root / 'blender-cache' / 'remote-assets' / libraries.remote_cache_dirname(
            row['remote_library']['url']) / row['remote_library']['entries'][0]['file']
        self.assertEqual(Path(fetched['blend']), expected)
        self.assertEqual(expected.read_bytes(), data)
        self.assertEqual(fetched['name'], 'Aluminum')
        self.assertEqual(fetched['source'], 'physicallybased')
        indexed_assets._rows.clear()  # A worker child has no session search registry.
        with patch.object(libraries, 'load_asset', return_value=tools.ToolResult('loaded')) as load:
            result = assets.load_fetched(json.loads(json.dumps(fetched)), apply_to=['Target'])
        self.assertEqual(result.text, 'loaded')
        load.assert_called_once_with(fetched, ['Target'])

    def test_a_models_listed_size_travels_to_the_load_and_other_kinds_send_none(self):
        row, data = self.remote_asset()
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)):
            fetched = assets.fetch_asset(source='physicallybased', id='Aluminum')
        self.assertIsNone(fetched['dimensions_m'])
        model = {**row, 'kind': 'model', 'dimensions_m': [0.2, 0.1, 0.15]}
        indexed_assets.remember(model)
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)):
            fetched = assets.fetch_asset(source='physicallybased', id='Aluminum')
        self.assertEqual(json.loads(json.dumps(fetched))['dimensions_m'], [0.2, 0.1, 0.15])

    def test_remote_wrong_hash_fails_before_load(self):
        row, _ = self.remote_asset()
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(b'wrong')):
            with self.assertRaisesRegex(tools.ToolError, 'published checksum'):
                assets.fetch_asset(source='physicallybased', id='Aluminum')
        library, entry = indexed_assets.remote_entry(row, self.root / 'blender-cache')
        self.assertFalse((library.path / entry.file).exists())

    def test_verified_cache_is_reused_and_bad_cached_bytes_are_replaced(self):
        row, data = self.remote_asset()
        library, entry = indexed_assets.remote_entry(row, self.root / 'blender-cache')
        path = library.path / entry.file
        path.parent.mkdir(parents=True)
        path.write_bytes(b'corrupt')
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)) as download:
            assets.fetch_asset(source='physicallybased', id='Aluminum')
            assets.fetch_asset(source='physicallybased', id='Aluminum')
        download.assert_called_once()
        self.assertTrue(asset_search._server_row(row, self.root / 'blender-cache')['cached'])

    def test_ambient_default_uses_remote_1k_and_higher_resolution_downloads_maps(self):
        self.remote_asset('ambientcg')
        with patch.object(libraries, 'fetch_entry', return_value={'blend': 'cache.blend'}) as fetch:
            result = assets.fetch_asset(source='ambientcg', id='AcousticFoam001')
        self.assertEqual(result['loader'], 'library')
        self.assertIn('1K-JPG', fetch.call_args.args[1].file)
        row = copy.deepcopy(self.rows[0])
        data = zip_bytes({'AcousticFoam001_2K-JPG_Color.jpg': b'color',
                          'AcousticFoam001_2K-JPG_NormalGL.jpg': b'normal',
                          'AcousticFoam001_2K-JPG_NormalDX.jpg': b'wrong convention',
                          'AcousticFoam001_2K-JPG_Roughness.jpg': b'rough',
                          'AcousticFoam001_2K-JPG_Displacement.jpg': b'disp'})
        row['downloads'][1]['size'] = len(data)
        indexed_assets.remember(row)
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)) as download:
            fetched = assets.fetch_asset(source='ambientcg', id='AcousticFoam001', resolution='2k')
        self.assertEqual(download.call_args.args[0], row['downloads'][1]['url'])
        self.assertEqual(set(fetched['maps']), {'base_color', 'normal', 'roughness', 'displacement'})
        self.assertEqual(Path(fetched['maps']['normal']).read_bytes(), b'normal')
        self.assertEqual(fetched['info']['provider'], 'ambientcg')
        with patch.object(polyhaven, '_import_texture', return_value=tools.ToolResult('material')) as build:
            result = assets.load_fetched(json.loads(json.dumps(fetched)), apply_to=['Mesh'])
        self.assertEqual(result.text, 'material')
        self.assertEqual(build.call_args.args[-1], ['Mesh'])
        self.assertEqual(build.call_args.args[1]['provider'], 'ambientcg')
        self.assertNotIn('Poly Haven', polyhaven._credit('AcousticFoam001', fetched['info']))

    def test_ambient_direct_1k_without_remote_and_4k_selection(self):
        row = copy.deepcopy(self.rows[0])
        row.pop('remote_library')
        data = zip_bytes({'Foam_1K_Color.jpg': b'color'})
        for item in row['downloads']:
            item['size'] = len(data)
        indexed_assets.remember(row)
        for resolution in ('1k', '4k'):
            with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)) as download:
                result = assets.fetch_asset(source='ambientcg', id=row['source_id'], resolution=resolution)
            self.assertIn(resolution.upper(), download.call_args.args[0])
            self.assertEqual(result['loader'], 'polyhaven')

    def test_trimmed_downloads_are_retrieved_by_public_id_only(self):
        row = copy.deepcopy(self.rows[0])
        row.pop('downloads')
        indexed_assets.remember(row)
        # The canonical mock retains full public download metadata for a one-result search.
        with patch.object(indexed_assets, '_ambient_download', return_value={'loader': 'polyhaven'}) as fetch:
            assets.fetch_asset(source='ambientcg', id=row['source_id'], resolution='4k')
        refreshed = fetch.call_args.args[0]
        self.assertTrue(refreshed['downloads'])
        jobs = [c['body'] for c in self.transport.calls if c['path'] == '/v1/jobs']
        self.assertEqual(jobs[0]['input'], {'query': row['source_id'], 'kind': 'material',
                                          'sources': ['ambientcg'], 'limit': 1})
        self.assertEqual(jobs[0]['files'], {})

    def test_ambient_hdri_and_model_downloads_use_existing_scene_loaders(self):
        for kind, fmt, data in [('hdri', 'exr', b'EXR image'),
                                ('model', 'gltf', b'{"asset":{"version":"2.0"},"buffers":[]}')]:
            with self.subTest(kind=kind):
                row = copy.deepcopy(self.rows[0])
                row.pop('remote_library')
                row['kind'] = kind
                row['downloads'] = [{'label': '1K', 'format': fmt, 'size': len(data),
                                     'url': 'https://assets.test/asset.' + fmt}]
                indexed_assets.remember(row)
                with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)):
                    fetched = assets.fetch_asset(source='ambientcg', id=row['source_id'])
                loader = '_import_hdri' if kind == 'hdri' else '_import_model'
                with patch.object(polyhaven, loader, return_value=tools.ToolResult('loaded')) as load:
                    self.assertEqual(assets.load_fetched(fetched).text, 'loaded')
                self.assertEqual(load.call_args.args[1]['provider'], 'ambientcg')
                if kind == 'model':
                    self.assertIsNone(load.call_args.args[3])
                    self.assertEqual(load.call_args.args[4], Path(fetched['gltf']))

    def test_ambient_gltf_cannot_reference_outside_its_cache(self):
        row = copy.deepcopy(self.rows[0])
        row.pop('remote_library')
        row['kind'] = 'model'
        data = b'{"asset":{"version":"2.0"},"buffers":[{"uri":"../../secret"}]}'
        row['downloads'] = [{'label': '1K', 'format': 'gltf', 'size': len(data),
                             'url': 'https://assets.test/unsafe.gltf'}]
        indexed_assets.remember(row)
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)):
            with self.assertRaisesRegex(tools.ToolError, 'unsafe glTF'):
                assets.fetch_asset(source='ambientcg', id=row['source_id'])

    def test_ambient_size_mismatch_is_clear(self):
        row = copy.deepcopy(self.rows[0])
        indexed_assets.remember(row)
        with patch.object(asset_common, 'open_url', return_value=io.BytesIO(b'bad')):
            with self.assertRaisesRegex(tools.ToolError, 'published size'):
                assets.fetch_asset(source='ambientcg', id=row['source_id'], resolution='2k')

    def test_zip_rejects_traversal_symlink_and_corrupt_bytes(self):
        for bad in ('../escape.jpg', '/absolute.jpg', 'a\\evil.jpg'):
            path = self.root / 'bad.zip'
            path.write_bytes(zip_bytes({bad: b'unsafe'}))
            with self.subTest(path=bad), self.assertRaisesRegex(tools.ToolError, 'unsafe file path'):
                indexed_assets._unpack(path, self.root / 'unpack')
        path.write_bytes(b'not a zip')
        with self.assertRaisesRegex(tools.ToolError, 'Could not unpack'):
            indexed_assets._unpack(path, self.root / 'unpack')
        with zipfile.ZipFile(path, 'w') as archive:
            link = zipfile.ZipInfo('link.jpg')
            link.external_attr = 0o120777 << 16
            archive.writestr(link, '/etc/passwd')
        with self.assertRaisesRegex(tools.ToolError, 'unsafe file path'):
            indexed_assets._unpack(path, self.root / 'unpack')

    def test_remote_reuses_published_method_and_checks_minimum_version(self):
        row, _ = self.remote_asset()
        row['remote_library']['entries'][0]['import_method'] = 'APPEND_REUSE'
        indexed_assets.remember(row)
        library, entry = indexed_assets.remote_entry(row, self.root / 'blender-cache')
        self.assertEqual(library.import_method, 'APPEND_REUSE')
        with patch.object(libraries, '_ensure_files', return_value=self.own.path / 'private.blend'):
            self.assertTrue(libraries.fetch_entry(library, entry)['reuse'])
        entry.min_version = '9.0'
        with self.assertRaisesRegex(tools.ToolError, 'needs Blender 9.0'):
            libraries.fetch_entry(library, entry)


    def test_loader_uses_exact_datablock_and_reuse_method(self):
        calls = []
        @contextlib.contextmanager
        def load(path, **options):
            calls.append((path, options))
            data_to = types.SimpleNamespace(node_groups=[])
            yield types.SimpleNamespace(node_groups=['Exact Name']), data_to
            self.assertEqual(data_to.node_groups, ['Exact Name'])
            data_to.node_groups = [types.SimpleNamespace(name='Exact Name')]
        with patch.object(libraries.bpy, 'data', types.SimpleNamespace(objects=[],
                libraries=types.SimpleNamespace(load=load)), create=True), \
             patch.object(libraries.bpy, 'context', types.SimpleNamespace(scene=object()), create=True):
            result = libraries.load_asset({'library': 'Provider', 'id_type': 'NODETREE', 'name': 'Exact Name',
                                          'blend': '/cache/a.blend', 'reuse': True})
        self.assertIn('Exact Name', result.text)
        self.assertEqual(calls, [('/cache/a.blend', {'link': False, 'reuse_local_id': True})])


class RawToolTest(FixtureTest):
    """loopcut_cloud__asset_search, the managed tool the model also gets, attaches its report as text. Ids
    taken from that report import like ids from search_assets (platform audit of 0.2.1: ambientCG and
    Physically Based ids were "Unknown", since only search_assets filled the registry)."""

    asset_jobs, remote_asset = ImportTest.asset_jobs, ImportTest.remote_asset

    def raw_search(self, query, kind):
        descriptor = next(d for d in self.runtime.descriptors() if d['name'] == 'asset_search')
        job = cloud_jobs.run({**descriptor, 'latency': 'seconds'}, {'query': query, 'kind': kind, 'limit': 10}, {},
                             project={'id': 'local', 'no_generation': False}, client=self.client)
        texts = []
        class Texts(list):
            def new(self, name):
                block = types.SimpleNamespace(name=name, write=texts.append)
                return block
        from loopcut import conversations, state
        with patch.object(sys.modules['bpy'], 'data', types.SimpleNamespace(texts=Texts()), create=True), \
                patch.object(cloud_jobs, 'stamp_provenance'), \
                patch.object(state, 'session', return_value=state.new_session()), patch.object(conversations, 'save'):
            summary = cloud_jobs.apply_result(descriptor, job)
        self.assertIn('Attached reports', summary)
        return json.loads(texts[0])

    def test_ids_from_the_raw_report_import_for_every_source(self):
        ambient, ambient_data = self.remote_asset('ambientcg')
        based, based_data = self.remote_asset('physicallybased')
        for row in (ambient, based):
            self.transport.state.asset_catalog[row['id']] = row
        indexed_assets._rows.clear()  # Nothing from search_assets: only the raw tool ran.
        report = self.raw_search('foam aluminum', 'material')
        found = {(r['import']['source'], r['import']['id']) for r in report['results']}
        for row, data in ((ambient, ambient_data), (based, based_data)):
            source, asset_id = row['import']['source'], row['import']['id']
            with self.subTest(source=source):
                self.assertIn((source, asset_id), found)
                with patch.object(asset_common, 'open_url', return_value=io.BytesIO(data)), \
                        patch.object(libraries, 'load_asset', return_value=tools.ToolResult('loaded')) as load:
                    result = assets.import_asset(source=source, id=asset_id)
                self.assertEqual(result.text.splitlines()[0], 'loaded', result.text)
                self.assertEqual(load.call_args.args[0]['name'], row['remote_library']['entries'][0]['name'])
        haven = self.raw_search('chair', 'model')
        slug = next(r['import']['id'] for r in haven['results'] if r['source'] == 'polyhaven')
        with patch.object(polyhaven, 'import_polyhaven', autospec=True, return_value=tools.ToolResult('ok')) as imported:
            self.assertEqual(assets.import_asset(source='polyhaven', id=slug).text, 'ok')
        imported.assert_called_once_with(asset_id=slug)
        self.assertEqual(len(self.asset_jobs()), 2, "no second search: the attached report was enough")

    def test_a_bad_raw_report_registers_nothing(self):
        rows = [copy.deepcopy(self.rows[0])]
        bad = copy.deepcopy(rows[0])
        bad['source'] = 'elsewhere'
        report = {'schema': asset_report.SCHEMA, 'filters': {'kind': 'material', 'license': 'cc0',
                  'sources': list(asset_report.PROVIDERS), 'limit': 10, 'max_polycount': None,
                  'min_resolution': None}, 'results': [bad, *rows]}
        self.assertEqual([r['id'] for r in asset_report.importable_rows(report)], [rows[0]['id']])
        for broken in (None, [], {'schema': 'other'}, {**report, 'filters': {'kind': 'boat'}}, {**report, 'results': {}}):
            self.assertEqual(asset_report.importable_rows(broken), [])


class RowsTest(unittest.TestCase):
    """What a combined-search row says an asset is, beside its thumbnail."""

    def server_row(self, **fields):
        row = copy.deepcopy(fixture('remote-libraries.json')['results'][0])
        row.update(fields)
        return row

    def test_index_rows_show_size_polycount_resolution_description_and_tags(self):
        row = self.server_row(kind='model', dimensions_m=[0.5, 0.4, 0.9], polycount=1234, max_resolution=4096,
                              description='Worn oak\nstool,\x07 hand-made ' + 'x' * 200,
                              tags=['stool', 'oak', 7, 'stool', 'rustic\tfarm'])
        line = asset_search._line(2, {'entry': None, 'server': row})
        self.assertNotIn('\n', line)
        self.assertNotIn('\x07', line)
        self.assertIn('; kind: model; 0.50x0.40x0.90 m; 1234 polys; 4k maps; Worn oak stool, hand-made x', line)
        self.assertIn('…; tags: stool, oak, rustic farm; import_asset(', line)
        flat = asset_search._line(1, {'entry': None, 'server': self.server_row(dimensions_m=[2, 2], polycount=None,
                                                                              max_resolution=512, description=None,
                                                                              tags=None)})
        self.assertIn('2.00x2.00 m tile; import_asset(', flat)
        odd = asset_search._line(1, {'entry': None, 'server': self.server_row(tags='stone', description=['x'])})
        self.assertNotIn('tags:', odd, "a tag string is not a list of letters")
        self.assertNotIn('maps', flat)

    def test_a_local_entry_brings_its_description_and_tags(self):
        entry = libraries.Entry(library='Mine', file='props.blend', id_type='OBJECT', name='Stool',
                                tags=['wood', 'stool'], description='My stool')
        line = asset_search._line(1, {'entry': entry, 'server': None}, 'kit: props (rows 1, 3)')
        self.assertIn('Stool (Mine); kind: model; My stool; tags: wood, stool; kit: props (rows 1, 3); import_asset(',
                      line)

    def test_kit_notes_name_rows_from_one_local_file_only(self):
        def entry(name, file, remote=False):
            return libraries.Entry(library='Mine', file=file, id_type='OBJECT', name=name,
                                   files=[{'path': file}] if remote else [])
        rows = [{'entry': entry('Chair', 'kit/cafe.blend'), 'server': None},
                {'entry': None, 'server': self.server_row()},
                {'entry': entry('Table', 'kit/cafe.blend'), 'server': None},
                {'entry': entry('Lamp', 'lamp.blend'), 'server': None},
                {'entry': entry('Foam N', 'foam.blend', True), 'server': None},
                {'entry': entry('Foam D', 'foam.blend', True), 'server': None}]
        with patch.object(asset_common, 'sheet', return_value=None), patch.object(indexed_assets, 'remember'):
            text = asset_search._present('cafe', rows, [None] * len(rows), [], []).text
        lines = text.splitlines()[1:7]
        self.assertIn('kit: cafe (rows 1, 3)', lines[0])
        self.assertIn('kit: cafe (rows 1, 3)', lines[2])
        for i in (1, 3, 4, 5):
            self.assertNotIn('kit:', lines[i], "one file of its own, an index row, or a remote file's variants")
        self.assertEqual(asset_common.kit_notes([None, ('A', 'x.blend'), ('B', 'x.blend')]), {},
                         "the same file name in two libraries is two kits")


class WorkerAndPanelTest(unittest.TestCase):
    def test_parent_search_resolves_deferred_result(self):
        from loopcut import waves
        wave = types.SimpleNamespace(on_main=lambda fn: fn(), turn=types.SimpleNamespace(cancel=threading.Event()))
        result = tools.ToolResult('preparing', then=lambda cancel, main: main(lambda: tools.ToolResult('finished')))
        with patch.object(tools, 'execute', return_value=result):
            finished = waves._HostRuntime._parent_tool(wave, 'search_assets')('{}')
        self.assertEqual(finished.text, 'finished')
        self.assertIsNone(finished.then)

    def test_panel_shows_credit_even_when_search_card_is_collapsed(self):
        from loopcut import state
        from test_layout import build
        for expanded in (False, True):
            session = state.new_session()
            session['items'] = [{'kind': 'tool', 'name': 'search_assets', 'summary': 'Search assets: chair',
                                 'status': 'done', 'expanded': expanded, 'code': '',
                                 'output': '1. chair\nPowered by Poly Haven'}]
            display = build(session)
            self.assertTrue(any(p.get('text') == 'Powered by Poly Haven' and '.tool.credit' in p['id']
                                for p in display['prims']))


if __name__ == '__main__':
    unittest.main()
