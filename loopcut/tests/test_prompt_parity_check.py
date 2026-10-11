"""Fast synthetic V1 transport checks. All fixture text is made in a temporary directory."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

HARNESS = Path(__file__).resolve().parents[1] / 'harness'
sys.path.insert(0, str(HARNESS))
import fake_model
import prompt_assembly as assembly
import prompt_parity_check as parity


def fixtures(root):
    prompts, skills = root / 'prompts', root / 'skills'
    prompts.mkdir()
    skills.mkdir()
    for role in assembly.PROMPT_IDS:
        (prompts / f'{role}.md').write_text(f'Dummy {role} "prompt" — 🌱\n', encoding='utf-8')
    (skills / 'core.md').write_text(
        '---\nid: dummy/core\nversion: 1\ntitle: Dummy\nwhen: Testing\nroles: [worker]\n'
        'requires_tools: []\ntier: paid\nkind: assist\npart: core\ncraft: modeling\n---\n'
        'Dummy skill — 🌱\n', encoding='utf-8')
    source = assembly.load(prompts, skills)
    index = root / 'index.json'
    parity.write_json(index, assembly.index(source))
    cases = []
    for role in (*assembly.PROMPT_IDS, 'summary', 'reference'):
        tail = '\n\nInstalled: Dummy.' if role in ('single', 'orchestrator') else ''
        user = '<role_notes>\nDummy skill — 🌱\n</role_notes>' if role == 'worker' else 'Hello — 🌱'
        system = source.prompts.get(role, 'Open dummy ' + role) + tail
        old = {'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
               'tools': [], 'reasoning_effort': 'low', 'max_tokens': 32}
        new = copy.deepcopy(old)
        if role in assembly.PROMPT_IDS:
            new['prompt_id'] = role
            new['messages'] = ([{'role': 'system', 'content': tail}] if tail else []) + [
                {'role': 'user', 'content': '<role_notes>\n<loopcut-skills ids="dummy/core"/>\n</role_notes>'
                 if role == 'worker' else user}]
        cases.append({'name': 'tiny-' + role, 'kind': 'wire', 'role': role, 'old': old, 'new': new,
                      'replies': [fake_model.text_reply('OK')]})
    return source, index, cases


class PromptParityTest(unittest.TestCase):
    def test_recorded_worker_edits_match_once_and_preserve_unrelated_bytes(self):
        edits = [(f'Old synthetic clause {n}.', f'New synthetic clause {n}.') for n in range(3)]
        old = '\n'.join(pair[0] for pair in edits)
        new = '\n'.join(pair[1] for pair in edits)
        self.assertEqual(parity.apply_server_edits(old, 'worker', edits), new)
        for role in ('single', 'orchestrator', 'router', 'summary', 'reference'):
            self.assertEqual(parity.apply_server_edits(old, role, edits), old)
        body = {'messages': [{'role': 'system', 'content': old},
                             {'role': 'user', 'content': edits[0][0]}]}
        # Noncanonical outer spacing must remain visible in byte comparisons.
        raw = b'  ' + json.dumps(body).encode() + b'\n'
        expected = raw.replace(json.dumps(old).encode(), json.dumps(new).encode())
        self.assertEqual(parity.rebaseline_body(raw, 'worker', edits), expected)
        for role in ('single', 'orchestrator', 'router'):
            self.assertEqual(parity.rebaseline_body(raw, role, edits), raw)
        for bad in ('No matching clause', old + '\n' + edits[0][0], new):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'expected exactly once'):
                parity.apply_server_edits(bad, 'worker', edits)

    def test_metadata_is_runtime_only_and_invalid_or_missing_is_incomplete(self):
        with tempfile.TemporaryDirectory(dir=parity.SCRATCH) as temp:
            root = Path(temp)
            source = root / 'SOURCE.json'
            edits = [[f'Old clause {n}', f'New clause {n}'] for n in range(3)]
            parity.write_json(source, {'server_edits': [{'prompt': 'worker', 'replace': edits},
                                                       {'prompt': 'router', 'replace': [['old', 'new']]}]})
            self.assertEqual(parity.load_server_edits(source), list(map(tuple, edits)))
            for bad in ({}, {'server_edits': []}, {'server_edits': [None]},
                        {'server_edits': [{'prompt': 'worker', 'replace': [['', 'new']]}]},
                        {'server_edits': [{'prompt': 'worker', 'replace': [['old', 1]]}]}):
                parity.write_json(source, bad)
                with self.assertRaises(parity.IncompleteParity):
                    parity.load_server_edits(source)
            source.unlink()
            args = SimpleNamespace(out=root / 'report', server_edits=source)
            with patch.object(parity, 'archive_old') as archive:
                self.assertEqual(parity.check(args), 2)
                archive.assert_not_called()
            report = json.loads((args.out / 'report.json').read_text())
            self.assertFalse(report['passed'])
            self.assertTrue(report['incomplete'])
            self.assertIn('INCOMPLETE', (args.out / 'report.md').read_text())

    def test_compare_runs_edits_only_old_worker_and_keeps_capture(self):
        with tempfile.TemporaryDirectory(dir=parity.SCRATCH) as temp:
            root = Path(temp)
            edits = [('Synthetic old clause', 'Synthetic new clause')]
            cases = [{'name': role, 'role': role, 'replies': [None]} for role in assembly.PROMPT_IDS]
            runs = []
            for side in ('old', 'new'):
                run = {'records': {}, 'outcomes': {}, 'returncode': 0, 'model_errors': [],
                       'remaining': [], 'transport_errors': []}
                for case in cases:
                    role = case['role']
                    path = root / f'{side}-{role}.json'
                    clause = edits[0][int(side == 'new' and role == 'worker')]
                    path.write_bytes(json.dumps({'messages': [{'role': 'system', 'content': clause}]}).encode())
                    run['records'][role] = [{'file': str(path), 'status': 200}]
                    run['outcomes'][role] = {'ok': True}
                runs.append(run)
            original = (root / 'old-worker.json').read_bytes()
            self.assertTrue(parity.compare_runs(cases, *runs)['differences'])
            report = parity.compare_runs(cases, *runs, edits)
            self.assertEqual(report['differences'], [])
            self.assertEqual(report['errors'], [])
            self.assertEqual(sum(row['identical'] for row in report['scenarios']), 4)
            self.assertEqual((root / 'old-worker.json').read_bytes(), original)
            (root / 'new-router.json').write_bytes(original + b' ')
            self.assertEqual(parity.compare_runs(cases, *runs, edits)['differences'][0]['kind'], 'serialization')

    def test_one_tiny_scenario_per_role_through_subprocess_proxy_and_model(self):
        with tempfile.TemporaryDirectory(prefix='loopcut-parity-test-', dir=parity.SCRATCH) as temp:
            root = Path(temp)
            source, index, cases = fixtures(root)
            old = parity.run_build(parity.REPO, 'old', cases, source, index, root / 'old')
            new = parity.run_build(parity.REPO, 'new', cases, source, index, root / 'new')
            report = parity.compare_runs(cases, old, new)
            self.assertEqual(report['errors'], [])
            self.assertEqual(report['differences'], [])
            self.assertEqual([row['identical'] for row in report['scenarios']], [1] * 6)
            parity.write_report(root, report)
            saved = json.loads((root / 'report.json').read_text())
            self.assertTrue(saved['passed'])
            self.assertEqual(saved['requests_per_role'], dict.fromkeys(
                ('single', 'orchestrator', 'worker', 'router', 'summary', 'reference'), 1))
            self.assertIn('| tiny-worker | 1/1 | 1 | 0 |', (root / 'report.md').read_text())
            for row in old['records'].values():
                body = json.loads(Path(row[0]['file']).read_bytes())
                self.assertTrue(body['messages'][0]['content'].startswith(('Dummy', 'Open dummy')))

            # A failed driver must fail even if every captured body matches.
            failed = copy.deepcopy(new)
            failed['outcomes']['tiny-worker']['ok'] = False
            result = parity.compare_runs(cases, old, failed)
            self.assertEqual(result['differences'], [])
            self.assertTrue(result['errors'])
            parity.write_report(root, result)
            self.assertFalse(json.loads((root / 'report.json').read_text())['passed'])

            # A missing request cannot disappear through zip() truncation.
            missing = copy.deepcopy(new)
            missing['records']['tiny-worker'].clear()
            result = parity.compare_runs(cases, old, missing)
            self.assertEqual(result['differences'][0]['kind'], 'missing_request')
            self.assertEqual(result['differences'][0]['scenario'], 'tiny-worker')
            self.assertTrue(result['errors'])

    def test_model_retains_exact_noncanonical_entity_bytes(self):
        server = fake_model.Server(usage=False).start()
        self.addCleanup(server.stop)
        server.script('raw', [fake_model.text_reply('OK')])
        raw = b'{ "messages" : [], "model":"raw", "x":"\\u0061" }\n'
        with urlopen(Request(server.base_url + '/chat/completions', data=raw,
                             headers={'Content-Type': 'application/json'}), timeout=5) as response:
            response.read()
        self.assertEqual(server.log[0].raw_body, raw)
        canonical = json.dumps(json.loads(raw)).encode()
        self.assertNotEqual(raw, canonical)
        diff = parity.compare_bytes(raw, canonical)
        self.assertEqual(diff['kind'], 'serialization')
        self.assertIsNone(diff['path'])

    def test_first_difference_path_and_bounded_diagnostics(self):
        old = {'messages': [{'role': 'system', 'content': 'synthetic secret ' * 1000}]}
        new = copy.deepcopy(old)
        new['messages'][0]['content'] += '!'
        diff = parity.compare_bytes(json.dumps(old).encode(), json.dumps(new).encode())
        self.assertEqual(diff['path'], '$["messages"][0]["content"]')
        self.assertEqual(diff['kind'], 'content')
        self.assertNotIn('synthetic secret', json.dumps(diff))
        self.assertEqual(parity.first_path({'a': [1]}, {'a': [1, 2]}), '$["a"][1]')
        self.assertEqual(parity.first_path({'a': 1}, {'a': True}), '$["a"]')

    def test_rejects_repository_artifact_destination(self):
        with self.assertRaises(ValueError):
            parity.private_dir(parity.REPO / 'accidental-private-output')


if __name__ == '__main__':
    unittest.main()
