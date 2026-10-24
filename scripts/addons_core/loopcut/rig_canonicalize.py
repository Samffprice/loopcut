"""Apply the shared pure planner to a derived Blender rig without changing skin."""
from copy import deepcopy

# Compatibility imports keep existing callers on the exact worker-vendored plan.
from .rig_plan import CanonicalizationError, PREFIX, dot, plan, sub


def apply(arm, meshes, *, planned=None):
    """Canonicalize a derived Blender rig, enforcing exact joint/weight invariants."""
    import bpy
    from mathutils import Matrix, Vector
    import numpy as np
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    if arm.animation_data or any(pb.constraints for pb in arm.pose.bones):
        raise CanonicalizationError('canonicalize before animation/constraints, on a derived rig')
    if any(pb.matrix_basis != Matrix.Identity(4) for pb in arm.pose.bones):
        raise CanonicalizationError('pose must be cleared before canonicalization')
    mw = arm.matrix_world.copy()
    scale = mw.to_scale()
    if min(scale) <= 0 or max(scale) - min(scale) > 1e-6 or mw.determinant() <= 0:
        raise CanonicalizationError('armature transform must have positive uniform scale')
    bones = list(arm.data.bones)
    ids = {b.name: i for i, b in enumerate(bones)}
    joints = [{'name': b.name, 'parent': ids[b.parent.name] if b.parent else None,
               'head': tuple(b.head_local)} for b in bones]
    if planned is None:
        inverse = mw.inverted()
        vertices = [tuple(inverse @ o.matrix_world @ v.co) for o in meshes for v in o.data.vertices]
        report = plan(joints, vertices)
    else:
        # The client computed this locally from exact sidecar heads and verified
        # native GLB positions in preparation metres, before staging. Never run
        # label inference on rounded Blender world coordinates or a different
        # seam multiplicity than the worker. Only drawing axes/tails change here.
        report = deepcopy(planned)
        if (len(report['labels']) != len(bones)
                or {row['source'] for row in report['labels']} != set(ids)):
            raise CanonicalizationError('planned joints do not match the derived armature')
    original_heads = {b.name: b.head_local.copy() for b in bones}
    mapping = {row['source']: row['name'] for row in report['labels']}
    groups = {o.name: [(g.index, g.name) for g in o.vertex_groups] for o in meshes}
    weights = {o.name: [[(g.group, g.weight) for g in v.groups] for v in o.data.vertices] for o in meshes}
    for o in meshes:
        old = [name for _, name in groups[o.name]]
        new = [mapping.get(name, name) for name in old]
        if len(set(new)) != len(new):
            raise CanonicalizationError('canonical names collide with unrelated vertex groups')
    def evaluated():
        bpy.context.view_layer.update()
        deps = bpy.context.evaluated_depsgraph_get()
        result = {}
        for o in meshes:
            ob = o.evaluated_get(deps)
            mesh = ob.to_mesh()
            result[o.name] = np.array([ob.matrix_world @ v.co for v in mesh.vertices])
            ob.to_mesh_clear()
        return result
    before = evaluated()
    bpy.ops.object.select_all(action='DESELECT')
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    # Never assign heads. Disconnect drawing-only tails from child heads so a
    # role-specific foot/chest tail cannot drag an existing joint in Edit mode.
    edit = {row['source']: arm.data.edit_bones[row['source']] for row in report['labels']}
    for b in edit.values():
        b.use_connect = False
    for row in report['labels']:
        b = edit[row['source']]
        b.tail = Vector(row['tail'])
        y = (b.tail - b.head).normalized()
        x = Vector(row['x_axis']).normalized()
        b.align_roll(x.cross(y).normalized())  # align_roll aligns local Z.
        b.name = '__autorig_canonical_' + str(row['index'])
    for row in report['labels']:
        edit[row['source']].name = row['name']
    bpy.ops.object.mode_set(mode='OBJECT')
    for o in meshes:
        for index, name in groups[o.name]:
            o.vertex_groups[index].name = mapping.get(name, name)
    after = evaluated()
    if len(arm.data.bones) != len(joints):
        raise CanonicalizationError('bone count changed')
    for row in report['labels']:
        bone = arm.data.bones[row['name']]
        parent = joints[ids[row['source']]]['parent']
        expected_parent = mapping[joints[parent]['name']] if parent is not None else None
        if (bone.parent.name if bone.parent else None) != expected_parent:
            raise CanonicalizationError('joint hierarchy changed')
    displacement = max((arm.data.bones[row['name']].head_local - original_heads[row['source']]).length
                       for row in report['labels'])
    if displacement != 0:
        raise CanonicalizationError(f'joint invariant failed: max displacement {displacement} m')
    neutral_delta = max(float(np.linalg.norm(after[n] - before[n], axis=1).max()) for n in before)
    neutral_delta *= bpy.context.scene.unit_settings.scale_length
    if neutral_delta > 1e-5:
        raise CanonicalizationError(f'neutral mesh invariant failed: {neutral_delta} m')
    for o in meshes:
        actual = [[(g.group, g.weight) for g in v.groups] for v in o.data.vertices]
        if actual != weights[o.name] or len(o.vertex_groups) != len(groups[o.name]):
            raise CanonicalizationError('weights/group indices changed')
        if any(o.vertex_groups[i].name != mapping.get(n, n) for i, n in groups[o.name]):
            raise CanonicalizationError('vertex group rename is not one-to-one')
    arm.data.display_type = 'STICK'
    arm.show_in_front = True
    # Validation/viewport consumers use Blender world directions and distances;
    # the worker projection contains only unchanged index/source/role/name.
    report['planning_space'] = 'preparation_metres' if planned is not None else 'armature_local'
    report['frame_space'] = 'blender_world'
    report['height'] *= scale[0]
    for key in ('up', 'front', 'left'):
        report['frame'][key] = tuple((mw.to_3x3() @ Vector(report['frame'][key])).normalized())
    for row in report['labels']:
        row['tail'] = tuple(mw @ Vector(row['tail']))
        row['x_axis'] = tuple((mw.to_3x3() @ Vector(row['x_axis'])).normalized())
    report.update(status='canonicalized', invariants={'max_joint_displacement_metres': displacement,
                  'max_rest_mesh_displacement_metres': neutral_delta, 'weights_identical': True,
                  'groups_renamed_one_to_one': True, 'bone_count_unchanged': True, 'hierarchy_unchanged': True})
    return report
