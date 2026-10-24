"""Full client rig lifecycle against HTTP + recorded M1 results, no paid calls.

Run with Loopcut --background --factory-startup --python-exit-code 1 --python
this_file -- --m1 PATH --out PATH, under the shared heavy_lock.
"""
import argparse
import copy
import json
import os
import struct
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

PROFILE = tempfile.TemporaryDirectory(prefix='loopcut-autorig-integration-')
profile = Path(PROFILE.name)
(profile / 'empty.env').touch()
os.environ.update(LOOPCUT_CONFIG_DIR=str(profile / 'config'), LOOPCUT_DATA_DIR=str(profile / 'data'),
                  LOOPCUT_ENV_FILE=str(profile / 'empty.env'), LOOPCUT_SERVICE_URL='http://127.0.0.1:9',
                  LOOPCUT_TELEMETRY='off')
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import checkout
checkout.use()
import bpy
from mathutils import Matrix, Vector
from loopcut import autorig, cloud, cloud_jobs, cloud_http, mainthread, motion_retarget
import autorig_fake as fake

RESULTS = []


def check(condition, message):
    RESULTS.append({'pass': bool(condition), 'message': message})
    print(('PASS ' if condition else 'FAIL ') + message, flush=True)
    if not condition:
        raise AssertionError(message)


def worker(fn, timeout=180):
    result = []
    def run():
        try:
            result.append(fn())
        except BaseException as ex:
            result.append(ex)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        mainthread._pump()
        time.sleep(.005)
    if thread.is_alive():
        raise TimeoutError('Offline rig job exceeded its test deadline')
    mainthread._pump()
    if isinstance(result[0], BaseException):
        raise result[0]
    return result[0]


def load(root, fid):
    autorig._prepared.clear()
    bpy.ops.wm.open_mainfile(filepath=str(root / 'prepared' / fid / 'original.blend'), load_ui=False, use_scripts=False)
    objects = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    autorig._select(objects)
    return objects


def run_job(service, arguments, files, cancel=None, progress=None):
    # Do not repair mismatched consent in the harness: the actual request and
    # immutable task must independently carry the same effective choice.
    assert arguments.get('accept_experimental', False) is json.loads(files['rig_task'].read_bytes()).get('accept_experimental', False)
    d = {**service.descriptor, 'backend': 'managed'}
    client = cloud_http.Client(base_url=service.url, api_key='lc_offline', client_headers={
        'x-loopcut-contract': '0.9', 'x-loopcut-client': 'loopcut-addon/test blender/test'})
    def run():
        try:
            cloud_jobs.run(d, arguments, files, project={'id': 'offline', 'no_generation': False}, client=client,
                           on_progress=progress, cancel=cancel)
        except cloud_http.CloudError as ex:
            if ex.code != 'quote_required':
                raise
            check(ex.extra['credits'] == 20, 'the ordinary quote path offers 20 fake credits')
            return cloud_jobs.run(d, {**arguments, 'quote_id': ex.extra['quote_id']}, files,
                project={'id': 'offline', 'no_generation': False}, client=client, cancel=cancel, on_progress=progress)
        raise AssertionError('A rig submitted without a quote')
    return worker(run)


