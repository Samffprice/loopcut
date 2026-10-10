"""Distillation preserves run evidence and makes strict, selectable drafts without Blender."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('distill', REPO / 'loopcut/harness/distill.py')
distill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(distill)
sys.path.insert(0, str(REPO / 'scripts/addons_core'))
fake_bpy = types.ModuleType('bpy')
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault('bpy', fake_bpy)
from loopcut import skills  # noqa: E402
from loopcut.skills import loader  # noqa: E402

VALID = '''---
id: distilled/studio-trap
version: 1
title: Studio rendering context
when: A studio study uses separate render and pack scenes
roles: [single, workspace]
requires_tools: []
tier: free
kind: assist
words: studio-trap
---
A separate scene has its own camera and render settings. Render resolution is measured in pixels.
'''


class DistillTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run = self.root / '20261001-000001'
        self.run.mkdir()
        self.out = self.root / 'packets'
        self.result = {'id': 'paint', 'passed': True, 'prompt': 'Studio study', 'steps': []}
        self.items = [
            {'kind': 'user', 'text': 'Paint a hero prop with a warm key.'},
            {'kind': 'tool', 'name': 'run_python', 'code': "bpy.data.objects['HeroChest'].data\nold_api()",
             'status': 'error', 'output': 'Traceback\nAttributeError: old_api is unavailable'},
            {'kind': 'tool', 'name': 'run_python', 'code': 'new_api()\ns.render.resolution_x=1920',
             'status': 'done', 'output': 'new_api works; render saved hero.png'},
            {'kind': 'assistant', 'text': 'The hero study is saved, with a warm key. Final render: hero.png'},
        ]
        (self.run / 'paint.conversation').mkdir()
        workspace = self.run / 'paint.workspace'
        workspace.mkdir()
        (workspace / 'finish.py').write_text("s.cycles.samples = 192\nlight.energy = 145\n"
                                           "p.inputs['Roughness'].default_value = 0.69\n"
                                           "s.render.resolution_y = 1080\n", encoding='utf-8')
        (self.run / 'hero.png').write_bytes(b'fake image; extraction does not decode it')
        self.save_run()

    def save_run(self):
        distill.write_json(self.run / 'results.json', {'results': [self.result, {'id': 'failed', 'passed': False},
                                                                 {'id': 'not_boolean', 'passed': 'true'}]})
        distill.write_json(self.run / 'paint.conversation/conversation.json', {'items': self.items})

    def packet(self):
        return distill.make_packets([self.run], self.out)[0]

    def skill(self, text=VALID):
        path = self.root / 'draft.md'
        path.write_text(text, encoding='utf-8')
        return path

    def drafter_files(self, args, texts=(VALID,), reply='Draft files written.'):
        scratch = Path(args[args.index('-C') + 1])
        for index, text in enumerate(texts):
            (scratch / 'skills' / f'skill-{index}.md').write_text(text, encoding='utf-8')
        Path(args[args.index('-o') + 1]).write_text(reply, encoding='utf-8')
        return scratch

    def test_packet_pass_filter_recovery_values_and_renders(self):
        paths = distill.make_packets([self.run], self.out)
        self.assertEqual(len(paths), 1)
        text = paths[0].read_text()
        for expected in ('Paint a hero prop', 'hero study is saved', 'old_api()', 'AttributeError',
                         'new_api()', '1920', '1080', 'samples = 192', 'energy = 145', '0.69', str(self.run / 'hero.png')):
            self.assertIn(expected, text)
        facts = text.split('## Failure')[1]
        self.assertNotIn('HeroChest', facts)
        provenance = distill.packet_provenance(paths[0], 'none')
        self.assertIn('HeroChest', provenance['object_names'])
        self.assertEqual(provenance['source_runs'], [str(self.run)])
        self.assertEqual(provenance['task'], 'paint')

    def test_packet_compact_and_does_not_read_dependencies(self):
        self.items[0]['text'] *= 1000
        self.items[-1]['text'] *= 1000
        self.save_run()
        deps = self.run / 'paint.workspace/deps'
        deps.mkdir()
        (deps / 'vendor.py').write_text('light.energy = 999999')
        text = self.packet().read_text()
        self.assertLessEqual(len(text), distill.MAX_PACKET_CHARS)
        self.assertNotIn('999999', text)
        self.assertIn('excerpt truncated', text)

    def test_recovery_prefers_shared_api_over_generic_workspace_log_text(self):
        self.items = [
            {'kind': 'tool', 'name': 'shell', 'code': 'python3 -c "import PIL, numpy"',
             'output': "exit 1\nModuleNotFoundError: No module named 'PIL'"},
            {'kind': 'tool', 'name': 'shell', 'code': 'unrelated_command', 'output': 'exit 0 in workspace'},
            {'kind': 'tool', 'name': 'shell', 'code': 'blender --python finish.py', 'output': 'exit 0'},
        ]
        (self.run / 'paint.workspace/finish.py').write_text('import numpy as np\ns.cycles.samples = 192\n')
        self.save_run()
        facts = self.packet().read_text().split('## Failure')[1].split('## Observed')[0]
        self.assertIn('blender --python finish.py', facts)
        self.assertIn('import numpy as np', facts)
        self.assertNotIn('unrelated_command', facts)

    def test_latest_scene_alias_assignment_supersedes_preview_setting(self):
        self.items.insert(-1, {'kind': 'tool', 'name': 'run_python', 'code': 'studio.cycles.samples = 256',
                              'status': 'done', 'output': 'updated'})
        self.save_run()
        facts = self.packet().read_text().split('## Observed')[1]
        self.assertIn('samples = 256', facts)
        self.assertNotIn('samples = 192', facts)

    def test_separate_material_function_scopes_preserve_both_roughness_values(self):
        text = distill.observed_values([('paint.py', "def wood():\n p.inputs['Roughness'].default_value=.82\n"
                                        "def metal():\n p.inputs['Roughness'].default_value=.69\n")], [])
        self.assertIn('.82', text)
        self.assertIn('.69', text)
        self.assertIn('paint.py:wood', text)
        self.assertIn('paint.py:metal', text)

    def test_recorded_script_execution_follows_earlier_live_preview_settings(self):
        tools = [{'name': 'run_python', 'code': 'studio.cycles.samples=16', 'output': 'done'},
                 {'name': 'shell', 'code': 'blender --python final.py', 'output': 'exit 0'}]
        sources = distill.settings_sources(tools, [('final.py', 's.cycles.samples=192')])
        text = distill.observed_values(sources, [])
        self.assertIn('samples=192', text)
        self.assertNotIn('samples=16', text)

    def test_brief_file_wins_and_transcript_fallback(self):
        (self.run / 'paint.brief.md').write_text('The actual submitted brief.')
        (self.run / 'paint.conversation/conversation.json').unlink()
        (self.run / 'paint.md').write_text('## user\nwrong brief\n\n## call run_python {}\nold_api()\n\n'
                                         '## tool\nTraceback\nAttributeError: old\n\n'
                                         '## call run_python {}\nnew_api()\n\n## tool\nworked\n\n'
                                         '## assistant\nFinal study saved.\n')
        text = self.packet().read_text()
        self.assertIn('actual submitted brief', text)
        self.assertNotIn('wrong brief', text)
        self.assertIn('old_api()', text)
        self.assertIn('new_api()', text)
        self.assertIn('Final study saved.', text)

    def test_steps_fallback_and_invalid_task_id(self):
        self.result['steps'] = self.items[1:3]
        self.save_run()
        (self.run / 'paint.conversation/conversation.json').unlink()
        self.assertIn('old_api', self.packet().read_text())
        self.result['id'] = '../outside'
        self.save_run()
        with self.assertRaisesRegex(ValueError, 'task id'):
            self.packet()

    def test_codex_call_uses_scratch_workspace_devnull_and_only_codex_home(self):
        packet = self.packet()
        original_files = {p.name: p.read_bytes() for p in packet.parent.iterdir()}
        def fake_run(args, **kwargs):
            self.assertEqual(args[:5], [distill.CODEX, 'exec', '-s', 'workspace-write', '-C'])
            scratch = Path(args[5])
            self.assertTrue(scratch.is_absolute())
            self.assertEqual(scratch.parent, self.root / 'drafts')
            self.assertEqual(kwargs['cwd'], scratch)
            self.assertEqual(args[6:8], ['--skip-git-repo-check', '-o'])
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(kwargs['env'], {'CODEX_HOME': str(Path.home() / '.codex')})
            self.assertTrue(kwargs['check'])
            self.assertIn('No numbered steps', args[-1])
            self.assertIn('Paint a hero prop', args[-1])
            self.drafter_files(args)
            (scratch / 'notes/skill-0.txt').write_text('One run; Blender version unknown.\n')
        with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
            drafts = distill.draft(packet, self.root / 'drafts')
        call.assert_called_once()
        self.assertEqual(drafts[0].read_text(), VALID)
        source = distill.read_json(drafts[0].with_suffix('.provenance.json'))
        self.assertEqual(source['packet_sha256'], distill.digest(packet.read_bytes()))
        self.assertEqual(source['drafter'], 'codex')
        self.assertTrue(source['date'])
        self.assertEqual(source['task'], 'paint')
        self.assertEqual(source['draft_notes'], 'One run; Blender version unknown.\n')
        self.assertEqual(original_files, {p.name: p.read_bytes() for p in packet.parent.iterdir()})
        replies = list((self.root / 'drafts').glob('*.response.txt'))
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].read_text(), 'Draft files written.')

    def test_file_protocol_ignores_malformed_json_reply_and_preserves_each_skill(self):
        texts = [VALID, VALID.replace('studio-trap', 'shader-trap'),
                 VALID.replace('studio-trap', 'image-trap')]
        raw = '{"skills": ["a malformed old reply"],"next skill"]}'
        out = self.root / 'drafts'
        with patch.object(distill.subprocess, 'run', side_effect=lambda args, **kw:
                          self.drafter_files(args, texts, raw)) as call:
            drafts = distill.draft(self.packet(), out)
        call.assert_called_once()
        self.assertEqual([p.read_text() for p in drafts], texts)
        self.assertEqual(next(out.glob('*.response.txt')).read_text(), raw)

    def test_parse_error_retries_once_with_quoted_error_and_retains_failed_attempt(self):
        out = self.root / 'drafts'
        scratches = []
        def fake_run(args, **kwargs):
            broken = VALID.replace('version: 1', 'version: zero')
            scratch = self.drafter_files(args, [broken] if not scratches else [VALID],
                                         reply=f'Attempt {len(scratches) + 1}')
            if scratches:
                error = (scratches[0] / 'error.txt').read_text().rstrip('\n')
                self.assertIn(json.dumps(error), args[-1])
                self.assertIn('fresh scratch directory', args[-1])
                self.assertNotEqual(scratch, scratches[0])
                self.assertFalse((out / 'distilled').exists())
            scratches.append(scratch)
        with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
            drafts = distill.draft(self.packet(), out)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(drafts[0].read_text(), VALID)
        self.assertIn('version: zero', (scratches[0] / 'skills/skill-0.md').read_text())
        self.assertEqual(len(list(out.glob('*.response.txt'))), 2)
        for scratch in scratches:
            self.assertTrue((scratch / 'prompt.md').is_file())

    def test_missing_files_retry_uses_fresh_directory_without_reusing_valid_files(self):
        calls = []
        def fake_run(args, **kwargs):
            texts = [VALID, VALID.replace('kind: assist', 'kind: invalid')] if not calls else []
            calls.append(self.drafter_files(args, texts))
        out = self.root / 'drafts'
        with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
            with self.assertRaisesRegex(ValueError, '1-3 complete skills'):
                distill.draft(self.packet(), out)
        self.assertEqual(call.call_count, 2)
        self.assertFalse((out / 'distilled').exists())
        self.assertTrue((calls[0] / 'skills/skill-0.md').is_file())
        self.assertEqual(list((calls[1] / 'skills').iterdir()), [])

    def test_invalid_utf8_skill_retries_and_succeeds(self):
        calls = []
        def fake_run(args, **kwargs):
            scratch = self.drafter_files(args)
            if not calls:
                (scratch / 'skills/skill-0.md').write_bytes(b'\xff')
            else:
                self.assertIn('decode', args[-1])
            calls.append(scratch)
        with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
            drafts = distill.draft(self.packet(), self.root / 'drafts')
        self.assertEqual(call.call_count, 2)
        self.assertEqual(drafts[0].read_text(), VALID)

    def test_invalid_file_sets_retry_once_and_never_publish(self):
        cases = ('empty', 'four', 'duplicate', 'wrong-id', 'unexpected-file',
                 'nested', 'symlink', 'linked-directory', 'orphan-notes', 'linked-notes')
        packet = self.packet()
        for case in cases:
            with self.subTest(case=case):
                out = self.root / case
                def fake_run(args, **kwargs):
                    texts = [] if case == 'empty' else [VALID] * 4 if case == 'four' else [VALID]
                    if case == 'duplicate':
                        texts *= 2
                    if case == 'wrong-id':
                        texts = [VALID.replace('distilled/', 'other/')]
                    scratch = self.drafter_files(args, texts)
                    if case == 'unexpected-file':
                        (scratch / 'skills/extra.txt').write_text('unexpected')
                    if case == 'nested':
                        (scratch / 'skills/nested.md').mkdir()
                    if case == 'symlink':
                        (scratch / 'skills/linked.md').symlink_to(scratch / 'skills/skill-0.md')
                    if case == 'linked-directory':
                        (scratch / 'skills').rename(scratch / 'original')
                        (scratch / 'skills').symlink_to(scratch / 'original', target_is_directory=True)
                    if case == 'orphan-notes':
                        (scratch / 'notes/absent.txt').write_text('unmatched')
                    if case == 'linked-notes':
                        (scratch / 'notes/skill-0.txt').symlink_to(self.root / 'missing')
                with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
                    with self.assertRaises(ValueError):
                        distill.draft(packet, out)
                self.assertEqual(call.call_count, 2)
                self.assertFalse((out / 'distilled').exists())
                self.assertEqual(len(list(out.glob('*.response.txt'))), 2)

    def test_draft_refuses_existing_targets_without_retry_or_partial_files(self):
        out = self.root / 'drafts'
        existing = out / 'distilled/studio-trap.md'
        existing.parent.mkdir(parents=True)
        existing.write_text('existing draft')
        texts = [VALID.replace('studio-trap', 'another-trap'), VALID]
        with patch.object(distill.subprocess, 'run', side_effect=lambda args, **kw:
                          self.drafter_files(args, texts)) as call:
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                distill.draft(self.packet(), out)
        call.assert_called_once()
        self.assertEqual(existing.read_text(), 'existing draft')
        self.assertFalse((out / 'distilled/another-trap.md').exists())

    def test_subprocess_failure_retains_artifacts_without_parse_retry(self):
        out = self.root / 'drafts'
        def fake_run(args, **kwargs):
            self.drafter_files(args, [], 'Execution failed')
            raise subprocess.CalledProcessError(1, args)
        with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
            with self.assertRaises(subprocess.CalledProcessError):
                distill.draft(self.packet(), out)
        call.assert_called_once()
        self.assertFalse((out / 'distilled').exists())
        self.assertEqual(next(out.glob('*.response.txt')).read_text(), 'Execution failed')

    def test_prompt_requests_transferable_facts_starting_values_and_notes_files(self):
        prompt = distill.drafting_prompt(self.packet())
        for phrase in ('agent about to work in', 'Blender concepts', '280 W', '145 W', 'about 2:1',
                       'bpy.data.images', 'pixels.foreach_set', 'No run-specific object, helper',
                       'notes/<descriptive-slug>.txt', 'not its body', 'No JSON envelope',
                       'only facts and actual values', 'approval is explicit'):
            self.assertIn(phrase, prompt)

    def test_none_writes_prompt_next_to_packet_without_subprocess(self):
        packet = self.packet()
        with patch.object(distill.subprocess, 'run') as call:
            files = distill.draft(packet, self.root / 'drafts', 'none')
        call.assert_not_called()
        self.assertEqual(files[0].parent, packet.parent)
        self.assertIn('1-3 independent', files[0].read_text())
        self.assertEqual(distill.read_json(packet.with_suffix('.draft-provenance.json'))['drafter'], 'none')

    def test_packet_tampering_is_caught(self):
        packet = self.packet()
        packet.write_text(packet.read_text() + 'changed\n')
        with self.assertRaisesRegex(ValueError, 'changed since extraction'):
            distill.draft(packet, self.root / 'drafts', 'none')

    def test_invalid_drafter_response_leaves_no_partial_drafts(self):
        def fake_run(args, **kwargs):
            self.drafter_files(args, [VALID, VALID.replace('kind: assist', 'kind: invalid')])
        out = self.root / 'drafts'
        with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
            with self.assertRaises(ValueError):
                distill.draft(self.packet(), out)
        self.assertEqual(call.call_count, 2)
        self.assertFalse((out / 'distilled').exists())

    def test_retry_retains_lint_only_drafts_with_per_skill_failed_checks(self):
        cases = ('ShopChest_Lid uses the material.\n', '1. Inspect the scene.\n',
                 'First inspect the scene, then render it.\n',
                 'Always check. Never skip. You must finish.\n',
                 'The excerpt does not establish compatibility; it is unresolved.\n')
        self.items[1]['code'] += "\nbpy.data.objects['ShopChest_Lid']"
        self.save_run()
        packet = self.packet()
        for index, body in enumerate(cases):
            with self.subTest(body=body):
                out = self.root / f'lint-{index}'
                texts = [VALID + body, VALID.replace('studio-trap', 'another-trap')]
                def fake_run(args, **kwargs):
                    scratch = self.drafter_files(args, texts)
                    (scratch / 'notes/skill-0.txt').write_text('One run; review required.\n')
                with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call, \
                        contextlib.redirect_stderr(io.StringIO()) as stderr:
                    drafts = distill.draft(packet, out)
                self.assertEqual(call.call_count, 2)
                self.assertEqual([p.read_text() for p in drafts], texts)
                source = distill.read_json(drafts[0].with_suffix('.provenance.json'))
                self.assertEqual(source['draft_notes'], 'One run; review required.\n')
                self.assertFalse(source['check']['passed'])
                self.assertEqual(source['check']['errors'], distill.check_file(drafts[0])[1])
                self.assertIn('retained draft failing check', stderr.getvalue())
                good_source = distill.read_json(drafts[1].with_suffix('.provenance.json'))
                self.assertTrue(good_source['check']['passed'])
                self.assertEqual(good_source['check']['errors'], [])
                self.assertEqual(len(list(out.glob('*.attempt-*/error.txt'))), 2)
                with self.assertRaises(ValueError):
                    distill.promote(drafts[0], destination=self.root / 'premium')
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(distill.main(['check', str(drafts[0])]), 1)

    def test_corrected_retry_marks_check_passed_and_does_not_keep_first_draft(self):
        calls = []
        def fake_run(args, **kwargs):
            text = VALID + '1. Inspect the scene.\n' if not calls else VALID
            calls.append(self.drafter_files(args, [text]))
        with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
            drafts = distill.draft(self.packet(), self.root / 'drafts')
        self.assertEqual(call.call_count, 2)
        self.assertEqual(drafts[0].read_text(), VALID)
        self.assertTrue(distill.read_json(drafts[0].with_suffix('.provenance.json'))['check']['passed'])

    def test_default_names_and_sentence_words_do_not_trigger_a_drafting_retry(self):
        self.items[1]['code'] += "\nbpy.data.objects['Light']\nbpy.data.objects['Render']"
        self.save_run()
        text = VALID + 'Light the prop from above. Render resolution is measured in pixels.\n'
        with patch.object(distill.subprocess, 'run', side_effect=lambda args, **kw:
                          self.drafter_files(args, [text])) as call:
            drafts = distill.draft(self.packet(), self.root / 'drafts')
        call.assert_called_once()
        self.assertEqual(drafts[0].read_text(), text)
        self.assertTrue(distill.read_json(drafts[0].with_suffix('.provenance.json'))['check']['passed'])

    def test_retry_with_lint_and_hard_errors_never_publishes_partial_drafts(self):
        cases = (VALID.replace('tier: free', 'tier: free\npart: single'),
                 VALID + 'x' * distill.MAX_SKILL_CHARS + '\n',
                 VALID.replace('words: studio-trap', 'words: (paint+)+'),
                 VALID.replace('version: 1', 'version: zero'),
                 VALID[:VALID.index('\n---\n') + 6])
        packet = self.packet()
        for index, bad in enumerate(cases):
            with self.subTest(index=index):
                out = self.root / f'hard-{index}'
                texts = [VALID + '1. Inspect the scene.\n', bad.replace('studio-trap', 'bad-trap')]
                with patch.object(distill.subprocess, 'run', side_effect=lambda args, **kw:
                                  self.drafter_files(args, texts)) as call:
                    with self.assertRaises(ValueError):
                        distill.draft(packet, out)
                self.assertEqual(call.call_count, 2)
                self.assertFalse((out / 'distilled').exists())

    def test_valid_check_uses_real_loader(self):
        skill, errors, warnings = distill.check_file(self.skill())
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(skill.id, loader.read_skill(self.root / 'draft.md').id)

    def test_extra_key_workflow_oversize_and_malformed_header(self):
        invalid = [VALID.replace('tier: free', 'tier: free\nextra: forbidden'),
                   VALID.replace('tier: free', 'tier: free\npart: single'),
                   VALID + '1. Inspect the scene.\n2. Render it.\n',
                   VALID + 'First inspect the scene, then render it.\n',
                   VALID + 'Always check. Never skip. You must finish.\n',
                   VALID + 'x' * distill.MAX_SKILL_CHARS + '\n',
                   VALID.replace('version: 1', 'version: zero')]
        for text in invalid:
            with self.subTest(text=text[-90:]):
                self.assertTrue(distill.check_text(text)[1])

    def test_hedge_heavy_audit_text_is_rejected_case_insensitively(self):
        for body in ('The excerpt does not establish compatibility; it is unresolved.\n',
                     'The EXCERPT has no independent comparison. Compatibility is UNRESOLVED.\n',
                     'This does not establish quality. It does not establish portability. '
                     'It does not establish compatibility.\n'):
            with self.subTest(body=body):
                self.assertIn('hedge-heavy', ' '.join(distill.check_text(VALID + body)[1]))

    def test_short_scope_note_and_api_negatives_are_not_hedge_heavy(self):
        text = VALID + ('Starting values from one approved run: warm key 280 W, cool rim 145 W, about 2:1.\n'
                        'Blender\'s bundled Python has numpy but no PIL. bpy.data.images and '
                        'pixels.foreach_set support image buffers. A scene does not share its camera.\n')
        self.assertEqual(distill.check_text(text)[1], [])
        self.assertEqual(distill.check_text(VALID + 'One excerpt leaves the version unresolved.\n')[1], [])
        self.assertEqual(distill.check_text(VALID.replace('title: Studio rendering context',
                                                       'title: Unresolved excerpt comparison'))[1], [])

    def test_hedges_in_sidecar_do_not_contaminate_the_skill_body(self):
        def fake_run(args, **kwargs):
            scratch = self.drafter_files(args)
            (scratch / 'notes/skill-0.txt').write_text('The excerpt does not establish compatibility; unresolved.\n')
        with patch.object(distill.subprocess, 'run', side_effect=fake_run) as call:
            drafts = distill.draft(self.packet(), self.root / 'drafts')
        call.assert_called_once()
        self.assertEqual(distill.check_file(drafts[0])[1], [])
        self.assertIn('unresolved', distill.read_json(drafts[0].with_suffix('.provenance.json'))['draft_notes'])

    def test_regex_contract_limits_and_nested_repetition(self):
        for pattern in ('(?i)paint', '(?<=paint)wood', '(paint+)+', 'x' * 1025, '['):
            with self.subTest(pattern=pattern[:20]):
                self.assertTrue(distill.check_text(VALID.replace('words: studio-trap', 'words: ' + pattern))[1])
        self.assertFalse(distill.check_text(VALID.replace('words: studio-trap', r'words: \b(?:wood|brass)\b'))[1])
        self.assertFalse(distill.check_text(VALID.replace('words: studio-trap', 'words: wood(?= grain)'))[1])

    def test_run_specific_names_with_provenance_and_warning(self):
        path = self.skill(VALID + 'HeroChest uses this material.\n')
        distill.write_json(path.with_suffix('.provenance.json'), {'object_names': ['HeroChest']})
        self.assertIn('run-specific object names', ' '.join(distill.check_file(path)[1]))
        path.with_suffix('.provenance.json').unlink()
        self.assertTrue(distill.check_file(path)[2])
        self.assertTrue(distill.check_file(path, self.packet())[1])
        self.assertTrue(distill.check_text(VALID + "bpy.data.objects['NamedProp']\n")[1])

    def test_default_datablock_names_and_numeric_variants_are_not_run_specific(self):
        names = ('Light', 'Camera', 'Cube', 'Sun', 'Area', 'Point', 'Spot', 'World',
                 'Material', 'Plane', 'Cylinder', 'Sphere', 'Empty', 'Collection', 'Scene')
        for name in names:
            for suffix in ('', '.001', '.123', '.1000'):
                with self.subTest(name=name, suffix=suffix):
                    identity = name + suffix
                    text = VALID + f"The default is {identity}. bpy.data.objects.get('{identity}')\n"
                    self.assertEqual(distill.check_text(text, names=[identity])[1], [])
                    self.assertEqual(distill.check_text(text)[1], [])
        self.assertEqual(distill.check_text(VALID + 'Light the prop from above.\n', names=['Light'])[1], [])
        for name in ('LightRig', 'Light.01', 'Camera_custom', 'Material.001_custom', 'light'):
            with self.subTest(name=name):
                self.assertIn(name, ' '.join(distill.check_text(VALID + name + '\n', names=[name])[1]))

    def test_sentence_opening_ordinary_words_are_ignored_only_in_prose(self):
        text = VALID + ('Render resolution is measured in pixels. Use a warm key.\n\n'
                        'Materials have independent settings.\n')
        self.assertEqual(distill.check_text(text, names=['Render', 'Use', 'Materials'])[1], [])
        for text in (VALID + 'The run used Render.\n', VALID + '`Render` is the identity.\n',
                     VALID + "bpy.data.objects['Render']\n", VALID.replace('words: studio-trap', 'words: Render')):
            with self.subTest(text=text):
                self.assertIn('Render', ' '.join(distill.check_text(text, names=['Render'])[1]))
        for name in ('ShopChest_Lid', 'ArcaneRecipe', 'flat_material', 'Zorg'):
            self.assertIn(name, ' '.join(distill.check_text(VALID + name + ' uses this setting.\n', names=[name])[1]))

    def test_explicit_renames_override_default_and_sentence_word_exemptions(self):
        script = ("light.name = 'Light'\ncamera.name: str = 'Camera.001'\n"
                  "obj.name = 'ShopChest_Lid'\nother.name = 'Render'\n"
                  "bpy.data.texts.new('ArcaneRecipe')\n"
                  "def flat_material(name):\n return bpy.data.materials.new(name)\n")
        (self.run / 'paint.workspace/finish.py').write_text(script)
        packet = self.packet()
        provenance = distill.packet_provenance(packet, 'none')
        self.assertEqual(provenance['renamed_object_names'], ['Camera.001', 'Light', 'Render', 'ShopChest_Lid'])
        self.assertIn('flat_material', provenance['object_names'])
        for name in ('Light', 'Camera.001', 'Render', 'ShopChest_Lid', 'ArcaneRecipe', 'flat_material'):
            with self.subTest(name=name):
                path = self.skill(VALID + name + ' uses this setting.\n')
                self.assertIn(name, ' '.join(distill.check_file(path, packet)[1]))
                distill.write_json(path.with_suffix('.provenance.json'), provenance)
                self.assertIn(name, ' '.join(distill.check_file(path)[1]))

    def test_factory_helpers_with_ordinary_names_do_not_taint_blender_prose(self):
        script = ("def camera(name):\n return bpy.data.cameras.new(name)\n"
                  "def flat_material(name):\n return bpy.data.materials.new(name)\n")
        names = distill.object_names([], [('paint.py', script)])
        self.assertNotIn('camera', names)
        self.assertIn('flat_material', names)
        self.assertEqual(distill.check_text(VALID, names=names)[1], [])

    def test_renamed_default_survives_retry_as_failed_check_with_provenance(self):
        self.items[1]['code'] += "\nlight.name = 'Light.001'"
        self.save_run()
        with patch.object(distill.subprocess, 'run', side_effect=lambda args, **kw:
                          self.drafter_files(args, [VALID + 'Light.001 uses this setting.\n'])) as call, \
                contextlib.redirect_stderr(io.StringIO()):
            drafts = distill.draft(self.packet(), self.root / 'drafts')
        self.assertEqual(call.call_count, 2)
        source = distill.read_json(drafts[0].with_suffix('.provenance.json'))
        self.assertIn('Light.001', source['renamed_object_names'])
        self.assertFalse(source['check']['passed'])
        self.assertIn('Light.001', ' '.join(distill.check_file(drafts[0])[1]))

    def test_check_cli_reports_all_drafts_and_exits_nonzero(self):
        good = self.skill()
        bad = self.root / 'bad.md'
        bad.write_text(VALID + '1. Do it.\n')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(distill.main(['check', str(good)]), 0)
            self.assertEqual(distill.main(['check', str(good), str(bad)]), 1)

    def test_promote_copies_provenance_and_refuses_overwrite(self):
        path = self.skill()
        distill.write_json(path.with_suffix('.provenance.json'), {'object_names': [], 'task': 'paint'})
        destination = self.root / 'premium'
        promoted = distill.promote(path, destination=destination)
        self.assertEqual(promoted, destination / 'studio-trap.md')
        self.assertEqual(promoted.read_bytes(), path.read_bytes())
        self.assertEqual(distill.read_json(promoted.with_suffix('.provenance.json'))['task'], 'paint')
        with self.assertRaisesRegex(ValueError, 'overwrite'):
            distill.promote(path, destination=destination)

    def test_baseline_rejects_paid_and_invalid_drafts(self):
        with self.assertRaisesRegex(ValueError, 'tier: free'):
            distill.promote(self.skill(VALID.replace('tier: free', 'tier: paid')), baseline=True,
                            destination=self.root / 'baseline')
        with self.assertRaisesRegex(ValueError, 'workflow'):
            distill.promote(self.skill(VALID + '1. Do it.\n'), baseline=True, destination=self.root / 'baseline')

    def test_baseline_loader_and_single_workspace_selection_keep_golden_outputs(self):
        # A temporary packaged root simulates the exact recursive builtins path used in production.
        original = loader.ROOT
        destination = self.root / 'packaged/distilled'
        for source in original.rglob('*.md'):
            target = destination.parent / source.relative_to(original)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        unmatched = ('an oak chair', 'a clear glass vase', 'arcane runes on a door')
        golden = {role: [skills.single_notes(text, role=role) for text in unmatched]
                  for role in ('single', 'workspace')}
        distill.promote(self.skill(), baseline=True, destination=destination)
        loader.builtins.cache_clear()
        try:
            with patch.object(loader, 'ROOT', destination.parent):
                # Simulate a fresh add-on import, with the promoted open baseline file.
                importlib.reload(skills)
                self.assertIn('distilled/studio-trap', {s.id for s in loader.builtins()})
                for role in ('single', 'workspace'):
                    self.assertIn('separate scene', skills.single_notes('studio-trap study', role=role))
                    self.assertEqual(golden[role], [skills.single_notes(text, role=role) for text in unmatched])
        finally:
            loader.builtins.cache_clear()
            importlib.reload(skills)

    def test_contract_craft_topic_routes_without_local_part_and_requires_words_for_leads(self):
        text = VALID.replace('roles: [single, workspace]', 'roles: [worker]').replace(
            'words: studio-trap', 'words: studio-trap\nleads: study\ncraft: lighting')
        promoted = distill.promote(self.skill(text), baseline=True, destination=self.root / 'baseline')
        self.assertEqual(loader.read_skill(promoted).front_matter['craft'], 'lighting')
        brief = {'requirements': [{'quote': 'studio-trap study'}]}
        self.assertIn('separate scene', skills.skills_for('lighting', brief, extra_dir=promoted.parent))
        self.assertIn('separate scene', skills.skills_for('lighting', {}, 'studio-trap study', extra_dir=promoted.parent))
        for craft in loader.CRAFT_NAMES:
            for plain in ({}, {'requirements': [{'quote': 'a lamp study'}]}, {'style': 'glass and brass'}):
                self.assertEqual(skills.skills_for(craft, plain),
                                 skills.skills_for(craft, plain, extra_dir=promoted.parent))

    def test_fresh_packaged_craft_obeys_role_and_required_tool_gates(self):
        packaged = self.root / 'packaged'
        for source in loader.ROOT.rglob('*.md'):
            target = packaged / source.relative_to(loader.ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        text = VALID.replace('roles: [single, workspace]', 'roles: [worker]').replace(
            'requires_tools: []', 'requires_tools: [render]').replace(
            'words: studio-trap', 'words: studio-trap\ncraft: lighting')
        distill.promote(self.skill(text), baseline=True, destination=packaged / 'distilled')
        loader.builtins.cache_clear()
        try:
            with patch.object(loader, 'ROOT', packaged):
                importlib.reload(skills)
                brief = {'style': 'studio-trap'}
                self.assertNotIn('separate scene', skills.skills_for('lighting', brief))
                self.assertIn('separate scene', skills.skills_for('lighting', brief, available_tools=['render']))
        finally:
            loader.builtins.cache_clear()
            importlib.reload(skills)

    def test_unrelated_draft_preserves_indexed_selection(self):
        from prompt_index_fixture import select, topics_for
        from test_skills import BRIEFS
        promoted = distill.promote(self.skill(VALID.replace('roles: [single, workspace]', 'roles: [worker]').replace(
            'words: studio-trap', 'words: studio-trap\ncraft: lighting')), baseline=True,
            destination=self.root / 'baseline')
        for brief in BRIEFS:
            for role in loader.CRAFT_NAMES:
                with self.subTest(role=role, brief=brief):
                    self.assertEqual(select(role, brief, extra_dir=promoted.parent), select(role, brief))
                    self.assertEqual(topics_for(role, brief, extra_dir=promoted.parent), topics_for(role, brief))


if __name__ == '__main__':
    unittest.main()
