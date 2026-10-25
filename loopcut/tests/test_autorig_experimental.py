"""Experimental tiers remain bounded, explicit, and unusable by paid motion tools."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from test_autorig import valid_sidecars, descriptor, inputs
from test_motion import FakeArmature, descriptor as motion_descriptor, video_descriptor
import platform_contract
from loopcut import autorig, cloud, cloud_account, cloud_jobs, rig_bundle, rig_experimental as exp


def experimental():
    rig, report, task = valid_sidecars()
    task['accept_experimental'] = True
    rig.update(tier='experimental', canonical_plan=None, joints=rig['joints'][:2],
               experimental={'reasons': ['too_few_joints'], 'joint_count': 2,
                             'inside_fraction': 0, 'unweighted_vertices': 0})
    report['checks'].append({**report['checks'][0], 'id': 'anatomy', 'status': 'fail'})
    return rig, report, task


class ExperimentalBundle(unittest.TestCase):
    def test_pushed_contract_standard_and_experimental_sidecars(self):
        root = platform_contract.require('fixtures')
        task = json.loads((root / 'rig-task/experimental.json').read_bytes())
        for filename in ('standard-v3.json', 'experimental.json', 'two-joint-experimental.json'):
            rig = json.loads((root / 'rig' / filename).read_bytes())
            report = json.loads((root / 'rig-validation/example.json').read_bytes())
            if rig['tier'] == 'experimental':
                report['checks'].append({**report['checks'][0], 'id': 'anatomy', 'status': 'fail'})
            with self.subTest(filename=filename):
                rig_bundle.validate(rig, report, task)

    def test_two_joint_floor_and_zero_containment_are_accepted(self):
        rig, report, task = experimental()
        self.assertIs(rig_bundle.validate(rig, report, task), rig)

    def test_no_opt_in_or_inconsistent_tier_never_applies(self):
        for mutate in (lambda r, v, t: t.pop('accept_experimental'),
                       lambda r, v, t: t.update(accept_experimental=False),
                       lambda r, v, t: r.update(tier='standard'),
                       lambda r, v, t: r.update(canonical_plan={'policy': 'mixamo-tokenrig-v1', 'labels': []}),
                       lambda r, v, t: r.pop('tier'),
                       lambda r, v, t: r.pop('experimental')):
            rig, report, task = experimental()
            mutate(rig, report, task)
            with self.subTest(mutate=mutate), self.assertRaises(autorig.RigError):
                rig_bundle.validate(rig, report, task)

    def test_structural_failures_and_false_diagnostics_are_rejected(self):
        changes = [lambda r, v: r.update(joints=r['joints'][:1]),
                   lambda r, v: r['joints'][1].update(parent=None),
                   lambda r, v: r['joints'][0].update(head=[float('nan'), 0, 0]),
                   lambda r, v: r['meshes'][0].update(source_vertex_weights=[[]]),
                   lambda r, v: r['meshes'][0].update(source_vertex_weights=[[[0, .4]]]),
                   lambda r, v: r['meshes'][0].update(source_vertex_weights=[[[0, float('inf')]]]),
                   lambda r, v: r['experimental'].update(reasons=['transport_failed']),
                   lambda r, v: r['experimental'].update(reasons=['too_few_joints'] * 2),
                   lambda r, v: r['experimental'].update(joint_count=3),
                   lambda r, v: r['experimental'].update(inside_fraction=float('nan')),
                   lambda r, v: r['experimental'].update(unweighted_vertices=1),
                   lambda r, v: v['checks'].pop(),
                   lambda r, v: v['checks'][0].update(status='fail')]
        for mutate in changes:
            rig, report, task = experimental()
            mutate(rig, report)
            with self.subTest(mutate=mutate), self.assertRaises(autorig.RigError):
                rig_bundle.validate(rig, report, task)

    def test_standard_and_legacy_keep_canonical_plan(self):
        rig, report, task = valid_sidecars()
        rig_bundle.validate(rig, report, task)
        rig['tier'], task['accept_experimental'] = 'standard', True
        rig_bundle.validate(rig, report, task)
        for bad in ({'canonical_plan': None}, {'experimental': None}):
            with self.assertRaises(autorig.RigError):
                rig_bundle.validate({**rig, **bad}, report, task)

    def test_every_artifact_agrees_with_sidecar_tier_and_reasons(self):
        rig, _, _ = experimental()
        meta = {'tier': 'experimental', 'reasons': ['too_few_joints'],
                'experimental': rig['experimental'], 'canonicalization': 'not_applicable'}
        outputs = [{'meta': copy.deepcopy(meta)} for _ in range(3)]
        rig_bundle.match_metadata(rig, outputs)
        for bad in ({}, {'tier': 'standard'}, {'reasons': ['pelvis_branches']},
                    {'reasons': [None]}, {'canonicalization': 'local_pending'}, {'experimental': None}):
            trial = copy.deepcopy(outputs)
            trial[2]['meta'] = {**meta, **bad} if bad else {}
            with self.assertRaises(autorig.RigError):
                rig_bundle.match_metadata(rig, trial)

    def test_chains_cover_every_native_bone_once_without_guessing_roles(self):
        joints = [{'name': f'bone_{i}', 'parent': parent} for i, parent in enumerate([None, 0, 1, 1, 3])]
        result = exp.describe(joints, 'Animal')
        self.assertEqual(result['bone_chains'], [['bone_0', 'bone_1'], ['bone_2'], ['bone_3', 'bone_4']])
        self.assertEqual((result['tier'], result['bone_count'], result['root']), ('experimental', 5, 'bone_0'))
        self.assertFalse(result['motion_tools_supported'])
        self.assertEqual(len(result['next_steps']), 4)


class ExperimentalDispatch(unittest.TestCase):
    def test_both_motion_tools_refuse_named_or_selected_experimental_before_any_request(self):
        arm, mesh = FakeArmature('Animal'), FakeArmature('Skin', 'MESH')
        arm._properties = {exp.TIER_PROPERTY: 'experimental'}
        mesh.parent = arm
        for mode in ('named_arm', 'named_mesh', 'active_arm', 'active_mesh', 'selected_arm', 'selected_mesh', 'mixed_selection'):
            for video in (False, True):
                d = video_descriptor() if video else motion_descriptor()
                d['backend'] = 'managed'
                arguments = {'video': 'Clip'} if video else {'prompt': 'walk'}
                if mode.startswith('named'):
                    arguments['target_armature'] = arm.name if mode.endswith('arm') else mesh.name
                selected = [arm if mode.endswith('arm') else mesh] if mode.startswith('selected') else []
                if mode == 'mixed_selection':
                    selected = [arm, FakeArmature('Other')]
                active = (arm if mode.endswith('arm') else mesh) if mode.startswith('active') else None
                fake_bpy = types.SimpleNamespace(data=types.SimpleNamespace(objects={'Animal': arm, 'Skin': mesh}),
                    context=types.SimpleNamespace(scene=None, selected_objects=selected,
                        view_layer=types.SimpleNamespace(objects=types.SimpleNamespace(active=active))))
                with tempfile.TemporaryDirectory() as tmp:
                    runtime = Mock(cache_dir=Path(tmp))
                    runtime.descriptors.return_value = [d]
                    with patch.dict(sys.modules, {'bpy': fake_bpy}), patch.object(cloud, '_runtime', runtime), \
                            patch.object(cloud, 'sync_settings'), patch.object(cloud, '_progress'), \
                            patch.object(cloud_jobs, 'resolve_files', return_value={}), \
                            patch.object(cloud_account, 'authorize') as authorize, \
                            patch.object(cloud_jobs, 'run') as run, \
                            patch.object(cloud_jobs.blobs, 'upload_missing') as upload:
                        result = cloud.execute(cloud.PREFIX + d['name'], arguments)
                    with self.subTest(mode=mode, video=video):
                        self.assertTrue(result.ok, 'R12: a condition, not a failure, so no red card')
                        self.assertIsNone(result.then)
                        self.assertEqual(result.text, exp.MOTION_REFUSAL)
                        self.assertEqual(result.model_note, exp.MOTION_NOTE)
                        authorize.assert_not_called()
                        run.assert_not_called()
                        upload.assert_not_called()
                        runtime.client.request.assert_not_called()

    def test_v3_client_injects_opt_in_before_export_and_keeps_it_out_of_model_schema(self):
        d = descriptor()
        d['input_schema']['properties']['accept_experimental'] = {'type': 'boolean', 'default': False}
        self.assertNotIn('accept_experimental', cloud.schema(d)['function']['parameters']['properties'])
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Mock(cache_dir=Path(tmp))
            runtime.descriptors.return_value = [d]
            with patch.object(cloud, '_runtime', runtime), patch.object(cloud, 'sync_settings'), \
                    patch.object(cloud, '_progress'), patch.object(cloud_jobs, 'resolve_files',
                        side_effect=autorig.RigError('Stop after the boundary')) as resolve:
                result = cloud.execute(cloud.PREFIX + 'auto_rig', {**inputs(), 'targets': ['Animal']})
            self.assertFalse(result.ok)
            self.assertIs(resolve.call_args.args[1]['accept_experimental'], True)

    def test_quote_uses_one_specific_warning_or_general_disclosure(self):
        with tempfile.TemporaryDirectory() as tmp:
            preparation = {'directory': str(Path(tmp).resolve()), 'inspection': {'warnings': ['unusual']}}
            with patch.dict(autorig._prepared, {'task': preparation}, clear=True):
                self.assertEqual(autorig.quote_notice(tmp), (exp.QUOTE, False))
                preparation['inspection']['warnings'] = [autorig.rig_inspect.CLOTHING_WARNING]
                self.assertEqual(autorig.quote_notice(tmp), (autorig.rig_inspect.CLOTHING_WARNING, False))
                preparation['inspection']['shape_hints'] = ['upright_body', 'leg_gap']
                self.assertEqual(autorig.quote_notice(tmp), (exp.QUOTE, False))
                preparation['inspection']['warnings'] = []
                self.assertEqual(autorig.quote_notice(tmp), (exp.QUOTE, False))

    def test_cloud_result_card_excludes_model_hierarchy(self):
        from loopcut import scene_diff
        d = descriptor()
        def apply(descriptor, job, *, details):
            details['rig'] = exp.describe([{'name': 'bone_0', 'parent': None},
                                           {'name': 'bone_1', 'parent': 0}], 'Animal')
            return exp.result_message(2)
        with patch.object(scene_diff, 'snapshot', return_value={}), patch.object(cloud, '_runtime', None), \
                patch.object(cloud_account, 'receipt', return_value='20 credits'):
            result = cloud._apply(types.SimpleNamespace(apply_result=apply), d, {})
        self.assertEqual(result.text, exp.result_message(2))
        self.assertNotIn('bone_0', result.text)
        self.assertIn('bone_0', result.model_note)
        self.assertEqual(json.loads(result.model_note.split('\n', 1)[1])['bone_count'], 2)


if __name__ == '__main__':
    unittest.main()