def exercise(args, fid, edit=None, label=None, followons=False):
    label = label or fid
    objects = load(args.m1, fid)
    source_transform = edit(objects) if edit else None
    objects = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    names = [o.name for o in objects]
    before = autorig.fingerprint(objects)
    before_ids = {k: set(getattr(bpy.data, k).keys())
                  for k in ('objects', 'meshes', 'armatures', 'collections', 'shape_keys', 'materials')}
    before_selected = set(o.name for o in bpy.context.selected_objects)
    before_active = bpy.context.view_layer.objects.active.name
    # Shared material ID user counts change on staging; persistent settings do not.
    scratch = objects[0].data.copy()
    check(autorig.fingerprint(objects) == before, label + ': temporary copy does not invalidate the source snapshot')
    bpy.data.meshes.remove(scratch)
    pointers = [(o.as_pointer(), o.data.as_pointer()) for o in objects]
    directory = args.out / label
    directory.mkdir(parents=True, exist_ok=True)
    arguments = {**fake.inputs(), 'targets': names}
    files = autorig.prepare(arguments, directory / 'input')
    task = json.loads(files['rig_task'].read_bytes())
    preparation = autorig._prepared[task['task_id']]
    native = args.m1 / 'results' / fid / 'native' / 'rigged.glb'
    data = fake.bundle(task, preparation, native, directory / 'worker', source_transform)
    service = fake.Server(lambda task: data)
    try:
        progress = []
        job = run_job(service, arguments, files, progress=lambda p: progress.append(p['message']))
        check(autorig.STAGES['queued'] in progress, label + ': GPU wait is visible')
        check(any('Rigging' in p for p in progress), label + ': rigging progress is visible')
        summary = cloud_jobs.apply_result(service.descriptor, job)
        check('Standard rig added' in summary, label + ': original character applied')
        sidecar = Path(next(o['path'] for o in job['outputs'] if o['name'] == 'rig.json'))
        (directory / 'local-validation.json').write_bytes(sidecar.with_name('local-validation.json').read_bytes())
        arm = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE' and autorig._KEY in o)
        check(len(arm.data.bones) == (19 if fid == 'B2' else 52), label + ': canonical bone count')
        check(all(b.name.startswith('mixamorig:') for b in arm.data.bones), label + ': all bones have Mixamo names')
        check(motion_retarget.rig_of(arm) is not None, label + ': shipped retargeter detects the rig')
        check(bpy.context.view_layer.objects.active == arm and arm.show_in_front and arm.data.display_type == 'STICK',
              label + ': selected visible stick bones')
        check([(o.as_pointer(), o.data.as_pointer()) for o in objects] == pointers,
              label + ': actual original objects and mesh datablocks retained')
        check(all(any(m.type == 'ARMATURE' and m.object == arm for m in o.modifiers) for o in objects),
              label + ': every character part is bound')
        check(all(sum(g.weight for g in v.groups if o.vertex_groups[g.group].name in arm.data.bones) > .999
                  for o in objects for v in o.data.vertices), label + ': every original vertex weighted')
        check(service.charge == 20, label + ': one fake charge, no retry')
        from loopcut.merge import _undo
        check(_undo('undo'), label + ': one Undo succeeds')
        objects = [bpy.data.objects[n] for n in names]
        check(autorig.fingerprint(objects) == before, label + ': one Undo restores exact protected original state')
        check(all(set(getattr(bpy.data, k).keys()) == v for k, v in before_ids.items()),
              label + ': one Undo restores the complete object/datablock inventory')
        check(set(o.name for o in bpy.context.selected_objects) == before_selected
              and bpy.context.view_layer.objects.active.name == before_active,
              label + ': one Undo restores the original selection')
        check(_undo('redo'), label + ': Redo restores the rig')
        objects = [bpy.data.objects[n] for n in names]
        arm = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE' and autorig._KEY in o)
        if followons:
            autorig.preview(arm.name)
            autorig.preview(arm.name, reset=True)
            bpy.ops.object.mode_set(mode='OBJECT')
            check(all(pb.matrix_basis == Matrix.Identity(4) for pb in arm.pose.bones), label + ': free preview resets')
            fixtures = HERE.parent / 'tests' / 'fixtures' / 'motion'
            clips = [('motion_from_text', fixtures / 'kimodo_walk_60.bvh'),
                     ('motion_from_video', fixtures / 'gemx_dance_60.bvh')]
            for kind, path in clips:
                before_motion = autorig._evaluated(objects)
                line = cloud_jobs._apply_motion({'id': 'offline_' + kind, 'tool': kind, 'outputs': [{'name': 'motion.bvh',
                    'mime': 'application/x-bvh', 'path': str(path)}],
                    'apply_args': {'target_armature': arm.name}, 'input': {'prompt': 'Offline ' + kind}})
                bpy.context.scene.frame_set(bpy.context.scene.frame_start + 12)
                after_motion = autorig._evaluated(objects)
                check(max((p - q).length for a, b in zip(before_motion, after_motion) for p, q in zip(a, b)) > .001,
                      label + ': ' + kind + ' motion clip moves the mesh via shipped retargeter')
                print(line, flush=True)
            autorig._select([arm, *objects])
            fbx = directory / 'character.fbx'
            bpy.ops.export_scene.fbx(filepath=str(fbx), use_selection=True, add_leaf_bones=False, bake_anim=False)
            previous = set(bpy.data.objects)
            bpy.ops.import_scene.fbx(filepath=str(fbx))
            imported = [o for o in bpy.data.objects if o not in previous]
            exported_arm = next(o for o in imported if o.type == 'ARMATURE')
            check(set(exported_arm.data.bones.keys()) == set(arm.data.bones.keys()), label + ': FBX round trip keeps Mixamo names')
            for ob in imported:
                bpy.data.objects.remove(ob, do_unlink=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(directory / 'rigged.blend'), check_existing=False)
        autorig.remove(arm.name)
        check(autorig.fingerprint([bpy.data.objects[n] for n in names]) == before,
              label + ': explicit remove restores exact protected original state')
        check(not any(o.type == 'ARMATURE' for o in bpy.context.scene.objects), label + ': no rig remains after removal')
    finally:
        service.close()


