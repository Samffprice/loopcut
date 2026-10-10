"""Skill files preserve selection and enforce the local/server selection contract.

The selection golden was verified byte for byte against the original skill bodies
before removing them from the fixture; it is never regenerated here. Replays use
only committed metadata and neutral bodies, without Blender, network or server files.
"""

import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/addons_core'))
fake_bpy = types.ModuleType('bpy')
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault('bpy', fake_bpy)

from loopcut import config, dev_reload, skills, workers  # noqa: E402
from loopcut.skills import loader  # noqa: E402
from prompt_index_fixture import built_index, index_patch, select, topics_for


def fields_for(identity):
    return {k: v for k, v in next(e for e in built_index()["skills"] if e["id"] == identity).items()
            if k not in ("chars", "sha256", "swapped_chars")}


BASE_FIELDS = dict(id='custom/glass', version=1, title='Glass note', when='The request mentions glass',
                   roles=['worker'], requires_tools=[], tier='free', kind='assist')


def markdown(body='Extra glass note', **fields):
    values = {**BASE_FIELDS, **fields}
    header = []
    for key, value in values.items():
        if isinstance(value, (list, tuple)):
            value = '[' + ', '.join(value) + ']'
        header.append(f'{key}: {value}')
    return '---\n' + '\n'.join(header) + '\n---\n' + body + '\n'


def fetched(body='Fetched glass', **fields):
    skill = loader.parse_skill(markdown(body, **fields))
    return dict(front_matter=dict(skill.front_matter), body=skill.body, sha256=skill.sha256)


class GoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / 'fixtures/skills_selection_golden.json'
        cls.golden = json.loads(path.read_text(encoding='utf-8'))
        # Unique neutral bodies expose the production selector's order, including duplicates.
        cls.fetched = [fetched(entry['id'], **fields_for(entry['id']))
                       for entry in built_index()['skills'] if entry.get('part') == 'single']

    def setUp(self):
        self.enterContext(mock.patch.object(config, 'skills_dir', return_value=None))
        self.enterContext(mock.patch.object(loader, 'builtins', return_value=()))
        self.enterContext(mock.patch('loopcut.cloud._runtime', None))
        self.enterContext(index_patch())

    def test_worker_outputs_match_selection_fixture(self):
        self.assertEqual(len(self.golden['cases']), 4405)
        for n, case in enumerate(self.golden['cases']):
            self.assertEqual(set(case['outputs']), {*loader.CRAFT_NAMES, 'unknown'})
            for role, expected in case['outputs'].items():
                with self.subTest(case=n, role=role):
                    actual = workers.role_notes(role, case['brief'], case['notes'])
                    self.assertEqual(actual, expected['role_notes'])
                    self.assertEqual(topics_for(role, case['brief'], case['notes']), expected['topics'])

    def test_single_outputs_match_selection_fixture(self):
        self.assertEqual(len(self.golden['single']), 480)
        for n, case in enumerate(self.golden['single']):
            for role in ('single', 'workspace'):
                with self.subTest(case=n, role=role):
                    actual = skills.single_notes(case['request'], server_skills=self.fetched, role=role)
                    self.assertEqual(actual.split('\n\n') if actual else [], case['skill_ids'])


