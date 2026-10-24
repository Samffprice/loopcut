"""Blender proof for native experimental rigs and ordinary four-leg keyframing."""
import json
import math
import os
from pathlib import Path
from unittest.mock import patch

import bpy
from mathutils import Matrix, Quaternion, Vector
from loopcut import autorig, rig_canonicalize, rig_experimental
from loopcut.merge import _undo


def contract_axes(base):
    root = os.environ.get('LOOPCUT_CONTRACT_DIR')
    if not root:
        return
    for filename in ('experimental.json', 'two-joint-experimental.json'):
        rig = json.loads((Path(root) / 'fixtures/rig' / filename).read_bytes())
        collection = bpy.data.collections.new('Native axis contract check')
        bpy.context.scene.collection.children.link(collection)
        arm = autorig._armature(rig, collection, Matrix.Identity(4))
        try:
            error = max((arm.data.bones[j['name']].matrix_local.to_3x3().col[axis].normalized()
                         - Matrix([j['rest_matrix'][k:k+4] for k in range(0, 16, 4)]).to_3x3().col[axis].normalized()).length
                        for j in rig['joints'] for axis in range(3))
            base.check(error < 1e-5, filename + ': display tails cannot reorient verified native axes')
        finally:
            data = arm.data
            bpy.data.objects.remove(arm, do_unlink=True)
            bpy.data.armatures.remove(data)
            bpy.data.collections.remove(collection)


def keyframe_legs(base, capture, arm, objects, report, directory):
    """Q1's native chains, chosen from its coordinates as an agent can do.

    No retargeter, humanoid labels, solver, remote motion, or fabricated rig.
    The rotations are ordinary bpy keys in the native bones' local bases.
    """
    legs = {'front_left': ('bone_12', 'bone_13', 'bone_15', 0),
            'front_right': ('bone_17', 'bone_18', 'bone_20', math.pi),
            'rear_left': ('bone_25', 'bone_26', 'bone_28', math.pi),
            'rear_right': ('bone_29', 'bone_30', 'bone_32', 0)}
    rest = autorig._evaluated(objects)
    scene = bpy.context.scene
    scene.frame_start, scene.frame_end = 1, 24
    for frame in (1, 7, 13, 19, 25):
        phase = 2 * math.pi * (frame - 1) / 24
        for upper, knee, _, offset in legs.values():
            wave = math.sin(phase + offset)
            for name, angle in ((upper, math.radians(24) * wave),
                                (knee, math.radians(22) * max(0, -wave))):
                bone = arm.pose.bones[name]
                axis = (arm.matrix_world @ bone.bone.matrix_local).to_3x3().inverted() @ Vector((1, 0, 0))
                bone.rotation_mode = 'QUATERNION'
                bone.rotation_quaternion = Quaternion(axis.normalized(), angle)
                bone.keyframe_insert(data_path='rotation_quaternion', frame=frame, group=name)
    tips, movement, samples, positions = {key: [] for key in legs}, [], [], []
    for frame in (1, 7, 13, 19):
        scene.frame_set(frame)
        after = autorig._evaluated(objects)
        positions.extend(p for mesh in after for p in mesh)
        movement.append(max((p - q).length for a, b in zip(rest, after) for p, q in zip(a, b)))
        for key, (_, _, tip, _) in legs.items():
            tips[key].append(arm.matrix_world @ arm.pose.bones[tip].head)
        base.check(all(math.isfinite(v) for mesh in after for p in mesh for v in p),
                   f'Q1: ordinary keyframes keep frame {frame} skin finite')
    displacement = {key: max((p - values[0]).length for p in values) for key, values in tips.items()}
    base.check(all(value > .01 for value in displacement.values()), 'Q1: all four distal legs visibly move')
    base.check(max(movement) > .01, 'Q1: plain bpy leg keys deform the original skin')
    camera = None
    for frame in (1, 7, 13, 19):
        scene.frame_set(frame)
        filename = f'legs-{frame:02d}.png'
        camera = capture(objects, report, directory / filename, camera, framing_points=positions,
                         camera_offset=(2.8, -1, .45))
        samples.append(filename)
    scene.frame_set(7)
    bpy.ops.wm.save_as_mainfile(filepath=str(directory / 'leg-keyframes.blend'), check_existing=False)
    result = {'method': 'plain bpy quaternion keyframes', 'keyed_bones': 8, 'key_frames': [1, 7, 13, 19, 25],
              'legs': {key: list(value[:3]) for key, value in legs.items()},
              'distal_displacement': displacement, 'mesh_max_displacement': max(movement),
              'samples': samples, 'paid_motion_requests': 0,
              'scope': 'Four-leg movement proof, not a finished walk or visual release approval.'}
    (directory / 'leg-keyframes.json').write_text(json.dumps(result, indent=2))
    return result