def failures(args):
    objects = load(args.m1, 'G3')
    original = autorig.fingerprint(objects)
    arguments = {**fake.inputs(), 'targets': [o.name for o in objects], 'accept_experimental': False}
    files = autorig.prepare(arguments, args.out / 'G3-input')
    from loopcut import rig_plan, rig_transport
    joints, points, _, _ = rig_transport.skin(args.m1 / 'results/G3/native/rigged.glb')
    planned = rig_plan.plan_json({'schema_version': 1,
        'joints': [{'name': j['name'], 'parent': j['parent'], 'head': list(j['head'])} for j in joints],
        'vertices': [list(p) for p in points]})
    check(planned['status'] == 'refused' and planned['anatomy_reason'] == 'pelvis_branches',
          'G3 animal is refused by the unchanged worker planner')
    service = fake.Server(lambda task: (_ for _ in ()).throw(AssertionError('No dog rig outputs')))
    service.failure, service.anatomy_reason = True, planned['anatomy_reason']
    try:
        try:
            run_job(service, arguments, files)
        except cloud_http.CloudError as ex:
            check("no credits used" in cloud._error_message(ex) and service.charge == 0,
                  'G3 animal refusal reports no charge')
            check(autorig.fingerprint(objects) == original, 'G3 refused original remains unchanged')
        else:
            raise AssertionError('G3 passed the worker humanoid check')
    finally:
        service.close()

    objects = load(args.m1, 'B2')
    d = args.out / 'failures'
    files = autorig.prepare({**fake.inputs(), 'targets': [o.name for o in objects]}, d / 'input')
    task = json.loads(files['rig_task'].read_bytes())
    preparation = autorig._prepared[task['task_id']]
    data = fake.bundle(task, preparation, args.m1 / 'results/B2/native/rigged.glb', d / 'worker')
    arguments = {**fake.inputs(), 'targets': [o.name for o in objects]}
    baseline = autorig.fingerprint(objects)
    object_names = set(bpy.data.objects.keys())
    service = fake.Server(lambda task: data)
    try:
        service.failure = True
        try:
            run_job(service, arguments, files)
        except cloud_http.CloudError as ex:
            check('20 credits returned' in cloud.error_text(ex)
                  and ex.extra.get('refunded') is True
                  and ex.extra['job']['credits']['refunded'] == 20
                  and ex.extra['job']['credits']['charged'] == 0,
                  'worker failure reports its confirmed 20-credit refund')
        else:
            raise AssertionError('worker failure was accepted')
        service.failure = False
        cancel = threading.Event()
        try:
            run_job(service, arguments, files, cancel=cancel,
                    progress=lambda p: cancel.set() if autorig.STAGES['queued'] == p['message'] else None)
        except cloud_http.CloudError as ex:
            check(ex.code == 'canceled' and ex.extra['job']['credits']['refunded'] == 20,
                  'cancel during queue refunds the hold')
        else:
            raise AssertionError('cancel did not interrupt the job')
        check(service.charge == 0, 'failure and cancellation use zero fake credits')
        service.corrupt = True
        try:
            run_job(service, arguments, files)
        except cloud_http.CloudError as ex:
            check(ex.code == 'hash_mismatch', 'corrupted download is rejected')
            corrupt_job = ex.extra['job']
        else:
            raise AssertionError('corrupted download was accepted')
        refund = worker(lambda: cloud_jobs.wait_rig_refund(corrupt_job['id']))
        check(refund['status'] == 'refunded' and service.charge == 0,
              'corrupted rig delivery receives a confirmed refund without applying')
        service.corrupt = False
        job = run_job(service, arguments, files)
        objects[0].data.vertices[0].co.x += .01
        try:
            cloud_jobs.apply_result(service.descriptor, job)
        except cloud_http.CloudError as ex:
            check(ex.code == 'source_changed', 'source edit during job refuses apply')
        else:
            raise AssertionError('changed source was rigged')
        refund = worker(lambda: cloud_jobs.wait_rig_refund(job['id']))
        check(refund['status'] == 'refunded' and refund['job']['credits']['charged'] == 0
              and refund['job']['credits']['refunded'] == 20,
              'changed-source failure receives a confirmed refund on the original job')
        check(refund['job']['outputs'] == job['outputs'], 'refund preserves verified recovery artifact paths')
        objects[0].data.vertices[0].co.x -= .01
        # Restore the exact float32 value for the hash, avoiding arithmetic drift.
        load(args.m1, 'B2')
        objects = [bpy.data.objects[n] for n in arguments['targets']]
        preparation['objects'] = [(o.as_pointer(), o.data.as_pointer()) for o in objects]
        preparation['scene'] = bpy.context.scene.as_pointer()
        autorig._prepared[task['task_id']] = preparation
        wrong = copy.deepcopy(job)
        sidecar = next(o for o in wrong['outputs'] if o['name'] == 'rig.json')
        bad = json.loads(Path(sidecar['path']).read_bytes())
        bad['source_model_sha256'] = '0' * 64
        payload = json.dumps(bad).encode()
        path = d / 'wrong-rig.json'
        path.write_bytes(payload)
        sidecar.update(path=str(path), size=len(payload), sha256=__import__('hashlib').sha256(payload).hexdigest())
        try:
            cloud_jobs.apply_result(service.descriptor, wrong)
        except cloud_http.CloudError as ex:
            check(ex.code == 'invalid_bundle', 'hash-valid but mismatched bundle is rejected')
        else:
            raise AssertionError('mismatched bundle accepted')
        with patch.object(autorig, '_hinges', side_effect=autorig.RigError('Injected hinge failure', 'validation_failed')):
            try:
                cloud_jobs.apply_result(service.descriptor, job)
            except cloud_http.CloudError as ex:
                check(ex.code == 'validation_failed' and 'no refund has been confirmed' in str(ex),
                      'failed derived-rig validation rolls back and reports actual settlement')
            else:
                raise AssertionError('failed hinge validation was committed')
        check(autorig.fingerprint(objects) == baseline and set(bpy.data.objects.keys()) == object_names,
              'all failed applications leave the original scene intact')
        check(service.reports[job['id']]['reason'] == 'other'
              and len([p for method, p, body in service.calls if p == '/v1/jobs/' + job['id'] + '/apply-failed']) == 1,
              'repeated apply failures reuse the first immutable receipt without another refund request')
    finally:
        service.close()


