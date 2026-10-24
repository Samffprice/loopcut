"""Replay M2 support regressions through the typed client bundle; strictly $0.

Run under heavy_lock with --m1, --eval and --out. The default also reruns the
complete M1 integration suite; --skip-m1 isolates the added M2 cases.
"""
import argparse
import hashlib
from itertools import product
import json
import math
from pathlib import Path
import sys
import traceback
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autorig_integration as base
from loopcut import autorig, rig_bundle, rig_plan, rig_transport, rig_experimental, rig_inspect
import bpy
from mathutils import Vector

REFUSED = {'B5': 'pelvis_branches', 'Q1': 'pelvis_branches', 'Q2': 'pelvis_branches',
           'P2': 'too_few_joints', 'R18': 'too_few_joints'}
FIXTURES = ({f'B{i}' for i in range(1, 9)} | {f'R{i}' for i in range(10, 19)}
            | {'R2', 'R4', 'P2', 'Q1', 'Q2'})
TYPED_CASES = {fid: REFUSED.get(fid, 'applied') for fid in sorted(FIXTURES)}


def capture(objects, report, path, camera=None, side=False, framing_points=None, camera_offset=None):
    """Neutral-shaded rest/walk samples for visual review, never quality scores."""
    scene = bpy.context.scene
    if camera is None:
        points = framing_points or [p for mesh in autorig._evaluated(objects) for p in mesh]
        low = Vector(tuple(min(p[k] for p in points) for k in range(3)))
        high = Vector(tuple(max(p[k] for p in points) for k in range(3)))
        target, span = (low + high) * .5, max(high - low)
        frame = (report['canonicalization']['frame'] if report['canonicalization'] else
                 {'front': (0, -1, 0), 'left': (1, 0, 0), 'up': (0, 0, 1)})
        front, left, up = [Vector(frame[k]).normalized()
                           for k in ('front', 'left', 'up')]
        data = bpy.data.cameras.new('Regression review')
        camera = bpy.data.objects.new(data.name, data)
        scene.collection.objects.link(camera)
        direction = Vector(camera_offset) if camera_offset else ((left * 2.8 + up * .1) if side
                                    else (front * 2.8 + left * .8 + up * .25))
        camera.location = target + direction * span
        rotation = (target - camera.location).to_track_quat('-Z', 'Y')
        camera.rotation_euler = rotation.to_euler()
        right, vertical = rotation @ Vector((1, 0, 0)), rotation @ Vector((0, 1, 0))
        half_span = max(abs((p - target).dot(axis)) for p in points for axis in (right, vertical))
        data.type, data.ortho_scale = 'ORTHO', half_span * 2.24
        data.clip_start, data.clip_end = max(span * .001, 1e-6), max(span * 30, 1.)
        scene.camera = camera
        scene.render.engine = 'BLENDER_WORKBENCH'
        scene.display.shading.light = 'STUDIO'
        scene.display.shading.color_type = 'SINGLE'
        scene.display.shading.single_color = (.64, .7, .78)
        scene.display.shading.background_type = 'VIEWPORT'
        scene.display.shading.background_color = (.035, .035, .035)
        scene.render.resolution_x = scene.render.resolution_y = 640
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'PNG'
        scene.render.film_transparent = False
    scene.render.filepath = str(path)
    scene.camera = camera
    bpy.ops.render.render(write_still=True)
    return camera


def typed_case(args, fid, expected):
    directory = args.out / fid
    directory.mkdir(parents=True, exist_ok=True)
    row = {'fixture': fid, 'expected': expected, 'real_spend_usd': 0,
           'test_only_preflight_bypass': False, 'production_pass': False}
    try:
        _typed_case(args, fid, expected, row)
    except Exception as ex:
        row.update(unexpected_error=str(ex), production_pass=False)
        raise
    finally:
        (directory / 'result.json').write_text(json.dumps(row, indent=2))