class ParserTest(unittest.TestCase):
    def test_empty_builtin_directory_is_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            loader.builtins.cache_clear()
            try:
                with mock.patch.object(loader, 'ROOT', Path(folder)):
                    self.assertEqual(loader.builtins(), ())
                    self.assertEqual(loader.load_skills(), ())
            finally:
                loader.builtins.cache_clear()

    def test_only_one_file_format_newline_is_removed(self):
        for body in ('', 'body', '  leading and trailing  ', '\nbody\n', 'body\n\n', 'body\r\n\t  '):
            with self.subTest(body=body):
                self.assertEqual(loader.parse_skill(markdown(body)).body, body)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'body.md'
            path.write_bytes(markdown('body\r\n\t  ').encode())
            self.assertEqual(loader.read_skill(path).body, 'body\r\n\t  ')

    def test_regex_values_are_raw_to_end_of_line(self):
        pattern = r'(?<!hand[- ])painted|^a:[b]$ # literal  '
        parsed = loader.parse_skill(markdown(words=pattern, leads=r'\btoon\b'))
        self.assertEqual(parsed.front_matter['words'], pattern)
        self.assertEqual(parsed.front_matter['leads'], r'\btoon\b')

    def test_swap_replacement_is_stripped_per_contract_0_3(self):
        for replacement in ('before ', ' before ', '  before: after  '):
            with self.subTest(replacement=replacement):
                parsed = loader.parse_skill(markdown(part='swap', craft='modeling', words='swap', replaces=replacement))
                self.assertEqual(parsed.front_matter['replaces'], replacement.strip())


    def test_malformed_front_matter_fails_with_source(self):
        valid = markdown()
        bad = [valid[4:], valid.replace('\n---\n', '\n--\n'), valid[:-1],
               valid.replace('version: 1', 'version: 1\nversion: 2'),
               valid.replace('title: Glass note\n', ''),
               valid.replace('when: The request mentions glass', 'when: |'),
               valid.replace('title: Glass note', ' title: Glass note'),
               valid.replace('title: Glass note', 'title: [wrong]'),
               valid.replace('roles: [worker]', 'roles: worker'),
               valid.replace('roles: [worker]', 'roles: [worker,]'),
               valid.replace('roles: [worker]', 'roles: [worker, worker]'),
               valid.replace('roles: [worker]', 'roles: [modeling]'),
               valid.replace('roles: [worker]', 'roles: []'),
               valid.replace('version: 1', 'version: 0'),
               valid.replace('version: 1', 'version: true'),
               valid.replace('version: 1', 'version: 1.5'),
               valid.replace('version: 1', 'version: 01'),
               valid.replace('kind: assist', 'kind: arbitrary'),
               valid.replace('tier: free', 'tier: arbitrary'),
               valid.replace('id: custom/glass', 'id: ../glass'),
               valid.replace('kind: assist', 'kind: assist\nwords: ['),
               valid.replace('kind: assist', 'kind: assist\nunknown: value'),
               valid.replace('kind: assist', 'kind: assist\npart: swap\ncraft: materials'),
               valid.replace('kind: assist', 'kind: assist\npart: topic\ncraft: other\nwords: glass')]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaisesRegex(loader.SkillError, 'broken.md:'):
                    loader.parse_skill(text, 'broken.md')

    def test_builtin_cache_is_used(self):
        first = loader.builtins()
        with mock.patch.object(loader, 'read_skill', side_effect=AssertionError('read twice')):
            self.assertIs(loader.builtins(), first)

    def test_malformed_builtin_fails_loudly(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'broken.md').write_text('---\ninvalid\n---\nbody\n')
            loader.builtins.cache_clear()
            try:
                with mock.patch.object(loader, 'ROOT', Path(folder)):
                    with self.assertRaisesRegex(loader.SkillError, 'broken.md'):
                        loader.builtins()
            finally:
                loader.builtins.cache_clear()
                loader.builtins()


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.setting = mock.patch.object(config, 'skills_dir', return_value=None)
        self.setting.start()
        self.addCleanup(self.setting.stop)

    def write(self, filename='glass.md', body='Local glass', **fields):
        (self.directory / filename).write_text(markdown(body, **fields), encoding='utf-8')

    def test_higher_version_wins_and_ties_prefer_builtin(self):
        built = loader.parse_skill(markdown('Built'), built_in=True)
        for version, expected in ((1, 'Built'), (2, 'Extra')):
            extra = loader.parse_skill(markdown('Extra', version=version))
            self.assertEqual(loader.merge_skills([built], [extra])[0].body, expected)
            self.assertEqual(loader.merge_skills([extra], [built])[0].body, expected)

    def test_same_version_among_extras_keeps_first(self):
        a = loader.parse_skill(markdown('A'))
        b = loader.parse_skill(markdown('B'))
        self.assertEqual(loader.merge_skills([], [a, b])[0].body, 'A')

    def test_extra_directory_override_and_new_topic(self):
        original = loader.parse_skill(markdown('Baseline glass', **fields_for('craft/materials/glass')))
        self.write(body='Overridden glass', **{**dict(original.front_matter), 'version': 2})
        self.write('extra.md', body='New ceramic glass note', id='custom/materials/new-glass',
                   craft='materials', part='topic', name='new-glass', order=0, words='glass')
        selected = topics_for('materials', 'glass', extra_dir=self.directory)
        self.assertEqual(selected, ['glass', 'new-glass'])
        text = select('materials', 'glass', extra_dir=self.directory)
        self.assertIn('Overridden glass\nNew ceramic glass note', text)
        # Explicit local files override the service even at the same version.
        self.write(body='Tied glass', **dict(original.front_matter))
        self.assertIn('Tied glass', select('materials', 'glass', extra_dir=self.directory))

    def test_added_topics_use_match_ranking_and_limits(self):
        for n in range(5):
            self.write(f'{n}.md', body=f'Topic {n}', id=f'custom/topic-{n}', craft='materials',
                       part='topic', words=f'newword{n}', name=f'new-{n}')
        text = 'newword0 newword1 newword2 newword3 newword4 newword4'
        self.assertEqual(topics_for('materials', text, extra_dir=self.directory),
                         ['new-4', 'new-0', 'new-1'])
        self.write('large.md', body='X' * skills.MAX_CHARS, id='custom/large', craft='materials',
                   part='topic', words='large', name='large')
        self.assertEqual(topics_for('materials', 'glass large', extra_dir=self.directory), ['glass'])

    def test_requires_tools_roles_and_kind_filter(self):
        self.write(craft='materials', part='topic', name='special', words='specialglass',
                   roles=['worker'], requires_tools=['loopcut_cloud.test'], kind='gen')
        options = dict(extra_dir=self.directory)
        self.assertNotIn('special', topics_for('materials', 'specialglass', **options))
        options['available_tools'] = {'loopcut_cloud.test'}
        self.assertIn('special', topics_for('materials', 'specialglass', **options))
        self.assertNotIn('special', topics_for('materials', 'specialglass', no_generation=True, **options))
        self.assertEqual(select('materials', 'specialglass', agent_role='single', **options), '')
        self.assertEqual(topics_for('materials', 'specialglass', agent_role='orchestrator', **options), [])
        with self.assertRaises(loader.SkillError):
            loader.load_skills(self.directory, role='materials')

    def test_ineligible_newer_version_does_not_fall_back(self):
        built = loader.parse_skill(markdown('Baseline glass', **fields_for('craft/materials/glass')))
        self.write(**{**dict(built.front_matter), 'version': 2, 'requires_tools': ['missing']})
        self.assertNotIn('glass', topics_for('materials', 'glass', extra_dir=self.directory))

    def test_single_and_workspace_notes_append_extras_and_filter(self):
        self.write(body='Request note', roles=['single', 'workspace'], words='painterly',
                   requires_tools=['paint_tool'], kind='gen')
        baseline = skills.single_notes('painterly')
        self.assertEqual(skills.single_notes('painterly', extra_dir=self.directory), baseline)
        for role in ('single', 'workspace'):
            options = dict(extra_dir=self.directory, role=role, available_tools=['paint_tool'])
            self.assertEqual(skills.single_notes('painterly', **options), 'Request note')
            self.assertEqual(skills.single_notes('painterly', no_generation=True, **options), baseline)
            self.assertEqual(skills.single_notes('realistic', **options), '')
        self.assertEqual(skills.single_notes('painterly', role='worker', extra_dir=self.directory), '')

    def test_single_override_keeps_builtin_position(self):
        single = loader.parse_skill(markdown('Baseline paint', **fields_for('single/painterly')))
        self.write(body='Replacement paint', **{**dict(single.front_matter), 'version': 2})
        self.assertTrue(skills.single_notes('painterly toon', extra_dir=self.directory)
                        .startswith('Replacement paint'))

    def test_directory_is_reread_and_bad_files_fail(self):
        self.write(body='First', roles=['single'], words='glass')
        self.assertEqual(skills.single_notes('glass', extra_dir=self.directory), 'First')
        self.write(body='Second', roles=['single'], words='glass')
        self.assertEqual(skills.single_notes('glass', extra_dir=self.directory), 'Second')
        (self.directory / 'bad.md').write_text('bad')
        with self.assertRaisesRegex(loader.SkillError, 'bad.md'):
            loader.load_skills(self.directory)
        with self.assertRaises(loader.SkillError):
            loader.load_skills(self.directory / 'missing')

    def test_configured_missing_directory_warns_and_keeps_builtins(self):
        baseline = select('materials', 'glass')
        missing = self.directory / 'missing'
        with mock.patch.object(config, 'skills_dir', return_value=missing):
            for load in (loader.load_skills, lambda: select('materials', 'glass')):
                with self.subTest(load=load), mock.patch('builtins.print') as warning:
                    result = load()
                    self.assertEqual(result, loader.builtins() if load is loader.load_skills else baseline)
                    warning.assert_called_once()
                    self.assertTrue(warning.call_args.args[0].startswith('Loopcut:'))
                    self.assertIn(str(missing), warning.call_args.args[0])

    def test_configured_bad_file_warns_and_keeps_valid_files(self):
        self.write('first.md', body='First note', craft='materials', part='topic', words='glass', name='first')
        self.write('last.md', body='Last note', id='custom/last', craft='materials', part='topic',
                   words='glass', name='last')
        baseline = loader.parse_skill(markdown('Baseline glass', **fields_for('craft/materials/glass')))
        bad = self.directory / 'bad.md'
        for content in (b'bad', b'\xff'):
            bad.write_bytes(content)
            with self.subTest(content=content), \
                    mock.patch.object(config, 'skills_dir', return_value=self.directory):
                with mock.patch('builtins.print') as warning:
                    records = loader.load_skills()
                    for built in loader.builtins():
                        self.assertIn(built, records)
                    self.assertEqual([s.body for s in records if not s.built_in], ['First note', 'Last note'])
                    warning.assert_called_once()
                    self.assertTrue(warning.call_args.args[0].startswith('Loopcut:'))
                    self.assertIn(str(bad), warning.call_args.args[0])
                with mock.patch('builtins.print') as warning:
                    text = select('materials', 'glass')
                    for body in ('craft/materials/glass', 'First note', 'Last note'):
                        self.assertIn(body, text)
                    warning.assert_called_once()
                with self.assertRaises((loader.SkillError, UnicodeError)), mock.patch('builtins.print') as warning:
                    select('materials', 'glass', extra_dir=self.directory)
                warning.assert_not_called()

    def test_explicit_missing_directory_stays_strict(self):
        with mock.patch('builtins.print') as warning:
            with self.assertRaisesRegex(loader.SkillError, 'missing'):
                select('materials', 'glass', extra_dir=self.directory / 'missing')
        warning.assert_not_called()

    def test_fetched_entries_merge_with_directory_and_baseline(self):
        built = loader.parse_skill(markdown('Baseline glass', **fields_for('craft/materials/glass')))
        fields = dict(built.front_matter)
        self.write(body='Local', **{**fields, 'version': 2})
        entry = fetched('Server', **{**fields, 'version': 3})
        self.assertIn('Server', select('materials', 'glass', extra_dir=self.directory,
                                                 server_skills=[entry]))
        records = skills.skills_from_dicts([entry])
        self.assertEqual(records[0].body, 'Server')
        flat = {**entry['front_matter'], 'body': entry['body'], 'sha256': entry['sha256']}
        self.assertEqual(skills.skills_from_dicts([flat])[0].body, 'Server')
        tied = fetched('Ignored', **fields)
        self.assertIn('Ignored', select('materials', 'glass', server_skills=[tied]))

    def test_fetched_entries_are_validated_without_trimming_body(self):
        entry = fetched('  fetched\n\n', words='glass', roles=['single'])
        self.assertEqual(skills.single_notes('glass', server_skills=[entry]), '  fetched\n\n')
        for invalid in (None, {}, {**entry, 'body': None}, {**entry, 'sha256': 'bad'},
                        {**entry, 'front_matter': {'version': True}}):
            with self.subTest(entry=invalid):
                with mock.patch('builtins.print') as warning:
                    self.assertEqual(skills.skills_from_dicts([invalid]), ())
                warning.assert_called_once()
                self.assertTrue(warning.call_args.args[0].startswith('Loopcut:'))
                self.assertIn('fetched skill 0', warning.call_args.args[0])

    def test_invalid_server_entry_keeps_builtins_and_other_entries(self):
        first = fetched('First note', roles=['single'], words='painterly')
        last = fetched('Last note', id='custom/last', roles=['single'], words='painterly')
        baseline = skills.single_notes('painterly')
        with mock.patch('builtins.print') as warning:
            self.assertEqual(skills.single_notes('painterly', server_skills=[first, None, last]),
                             'First note\n\nLast note')
        warning.assert_called_once()
        self.assertTrue(warning.call_args.args[0].startswith('Loopcut:'))
        self.assertIn('fetched skill 1', warning.call_args.args[0])

    def test_malformed_builtin_stays_fatal_with_optional_sources(self):
        (self.directory / 'broken.md').write_text('bad')
        loader.builtins.cache_clear()
        try:
            with mock.patch.object(loader, 'ROOT', self.directory), \
                    mock.patch('builtins.print') as warning:
                with self.assertRaisesRegex(loader.SkillError, 'broken.md'):
                    loader.load_skills(server_skills=[fetched()])
            warning.assert_not_called()
        finally:
            loader.builtins.cache_clear()
            loader.builtins()