def transformed(objects):
    transform = Matrix.Translation((1., -2., .3)) @ Matrix.Rotation(.7, 4, 'Z') @ Matrix.Rotation(.2, 4, 'X') @ Matrix.Scale(1.7, 4)
    for ob in objects:
        ob.matrix_world = transform @ ob.matrix_world
    bpy.context.view_layer.update()
    return transform


def nonuniform(objects):
    transform = Matrix.Translation((-1., 2., .1)) @ Matrix.Rotation(.8, 4, 'Z') @ Matrix.Diagonal((1.3, .8, 1.7, 1.))
    for ob in objects:
        ob.matrix_world = transform @ ob.matrix_world
    bpy.context.view_layer.update()
    return transform


def protected_features(objects):
    ob = objects[0]
    if not ob.data.shape_keys:
        ob.shape_key_add(name='Basis')
    key = ob.shape_key_add(name='Smile stays local')
    key.data[0].co.x += .002
    key.value = 0
    driver = key.driver_add('value').driver
    driver.type = 'AVERAGE'
    variable = driver.variables.new()
    variable.name = 'smile'
    variable.type = 'SINGLE_PROP'
    variable.targets[0].id = ob
    ob['smile'] = .4
    variable.targets[0].data_path = '["smile"]'
    modifier = ob.modifiers.new('Keep subdivision', 'SUBSURF')
    modifier.levels = 1
    modifier.render_levels = 1
    group = ob.vertex_groups.new(name='Keep mask')
    group.add([0], .4, 'REPLACE')
    bpy.context.view_layer.update()
    return None