def exercise(base, capture, job, service, objects, fingerprint, inventory, preparation, directory, row):
    fid = row['fixture']
    names = [o.name for o in objects]
    details = {}
    # Fail the gate if the experimental path even attempts anatomical planning.
    with patch.object(rig_canonicalize, 'plan', side_effect=AssertionError('Native rig must not be planned')), \
            patch.object(rig_canonicalize, 'apply', side_effect=AssertionError('Native rig must not be oriented')), \
            patch.object(autorig, '_hinges', side_effect=AssertionError('No humanoid hinge gate for native rig')):
        row['message'] = base.cloud_jobs.apply_result(service.descriptor, job, details=details)
    sidecar = Path(next(o['path'] for o in job['outputs'] if o['name'] == 'rig.json'))
    rig = json.loads(sidecar.read_bytes())
    report = json.loads(sidecar.with_name('local-validation.json').read_bytes())
    (directory / 'local-validation.json').write_text(json.dumps(report, indent=2))
    arm = bpy.context.view_layer.objects.active
    base.check(rig_experimental.marked(arm), fid + ': experimental armature marker survives application')
    base.check({b.rotation_mode for b in arm.pose.bones} == {'XYZ'}, fid + ': native bones take rotation_euler keys')
    base.check(set(arm.data.bones.keys()) == {j['name'] for j in rig['joints']}, fid + ': every native name retained')
    base.check(row['message'] == rig_experimental.result_message(len(rig['joints'])), fid + ': exact real-count result')
    base.check(details['rig']['bone_count'] == len(rig['joints']) and not details['rig']['motion_tools_supported'],
               fid + ': model gets native chains and actionable continuation')
    base.check(autorig.fingerprint(objects, binding=arm) == fingerprint, fid + ': protected original unchanged')
    base.check(details['rig']['armature'] == arm.name and details['rig']['meshes'] == names,
               fid + ': model receives the exact armature and skinned mesh object names')
    head_error = max((arm.matrix_world @ arm.data.bones[j['name']].head_local
                      - preparation['to_world'] @ Vector(j['head'])).length for j in rig['joints'])
    axes_error = max((arm.data.bones[j['name']].matrix_local.to_3x3().col[axis].normalized()
                      - Matrix([j['rest_matrix'][k:k+4] for k in range(0, 16, 4)]).to_3x3().col[axis].normalized()).length
                     for j in rig['joints'] for axis in range(3))
    weight_error = 0.
    for ob, mesh in zip(objects, rig['meshes']):
        for vertex, expected in zip(ob.data.vertices, mesh['source_vertex_weights']):
            actual = {ob.vertex_groups[g.group].name: g.weight for g in vertex.groups if ob.vertex_groups[g.group].name in arm.data.bones}
            wanted = {rig['joints'][i]['name']: value for i, value in expected}
            base.check(set(actual) == set(wanted), fid + ': native influence identity') if set(actual) != set(wanted) else None
            weight_error = max(weight_error, max(abs(actual[name] - value) for name, value in wanted.items()))
    base.check(head_error < 1e-5 and axes_error < 1e-5 and weight_error < 1e-7,
               fid + ': native joint positions, axes and weights preserved')
    base.check(report['rest_delta_metres'] <= 1e-5 and report['canonicalization'] is None and not report['hinges'],
               fid + ': rest shape retained without humanoid transformations')
    base.check(_undo('undo'), fid + ': Undo succeeds')
    objects = [bpy.data.objects[name] for name in names]
    base.check(autorig.fingerprint(objects) == fingerprint
               and all(set(getattr(bpy.data, k).keys()) == value for k, value in inventory.items()),
               fid + ': one Undo restores exact original and inventory')
    base.check(_undo('redo'), fid + ': Redo succeeds')
    objects = [bpy.data.objects[name] for name in names]
    arm = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE' and rig_experimental.marked(o))
    capture(objects, report, directory / 'rest.png')
    bpy.ops.wm.save_as_mainfile(filepath=str(directory / 'native.blend'), check_existing=False)
    if fid == 'Q1':
        row['leg_keyframes'] = keyframe_legs(base, capture, arm, objects, report, directory)
    autorig.remove(arm.name)
    base.check(autorig.fingerprint(objects) == fingerprint, fid + ': Remove restores the original')
    base.check(service.charge == 20, fid + ': experimental costs exactly 20 fake credits')
    row.update(outcome='experimental', production_pass=True, original_preserved=True,
               fake_credits=service.charge, bone_count=len(rig['joints']), model_result=details['rig'],
               head_error=head_error, axes_error=axes_error, weight_error=weight_error,
               worker_inside_fraction=rig['experimental']['inside_fraction'],
               local_inside_fraction=report['inside']['fraction'], one_undo_redo=True, remove=True)
