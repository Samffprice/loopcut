"""Offline eligibility, strict bundle, descriptor and cloud lifecycle regressions."""
import copy
from contextlib import nullcontext
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.path.insert(0, str(Path(__file__).parent))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))
from loopcut import autorig, cloud, cloud_account, cloud_http, cloud_jobs, rig_bundle, rig_inspect, rig_transport
from test_cloud_jobs import Reply, Transport, PROJECT, job
from test_motion import descriptor as motion_descriptor


def descriptor():
    d = motion_descriptor('auto_rig')
    d.update(version=3, apply='apply_rig', output={'type': 'rig', 'formats': ['glb', 'json']},
             kind='assist', data_class='A', backend='managed',
             input_schema={'type': 'object', 'properties': {
                 k: {'type': 'string', 'enum': [v]} for k, v in inputs().items()},
                 'required': list(inputs()), 'additionalProperties': False},
             file_inputs=[{'field': 'model', 'accept': ['model/gltf-binary'], 'max_mb': 50, 'required': True},
                          {'field': 'rig_task', 'accept': ['application/json'], 'max_mb': 1, 'required': True}],
             apply_args={'type': 'object', 'properties': {'targets': {'type': 'array', 'minItems': 1, 'maxItems': 64,
                         'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 63}}},
                         'required': ['targets'], 'additionalProperties': False})
    return d


def inputs():
    return {'category': 'humanoid', 'route': 'managed', 'controls': 'deform_only',
            'skeleton_version': 'tokenrig-native-273b691-v1'}


def boxes(bounds):
    points, triangles = [], []
    faces = [(0, 2, 3), (0, 3, 1), (4, 5, 7), (4, 7, 6), (0, 1, 5), (0, 5, 4),
             (2, 6, 7), (2, 7, 3), (0, 4, 6), (0, 6, 2), (1, 3, 7), (1, 7, 5)]
    for lo, hi in bounds:
        base = len(points)
        points.extend((x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2]))
        triangles.extend(tuple(base + i for i in t) for t in faces)
    return points, triangles


def human():
    return boxes([((-.23, -.1, .85), (.23, .1, 1.6)), ((-.14, -.12, 1.58), (.14, .12, 2)),
                  ((-.23, -.1, 0), (-.05, .1, .95)), ((.05, -.1, 0), (.23, .1, .95)),
                  ((-.9, -.08, 1.42), (-.2, .08, 1.58)), ((.2, -.08, 1.42), (.9, .08, 1.58))])


class Eligibility(unittest.TestCase):
    def test_humanoid_is_free_and_eligible(self):
        self.assertTrue(rig_inspect.geometry(*human())['eligible'])

    def test_uncertain_shapes_warn_instead_of_guessing_anatomy(self):
        for bounds in [((-.5, -.5, 0), (.5, .5, 1)), ((-1, -.3, 0), (1, .3, .5))]:
            report = rig_inspect.geometry(*boxes([bounds]))
            self.assertTrue(report['eligible'])
            self.assertEqual(report['warnings'], [rig_inspect.SHAPE_WARNING, rig_inspect.CLOTHING_WARNING])
        p, t = human()
        report = rig_inspect.geometry([(x, z, y) for x, y, z in p], t)
        self.assertTrue(report['eligible'])
        self.assertIn('upright_body', report['shape_hints'])

    def test_separate_complete_characters_refuse_but_overlapping_clothes_do_not(self):
        p, t = human()
        with self.assertRaisesRegex(rig_inspect.RigError, 'Select just one character'):
            rig_inspect.selection([('body', p, t), ('other', [(x+5, y, z) for x, y, z in p], t)])
        rig_inspect.selection([('body', p, t), ('clothes', [(x*1.01, y*1.01, z) for x, y, z in p], t)])

    def test_empty_mesh_and_vertex_limit_still_refuse_without_charge(self):
        for points, triangles in [([], []), ([(0, 0, 0)] * 300001, [(0, 1, 2)])]:
            with self.assertRaisesRegex(rig_inspect.RigError, 'No credits used'):
                rig_inspect.geometry(points, triangles)

    def test_triangle_limit_and_nonfinite(self):
        p, t = human()
        with self.assertRaisesRegex(rig_inspect.RigError, '100,000'):
            rig_inspect.geometry(p, [t[0]] * 100001)
        p[0] = (float('nan'), 0, 0)
        with self.assertRaisesRegex(rig_inspect.RigError, 'unusable point positions'):
            rig_inspect.geometry(p, t)


class TransportCheck(unittest.TestCase):
    def test_exact_geometry_only_export_and_manifest_hashes(self):
        p, t = human()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'model.glb'
            rows = rig_transport.export(path, [('opaque', p, t)])
            doc = rig_transport.glb_json(path)
            self.assertFalse(set(doc) & {'skins', 'images', 'textures', 'materials', 'animations'})
            self.assertNotIn('name', doc['nodes'][0])
            self.assertEqual(rows[0]['position_sha256'], rig_transport.digest(
                b''.join(struct.pack('<3f', x, z, -y) for x, y, z in p)))
            self.assertEqual(rows[0]['topology_sha256'], rig_transport.digest(
                b''.join(struct.pack('<3I', *face) for face in t)))

    def test_artifacts_reject_duplicates_bad_hash_and_size(self):
        with self.assertRaises(rig_inspect.RigError):
            rig_bundle.artifacts([])
        with tempfile.TemporaryDirectory() as tmp:
            rows = []
            for name, (_, mime) in rig_bundle.LIMITS.items():
                path = Path(tmp) / name
                path.write_bytes(b'bad')
                rows.append({'name': name, 'mime': mime, 'path': str(path), 'size': 3, 'sha256': '0' * 64})
            with self.assertRaisesRegex(rig_inspect.RigError, 'damaged'):
                rig_bundle.artifacts(rows)


class AgentSurface(unittest.TestCase):
    def test_local_refusal_shows_no_price_and_does_not_upload_or_warm(self):
        from loopcut import tools
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Mock(cache_dir=Path(tmp))
            runtime.descriptors.return_value = [descriptor()]
            with patch.object(cloud, '_runtime', runtime), patch.object(cloud, 'sync_settings'), \
                    patch.object(cloud, '_progress'), patch.object(tools, '_editor_context', return_value=nullcontext()), \
                    patch.object(autorig, 'inspect', side_effect=autorig.RigError('Select a complete body. No credits used.')), \
                    patch.object(cloud_jobs.blobs, 'upload_missing') as upload, \
                    patch.object(cloud_account, 'authorize') as authorize:
                result = cloud.execute(cloud.PREFIX + 'auto_rig', {**inputs(), 'targets': ['Character']})
            self.assertFalse(result.ok)
            self.assertIn('Repair the character', result.text)
            self.assertNotIn('20', result.text)
            upload.assert_not_called()
            authorize.assert_not_called()

    def test_valid_uploads_start_quote_warmup_before_price_approval(self):
        events = []
        d = descriptor()
        client = Mock()
        def request(method, path, payload):
            self.assertEqual((method, path), ('POST', '/v1/quotes'))
            self.assertEqual(set(payload['files']), {'model', 'rig_task'})
            self.assertEqual(payload['tool_version'], 3)
            events.append('quote_and_server_warmup')
            return {'quote_id': 'q_early', 'credits': 20, 'expires_at': '2099-01-01T00:00:00Z'}
        def consent(details):
            self.assertEqual(events, ['both_validated_uploads', 'quote_and_server_warmup'])
            self.assertEqual(details['credits'], 20)
            from loopcut.rig_experimental import QUOTE
            self.assertEqual(details['rig_notice'], QUOTE)
            self.assertIn('20 credits', details['text'])
            events.append('price_approval')
            return False
        client.request.side_effect = request
        with tempfile.TemporaryDirectory() as tmp:
            files = {k: Path(tmp) / name for k, name in [('model', 'model.glb'), ('rig_task', 'rig_task.json')]}
            for path in files.values():
                path.write_bytes(b'local fixture')
            runtime = types.SimpleNamespace(cache_dir=Path(tmp), lock=threading.RLock(), quotes={},
                quote_requests={}, manifest={}, handle_error=Mock())
            with patch.object(cloud_jobs.blobs, 'upload_missing',
                              side_effect=lambda *a, **kw: events.append('both_validated_uploads')), \
                    patch.object(cloud_jobs, '_blob_multipart', return_value=False), \
                    cloud_account.approval_context(consent), self.assertRaises(cloud_http.CloudError) as caught:
                from loopcut.rig_experimental import QUOTE
                cloud_account.authorize(cloud_jobs, d, inputs(), files, project=PROJECT,
                    cancel=threading.Event(), runtime=runtime, client=client, notice=QUOTE, notice_first=True)
            self.assertEqual(caught.exception.code, 'approval_required')
            self.assertEqual(events, ['both_validated_uploads', 'quote_and_server_warmup', 'price_approval'])
            self.assertEqual(client.request.call_count, 1)  # No job or speculative warm route.

    def test_all_anatomy_refusals_keep_plain_reason_and_confirmed_no_charge(self):
        words = {'finger_count': 'finger layout', 'front_ambiguous': 'facing direction',
                 'pelvis_branches': 'hip and leg layout', 'too_few_joints': 'body structure', 'not_humanoid': 'body layout'}
        for reason, word in words.items():
            row = {**job('failed'), 'tool': 'auto_rig', 'outputs': [],
                'credits': {'reserved': 20, 'charged': 0, 'refunded': 20},
                'error': {'code': 'vendor_failed', 'type': 'vendor', 'message': 'private backend detail',
                          'rig_code': 'unsupported_anatomy', 'anatomy_reason': reason, 'refunded': 20}}
            with self.assertRaises(cloud_http.CloudError) as caught:
                cloud_jobs._raise_failed(row)
            message = cloud._error_message(caught.exception)
            self.assertIn(word, message)
            self.assertIn("no credits used", message)
            self.assertNotIn('private backend', message)
            self.assertNotIn('vendor_failed', message)
            # Production errors confirm refunds with a boolean; the numeric
            # credits remain in the job receipt even after the hold is returned.
            caught.exception.extra['refunded'] = True
            self.assertIn("no credits used", cloud._error_message(caught.exception))
            self.assertEqual(cloud.error_text(caught.exception), cloud._error_message(caught.exception))
        for reason in ('unknown', [], None):
            error = cloud_http.CloudError('private', 'vendor_failed', extra={
                'rig_code': 'unsupported_anatomy', 'anatomy_reason': reason, 'refunded': 20})
            self.assertIn('body layout', cloud._error_message(error))

    def test_anatomy_refusal_never_claims_unconfirmed_or_contradictory_refund(self):
        for refund in (None, False, -1, '20', float('nan')):
            error = cloud_http.CloudError('private', 'vendor_failed', extra={
                'rig_code': 'unsupported_anatomy', 'anatomy_reason': 'finger_count', 'refunded': refund})
            self.assertIn('charge unconfirmed', cloud._error_message(error))
        error.extra.update(refunded=20, job={'status': 'failed', 'credits': {'charged': 20}})
        self.assertIn('charge unconfirmed', cloud._error_message(error))

    def test_v2_admission_version_is_present_on_the_quote(self):
        d = descriptor()
        d['version'] = 2  # Legacy direct callers retain their explicit admission version.
        d['file_inputs'] = []
        client = Mock()
        client.request.return_value = {'quote_id': 'q_rig', 'credits': 20, 'expires_at': 'later'}
        with patch.object(cloud_jobs.blobs, 'upload_missing'), self.assertRaises(cloud_http.CloudError) as caught:
            cloud_jobs.run(d, {**inputs(), 'targets': ['Private name']}, {}, project=PROJECT, client=client, quote=True)
        self.assertEqual(caught.exception.code, 'quote_required')
        method, path, payload = client.request.call_args.args
        self.assertEqual((method, path, payload['tool_version']), ('POST', '/v1/quotes', 2))
        self.assertEqual(payload['input'], inputs())

    def test_job_states_always_get_beginner_facing_rig_progress(self):
        for state in ('queued', 'running'):
            row = {**job(state), 'tool': 'auto_rig', 'progress': {'pct': 10, 'message': 'backend internals'}}
            self.assertEqual(cloud_jobs._job_progress(row)['message'], autorig.STAGES[state])
            self.assertEqual(row['progress']['message'], 'backend internals')
        row['progress']['message'] = 'Downloading outputs'
        self.assertEqual(cloud_jobs._job_progress(row)['message'], 'Preparing rig')

    def test_inspection_reads_but_preview_and_removal_mutate(self):
        from loopcut import tools, workspace
        self.assertIn('character_rig', tools.CHANGES_SCENE)
        self.assertIn('character_rig', workspace.LIVE_TOOLS)
        self.assertTrue(tools.only_reads('character_rig', {'action': 'inspect'}))
        for action in ('preview', 'reset_preview', 'remove'):
            self.assertFalse(tools.only_reads('character_rig', {'action': action}))

    def test_generated_fields_hidden_and_not_required(self):
        d = descriptor()
        self.assertTrue(cloud._descriptor(d))
        params = cloud.schema(d)['function']['parameters']
        self.assertNotIn('model', params['properties'])
        self.assertNotIn('rig_task', params['required'])
        cloud._validate_arguments(d, {**inputs(), 'targets': ['Character']})
        with self.assertRaises(ValueError):
            cloud._validate_arguments(d, {**inputs(), 'targets': ['Character'], 'model': '/private/source.blend'})

    def test_local_targets_never_enter_job_input(self):
        d = descriptor()
        server = Transport(final={**job(), 'tool': 'auto_rig', 'outputs': []})
        server.final['provenance'].update(tool='auto_rig', kind='assist')
        with tempfile.TemporaryDirectory() as tmp:
            files = {}
            for field, suffix in [('model', 'glb'), ('rig_task', 'json')]:
                path = Path(tmp) / ('input.' + suffix)
                path.write_bytes(b'fake local fixture')
                files[field] = path
            with patch.object(cloud_jobs, '_redraw'), patch.object(cloud_jobs, '_blob_multipart', return_value=False):
                result = cloud_jobs.run(d, {**inputs(), 'targets': ['Private character name']}, files,
                                        project=PROJECT, client=server.client())
            self.assertEqual(result['status'], 'succeeded')
            self.assertEqual(server.posts[0]['input'], inputs())
            self.assertNotIn('Private character name', json.dumps(server.posts))

    def test_cancel_before_upload_and_failure_refund(self):
        cancel = threading.Event()
        cancel.set()
        server = Transport()
        with self.assertRaises(cloud_http.CloudError) as caught:
            cloud_jobs.run(descriptor(), inputs(), {}, project=PROJECT, cancel=cancel, client=server.client())
        self.assertEqual(caught.exception.code, 'canceled')
        self.assertFalse(server.calls)
        failed = {**job('failed'), 'tool': 'auto_rig', 'error': {'message': 'Rig worker failed',
                  'code': 'vendor_failed', 'type': 'vendor', 'refunded': 20},
                  'credits': {'reserved': 20, 'charged': 0, 'refunded': 20}}
        with self.assertRaises(cloud_http.CloudError) as caught:
            cloud_jobs._raise_failed(failed)
        self.assertIn('20', cloud.error_text(caught.exception))


def valid_sidecars():
    task = {'task_id': 'offline', 'model_sha256': '1' * 64, 'source_snapshot_sha256': '2' * 64,
            'skeleton_version': autorig.SKELETON_VERSION,
            'meshes': [{'mesh_id': 'mesh_0', 'vertex_count': 1, 'position_sha256': '3' * 64,
                        'topology_sha256': '4' * 64}]}
    joints = []
    for i in range(15):
        joints.append({'name': 'bone_' + str(i), 'parent': i - 1 if i else None, 'deform': True,
                       'semantic_role': None, 'head': [0, 0, i], 'tail': [0, .01, i],
                       'rest_matrix': [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, i, 0, 0, 0, 1]})
    rig = {'schema': 'loopcut.rig/0.1', 'task_id': task['task_id'], 'source_model_sha256': task['model_sha256'],
           'source_snapshot_sha256': task['source_snapshot_sha256'], 'model': {'code_commit': '5' * 40,
               'weights_revision': 'offline', 'weights_sha256': ['6' * 64]}, 'skeleton_version': autorig.SKELETON_VERSION,
           'coordinates': {'units': 'm', 'up': 'Z', 'space': 'blender_world'},
           'gltf_to_blender': rig_transport.TO_BLENDER, 'joints': joints, 'confidence': {'value': None, 'calibration': None},
           'canonical_plan': {'policy': 'mixamo-tokenrig-v1', 'labels': [
               {'index': i, 'source': j['name'], 'role': f'Joint{i}', 'name': f'mixamorig:Joint{i}'}
               for i, j in enumerate(joints)]},
           'warnings': [], 'meshes': [{'mesh_id': 'mesh_0', 'source_vertex_count': 1, 'source_vertex_weights': [[[0, 1.]]],
               'correspondence': {'method': 'exact', 'source_position_sha256': '3' * 64,
                                  'source_topology_sha256': '4' * 64, 'max_error_metres': 0}}]}
    report = {'schema': 'loopcut.rig-validation/0.1', 'task_id': task['task_id'], 'phase': 'worker', 'status': 'pass',
              'checks': [{'id': 'correspondence', 'status': 'pass', 'measured': 0, 'threshold': 1e-5,
                          'detail': '', 'mesh_id': None, 'vertex_indices': []}],
              'unweighted_vertices': 0, 'correspondence_valid': True,
              'preservation': dict.fromkeys(['geometry', 'uvs', 'materials', 'shape_keys', 'independent_drivers']),
              'timings': dict.fromkeys(['load_s', 'preprocess_s', 'inference_s', 'export_s', 'total_s'], 0),
              'peak_vram_bytes': 0, 'exit_code': 0, 'warnings': [], 'visual_approval': 'not_reviewed',
              'release_accepted': False}
    return rig, report, task


class BundleCheck(unittest.TestCase):
    def test_canonical_plan_missing_malformed_and_mismatched_labels_refuse(self):
        changes = [lambda r: r.pop('canonical_plan'),
                   lambda r: r['canonical_plan'].update(policy='unknown'),
                   lambda r: r['canonical_plan']['labels'].pop(),
                   lambda r: r['canonical_plan']['labels'][0].update(index=True),
                   lambda r: r['canonical_plan']['labels'][0].update(source='other'),
                   lambda r: r['canonical_plan']['labels'][0].update(role='Joint1', name='mixamorig:Joint1'),
                   lambda r: r['canonical_plan']['labels'][0].update(name='arbitrary'),
                   lambda r: r['canonical_plan']['labels'][0].update(extra=True)]
        for change in changes:
            rig, report, task = valid_sidecars()
            change(rig)
            with self.subTest(change=change), self.assertRaises(rig_inspect.RigError):
                rig_bundle.validate(rig, report, task)
        rig, report, task = valid_sidecars()
        local = {'labels': list(reversed(rig['canonical_plan']['labels']))}
        rig_bundle.match_plan(rig, local)
        local = copy.deepcopy(local)
        local['labels'][0].update(role='Head', name='mixamorig:Head')
        with self.assertRaisesRegex(rig_inspect.RigError, 'disagree'):
            rig_bundle.match_plan(rig, local)

    def test_valid_sidecars_match_exact_task(self):
        rig, report, task = valid_sidecars()
        self.assertIs(rig_bundle.validate(rig, report, task), rig)

    def test_foreign_task_and_fuzzy_correspondence_are_refused(self):
        for change in (lambda r: r.update(task_id='other'),
                       lambda r: r.update(source_model_sha256='9' * 64),
                       lambda r: r.update(skeleton_version='native'),
                       lambda r: r['meshes'][0]['correspondence'].update(method='nearest'),
                       lambda r: r['meshes'][0]['correspondence'].update(max_error_metres=.0001)):
            rig, report, task = valid_sidecars()
            change(rig)
            with self.assertRaises(rig_inspect.RigError):
                rig_bundle.validate(rig, report, task)

    def test_missing_duplicate_invalid_and_unweighted_influences_are_refused(self):
        for rows in ([], [[]], [[[0, .6]]], [[[0, .5], [0, .5]]], [[[True, 1.]]], [[[15, 1.]]], [[[0, float('nan')]]]):
            rig, report, task = valid_sidecars()
            rig['meshes'][0]['source_vertex_weights'] = rows
            with self.subTest(rows=rows), self.assertRaises(rig_inspect.RigError):
                rig_bundle.validate(rig, report, task)

    def test_invalid_hierarchy_names_and_rest_pose_are_refused(self):
        for change in (lambda j: j.update(parent=2), lambda j: j.update(name='bone_0'),
                       lambda j: j.update(name='é' * 40), lambda j: j.update(name='bad\x00name'),
                       lambda j: j.update(head=[float('nan'), 0, 0]), lambda j: j.update(tail=j['head']),
                       lambda j: j['rest_matrix'].__setitem__(0, 0)):
            rig, report, task = valid_sidecars()
            change(rig['joints'][1])
            with self.assertRaises(rig_inspect.RigError):
                rig_bundle.validate(rig, report, task)

    def test_worker_cannot_claim_local_preservation_or_visual_approval(self):
        for change in (lambda r: r.update(visual_approval='accepted'),
                       lambda r: r.update(release_accepted=True),
                       lambda r: r['preservation'].update(materials=True),
                       lambda r: r['checks'][0].update(status='fail'),
                       lambda r: r.update(extra='unsupported')):
            rig, report, task = valid_sidecars()
            change(report)
            with self.assertRaises(rig_inspect.RigError):
                rig_bundle.validate(rig, report, task)


if __name__ == '__main__':
    unittest.main()