def multiple(objects):
    ob = max(objects, key=lambda o: len(o.data.polygons))
    autorig._select([ob])
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='DESELECT')
    bpy.ops.object.mode_set(mode='OBJECT')
    for face in list(ob.data.polygons)[:len(ob.data.polygons) // 2]:
        face.select = True
    bpy.context.tool_settings.mesh_select_mode = (False, False, True)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.separate(type='SELECTED')
    bpy.ops.object.mode_set(mode='OBJECT')
    check(len([o for o in bpy.context.scene.objects if o.type == 'MESH']) >= 2, 'multi-mesh fixture has separate parts')
    return None


def preflight_failures(args):
    objects = load(args.m1, 'B2')
    arm_data = bpy.data.armatures.new('Existing rig')
    arm = bpy.data.objects.new('Existing rig', arm_data)
    bpy.context.scene.collection.objects.link(arm)
    mod = objects[0].modifiers.new('Existing rig', 'ARMATURE')
    mod.object = arm
    try:
        autorig.inspect([o.name for o in objects])
    except autorig.RigError as ex:
        check(ex.code == 'already_rigged' and 'No credits used' in str(ex), 'already-rigged character refuses free')
    else:
        raise AssertionError('Existing rig was overwritten')
    objects = load(args.m1, 'B2')
    # Real geometry above the limit, without an expensive subdivision evaluation.
    p = [(0., 0., 0.), (1., 0., 0.), (0., 1., 0.)]
    mesh = bpy.data.meshes.new('Over limit')
    mesh.from_pydata(p, [], [(0, 1, 2)] * 100001)
    ob = bpy.data.objects.new('Over limit', mesh)
    bpy.context.scene.collection.objects.link(ob)
    try:
        autorig.inspect([ob.name])
    except autorig.RigError as ex:
        check(ex.code == 'triangle_limit' and 'No credits used' in str(ex), 'over-limit character refuses before upload')
    else:
        raise AssertionError('Over-limit mesh was accepted')


def transport_boundaries(args):
    from loopcut import rig_transport
    directory = args.out / 'transport'
    directory.mkdir(exist_ok=True)
    for component, code, one in [(5121, 'B', 255), (5123, 'H', 65535)]:
        doc = {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': [0, 1]}],
               'nodes': [{'mesh': 0, 'skin': 0}, {'name': 'root'}], 'bufferViews': [], 'accessors': []}
        binary = bytearray()
        def accessor(values, fmt, component_type, kind, count, normalized=False):
            start = len(binary)
            binary.extend(struct.pack('<' + fmt, *values))
            view = len(doc['bufferViews'])
            doc['bufferViews'].append({'buffer': 0, 'byteOffset': start, 'byteLength': len(binary) - start})
            binary.extend(b'\0' * (-len(binary) % 4))
            index = len(doc['accessors'])
            doc['accessors'].append({'bufferView': view, 'componentType': component_type, 'count': count,
                                     'type': kind, 'normalized': normalized})
            return index
        position = accessor([0, 0, 0, 1, 0, 0, 0, 1, 0], '9f', 5126, 'VEC3', 3)
        joint = accessor([0] * 12, '12B', 5121, 'VEC4', 3)
        weight = accessor([one, 0, 0, 0] * 3, '12' + code, component, 'VEC4', 3, True)
        indices = accessor([0, 1, 2], '3I', 5125, 'SCALAR', 3)
        bind = accessor([v for row in Matrix.Identity(4) for v in row], '16f', 5126, 'MAT4', 1)
        doc['skins'] = [{'joints': [1], 'inverseBindMatrices': bind}]
        doc['meshes'] = [{'primitives': [{'attributes': {'POSITION': position, 'JOINTS_0': joint, 'WEIGHTS_0': weight},
                                         'indices': indices}]}]
        doc['buffers'] = [{'byteLength': len(binary)}]
        def save():
            header = rig_transport.json_bytes(doc)
            header += b' ' * (-len(header) % 4)
            path = directory / (str(component) + '.glb')
            path.write_bytes(struct.pack('<4sII', b'glTF', 2, 28 + len(header) + len(binary))
                             + struct.pack('<II', len(header), 0x4e4f534a) + header
                             + struct.pack('<II', len(binary), 0x004e4942) + binary)
            return path
        _, _, _, weights = rig_transport.skin(save())
        check(weights == [{0: 1.}] * 3, 'normalized ' + code + ' skin weights decode without loss')
        doc['accessors'][joint]['normalized'] = True
        try:
            rig_transport.skin(save())
        except ValueError:
            check(True, 'normalized JOINTS are refused before scene mutation')
        else:
            raise AssertionError('Normalized joint indices accepted')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--m1', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--only', default='')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.only:
        transport_boundaries(args)
    for fid in ['B1', 'B2', 'R2', 'R4']:
        if not args.only or args.only == fid:
            exercise(args, fid, followons=fid == 'B2')
    if not args.only:
        failures(args)
        preflight_failures(args)
        exercise(args, 'B2', transformed, 'transformed')
        exercise(args, 'B2', nonuniform, 'nonuniform')
        exercise(args, 'B2', protected_features, 'protected')
        exercise(args, 'B1', multiple, 'multi-mesh')
    (args.out / 'integration-results.json').write_text(json.dumps(RESULTS, indent=2))
    print(f'PASS {len(RESULTS)} autorig integration checks', flush=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
