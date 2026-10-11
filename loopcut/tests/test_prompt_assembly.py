"""Contract 006 injection, using synthetic texts only."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

HARNESS = Path(__file__).resolve().parents[1] / 'harness'
sys.path.insert(0, str(HARNESS))
import prompt_assembly as pa
import platform_contract


def skill_text(skill_id='craft/core', body='seed seed', **fields):
    metadata = {'id': skill_id, 'version': 1, 'title': 'Dummy skill', 'when': 'Testing',
                'roles': ['single', 'worker'], 'requires_tools': [], 'tier': 'paid', 'kind': 'assist', **fields}
    lines = [f'{key}: ' + ('[' + ', '.join(value) + ']' if isinstance(value, list) else str(value))
             for key, value in metadata.items()]
    return '---\n' + '\n'.join(lines) + '\n---\n' + body + '\n'


def skill(skill_id='craft/core', body='seed seed', **fields):
    return pa.skill_loader().parse_skill(skill_text(skill_id, body, **fields))


def source():
    records = [skill(part='core', craft='modeling', everyday=['basic']),
               skill('craft/swap-a', 'bud', part='swap', craft='modeling', words='a', replaces='seed'),
               skill('craft/swap-b', '🌱🌱', part='swap', craft='modeling', words='b', replaces='bud'),
               skill('craft/swap-c', 'unused', part='swap', craft='modeling', words='c', replaces='seed'),
               skill('craft/topic', 'Topic.\n', part='topic', craft='modeling', words=r'\btopic\b', order=3,
                     name='basic', leads='topic'),
               skill('extra', 'More.')]
    return pa.Source({key: f'Dummy {key} 🌍' for key in pa.PROMPT_IDS}, {s.id: s for s in records})


def placeholder(*ids):
    return '<loopcut-skills ids="' + ','.join(ids) + '"/>'


def write_source(root):
    prompts, skills = root / 'prompts', root / 'skills'
    prompts.mkdir()
    skills.mkdir()
    for key in pa.PROMPT_IDS:
        (prompts / f'{key}.md').write_bytes(f'Dummy {key} 🌍\n'.encode())
    (skills / 'core.md').write_text(skill_text(part='core', craft='modeling'), encoding='utf-8')
    return prompts, skills


class AssemblyTest(unittest.TestCase):
    def setUp(self):
        self.source = source()

    def expand(self, text, prompt='worker'):
        return pa.assemble([{'role': 'user', 'content': text}], prompt, self.source)[1]['content']

    def error(self, code, messages, prompt='worker', extra=None):
        before = copy.deepcopy(messages)
        with self.assertRaises(pa.AssemblyError) as caught:
            pa.assemble(messages, prompt, self.source)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.extra, extra or {})
        self.assertEqual(messages, before)

    def test_system_merge_preserves_tail_and_properties(self):
        for tail in ('\n\nInstalled: Dummy.', ''):
            with self.subTest(tail=tail):
                messages = [{'role': 'system', 'content': tail, 'name': 'prefix'}, {'role': 'user', 'content': 'Hi'}]
                before = copy.deepcopy(messages)
                result = pa.assemble(messages, 'orchestrator', self.source)
                self.assertEqual(result[0], {'role': 'system', 'content': 'Dummy orchestrator 🌍' + tail, 'name': 'prefix'})
                self.assertEqual(messages, before)
                self.assertIsNot(result, messages)
                result[1]['content'] = 'changed'
                self.assertEqual(messages, before)

    def test_insert_system_at_zero_including_empty_conversation(self):
        for messages in ([], [{'role': 'user', 'content': 'Hi'}]):
            result = pa.assemble(messages, 'router', self.source)
            self.assertEqual(result, [{'role': 'system', 'content': 'Dummy router 🌍'}, *messages])

    def test_unknown_prompt(self):
        for prompt in ('bogus', '', ' worker', 'WORKER'):
            self.error('unknown_prompt', [], prompt)
        self.source.prompts['bogus'] = 'text'
        self.error('unknown_prompt', [], 'bogus')

    def test_bad_system_messages(self):
        for messages in ([{'role': 'user'}, {'role': 'system', 'content': ''}],
                         [{'role': 'system', 'content': ''}, {'role': 'system', 'content': ''}],
                         [{'role': 'system', 'content': []}], [{'role': 'system', 'content': None}],
                         [{'role': 'system'}], None, [None]):
            with self.subTest(messages=messages):
                self.error('bad_system_message', messages)

    def test_string_and_text_parts_and_all_prompt_ids(self):
        text = 'Before ' + placeholder('craft/core', 'craft/topic') + ' after'
        for prompt in pa.PROMPT_IDS:
            self.assertEqual(self.expand(text, prompt), 'Before seed seed\nTopic.\n after')
        parts = [{'type': 'text', 'text': text, 'cache_control': {'type': 'ephemeral'}},
                 {'type': 'image_url', 'image_url': {'url': placeholder('missing')}},
                 {'type': 'input_text', 'text': '<loopcut-skills invalid'}]
        messages = [{'role': 'user', 'content': parts}]
        before = copy.deepcopy(messages)
        result = pa.assemble(messages, 'single', self.source)
        self.assertEqual(result[1]['content'][0]['text'], 'Before seed seed\nTopic.\n after')
        self.assertEqual(result[1]['content'][1:], parts[1:])
        result[1]['content'][0]['cache_control']['type'] = 'changed'
        self.assertEqual(messages, before)

    def test_non_user_messages_are_not_expanded_or_validated(self):
        messages = [{'role': role, 'content': '<loopcut-skills malformed'} for role in ('system', 'assistant', 'tool')]
        result = pa.assemble(messages, 'worker', self.source)
        self.assertEqual(result[0]['content'], 'Dummy worker 🌍<loopcut-skills malformed')
        self.assertEqual(result[1:], messages[1:])
        self.assertEqual(self.expand('Ordinary text'), 'Ordinary text')

    def test_multiple_placeholders_and_reuse_across_them(self):
        self.assertEqual(self.expand(placeholder('extra') + '\n\n' + placeholder('extra', 'craft/core')),
                         'More.\n\nMore.\nseed seed')

    def test_bad_placeholder_grammar(self):
        for bad in ('<loopcut-skills', '<loopcut-skills/>', '<loopcut-skills ids=""/>',
                    '<loopcut-skills ids="extra, craft/core"/>', '<loopcut-skills ids="extra,"/>',
                    '<loopcut-skills ids="extra" />', "<loopcut-skills ids='extra'/>",
                    '<loopcut-skills ids="extra"></loopcut-skills>', '<loopcut-skills-other>',
                    '<loopcut-skills ids="../extra"/>', '<loopcut-skills ids="extra\n"/>'):
            with self.subTest(bad=bad):
                self.error('bad_skill_placeholder', [{'role': 'user', 'content': 'text ' + bad}])

    def test_repeated_id_within_one_placeholder(self):
        self.error('bad_skill_placeholder', [{'role': 'user', 'content': placeholder('extra', 'extra')}])

    def test_unknown_skill_id_extra(self):
        self.error('unknown_skill', [{'role': 'user', 'content': placeholder('missing/id')}], extra={'id': 'missing/id'})

    def test_swaps_replace_every_occurrence_in_core_and_emit_nothing(self):
        self.assertEqual(self.expand(placeholder('craft/core', 'craft/swap-a', 'craft/topic')), 'bud bud\nTopic.\n')

    def test_interacting_swaps_follow_placeholder_order(self):
        self.assertEqual(self.expand(placeholder('craft/core', 'craft/swap-a', 'craft/swap-b')), '🌱🌱 🌱🌱')
        self.assertEqual(self.expand(placeholder('craft/core', 'craft/swap-b', 'craft/swap-a')), 'bud bud')

    def test_consumed_replaces_is_a_noop(self):
        self.assertEqual(self.expand(placeholder('craft/core', 'craft/swap-a', 'craft/swap-c')), 'bud bud')

    def test_swap_requires_earlier_same_craft_core_in_same_placeholder(self):
        self.source.skills['other/core'] = skill('other/core', 'seed', part='core', craft='camera')
        for text in (placeholder('craft/swap-a', 'craft/core'),
                     placeholder('craft/core') + placeholder('craft/swap-a'),
                     placeholder('other/core', 'craft/swap-a')):
            self.error('bad_skill_placeholder', [{'role': 'user', 'content': text}])

    def test_injected_text_is_not_recursively_expanded(self):
        self.source.skills['extra'] = skill('extra', '<loopcut-skills broken')
        self.assertEqual(self.expand(placeholder('extra')), '<loopcut-skills broken')

    def test_placeholder_limit_across_messages_and_text_parts(self):
        messages = [{'role': 'user', 'content': placeholder('extra') * 32},
                    {'role': 'user', 'content': [{'type': 'text', 'text': placeholder('extra') * 32}]}]
        self.assertEqual(pa.assemble(messages, 'worker', self.source)[1]['content'], 'More.' * 32)
        messages[1]['content'].append({'type': 'text', 'text': placeholder('extra')})
        self.error('skills_over_limit', messages)

    def test_distinct_ids_limit_across_placeholders(self):
        ids = [f's{i}' for i in range(25)]
        self.source.skills = {key: skill(key, '') for key in ids}
        self.assertEqual(self.expand(placeholder(*ids[:24])), '\n' * 23)
        self.error('skills_over_limit', [{'role': 'user', 'content': placeholder(*ids[:24]) + placeholder(ids[24])}])

    def test_char_limit_is_code_points_and_counts_distinct_ids_once(self):
        self.source.skills['extra'] = skill('extra', '🌍' * 32000)
        self.assertEqual(len(self.expand(placeholder('extra') * 2)), 64000)
        self.source.skills['extra'] = skill('extra', '🌍' * 32001)
        self.error('skills_over_limit', [{'role': 'user', 'content': placeholder('extra')}])

    def test_char_limit_sums_distinct_bodies_including_swaps_before_expansion(self):
        self.source.skills['craft/core'] = skill('craft/core', 'a' * 16000, part='core', craft='modeling')
        self.source.skills['craft/swap-a'] = skill('craft/swap-a', 'b' * 16000, part='swap', craft='modeling',
                                                replaces='a' * 16000, words='a')
        self.assertEqual(len(self.expand(placeholder('craft/core', 'craft/swap-a'))), 16000)
        self.error('skills_over_limit', [{'role': 'user', 'content': placeholder('craft/core', 'craft/swap-a', 'extra')}])


class IndexTest(unittest.TestCase):
    def test_index_sizes_fields_order_and_interacting_swaps(self):
        src = source()
        src.skills['fragment'] = skill('fragment', 'Dummy', part='fragment', symbol='DUMMY')
        result = pa.index(src)
        self.assertEqual(result['prompts']['items']['single']['chars'], len('Dummy single 🌍'))
        self.assertRegex(result['prompts']['version'], r'^[a-f0-9]{12}$')
        self.assertEqual([s['id'] for s in result['skills']], sorted(src.skills))
        core = next(s for s in result['skills'] if s['id'] == 'craft/core')
        self.assertEqual(core['chars'], 9)
        self.assertEqual(core['everyday'], ['basic'])
        self.assertEqual(core['swapped_chars'], {
            'craft/swap-a': 7, 'craft/swap-b': 9, 'craft/swap-c': 13,
            'craft/swap-a,craft/swap-b': 5, 'craft/swap-a,craft/swap-c': 7,
            'craft/swap-b,craft/swap-c': 13, 'craft/swap-a,craft/swap-b,craft/swap-c': 5})
        for entry in result['skills']:
            self.assertNotIn('replaces', entry)
            self.assertNotIn('symbol', entry)
            self.assertNotIn('body', entry)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_version_covers_every_text_and_is_order_independent(self):
        src = source()
        before = pa.version(src)
        self.assertEqual(pa.version(pa.Source(dict(reversed(list(src.prompts.items()))),
                                              dict(reversed(list(src.skills.items()))))), before)
        for key in src.prompts:
            changed = pa.Source({**src.prompts, key: src.prompts[key] + '!'}, src.skills)
            self.assertNotEqual(pa.version(changed), before)
        for key, old in src.skills.items():
            changed = pa.Source(src.prompts, {**src.skills, key: pa.skill_loader().Skill(old.front_matter, old.body + '!', old.sha256)})
            self.assertNotEqual(pa.version(changed), before)


class LoadTest(unittest.TestCase):
    def test_env_defaults_parser_hash_and_exact_final_lf(self):
        with tempfile.TemporaryDirectory() as directory:
            prompts, skills = write_source(Path(directory))
            (prompts / 'single.md').write_bytes(b'Dummy\r\n\n')
            raw = skill_text(body='Dummy\r\n\n', part='core', craft='modeling')
            (skills / 'core.md').write_bytes(raw.encode())
            (skills / 'README.md').write_text('Not a skill')
            with mock.patch.dict(os.environ, {'LOOPCUT_PROMPTS_DIR': str(prompts), 'LOOPCUT_SERVER_SKILLS_DIR': str(skills)}):
                src = pa.load()
            self.assertEqual(src.prompts['single'], 'Dummy\r\n')
            self.assertEqual(src.skills['craft/core'].body, 'Dummy\r\n\n')
            self.assertEqual(src.skills['craft/core'].sha256, hashlib.sha256(raw.encode()).hexdigest())
            self.assertEqual(src.prompt_files['router'], (str((prompts / 'router.md').resolve()),))

    def test_manifest_parts_and_join(self):
        with tempfile.TemporaryDirectory() as directory:
            prompts, skills = write_source(Path(directory))
            entries = {key: {'parts': [key + '.md'], 'join': ''} for key in pa.PROMPT_IDS}
            entries['orchestrator'] = {'parts': ['single.md', 'orchestrator.md'], 'join': '\n\n'}
            (prompts / 'manifest.json').write_text(json.dumps({'prompts': entries}))
            src = pa.load(prompts, skills)
            self.assertEqual(src.prompts['orchestrator'], 'Dummy single 🌍\n\nDummy orchestrator 🌍')
            entries['worker']['parts'] = ['../escape.md']
            (prompts / 'manifest.json').write_text(json.dumps({'prompts': entries}))
            with self.assertRaisesRegex(ValueError, 'inside the prompts directory'):
                pa.load(prompts, skills)

    def test_missing_dirs_duplicate_skill_and_bad_skill_fail_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'LOOPCUT_PROMPTS_DIR'):
                pa.load(root / 'missing', root)
            prompts, skills = write_source(root)
            with self.assertRaisesRegex(ValueError, 'LOOPCUT_SERVER_SKILLS_DIR'):
                pa.load(prompts, root / 'missing')
            (skills / 'dup.md').write_bytes((skills / 'core.md').read_bytes())
            with self.assertRaisesRegex(ValueError, 'duplicate skill id'):
                pa.load(prompts, skills)
            (skills / 'dup.md').write_text('broken')
            with self.assertRaisesRegex(ValueError, 'opening ---'):
                pa.load(prompts, skills)

    def test_index_and_assemble_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts, skills = write_source(root)
            command = [sys.executable, str(HARNESS / 'prompt_assembly.py')]
            args = ['--prompts', str(prompts), '--skills', str(skills)]
            subprocess.run(command + ['index', *args, '--out', str(root / 'index.json')], check=True, capture_output=True)
            self.assertEqual(json.loads((root / 'index.json').read_text()), pa.index(pa.load(prompts, skills)))
            result = subprocess.run(command + ['assemble', '--prompt', 'worker', *args], text=True,
                                    input=json.dumps({'messages': [{'role': 'user', 'content': placeholder('craft/core')}]}),
                                    check=True, capture_output=True)
            self.assertEqual(json.loads(result.stdout), [{'role': 'system', 'content': 'Dummy worker 🌍'},
                                                         {'role': 'user', 'content': 'seed seed'}])

    def test_export_dummy_constants_and_clear_failure_when_removed(self):
        modules = {'loopcut.agent': SimpleNamespace(SYSTEM_PROMPT='Base'),
                   'loopcut.orchestrator': SimpleNamespace(PROMPT='Orch'),
                   'loopcut.workers': SimpleNamespace(PROMPT='Worker'),
                   'loopcut.router': SimpleNamespace(PROMPT='Router')}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(pa.importlib, 'import_module', side_effect=modules.__getitem__):
            out = Path(directory) / 'export'
            pa.export_addon(out)
            self.assertEqual((out / 'orchestrator.md').read_bytes(), b'Base\n\nOrch\n')
            for key, expected in (('single', b'Base\n'), ('worker', b'Worker\n'), ('router', b'Router\n')):
                self.assertEqual((out / f'{key}.md').read_bytes(), expected)
            del modules['loopcut.workers'].PROMPT
            with self.assertRaisesRegex(ValueError, 'Cannot export add-on prompts: constants removed:'):
                pa.export_addon(Path(directory) / 'removed')
            self.assertFalse((Path(directory) / 'removed').exists())


class ContractParityTest(unittest.TestCase):
    def test_all_published_fixtures(self):
        contract = platform_contract.require()
        directories = [contract / 'fixtures' / name for name in ('prompt-assembly', 'prompt-assembly-case')]
        if not any(path.is_dir() for path in directories):
            self.skipTest('Contract 006 prompt-assembly fixtures are not published yet')
        paths = sorted(path for directory in directories for path in directory.rglob('*.json'))
        self.assertTrue(paths, 'Published prompt-assembly directory must contain fixtures')
        # The mock uses base + orchestrator parts without a manifest, unlike the
        # harness's full-text fallback. Mirror mock.read_prompts, not its assembler.
        texts = {name: (contract / 'mock/prompts' / f'{name}.md').read_bytes().decode('utf-8').removesuffix('\n')
                 for name in ('base', 'orchestrator', 'worker', 'router')}
        prompts = {'single': texts['base'], 'orchestrator': texts['base'] + '\n\n' + texts['orchestrator'],
                   'worker': texts['worker'], 'router': texts['router']}
        skills = [pa.skill_loader().parse_skill(path.read_bytes().decode('utf-8'), str(path))
                  for path in (contract / 'mock/skills').rglob('*.md') if path.name != 'README.md']
        src = pa.Source(prompts, {s.id: s for s in skills})
        for path in paths:
            with self.subTest(fixture=path.name):
                case = json.loads(path.read_text())
                request, expected = case['request'], case['expect']
                prompt_id = case['headers'].get('x-loopcut-prompt') or None  # Empty is pass-through, as absent.
                try:
                    actual = {'messages': pa.assemble(request['messages'], prompt_id, src)
                              if prompt_id is not None else copy.deepcopy(request['messages'])}
                except pa.AssemblyError as ex:
                    actual = {'status': 400, 'error': {'code': ex.code, **ex.extra}}
                self.assertEqual(actual, expected)


if __name__ == '__main__':
    unittest.main()