class IntegrationTest(unittest.TestCase):
    def test_worker_and_agent_import_the_package(self):
        self.assertIs(importlib.import_module('.skills', 'loopcut'), skills)
        self.assertTrue(Path(skills.__file__).name == '__init__.py')
        with mock.patch.object(config, 'skills_dir', return_value=None), index_patch():
            self.assertEqual(workers._craft('lighting', {'aspects': ['camera']}),
                             select('lighting', {'aspects': ['camera']}))
        # agent.py's relative import uses the package's single_notes; test_workspace exercises it.
        from loopcut import skills as imported
        self.assertIs(imported.single_notes, skills.single_notes)

    def test_config_uses_environment_preferences_then_env_file(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / '.env'
            env.write_text('LOOPCUT_SKILLS_DIR=/file/skills\n')
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch.object(config, '_find_env_file', return_value=env), \
                    mock.patch.object(config, '_preference_values', return_value={'LOOPCUT_SKILLS_DIR': '/prefs/skills'}):
                self.assertEqual(config.skills_dir(), Path('/prefs/skills'))
                with mock.patch.dict(os.environ, {'LOOPCUT_SKILLS_DIR': '/env/skills'}):
                    self.assertEqual(config.skills_dir(), Path('/env/skills'))
                with mock.patch.object(config, '_preference_values', return_value={}):
                    self.assertEqual(config.skills_dir(), Path('/file/skills'))
                    with mock.patch.object(config, '_find_env_file', return_value=None):
                        self.assertIsNone(config.skills_dir())

    def test_environment_source_and_explicit_path_take_effect(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as explicit:
            Path(folder, 'note.md').write_text(markdown('From env', roles=['single'], words='glass'))
            with mock.patch.dict(os.environ, {'LOOPCUT_SKILLS_DIR': folder}), \
                    mock.patch.object(config, '_find_env_file', return_value=None), \
                    mock.patch.object(config, '_preference_values', return_value={}):
                self.assertEqual(skills.single_notes('glass'), 'From env')
                self.assertEqual(skills.single_notes('glass', extra_dir=explicit), '')

    def test_dev_reload_watches_package_and_markdown_edits_and_removals(self):
        self.assertEqual(dev_reload._path('skills'), Path(skills.__file__))
        self.assertEqual(dev_reload._path('skills.loader'), Path(loader.__file__))
        self.assertLess(dev_reload._MODULES.index('skills.loader'), dev_reload._MODULES.index('skills'))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'skills').mkdir()
            (root / 'skills/__init__.py').write_text('')
            (root / 'skills/loader.py').write_text('')
            note = root / 'skills/note.md'
            note.write_text('first')
            with mock.patch.object(dev_reload, '_ROOT', root), \
                    mock.patch.object(dev_reload, '_MODULES', ('skills.loader', 'skills')), \
                    mock.patch.object(dev_reload, '_mtimes', {}):
                self.assertEqual(dev_reload._changed(), [])
                os.utime(note, ns=(note.stat().st_atime_ns, note.stat().st_mtime_ns + 1_000_000))
                self.assertEqual(dev_reload._changed(), ['skills.loader'])
                self.assertEqual(dev_reload._changed(), [])
                note.unlink()
                self.assertEqual(dev_reload._changed(), ['skills.loader'])


if __name__ == '__main__':
    unittest.main()