def _typed_case(args, fid, expected, row):
    objects = base.load(args.eval, fid)
    names = [o.name for o in objects]
    fingerprint = autorig.fingerprint(objects)
    inventory = {k: set(getattr(bpy.data, k).keys())
                 for k in ('objects', 'meshes', 'armatures', 'collections', 'shape_keys', 'materials')}
    directory = args.out / fid
    directory.mkdir(parents=True, exist_ok=True)
    row['preflight'] = autorig.inspect(names)[3]
    base.check(row['preflight']['eligible'], fid + ': ordinary local admission passes without bypass')
    if fid in {'B4', 'B7', 'R12', 'R13', 'R16'}:
        base.check(bool(row['preflight']['warnings']), fid + ': unusual shape gets a soft quote warning')
    arguments = {**base.fake.inputs(), 'targets': names, 'accept_experimental': True}
    files = autorig.prepare(arguments, directory / 'input')
    coat = (rig_inspect.CLOTHING_WARNING in row['preflight']['warnings']
            and 'upright_body' not in row['preflight']['shape_hints'])
    base.check(autorig.quote_notice(directory / 'input') == (rig_inspect.CLOTHING_WARNING if coat else rig_experimental.QUOTE, False),
               fid + ': quote warning belongs to this exact preparation')
    task = json.loads(files['rig_task'].read_bytes())
    preparation = autorig._prepared[task['task_id']]
    if expected != 'applied':
        joints, points, _, _ = rig_transport.skin(args.eval / 'results' / fid / 'native/rigged.glb')
        native = rig_plan.plan_json({'schema_version': 1,
            'joints': [{'name': j['name'], 'parent': j['parent'], 'head': list(j['head'])} for j in joints],
            'vertices': [list(p) for p in points]})
        row['native_planner'] = native
        base.check(native['status'] == 'refused' and native['anatomy_reason'] == expected,
                   fid + ': decoded native rig refuses inside Blender with the expected reason')
    review = json.loads((args.eval / 'review' / fid / 'review.json').read_bytes()) if fid in REFUSED else None
    data = base.fake.bundle(task, preparation, args.eval / 'results' / fid / 'native/rigged.glb',
                            directory / 'worker', inside_fraction=review['raw_bones_inside']['fraction'] if review else None)
    row['adversarial_label_plan'] = False
    if fid == 'B8':
        rig = json.loads(data['rig.json'])
        rig_bundle.validate(rig, json.loads(data['validation.json']), task)
        collection = bpy.data.collections.new('Tiny typed-rig check')
        bpy.context.scene.collection.children.link(collection)
        arm = autorig._armature(rig, collection, preparation['to_world'])
        unit = bpy.context.scene.unit_settings.scale_length
        error = max((arm.matrix_world @ arm.data.bones[j['name']].head_local
                     - preparation['to_world'] @ Vector(j['head'])).length
                    for j in rig['joints']) * unit
        lengths = [(arm.matrix_world @ b.tail_local - arm.matrix_world @ b.head_local).length * unit
                   for b in arm.data.bones]
        row['direct_typed_armature'] = {'max_head_error_metres': error,
            'max_length_metres': max(lengths), 'min_length_metres': min(lengths)}
        base.check(error < 1e-7 and row['direct_typed_armature']['max_length_metres'] < .004,
                   'B8: direct typed armature avoids the GLB importer 4 mm floor')
        arm_data = arm.data
        bpy.data.objects.remove(arm, do_unlink=True)
        bpy.data.armatures.remove(arm_data)
        bpy.data.collections.remove(collection)
        autorig._select(objects)
        row['correspondence'] = 'Exact original correspondence passes with the client geometry-only exporter.'
    service = base.fake.Server(lambda task: data)
    try:
        job = base.run_job(service, arguments, files)
        if fid in REFUSED:
            import autorig_experimental_check
            autorig_experimental_check.exercise(base, capture, job, service, objects, fingerprint,
                                                inventory, preparation, directory, row)
            return
        try:
            details = {}
            row['message'] = base.cloud_jobs.apply_result(service.descriptor, job, details=details)
            row['outcome'] = 'applied'
        except base.cloud_http.CloudError as ex:
            row.update(outcome=ex.code, message=str(ex))
            base.check('no refund has been confirmed' in str(ex), fid + ': actual settlement is disclosed')
            base.check(autorig.fingerprint(objects) == fingerprint
                       and all(set(getattr(bpy.data, k).keys()) == value for k, value in inventory.items()),
                       fid + ': refusal preserves original state and complete ID inventory')
            refund = base.worker(lambda: base.cloud_jobs.wait_rig_refund(job['id']))
            row['refund'] = {'status': refund['status'], 'credits': refund['job']['credits'],
                             'message': refund['message']}
            base.check(refund['status'] == 'refunded' and refund['job']['credits']['charged'] == 0
                       and service.charge == 0, fid + ': precise refusal receives a confirmed refund')
            row['production_pass'] = False
            raise AssertionError(fid + ': planner accepted but local apply refused: ' + str(ex)) from ex
        else:
            base.check(expected == 'applied', fid + ': typed rig accepted only as expected')
            sidecar = Path(next(o['path'] for o in job['outputs'] if o['name'] == 'rig.json'))
            (directory / 'local-validation.json').write_bytes(sidecar.with_name('local-validation.json').read_bytes())
            arm = bpy.context.view_layer.objects.active
            base.check(row['message'].startswith('Standard rig added:'),
                       fid + ': standard result resolves any local shape warning')
            base.check(details['rig']['armature'] == arm.name and details['rig']['meshes'] == names,
                       fid + ': model receives the exact armature and skinned mesh object names')
            row['model_rig_details'] = details['rig']
            before = autorig._evaluated(objects)
            report = json.loads((directory / 'local-validation.json').read_bytes())
            capture(objects, report, directory / 'rest.png')
            capture(objects, report, directory / 'rest-side.png', side=True)
            bpy.ops.wm.save_as_mainfile(filepath=str(directory / 'canonical.blend'), check_existing=False)
            base.cloud_jobs._apply_motion({'id': 'offline_eval_walk', 'tool': 'motion_from_text', 'outputs': [{
                'name': 'motion.bvh', 'mime': 'application/x-bvh',
                'path': str(base.HERE.parent / 'tests/fixtures/motion/kimodo_walk_60.bvh')}],
                'apply_args': {'target_armature': arm.name}, 'input': {'prompt': 'Offline regression walk'}})
            movement, bounds = [], []
            row['visual_samples'] = ['rest.png', 'rest-side.png']
            for frame in (1, 13, 25, 37):
                bpy.context.scene.frame_set(bpy.context.scene.frame_start + frame - 1)
                after = autorig._evaluated(objects)
                base.check(all(math.isfinite(v) for mesh in after for p in mesh for v in p),
                           fid + f': frame {frame} has finite evaluated skin')
                movement.append(max((p - q).length for a, b in zip(before, after) for p, q in zip(a, b)))
                points = [p for mesh in after for p in mesh]
                bounds += [Vector(tuple(min(p[k] for p in points) for k in range(3))),
                           Vector(tuple(max(p[k] for p in points) for k in range(3)))]
            # Root motion can leave a camera fitted to the resting pose. Fit one
            # shared walk camera to the union of all sampled poses, including
            # the corners of that bound, then render each pose with that camera.
            corners = [Vector(p) for p in product(*[(min(p[k] for p in bounds), max(p[k] for p in bounds))
                                                    for k in range(3)])]
            camera = None
            for frame in (1, 13, 25, 37):
                bpy.context.scene.frame_set(bpy.context.scene.frame_start + frame - 1)
                filename = 'walk.png' if frame == 13 else f'walk-{frame}.png'
                camera = capture(objects, report, directory / filename, camera, framing_points=corners)
                row['visual_samples'].append(filename)
            base.check(max(movement) > report['canonicalization']['height'] * .001,
                       fid + ': shipped retargeter moves the accepted original at its own scale')
            row.update(motion_max_displacement=max(movement), labels=len(report['canonicalization']['labels']),
                       hands=report['canonicalization']['hands'])
            bpy.context.scene.frame_set(bpy.context.scene.frame_start + 12)
            bpy.ops.wm.save_as_mainfile(filepath=str(directory / 'walk.blend'), check_existing=False)
            autorig.remove(arm.name)
            base.check(autorig.fingerprint(objects) == fingerprint, fid + ': removal restores the protected original')
            row.update(original_preserved=True, production_pass=not row['test_only_preflight_bypass'])
        row['fake_credits'] = service.charge
    finally:
        service.close()
        if service.contract:
            proof = service.contract.proof()
            (directory / 'contract-proof.json').write_text(json.dumps(proof, indent=2))
            required = {'rig-task', 'quote-request', 'quote', 'job-create-request', 'job', 'rig', 'rig-result', 'rig-validation'}
            base.check(required <= proof['schemas_checked'].keys()
                       and len(proof['quote_job_bindings']) == 1
                       and all(not r['errors'] for r in proof['records']), fid + ': actual loopback payloads pass platform schemas')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--m1', type=Path, required=True)
    parser.add_argument('--eval', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--skip-m1', action='store_true')
    parser.add_argument('--only', choices=tuple(TYPED_CASES))
    parser.add_argument('--require-contract', action='store_true')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    if args.require_contract:
        base.fake.autorig_contract.from_environment(required=True)
    args.out.mkdir(parents=True, exist_ok=True)
    import autorig_experimental_check
    autorig_experimental_check.contract_axes(base)
    if not args.skip_m1:
        original_argv = sys.argv
        sys.argv = [sys.argv[0], '--', '--m1', str(args.m1), '--out', str(args.out / 'm1')]
        try:
            base.main()
        finally:
            sys.argv = original_argv
        base.RESULTS.clear()
    planners = []
    paths = sorted((args.eval / 'regressions').glob('*/case.json'))
    base.check({p.parent.name for p in paths} == FIXTURES, 'all 22 final eval regression manifests are present')
    for path in paths:
        case = json.loads(path.read_bytes())
        fid = path.parent.name
        for artifact in [*case['artifacts'].values(), case['planner_input']]:
            actual = args.eval / artifact['path']
            base.check(actual.resolve().is_relative_to(args.eval.resolve())
                       and hashlib.sha256(actual.read_bytes()).hexdigest() == artifact['sha256'],
                       fid + ': retained ' + artifact['path'] + ' hash matches')
        manifest = json.loads((args.eval / case['artifacts']['prepared']['path']).read_bytes())
        original = manifest['artifacts']['original']
        base.check(hashlib.sha256((args.eval / 'prepared' / fid / original['path']).read_bytes()).hexdigest()
                   == original['sha256'], fid + ': protected original hash matches')
        value = json.loads((args.eval / case['planner_input']['path']).read_bytes())
        planned = rig_plan.plan_json({'schema_version': 1, **value})
        outcome = 'pass' if planned['status'] == 'planned' else planned['anatomy_reason']
        base.check(outcome == REFUSED.get(fid, 'pass'), fid + ': planner support policy')
        if outcome == 'pass':
            base.check(len(planned['canonical_plan']['labels']) == len(value['joints']), fid + ': retain every joint')
        directory = args.out / fid
        directory.mkdir(exist_ok=True)
        (directory / 'planner-result.json').write_text(json.dumps(planned, indent=2, allow_nan=False))
        planners.append({'fixture': fid, 'outcome': outcome, 'joints': len(value['joints'])})
    (args.out / 'planner-results.json').write_text(json.dumps(planners, indent=2))
    failures = []
    for fid, expected in TYPED_CASES.items():
        if not args.only or args.only == fid:
            try:
                typed_case(args, fid, expected)
            except Exception as ex:
                traceback.print_exc()
                failures.append({'fixture': fid, 'error': str(ex)})
    (args.out / 'unexpected-failures.json').write_text(json.dumps(failures, indent=2))
    (args.out / 'regression-results.json').write_text(json.dumps(base.RESULTS, indent=2))
    if failures:
        raise AssertionError(f'{len(failures)} fixture failures; see unexpected-failures.json')
    if not args.only:
        persona_starts(args)
    print(f'PASS {len(base.RESULTS)} M2 client regression checks', flush=True)


def persona_starts(args):
    """Load all four pinned task starts in Blender without any agent or cloud calls."""
    sys.path.insert(0, str(Path(__file__).parent / 'evals'))
    import tasks
    from evidence import task_manifest
    records = []
    for suffix, fixture in [('generated_adult', 'R10'), ('clothed_multimesh', 'B7'), ('robe', 'R13'), ('dog', 'G3')]:
        task = tasks.BY_ID['autorig_' + suffix]
        task.setup()
        meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
        base.check(bool(meshes) and not any(o.type == 'ARMATURE' for o in bpy.context.scene.objects),
                   task.id + ': hash-checked unrigged start loads')
        base.check(not task.check(None), task.id + ': light UX safeguards pass untouched')
        if fixture in {'R10', 'B7'}:
            base.check((len(meshes) == 1) if fixture == 'R10' else len(meshes) > 1,
                       task.id + ': single/multiple mesh start matches brief')
        inspection = autorig.inspect([o.name for o in meshes])[3]
        records.append({'task': task.id, 'fixture': fixture, 'meshes': len(meshes),
            'description': bpy.context.scene['autorig_persona_scene_description'],
            'source_sha256': bpy.context.scene['autorig_persona_source_sha256'],
            'manifest': task_manifest(task), 'inspection': inspection})
    (args.out / 'persona-starts.json').write_text(json.dumps(records, indent=2))
    (args.out / 'regression-results.json').write_text(json.dumps(base.RESULTS, indent=2))


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        base.os._exit(1)
