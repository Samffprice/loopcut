"""Preserved-original standard and experimental rigging; scene operations run on main.

Preparation is private and free. Cloud job plumbing owns payment and cancellation.
Application builds and validates disposable copies, then binds the same original
objects/data in one undo step. No imported model ever replaces the user's mesh.
"""
import json
import math
import struct
import threading
import uuid
from operator import itemgetter
from pathlib import Path

from . import (rig_bundle, rig_canonicalize, rig_correspondence, rig_deform, rig_experimental, rig_inspect,
               rig_parts, rig_pose, rig_transport)
from .rig_inspect import RigError

from .rig_text import STAGES, TIME_HINT

_prepared = {}
_KEY = 'loopcut_auto_rig'
SKELETON_VERSION = 'tokenrig-native-273b691-v1'
# Keep inference aligned with the worker's R3 A5000/4090 fixture gate. A seed
# changes inferred anatomy even when the exported triangle surface is identical.
INFERENCE_SEED = 20261021


def _main():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Auto-rig scene operations require the main thread')


def _select(objects):
    import bpy
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for ob in bpy.context.selected_objects:
        ob.select_set(False)
    for ob in objects:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def targets(names=None):
    import bpy
    _main()
    if names is not None and (not isinstance(names, list) or not 1 <= len(names) <= 64
                             or any(not isinstance(n, str) or not 1 <= len(n) <= 63 for n in names)
                             or len(set(names)) != len(names)):
        rig_inspect.refuse('Choose between 1 and 64 character mesh objects. Join some parts first if there are more.',
                           'invalid_input')
    if names:
        objects = [bpy.context.scene.objects.get(n) for n in names]
        if any(o is None for o in objects):
            rig_inspect.refuse('A selected character part is no longer in this scene. Select the character again.',
                               'source_changed')
    else:
        objects = [o for o in bpy.context.selected_objects if o.type == 'MESH']
    if not objects or any(o.type != 'MESH' for o in objects):
        rig_inspect.refuse('Select the character meshes, including clothes, hair and eyes.', 'invalid_input')
    if len(objects) > 64:
        rig_inspect.refuse('Select one character with at most 64 mesh objects. Join some of its parts first.',
                           'mesh_limit')
    for ob in objects:
        if ob.mode != 'OBJECT':
            rig_inspect.refuse('Switch to Object Mode before checking or rigging this character.', 'invalid_mode')
        if not ob.data.vertices or not ob.data.polygons:
            rig_inspect.refuse('A selected character part has no faces. Select only the character meshes.', 'invalid_mesh')
        if ob.library or ob.data.library or ob.data.users != 1:
            rig_inspect.refuse('Make independent, editable copies of the selected character parts before rigging.', 'shared_mesh')
        if ob.find_armature() or any(m.type == 'ARMATURE' for m in ob.modifiers):
            rig_inspect.refuse('This character already has a skeleton. Pose or animate the existing rig, '
                               'or remove it explicitly before making a new one.', 'already_rigged')
        if _KEY in ob or any(g.name.startswith('mixamorig:') or
                             g.name.startswith('bone_') and g.name[5:].isdigit() for g in ob.vertex_groups):
            rig_inspect.refuse('This character already has controls with names reserved for this skeleton. '
                               'Rename those controls, or use the existing rig.', 'group_conflict')
        if abs(ob.matrix_world.determinant()) < 1e-12:
            rig_inspect.refuse('This character has zero scale. Restore its scale before rigging.', 'invalid_transform')
    return objects


def _rna(value):
    """Persistent RNA settings only; temporary copies change read-only ID user counts."""
    import bpy
    out = {}
    for p in value.bl_rna.properties:
        key = p.identifier
        if key in ('rna_type', 'is_active') or p.type == 'COLLECTION' or p.is_readonly:
            continue
        v = getattr(value, key)
        if p.type == 'POINTER':
            out[key] = [v.bl_rna.identifier, v.name_full] if isinstance(v, bpy.types.ID) else None
        elif getattr(p, 'is_array', False):
            out[key] = list(v)
        elif isinstance(v, (bool, int, float, str)) or v is None:
            out[key] = v
    return out


def _drivers(block):
    ad = getattr(block, 'animation_data', None)
    if ad is None:
        return None
    return {'action': ad.action.name_full if ad.action else None,
            'drivers': [{'path': f.data_path, 'index': f.array_index, 'expression': f.driver.expression,
                         'type': f.driver.type, 'mute': f.mute,
                         'variables': [{'name': v.name, 'type': v.type,
                                        'targets': [_rna(t) for t in v.targets]} for v in f.driver.variables]}
                        for f in ad.drivers]}


def fingerprint(objects, binding=None):
    """All protected source state, including evaluated modifier settings and drivers."""
    rows = []
    for o in objects:
        m = o.data
        keys = m.shape_keys
        rows.append({'name': o.name, 'mesh': m.name, 'matrix': [v for row in o.matrix_world for v in row],
                     'parent': o.parent.name if o.parent else None,
                     'vertices': [list(v.co) for v in m.vertices],
                     'edges': [list(e.vertices) for e in m.edges],
                     'faces': [(list(p.vertices), p.material_index, p.use_smooth) for p in m.polygons],
                     'uvs': [(u.name, [list(d.uv) for d in u.data]) for u in m.uv_layers],
                     'materials': [(s.link, _material(s.material)) for s in o.material_slots],
                     'keys': [(k.name, k.value, k.mute, k.relative_key.name,
                               [list(v.co) for v in k.data]) for k in keys.key_blocks] if keys else None,
                     'drivers': [_drivers(o), _drivers(m), _drivers(keys) if keys else None],
                     'modifiers': [_rna(v) for v in o.modifiers
                                   if not (binding and v.type == 'ARMATURE' and v.object == binding)],
                     'constraints': [_rna(v) for v in o.constraints],
                     'groups': [(g.name, g.lock_weight) for g in o.vertex_groups
                                if not binding or g.name not in binding.data.bones],
                     'weights': [[(g.group, g.weight) for g in v.groups
                                  if not binding or o.vertex_groups[g.group].name not in binding.data.bones]
                                 for v in m.vertices]})
    return rig_transport.digest(rig_transport.json_bytes(rows))


def _material(material):
    if material is None:
        return None
    tree = material.node_tree
    return {'properties': _rna(material), 'drivers': _drivers(material),
            'nodes': [{'properties': _rna(n), 'inputs': [_rna(s) for s in n.inputs]} for n in tree.nodes] if tree else [],
            'links': [(l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier)
                      for l in tree.links] if tree else [], 'tree_drivers': _drivers(tree) if tree else None}


def geometry(objects):
    """Bake transforms only into the upload; original transforms and modifiers stay intact."""
    from mathutils import Matrix
    anchor = max(objects, key=lambda o: len(o.data.vertices))
    # An unapplied object rotation is safe to remove in the private transport.
    # Baked lying-down geometry gets an advisory warning; the worker checks anatomy.
    frame = anchor.matrix_world.to_quaternion().to_matrix().to_4x4()
    frame.translation = anchor.matrix_world.translation
    inverse = frame.inverted()
    scale = __import__('bpy').context.scene.unit_settings.scale_length
    if not math.isfinite(scale) or scale <= 0:
        rig_inspect.refuse('Set a positive scene unit scale before rigging.', 'invalid_transform')
    metric = Matrix.Scale(scale, 4) @ inverse
    rows, all_points, all_triangles = [], [], []
    for i, ob in enumerate(objects):
        ob.data.calc_loop_triangles()
        # Basis geometry carries exact vertex identity even when keys are posed.
        basis = ob.data.shape_keys.key_blocks[0].data if ob.data.shape_keys else ob.data.vertices
        points = [tuple(metric @ ob.matrix_world @ v.co) for v in basis]
        triangles = [tuple(int(v) for v in t.vertices) for t in ob.data.loop_triangles]
        rows.append((f'mesh_{i}', points, triangles))
        all_triangles.extend(tuple(len(all_points) + v for v in t) for t in triangles)
        all_points.extend(points)
    return rows, all_points, all_triangles, metric.inverted()


def inspect(names=None):
    objects = targets(names)
    rows, points, triangles, back = geometry(objects)
    report = rig_inspect.geometry(points, triangles)
    if len(rows) > 1:
        rig_inspect.selection(rows)
    # The object's own up axis is only how it was uploaded, which the upload undoes: as "lying_down"
    # it had the model ask whether a standing dog was lying down (ar-dog-1).
    report.update(orientation_handling='Object rotation is removed from the upload; the rig returns in the original orientation.')
    report.update(targets=[o.name for o in objects], meshes=len(objects),
                  existing_armatures=[], transforms=[{'object': o.name, 'scale': list(o.scale),
                  'rotation': list(o.rotation_euler)} for o in objects],
                  transform_handling='Size, position and rotation are included in the upload; the originals stay unchanged.')
    return objects, rows, back, report


def prepare(arguments, directory):
    _main()
    # Contract 0.9: omitted consent is false on both the request and hashed task.
    # The v3 UI explicitly supplies true before presenting its disclosed quote.
    if type(arguments.get('accept_experimental', False)) is not bool:
        raise RigError('The experimental rig choice could not be read.', 'invalid_input')
    for field, value in {'category': 'humanoid', 'route': 'managed', 'controls': 'deform_only',
                         'skeleton_version': SKELETON_VERSION}.items():
        if arguments.get(field) != value:
            rig_inspect.refuse('Use the character rigger with its default settings. '
                               'Facial expressions and hand or foot position controls are not included.',
                               'unsupported_mode')
    if len(_prepared) >= 8:
        rig_inspect.refuse('Finish or cancel the pending rig jobs before preparing another character.', 'too_many_jobs')
    objects, rows, back, report = inspect(arguments.get('targets'))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    model = directory / 'model.glb'
    meshes = rig_transport.export(model, rows)
    task = {'schema': 'loopcut.rig-task/0.1', 'task_id': uuid.uuid4().hex,
            'model_sha256': rig_transport.digest(model.read_bytes()), 'category': 'humanoid',
            'controls': 'deform_only', 'mode': 'native', 'skeleton_version': arguments['skeleton_version'],
            'source_snapshot_sha256': fingerprint(objects), 'units': 'm', 'up': 'Y', 'meshes': meshes,
            'fitted_skeleton_sha256': None, 'seed': INFERENCE_SEED,
            'accept_experimental': arguments.get('accept_experimental', False)}
    path = directory / 'rig_task.json'
    path.write_bytes(rig_transport.json_bytes(task))
    # Re-read float32 upload points: correspondence is to what was sent, not float64 intermediates.
    for row in rows:
        row[1][:] = [tuple(struct.unpack('<3f', struct.pack('<3f', *p))) for p in row[1]]
    _prepared[task['task_id']] = {'task': task, 'names': [o.name for o in objects],
                                'directory': str(directory.resolve()),
                                'objects': [(o.as_pointer(), o.data.as_pointer()) for o in objects],
                                'scene': __import__('bpy').context.scene.as_pointer(),
                                'geometry': rows, 'to_world': back.copy(), 'inspection': report}
    return {'model': model, 'rig_task': path}


def quote_notice(directory):
    """Main thread: freeze this preparation's local warning for its own price card."""
    _main()
    directory = str(Path(directory).resolve())
    preparation = next((p for p in _prepared.values() if p['directory'] == directory), None)
    inspection = preparation['inspection'] if preparation else {}
    warnings = inspection.get('warnings', [])
    # The specific coat warning replaces the general disclosure on this card.
    coat = rig_inspect.CLOTHING_WARNING in warnings and 'upright_body' not in inspection.get('shape_hints', [])
    notice = rig_inspect.CLOTHING_WARNING if coat else rig_experimental.QUOTE
    return notice, False


def discard(directory):
    """Release a preparation after consent refusal or a confirmed failed/canceled job."""
    _main()
    directory = str(Path(directory).resolve())
    for task_id, preparation in list(_prepared.items()):
        if preparation['directory'] == directory:
            del _prepared[task_id]


def _recheck(preparation):
    import bpy
    if preparation['scene'] != bpy.context.scene.as_pointer():
        raise RigError('Switch back to the scene that requested this rig.', 'source_changed')
    objects = [bpy.data.objects.get(n) for n in preparation['names']]
    if (any(o is None for o in objects)
            or [(o.as_pointer(), o.data.as_pointer()) for o in objects] != preparation['objects']
            or fingerprint(objects) != preparation['task']['source_snapshot_sha256']):
        raise RigError('Your character changed while it was being rigged. Nothing was applied. '
                       'Restore the original state before applying this saved result.', 'source_changed')
    return objects


def _cross_check(path, rig, preparation):
    joints, points, triangles, weights = rig_transport.skin(path)
    by_name = {j['name']: i for i, j in enumerate(joints)}
    rig_bundle.require(len(by_name) == len(rig['joints']), 'The returned rig files describe different bones.')
    conversion = {}
    for i, joint in enumerate(rig['joints']):
        rig_bundle.require(joint['name'] in by_name, 'A returned rig file is missing a bone.')
        index = by_name[joint['name']]
        actual = joints[index]
        actual_parent = joints[actual['parent']]['name'] if actual['parent'] is not None else None
        expected_parent = rig['joints'][joint['parent']]['name'] if joint['parent'] is not None else None
        rig_bundle.require(actual_parent == expected_parent and math.dist(actual['head'], joint['head']) <= 1e-5,
                           'The returned rig files disagree about the skeleton.')
        if rig.get('tier') == 'experimental':
            matrix = actual['matrix']
            error = max(abs(matrix[r][c] - joint['rest_matrix'][r * 4 + c])
                        for r in range(4) for c in range(4))
            rig_bundle.require(error <= 1e-5,
                               'The returned rig files disagree about the skeleton.')
        # A sidecar display tail need not follow the native GLB's +Y axis.
        # Experimental bones use the verified native basis, standard bones are
        # reoriented later by the anatomical canonicalizer.
        conversion[index] = i
    remapped = [{conversion[j]: w for j, w in row.items()} for row in weights]
    source_points, source_triangles = [], []
    for _, vertices, faces in preparation['geometry']:
        source_triangles.extend(tuple(len(source_points) + i for i in t) for t in faces)
        source_points.extend(vertices)
    extent = max(max(p[k] for p in source_points) - min(p[k] for p in source_points) for k in range(3))
    # Use the worker's scale-relative precision, bounded by the contract's
    # absolute maximum. A fixed 10 micrometres conflates distinct tiny vertices.
    tolerance = min(1e-5, max(extent * 1e-6, 1e-8))
    try:
        exact = rig_correspondence.transfer_weights(source_points, source_triangles, points, triangles, remapped, tolerance)
    except ValueError as ex:
        raise RigError('The returned rig cannot be matched exactly to your original shape. '
                       'Some parts have changed or cannot be told apart safely. Nothing was applied.',
                       'correspondence_failed') from ex
    expected = {m['mesh_id']: m['source_vertex_weights'] for m in rig['meshes']}
    offset = 0
    for mesh_id, vertices, _ in preparation['geometry']:
        for i, row in enumerate(expected[mesh_id]):
            a, b = exact[offset + i], dict(row)
            rig_bundle.require(all(abs(a.get(k, 0) - b.get(k, 0)) <= 1e-5 for k in a.keys() | b.keys()),
                               'The returned rig files disagree about how the character should bend.')
        offset += len(vertices)
    return points


def _armature(rig, collection, back):
    import bpy
    from mathutils import Matrix, Vector
    data = bpy.data.armatures.new('Character Rig')
    arm = bpy.data.objects.new('Character Rig', data)
    collection.objects.link(arm)
    complete = False
    try:
        _select([arm])
        bpy.ops.object.mode_set(mode='EDIT')
        bones = []
        for j in rig['joints']:
            bone = data.edit_bones.new(j['name'])
            bone.head, bone.tail = Vector(j['head']), Vector(j['tail'])
            m = Matrix([j['rest_matrix'][k:k + 4] for k in range(0, 16, 4)])
            if rig.get('tier') == 'experimental':
                length = (bone.tail - bone.head).length
                bone.tail = bone.head + m.to_3x3().col[1].normalized() * length
            bone.align_roll(m.to_3x3().col[2])
            bone.use_deform = j['deform']
            if j['parent'] is not None:
                bone.parent = bones[j['parent']]
            bones.append(bone)
        bpy.ops.object.mode_set(mode='OBJECT')
        if rig.get('tier') == 'experimental':
            rig_experimental.euler_pose(arm)  # The model keys native rigs with ordinary rotation_euler code.
        # Worker coordinates are preparation metres. Retain scene-unit scale on
        # the rig so the original mesh transforms need no changes.
        arm.matrix_world = back
        complete = True
        return arm
    finally:
        if not complete:
            if arm.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.data.objects.remove(arm, do_unlink=True)
            bpy.data.armatures.remove(data)


def _bind(obj, arm, names, rows):
    if any(obj.vertex_groups.get(n) for n in names):
        raise RigError('Some existing controls use names this rig needs. Rename those controls first.', 'group_conflict')
    groups = [obj.vertex_groups.new(name=n) for n in names]
    for v, row in enumerate(rows):
        for j, weight in row:
            groups[j].add([v], weight, 'REPLACE')
    modifier = obj.modifiers.new('Loopcut Rig', 'ARMATURE')
    modifier.object = arm
    # Skin before the user's subdivision/geometry modifiers; keep their order/settings.
    obj.modifiers.move(len(obj.modifiers) - 1, 0)
    return modifier


def _evaluated(objects):
    import bpy
    bpy.context.view_layer.update()
    deps = bpy.context.evaluated_depsgraph_get()
    out = []
    for o in objects:
        evaluated = o.evaluated_get(deps)
        mesh = evaluated.to_mesh()
        try:
            out.append([evaluated.matrix_world @ v.co for v in mesh.vertices])
        finally:
            evaluated.to_mesh_clear()
    return out


def _hinges(arm, meshes, report):
    import bpy
    from mathutils import Quaternion, Vector
    baseline = _evaluated(meshes)
    forward = Vector(report['frame']['front'])
    results = []
    for side in ('Left', 'Right'):
        for first, mid, last, sign in [('Arm', 'ForeArm', 'Hand', 1), ('UpLeg', 'Leg', 'Foot', -1)]:
            a, b, c = [arm.pose.bones['mixamorig:' + side + role] for role in (first, mid, last)]
            original = b.matrix_basis.copy()
            mode = b.rotation_mode
            try:
                u, v = b.head - a.head, c.head - b.head
                hinge = b.matrix.to_3x3().col[0].normalized()
                angle = math.atan2(u.cross(v).dot(hinge), u.dot(v))
                b.rotation_mode = 'QUATERNION'
                b.rotation_quaternion = Quaternion((1, 0, 0), math.pi / 2 - angle)
                bpy.context.view_layer.update()
                actual = (b.head - a.head).angle(c.head - b.head)
                displacement = arm.matrix_world.to_3x3() @ (c.head - b.head - v)
                moved = _evaluated(meshes)
                delta = max((p - q).length for before, after in zip(baseline, moved) for p, q in zip(before, after))
                forward_delta = displacement.dot(forward) * sign
                passed = abs(actual - math.pi / 2) < math.radians(3) and forward_delta > 0 and delta > 1e-7
                results.append({'joint': side + mid, 'passed': passed, 'bend_degrees': math.degrees(actual),
                                'mesh_displacement': delta, 'forward_displacement': forward_delta})
            finally:
                b.rotation_mode = mode
                b.matrix_basis = original
                bpy.context.view_layer.update()
    if not all(row['passed'] for row in results):
        print('Loopcut rig hinge validation:', json.dumps(results))
        raise RigError('The rig did not bend both knees and elbows correctly. Nothing was applied.', 'validation_failed')
    return results


def _inside(arm, meshes, *, required=True):
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    vertices, faces = [], []
    for o in meshes:
        o.data.calc_loop_triangles()
        faces.extend(tuple(len(vertices) + i for i in t.vertices) for t in o.data.loop_triangles)
        vertices.extend(o.matrix_world @ v.co for v in o.data.vertices)
    tree = BVHTree.FromPolygons(vertices, faces, all_triangles=True)
    reach = max((p - vertices[0]).length for p in vertices) * 5
    rows = []
    for bone in arm.data.bones:
        p = arm.matrix_world @ ((bone.head_local + bone.tail_local) / 2)
        votes = 0
        for direction in [(1, .031, .017), (.023, 1, .041), (.013, .029, 1)]:
            d, start, count, distance = Vector(direction).normalized(), p.copy(), 0, 0.
            while count < 64 and distance < reach:
                hit, _, _, step = tree.ray_cast(start, d, reach - distance)
                if hit is None:
                    break
                count += 1
                distance += step + reach * 1e-7
                start = hit + d * (reach * 1e-7)
            votes += count % 2
        rows.append({'bone': bone.name, 'inside': votes >= 2})
    fraction = sum(row['inside'] for row in rows) / len(rows)
    if required and fraction < .7:
        raise RigError('Too much of the skeleton sits outside the character. Nothing was applied.', 'validation_failed')
    return {'fraction': fraction, 'bones': rows, 'threshold': .7}


def apply(outputs, progress=lambda stage: None, *, details=None):
    import bpy
    _main()
    paths = rig_bundle.artifacts(outputs)
    rig = rig_transport.read_json(paths['rig.json'], 32_000_000)
    worker_report = rig_transport.read_json(paths['validation.json'], 2_000_000)
    preparation = _prepared.get(rig.get('task_id')) if isinstance(rig, dict) else None
    if preparation is None:
        raise RigError('The saved copy of this character is missing. Check the character again; '
                       'nothing was applied.', 'missing_snapshot')
    rig_bundle.validate(rig, worker_report, preparation['task'])
    rig_bundle.match_metadata(rig, outputs)
    experimental = rig.get('tier') == 'experimental'
    originals = _recheck(preparation)
    points = _cross_check(paths['rigged.glb'], rig, preparation)
    try:
        planned = (None if experimental else rig_canonicalize.plan(
            [{k: j[k] for k in ('name', 'parent', 'head')} for j in rig['joints']], points))
    except rig_canonicalize.CanonicalizationError as ex:
        messages = {
            'unsupported_hands': 'The returned arms or fingers could not be identified safely. '
                'Try a neutral pose with the hands open and separated from the body.',
            'ambiguous_front': 'The returned feet do not clearly agree on which way the body faces. '
                'Use a neutral pose with both feet pointing forward.',
        }
        message = messages.get(ex.code, 'The returned skeleton could not be safely converted to a standard humanoid rig.')
        print('Loopcut rig validation:', ex)
        raise RigError(message + ' Nothing was applied.', ex.code) from ex
    if not experimental:
        rig_bundle.match_plan(rig, planned)
    progress('applying')
    selected = [o.name for o in bpy.context.selected_objects]
    active = bpy.context.view_layer.objects.active
    active_name = active.name if active else None
    mode = active.mode if active else 'OBJECT'
    from .merge import _undo
    if not _undo('push', 'Before character rig'):
        raise RigError('Blender could not create an undo step. Enable Global Undo and try applying the saved result again.',
                       'undo_unavailable')
    collection = bpy.data.collections.new('Rig checks (temporary)')
    bpy.context.scene.collection.children.link(collection)
    copies, arm, committed, added = [], None, False, []
    try:
        arm = _armature(rig, collection, preparation['to_world'])
        weights = {m['mesh_id']: m['source_vertex_weights'] for m in rig['meshes']}
        native_names = [j['name'] for j in rig['joints']]
        before = _evaluated(originals)
        for i, o in enumerate(originals):
            duplicate = o.copy()
            duplicate.data = o.data.copy()
            collection.objects.link(duplicate)
            copies.append(duplicate)
            _bind(duplicate, arm, native_names, weights[f'mesh_{i}'])
        try:
            canonical = None if experimental else rig_canonicalize.apply(arm, copies, planned=planned)
        except rig_canonicalize.CanonicalizationError as ex:
            print('Loopcut rig validation:', ex)
            raise RigError('The returned skeleton could not be safely converted to a standard humanoid rig. '
                           'Nothing was applied.', ex.code) from ex
        if not experimental:
            rig_bundle.match_plan(rig, canonical)
        progress('validating_experimental' if experimental else 'validating')
        inside = _inside(arm, copies, required=False) if experimental else _inside(arm, copies)
        hinges = [] if experimental else _hinges(arm, copies, canonical)
        after = _evaluated(copies)
        if any(len(a) != len(b) for a, b in zip(before, after)):
            raise RigError('The rig changed how your character looks before posing. Nothing was applied.', 'validation_failed')
        delta = max((p - q).length for a, b in zip(before, after) for p, q in zip(a, b))
        if delta * bpy.context.scene.unit_settings.scale_length > 1e-5:
            raise RigError('The rig moved your character before posing. Nothing was applied.', 'validation_failed')
        _recheck(preparation)
        names_by_source = ({n: n for n in native_names} if experimental else
                           {r['source']: r['name'] for r in canonical['labels']})
        names = [names_by_source[n] for n in native_names]
        if any(o.vertex_groups.get(n) for o in originals for n in names):
            raise RigError('Some existing controls use names this rig needs. Rename those controls first.', 'group_conflict')
        # Remove disposable copies before taking the undo baseline.
        for o in copies:
            mesh = o.data
            bpy.data.objects.remove(o, do_unlink=True)
            bpy.data.meshes.remove(mesh)
        copies.clear()
        bpy.context.scene.collection.objects.link(arm)
        collection.objects.unlink(arm)
        bpy.data.collections.remove(collection)
        collection = None
        for i, o in enumerate(originals):
            added.append(o)
            modifier = _bind(o, arm, names, weights[f'mesh_{i}'])
            o[_KEY] = json.dumps({'armature': arm.name, 'groups': names, 'modifier': modifier.name})
        if fingerprint(originals, binding=arm) != preparation['task']['source_snapshot_sha256']:
            raise RigError('The rig changed parts of your original character that should be kept. Nothing was applied.', 'validation_failed')
        arm[_KEY] = json.dumps({'targets': preparation['names'], 'selection': selected, 'active': active_name,
                                'source_snapshot_sha256': preparation['task']['source_snapshot_sha256']})
        arm[rig_experimental.TIER_PROPERTY] = 'experimental' if experimental else 'standard'
        arm['loopcut_rig_rest'] = json.dumps(_rest(arm))  # The joints the skin is weighted to (check_deformation).
        arm.data.display_type = 'STICK'
        arm.show_in_front = True
        _select([arm])
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    area.spaces.active.overlay.show_overlays = True
        report = {'tier': 'experimental' if experimental else 'standard',
                  'canonicalization': canonical, 'inside': inside, 'hinges': hinges,
                  'rest_delta_metres': delta * bpy.context.scene.unit_settings.scale_length,
                  'preservation': dict.fromkeys(['geometry', 'uvs', 'materials', 'shape_keys', 'independent_drivers'], True)}
        (paths['rig.json'].parent / 'local-validation.json').write_bytes(rig_transport.json_bytes(report))
        if not _undo('push', 'Rig character'):
            raise RigError('Blender could not save the rig undo step. Nothing was applied.', 'undo_unavailable')
        committed = True
        _prepared.pop(rig['task_id'], None)
        if details is not None:
            details['rig'] = {'tier': 'standard', 'armature': arm.name, 'bone_count': len(names),
                              'motion_tools_supported': True}
        if experimental:
            if details is not None:
                details['rig'] = rig_experimental.describe(rig['joints'], arm.name)
                details['rig']['meshes'] = [o.name for o in originals]
            return rig_experimental.result_message(len(names))
        if details is not None:
            details['rig']['meshes'] = [o.name for o in originals]
            walk = motion_price()
            if walk:  # R5-B1: under Balanced the walk's 10 credits ran with no card and no word about its price.
                details['rig']['next'] = f'Before a ready-made walk or run, tell the user its price: {walk}.'
        return f'Standard rig added: {len(names)} bones; try a free test pose or ask for a walk.'
    finally:
        if not committed:
            for o in added:
                for m in list(o.modifiers):
                    if m.type == 'ARMATURE' and m.object == arm:
                        o.modifiers.remove(m)
                for n in locals().get('names', []):
                    if o.vertex_groups.get(n):
                        o.vertex_groups.remove(o.vertex_groups[n])
                if _KEY in o:
                    del o[_KEY]
            if bpy.context.object and bpy.context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            for o in copies:
                mesh = o.data
                bpy.data.objects.remove(o, do_unlink=True)
                bpy.data.meshes.remove(mesh)
            if arm:
                data = arm.data
                bpy.data.objects.remove(arm, do_unlink=True)
                bpy.data.armatures.remove(data)
            if collection:
                bpy.data.collections.remove(collection)
            _select([bpy.data.objects[n] for n in selected if n in bpy.data.objects])
            if active_name:
                bpy.context.view_layer.objects.active = bpy.data.objects.get(active_name)
                if mode != 'OBJECT':
                    bpy.ops.object.mode_set(mode=mode)


def _selected_rig(name):
    import bpy
    ob = bpy.data.objects.get(name) if name is not None else bpy.context.view_layer.objects.active
    arm = ob if ob and ob.type == 'ARMATURE' else ob.find_armature() if ob else None
    if arm is None or _KEY not in arm:
        raise RigError('Select a character rig made by Loopcut.', 'not_loopcut_rig')
    return arm


def remove(name=None):
    import bpy
    from .merge import _undo
    _main()
    arm = _selected_rig(name)
    saved = json.loads(arm[_KEY])
    if any(bpy.data.objects.get(n) is None or _KEY not in bpy.data.objects[n] for n in saved['targets']):
        raise RigError('A rigged character part is missing. Use Undo to restore it first.', 'source_changed')
    _undo('push', 'Before removing character rig')
    for n in saved['targets']:
        obj = bpy.data.objects.get(n)
        if obj is None or _KEY not in obj:
            raise RigError('A rigged character part is missing. Use Undo to restore it first.', 'source_changed')
        data = json.loads(obj[_KEY])
        for m in list(obj.modifiers):
            if m.type == 'ARMATURE' and m.object == arm:
                obj.modifiers.remove(m)
        for n in data['groups']:
            if obj.vertex_groups.get(n):
                obj.vertex_groups.remove(obj.vertex_groups[n])
        del obj[_KEY]
    data = arm.data
    bpy.data.objects.remove(arm, do_unlink=True)
    bpy.data.armatures.remove(data)
    _select([bpy.data.objects[n] for n in saved['selection'] if n in bpy.data.objects])
    bpy.context.view_layer.objects.active = bpy.data.objects.get(saved['active']) if saved['active'] else None
    _undo('push', 'Remove character rig')
    return 'Rig removed. Your original character is back.'


def _rows(m):
    return [list(r) for r in m]


def _skeleton(arm):
    """rig_pose's bones: armature-space rest, the current basis and the length, by name."""
    return {b.name: (b.parent.name if b.parent else None, _rows(b.matrix_local),
                     _rows(arm.pose.bones[b.name].matrix_basis), b.length) for b in arm.data.bones}


def gentle(side):
    """The gentle pose's result says what it bent and what it left untested: ar-artist-2's model called its
    joints, and the untouched shoulders, hips and wrists, clean."""
    bent = 'both knees and elbows' if side == 'both' else f'the {side} knee and elbow'
    other = '' if side == 'both' else f', nor the {"right" if side == "left" else "left"} side'
    return (f'Free test pose: {bent} bent {rig_pose.GENTLE}°. Shoulders, hips and wrists were not tested{other}; '
            'pose stress bends and checks every joint.')


def preview(name=None, reset=False, pose='gentle', side='both'):
    import bpy
    from .merge import _undo
    from mathutils import Matrix, Quaternion
    arm = _selected_rig(name)
    if rig_experimental.marked(arm):
        raise RigError('This experimental rig has no built-in test poses: pose or keyframe its bones, then run '
                       'check_deformation.', 'experimental_rig')
    ad = arm.animation_data
    if ad and (ad.action or len(ad.nla_tracks) or len(ad.drivers)) or any(b.constraints for b in arm.pose.bones):
        raise RigError('This rig already has animation or controls, so test poses are off; check_deformation '
                       'checks its animation.', 'already_animated')
    saved = json.loads(arm.get('loopcut_rig_preview', '{}'))
    if reset and not saved:
        return 'There is no test pose to reset.'
    # Solved from the original pose (an earlier test pose undone), before anything changes.
    skeleton = {n: (parent, rest, saved[n]['matrix'] if n in saved else basis, length)
                for n, (parent, rest, basis, length) in _skeleton(arm).items()}
    try:
        turns, notes = ({}, []) if reset else rig_pose.solve(skeleton, pose, side)
    except rig_pose.PoseError as ex:
        raise RigError(str(ex), 'invalid_input') from ex
    _undo('push', 'Before free rig test pose')
    for bone_name, row in saved.items():
        bone = arm.pose.bones.get(bone_name)
        if bone is not None:
            bone.rotation_mode = row['mode']
            bone.matrix_basis = Matrix(row['matrix'])
    if reset:
        del arm['loopcut_rig_preview']
        if 'loopcut_rig_test_pose' in arm:
            del arm['loopcut_rig_test_pose']
    else:
        for bone_name, q in turns.items():
            bone = arm.pose.bones[bone_name]
            saved.setdefault(bone.name, {'mode': bone.rotation_mode, 'matrix': _rows(bone.matrix_basis)})
            bone.rotation_mode = 'QUATERNION'
            bone.rotation_quaternion = Quaternion(q)
        arm['loopcut_rig_preview'] = json.dumps(saved)
        # What this test pose set, so the check can tell it from a pose made by hand.
        arm['loopcut_rig_test_pose'] = json.dumps({'pose': pose, 'side': side,
                                                   'turns': {n: list(q) for n, q in turns.items()}})
    _select([arm])
    bpy.ops.object.mode_set(mode='POSE')
    _undo('push', 'Free rig test pose')
    if reset:
        return 'Original pose restored.'
    # Plain words for the user (ar-artist-1's reply offered to "run reset_preview").
    back = ' To put it back, reset the test pose or undo once; tell the user it is the test pose.'
    if pose == 'gentle':
        return gentle(side) + back
    bpy.context.view_layer.update()
    posed = {b.name: _rows(b.matrix) for b in arm.pose.bones}
    measured = rig_pose.measure(posed, _skeleton(arm), pose, side)
    where = '' if side == 'both' else f', {side} side only'
    text = (f'Free test pose: {rig_pose.WHAT[pose]}{where}. Measured: {"; ".join(measured)}.'
            + ''.join(f' Left out: {note}.' for note in notes) + back)
    return text + '\n\n' + check_deformation(arm.name)


def _segment_distance(c, a, b):
    d = b - a
    t = max(0., min(1., (c - a).dot(d) / d.length_squared)) if d.length_squared else 0.
    return (a + d * t - c).length


def _joints(arm):
    """(names, armature-space rest heads, posed heads, radii, turn degrees, bend from straight (elbows and
    knees, else None), and the deforming bones near the joint, whose turns make its pose) of the joint
    regions this rig has."""
    found = []
    bones, pose = arm.data.bones, arm.pose.bones
    for side in ('Left', 'Right'):
        for region, role, share in rig_deform.REGIONS:
            bone = bones.get(rig_pose.PREFIX + side + role)
            if bone is None:
                continue
            turned = [pose[n].matrix_basis.to_quaternion().angle for n in
                      [bone.name] + ([rig_pose.PREFIX + side + 'Shoulder'] if region == 'shoulder' else [])
                      if n in pose]
            radius = bone.length * share
            near = [b.name for b in bones if b == bone or b == bone.parent or b.use_deform
                    and _segment_distance(bone.head_local, b.head_local, b.tail_local) < radius]
            bend = (math.degrees((pose[bone.name].tail - pose[bone.name].head).angle(
                pose[bone.parent.name].tail - pose[bone.parent.name].head, 0.))
                    if region in rig_deform.HINGES and bone.parent else None)
            found.append((f'{side.lower()} {region}', bone.head_local.copy(), pose[bone.name].head.copy(),
                          radius, math.degrees(max(turned)), bend, near))
    return found


def _rest(arm):
    """Each bone's rest head and Y and Z axes, armature space, as saved at apply (rig_deform.moved)."""
    return {b.name: [round(x, 6) for x in (*b.head_local, *b.matrix_local.col[1][:3], *b.matrix_local.col[2][:3])]
            for b in arm.data.bones}


def _json(arm, key, default):
    try:
        return json.loads(arm.get(key, ''))
    except (TypeError, ValueError):
        return default


def _mesh_state(obj, deps, topology=True):
    """(points, edges, triangles) of obj as evaluated, read in place (a to_mesh copy of a subdivided
    66k-vertex character took 0.35 s); points only without topology."""
    from array import array
    mesh = obj.evaluated_get(deps).data
    co = array('f', bytes(4 * 3 * len(mesh.vertices)))
    mesh.vertices.foreach_get('co', co)
    points = list(zip(co[0::3], co[1::3], co[2::3]))
    if not topology:
        return points, [], []
    edges = array('i', bytes(4 * 2 * len(mesh.edges)))
    mesh.edges.foreach_get('vertices', edges)
    if mesh.polygons and not mesh.loop_triangles:
        mesh.calc_loop_triangles()
    tris = array('i', bytes(4 * 3 * len(mesh.loop_triangles)))
    mesh.loop_triangles.foreach_get('vertices', tris)
    return points, list(zip(edges[0::2], edges[1::2])), list(zip(tris[0::3], tris[1::3], tris[2::3]))


SMOOTHING = {'SUBSURF', 'MULTIRES'}  # After the rig they smooth its cage; the check reads the cage.


def _rig_meshes(arm):
    import bpy
    return [o for o in (bpy.data.objects.get(n) for n in json.loads(arm[_KEY])['targets'])
            if o is not None and o.type == 'MESH']


def _placement(o, arm):
    """(armature space -> o's mesh space, armature units -> mesh units) as the rig is placed now."""
    return o.matrix_world.inverted() @ arm.matrix_world, arm.matrix_world.to_scale()[0] / o.matrix_world.to_scale()[0]


def _standard_now(arm, meshes):
    """(rows' joint specs, {mesh: [(rest centre, posed centre, radius)]} in its space, the bones near each joint)
    of a standard rig's joint regions in its current, evaluated pose."""
    joints = _joints(arm)
    centres = {}
    for o in meshes:
        to_mesh, size = _placement(o, arm)
        centres[o.name] = [(tuple(to_mesh @ a), tuple(to_mesh @ b), r * size) for _, a, b, r, *_ in joints]
    return ([{'name': n, 'bent': bent, 'bend': bend} for n, _, _, _, bent, bend, _ in joints], centres,
            {n: near for n, *_, near in joints})


def _turns(arm):
    """{bone: degrees turned from rest} in the current pose; a bone moved a fifth of its length off its rest head
    counts as turned (an experimental rig's keys may move bones)."""
    out = {}
    for b in arm.pose.bones:
        basis = b.matrix_basis
        moved = basis.translation.length > .2 * b.length
        out[b.name] = max(math.degrees(basis.to_quaternion().angle), rig_deform.BENT if moved else 0.)
    return out


def _parts(arm, meshes, rest):
    """An experimental rig's body parts (rig_parts) and each vertex's part, -1 for none, by its strongest bone.
    rest: {mesh: rest points in its space}. Positions are read in armature space, up being the world's."""
    from mathutils import Vector
    bones, data = {}, arm.data.bones
    up = (arm.matrix_world.inverted().to_3x3() @ Vector((0., 0., 1.))).normalized()
    owners, everything = {}, []
    for o in meshes:
        to_arm = arm.matrix_world.inverted() @ o.matrix_world
        names = {g.index: g.name for g in o.vertex_groups if g.name in data and data[g.name].use_deform}
        top = []
        for v in o.data.vertices:
            best = max(((g.weight, names[g.group]) for g in v.groups if g.group in names and g.weight > 0),
                       default=None)
            top.append(best[1] if best else None)
        points = [to_arm @ Vector(p) for p in rest[o.name]]
        owners[o.name] = top
        everything += points
        for name, p in zip(top, points):
            if name is not None:
                row = bones.setdefault(name, [0, math.inf, Vector()])
                row[0] += 1
                row[1] = min(row[1], p.dot(up))
                row[2] += p
    heights = [p.dot(up) for p in everything]
    ground, height = min(heights), max(heights) - min(heights) or 1.
    flat = lambda v: v - up * v.dot(up)  # noqa: E731
    low = [flat(p) for p, h in zip(everything, heights) if h <= ground + .06 * height]
    above = [flat(p) for p, h in zip(everything, heights) if ground + .08 * height <= h <= ground + .18 * height]
    feet = tuple(sum(low, Vector()) / len(low) - sum(above, Vector()) / len(above)) if low and above else None
    found = rig_parts.parts({b.name: {'parent': b.parent.name if b.parent else None, 'head': tuple(b.head_local),
                                      'vertices': bones.get(b.name, [0])[0],
                                      'low': bones[b.name][1] if b.name in bones else None,
                                      'centroid': tuple(bones[b.name][2] / bones[b.name][0]) if b.name in bones else None}
                             for b in data}, ground, height, tuple(up), feet)
    index = {b: k for k, part in enumerate(found['parts']) for b in part['bones']}
    for part in found['parts']:
        apex = sum((data[b].head_local for b in part['apex']), Vector()) / len(part['apex'])
        part['radius'] = max([.05 * height] + [(data[b].head_local - apex).length for b in part['bones']]
                             + [(data[b].tail_local - apex).length for b in part['bones']])
    regions = {o.name: [index.get(n, -1) if n is not None else -1 for n in owners[o.name]] for o in meshes}
    return found, regions


def _pose(arm):
    """What a part check needs of the current, evaluated pose: each bone's turn and posed head."""
    return _turns(arm), {b.name: b.head.copy() for b in arm.pose.bones}


def _parts_now(arm, meshes, found, pose=None):
    """_standard_now for an experimental rig's parts, in the current pose or one _pose() read earlier."""
    from mathutils import Vector
    (turns, heads), data = pose or _pose(arm), arm.data.bones
    specs, centres = [], {o.name: [] for o in meshes}
    for part in found['parts']:
        specs.append({'name': part['name'], 'bent': max(turns.get(b, 0.) for b in part['bones'])})
        rest = sum((data[b].head_local for b in part['apex']), Vector()) / len(part['apex'])
        posed = sum((heads[b] for b in part['apex']), Vector()) / len(part['apex'])
        for o in meshes:
            to_mesh, size = _placement(o, arm)
            centres[o.name].append((tuple(to_mesh @ rest), tuple(to_mesh @ posed), part['radius'] * size))
    return specs, centres, {}


def _fcurves(arm):
    """The F-curves of the rig's active action for its slot (actions are layered and slotted since 4.4)."""
    ad = arm.animation_data
    action = ad.action if ad else None
    if action is None:
        return None, []
    legacy = getattr(action, 'fcurves', None)
    if legacy is not None and not hasattr(action, 'layers'):
        return action, list(legacy)
    slot, out = getattr(ad, 'action_slot', None), []
    for layer in getattr(action, 'layers', ()):
        for strip in layer.strips:
            bag = strip.channelbag(slot) if slot is not None else None
            if bag is not None:
                out += list(bag.fcurves)
    return action, out


def _channels(arm, curves=None):
    """(action, [(owner, property, index or None, fcurve)]) the rig's animation sets, read without frame_set: a
    frame change would also reset every other animated object's unkeyed edits and run frame handlers."""
    action, curves = _fcurves(arm) if curves is None else curves
    out = []
    for fc in curves:
        if fc.mute or fc.data_path.endswith(']'):
            continue  # Custom properties (rig switches) keep their value.
        path, _, prop = fc.data_path.rpartition('.')
        try:
            owner = arm.path_resolve(path) if path else arm
            value = getattr(owner, prop)
        except (AttributeError, ValueError):
            continue
        array = hasattr(value, '__len__') and not isinstance(value, str)
        out.append((owner, prop, fc.array_index if array else None, fc))
    return action, out


def _curves_only(arm, meshes, curves, channels):
    """Whether setting the action's curves gives what frame_set would: no constraints, drivers, NLA, action blending
    or skipped (custom property) curves on the rig, and nothing animated on its meshes or their shape keys."""
    ad = arm.animation_data
    if (len(channels) != len(curves) or len(ad.drivers) or ad.action_influence < 1 or ad.action_blend_type != 'REPLACE'
            or any(not t.mute and len(t.strips) for t in ad.nla_tracks)
            or any(c.influence > 0 and not c.mute for b in arm.pose.bones for c in b.constraints)
            or any(c.influence > 0 and not c.mute for c in arm.constraints)
            or getattr(arm.data, 'animation_data', None)):
        return False
    for o in meshes:
        for block in (o, o.data, o.data.shape_keys):
            data = getattr(block, 'animation_data', None) if block is not None else None
            if data is not None and (data.action is not None or len(data.drivers) or len(data.nla_tracks)):
                return False
    return True


def _get(owner, prop, index):
    value = getattr(owner, prop)
    return value[index] if index is not None else value


def _put(owner, prop, index, value):
    try:
        if index is None:
            setattr(owner, prop, type(getattr(owner, prop))(value))
        else:
            getattr(owner, prop)[index] = value
    except (TypeError, ValueError, AttributeError):
        pass


def _span(action, channels):
    """rig_deform.span for the rig's action and the scene's playback range."""
    import bpy
    scene = bpy.context.scene
    cyclic = any(m.type == 'CYCLES' for *_, fc in channels for m in fc.modifiers)
    return rig_deform.span(tuple(action.frame_range), (scene.frame_start, scene.frame_end), cyclic) if channels else None


def _frames(first, last, channels):
    """rig_deform.frames, with each frame's turn from rest read from the curves, so nothing is evaluated."""
    from mathutils import Euler, Quaternion
    rotations = {}
    for owner, prop, index, fc in channels:
        if prop in ('rotation_quaternion', 'rotation_euler') and index is not None and hasattr(owner, 'rotation_mode'):
            rotations.setdefault((owner.name, prop), [owner.rotation_mode, {}])[1][index] = fc

    def amount(frame):
        total = 0.
        for (_, prop), (mode, curves) in rotations.items():
            if prop == 'rotation_quaternion' and mode == 'QUATERNION':
                q = Quaternion([curves[k].evaluate(frame) if k in curves else float(k == 0) for k in range(4)])
                total += q.normalized().angle if q.magnitude > 1e-9 else 0.
            elif prop == 'rotation_euler' and mode not in ('QUATERNION', 'AXIS_ANGLE'):
                total += Euler([curves[k].evaluate(frame) if k in curves else 0. for k in range(3)],
                               mode).to_quaternion().angle
        return total
    return rig_deform.frames(first, last, amount)


def check_deformation(name=None):
    """Free: the current pose against rest, per joint region (rig_deform), or per body part on an experimental rig
    (rig_parts); on a rig with an animation, sampled frames of it, worst first. Changes nothing in the scene: the
    rig is put at rest for one evaluation and back (an animation's frames are set from its curves and put back),
    with any subdivision after it off for every read (the check is calibrated on cages, and a live 66k-vertex
    subdivision made it 0.8 s). It remembers its numbers on the rig, to compare the next check of the same pose
    or animation with."""
    import bpy
    _main()
    arm = _selected_rig(name)
    experimental = rig_experimental.marked(arm)
    meshes = _rig_meshes(arm)
    layer = bpy.context.view_layer
    smoothing = [m for o in meshes for k, m in enumerate(o.modifiers) if m.type in SMOOTHING and m.show_viewport
                 and any(r.type == 'ARMATURE' and r.object == arm for r in list(o.modifiers)[:k])]
    position = arm.data.pose_position
    curves = _fcurves(arm)
    action, channels = _channels(arm, curves)
    span = _span(action, channels) if action is not None else None
    saved = [(owner, prop, index, _get(owner, prop, index)) for owner, prop, index, _ in channels] if span else []
    scene = bpy.context.scene
    # With constraints, drivers, NLA or animated meshes the frame is set by Blender (then put back); otherwise from the
    # curves, which leaves other objects' unkeyed edits alone.
    direct = span is not None and _curves_only(arm, meshes, curves[1], channels)
    frame_now = (scene.frame_current, scene.frame_subframe)
    now = (lambda found: _parts_now(arm, meshes, found)) if experimental else (lambda found: _standard_now(arm, meshes))
    frames, sampled = [], []
    try:
        for m in smoothing:
            m.show_viewport = False
        layer.update()
        if not meshes:
            raise RigError('This rig has no skinned mesh to check.', 'invalid_input')
        if span is None:  # The posed state, read before the rest evaluation replaces it.
            current = _pose(arm) if experimental else now(None)
            deps = bpy.context.evaluated_depsgraph_get()
            posed = {o.name: _mesh_state(o, deps) for o in meshes}
        arm.data.pose_position = 'REST'
        layer.update()
        deps = bpy.context.evaluated_depsgraph_get()
        rest = {o.name: _mesh_state(o, deps, topology=span is not None) for o in meshes}
        arm.data.pose_position = position
        found, regions = _parts(arm, meshes, {n: r[0] for n, r in rest.items()}) if experimental else (None, {})
        if span is not None:
            layer.update()
            first_spec, first_centres, nears = now(found)
            if not first_spec:
                raise RigError('This rig has no joints to check.', 'invalid_input')
            inputs, kept = [], []
            for o in meshes:
                points, edges, tris = rest[o.name]
                inputs.append({'rest': points, 'edges': edges, 'tris': tris, 'region': regions.get(o.name),
                               'boundary': experimental, 'scale': o.matrix_world.to_scale()[0] * _unit(),
                               'joints': first_centres[o.name]})
                kept.append(o)
            planned = rig_deform.plan(inputs)
            step = (span[1] - span[0]) / (rig_deform.ANIMATION_FRAMES - 1)

            def sample(frame):
                if direct:
                    for owner, prop, index, fc in channels:
                        _put(owner, prop, index, fc.evaluate(frame))
                    layer.update()
                else:
                    scene.frame_set(frame)
                spec, centres, _ = now(found)
                deps = bpy.context.evaluated_depsgraph_get()
                posed_now = []
                for o, mesh in zip(kept, planned):
                    points = _mesh_state(o, deps, topology=False)[0]
                    posed_now.append((points if len(points) == len(mesh['rest']) else mesh['rest'],
                                      [c for _, c, _ in centres[o.name]]))
                frames.append((frame, rig_deform.measure_pose(planned, spec, posed_now)))
                sampled.append(frame)

            for frame in _frames(span[0], span[1], channels):
                sample(frame)
            # Halfway to the samples beside the worst one: a stretch peaks between samples.
            top = max(range(len(frames)), key=lambda k: max(map(rig_deform.severity, frames[k][1])))
            for frame in sorted({round(frames[top][0] - step / 2), round(frames[top][0] + step / 2)}):
                if span[0] <= frame <= span[1] and frame not in sampled:
                    sample(frame)
    finally:
        if span is not None and not direct and frames:
            scene.frame_set(frame_now[0], subframe=frame_now[1])
        for owner, prop, index, value in saved:
            _put(owner, prop, index, value)
        arm.data.pose_position = position
        for m in smoothing:
            m.show_viewport = True
        layer.update()  # Pose bones read their matrices from the last evaluation: put the posed one back.
    word = 'part' if experimental else 'joint'
    saved_rest = _json(arm, 'loopcut_rig_rest', {})
    label = ({b: part['name'] for part in found['parts'] for b in part['bones']} if experimental else None)
    edited = rig_deform.moved(saved_rest, _rest(arm), {b.name: b.parent and b.parent.name for b in arm.data.bones},
                              arm.matrix_world.to_scale()[0] * _unit(), label) if isinstance(saved_rest, dict) else ''
    edited = [edited] if edited else []
    if span is not None:
        rows = rig_deform.worst(sorted(frames, key=itemgetter(0)))
        memory = _json(arm, 'loopcut_rig_animation_checks', [])
        lead, remembered = rig_deform.remember_animation(memory if isinstance(memory, list) else [], action.name, rows)
        if remembered != memory:
            arm['loopcut_rig_animation_checks'] = json.dumps(remembered)
        animation = {'frames': len(frames), 'first': span[0], 'last': span[1]}
        return '\n'.join(rig_deform.lines(rows, lead + edited, (), animation, word))
    specs, centres, nears = _parts_now(arm, meshes, found, current) if experimental else current
    if not specs:
        raise RigError('This rig has no joints to check.', 'invalid_input')
    inputs = []
    for o in meshes:
        points, edges, tris = posed[o.name]
        if len(rest[o.name][0]) != len(points):
            continue  # A modifier that changes the vertex count with the pose has no rest to compare.
        inputs.append({'rest': rest[o.name][0], 'posed': points, 'edges': edges, 'tris': tris,
                       'region': regions.get(o.name), 'boundary': experimental,
                       'scale': o.matrix_world.to_scale()[0] * _unit(), 'joints': centres[o.name]})
    rows = rig_deform.measure(inputs, specs)
    # Each joint against the last check of it in the same pose (its nearby bones' turns, or a part's own),
    # remembered on the rig: ar-artist-2's "fix" took a shoulder from 97% to 54% volume and the check still
    # called it clean.
    if experimental:
        nears = {part['name']: part['bones'] for part in found['parts']}
    poses = {n: {b: [round(x, 5) for x in arm.pose.bones[b].matrix_basis.to_quaternion()] for b in near}
             for n, near in nears.items()}
    memory = _json(arm, 'loopcut_rig_checks', [])
    lead, remembered = rig_deform.remember(memory if isinstance(memory, list) else [], rows, poses)
    if remembered != memory:
        arm['loopcut_rig_checks'] = json.dumps(remembered)
    test = _json(arm, 'loopcut_rig_test_pose', {})
    by_hand = rig_pose.by_hand(test.get('turns', {}) if isinstance(test, dict) else {},
                               {b.name: tuple(b.matrix_basis.to_quaternion()) for b in arm.pose.bones})
    notes = ([BY_HAND] if by_hand and not experimental and any(row['bent'] >= rig_deform.BENT for row in rows)
             else [])
    return '\n'.join(rig_deform.lines(rows, lead + edited, notes, None, word))


def _unit():
    import bpy
    return bpy.context.scene.unit_settings.scale_length or 1.


# ar-artist-2's model hand-wrote its stress pose twice in run_python and its elbows reached 13°.
BY_HAND = ('Posed by hand, not with a built-in test pose: for test poses use preview with pose stress (free), even '
           'when other angles were asked; it bends every joint to known angles and checks them.')


SCHEMAS = [{'type': 'function', 'function': {
    'name': 'character_rig', 'title': 'Checking your character', 'description': 'Free local inspect, standard-rig test poses (preview pose stress, even for '
    'other angles; not run_python), reset, bend check (any Loopcut rig; on an animated one, its whole animation) and '
    'rig removal. Inspect before loopcut_cloud.auto_rig (it quotes first). Experimental rigs: bone keyframes, no motion '
    'tools.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['inspect', 'preview', 'check_deformation', 'reset_preview', 'remove']},
        'targets': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Mesh names to inspect; defaults to selection.'},
        'rig': {'type': 'string', 'description': 'Rig name; defaults to selection.'},
        'pose': {'type': 'string', 'enum': list(rig_pose.POSES), 'description': 'stress: arms up, elbows and knees bent, fists'},
        'side': {'type': 'string', 'enum': list(rig_pose.SIDES)}}, 'required': ['action']}}}]


def _animation_digest(arm):
    """A short hash of the rig's active action: its name, curves, keys and modifiers; None without one."""
    import hashlib
    from array import array
    action, curves = _fcurves(arm)
    if action is None:
        return None
    digest = hashlib.blake2b(action.name.encode(), digest_size=8)
    for fc in curves:
        digest.update(f'{fc.data_path}|{fc.array_index}|{fc.mute}|{[m.type for m in fc.modifiers]}'.encode())
        co = array('f', bytes(8 * len(fc.keyframe_points)))
        fc.keyframe_points.foreach_get('co', co)
        digest.update(co.tobytes())
    return digest.hexdigest()


def rig_state():
    """What run_python compares around the model's code (R5-B2, three weight "fixes" told to the user unchecked):
    ({mesh: (its weights' hash, its rig)} of Loopcut-rigged meshes, {rig: its animation's hash})."""
    import bpy
    from .ownership import weights_hash
    weights, motion = {}, {}
    for ob in bpy.context.scene.objects:
        if _KEY not in ob:
            continue
        if ob.type == 'ARMATURE':
            motion[ob.name] = _animation_digest(ob)
        elif ob.type == 'MESH' and ob.vertex_groups:
            try:
                rig = json.loads(ob[_KEY]).get('armature')
            except (TypeError, ValueError, AttributeError):
                continue
            weights[ob.name] = (weights_hash(ob.data), rig)
    return weights, motion


def rig_changes(before, after):
    """run_python's line when the code changed a Loopcut rig's weights or its animation, else ''."""
    import bpy
    (weights, motion), (weights_now, motion_now) = before, after
    changed = [n for n, (digest, _) in weights_now.items() if n in weights and weights[n][0] != digest]
    keyed = [n for n, digest in motion_now.items() if digest is not None and motion.get(n) != digest]
    out = []
    if changed:
        rigs = {weights_now[n][1] for n in changed}
        if any(motion_now.get(r) for r in rigs):
            how = 'run character_rig check_deformation: it checks the whole animation'
        elif any(rig_experimental.marked(bpy.data.objects.get(r)) for r in rigs):
            how = 'pose its bones and run character_rig check_deformation'
        else:
            how = 'run character_rig preview with pose stress (free): it bends every joint and checks it'
        out.append(f"Weights changed on {', '.join(changed)}: before telling the user it is fixed, {how}.")
    if keyed:
        out.append(f"Animation changed on {', '.join(keyed)}: run character_rig check_deformation before telling the "
                   "user it works.")
    return '\n'.join(out)


# Conditions, not failures, so their cards are not red (ar-dog-3: three red X cards in a row, "feels like
# everything is failing"): the model gets what to do instead.
EXPECTED = frozenset(('experimental_rig', 'already_animated', 'not_loopcut_rig'))


def prices():
    """(rig credits, ready-made motion credits per second, its default seconds) from the signed-in catalog's
    estimates (credits_estimate prices a tool's default arguments), or None where it has none."""
    from . import cloud
    runtime = cloud._runtime
    try:
        rows = {d.get('name'): d for d in (runtime.descriptors() if runtime else [])}
    except Exception:  # A catalog that cannot be read gives no prices, never a failed check.
        rows = {}
    def estimate(name):
        value = ((rows.get(name) or {}).get('cost') or {}).get('credits_estimate')
        return value if type(value) in (int, float) and value > 0 else None
    seconds = ((((rows.get('motion_from_text') or {}).get('input_schema') or {}).get('properties') or {})
               .get('duration_s') or {}).get('default', MOTION_SECONDS)
    motion = estimate('motion_from_text')
    per = motion / seconds if motion and type(seconds) in (int, float) and seconds > 0 else None
    return estimate('auto_rig'), per, seconds if per else None


MOTION_SECONDS = 5  # The contract's default motion length, which credits_estimate prices (CATALOG.md).


def motion_price():
    """'2 credits a second (10 for 5 s)', or '' without a catalog."""
    _, per, seconds = prices()
    return f'{per:g} credits a second ({per * seconds:g} for {seconds:g} s)' if per else ''


def price_note():
    """R5-B1: the walk's 10 credits ran without the model ever saying a walk costs extra."""
    rig, _, _ = prices()
    walk = motion_price()
    if not rig and not walk:
        return ''
    parts = ([f'the rig is {rig:g} credits'] if rig else []) + ([f'a ready-made walk or run is {walk}'] if walk else [])
    return 'Before rigging, tell the user the total: ' + ' and '.join(parts) + '.'


def rigged(names=None):
    """inspect on a character that already has a skeleton describes it instead of refusing (ar-dog-3 got auto-rig's
    "already has a skeleton… remove it" after its own rig was applied): what it is and what works. None when the
    meshes have no skeleton."""
    import bpy
    _main()
    if names:
        objects = [o for o in (bpy.context.scene.objects.get(n) for n in names if isinstance(n, str)) if o is not None]
    else:
        objects = list(bpy.context.selected_objects) or [o for o in [bpy.context.view_layer.objects.active] if o]
    for ob in objects:
        arm = ob if ob.type == 'ARMATURE' else ob.find_armature() if ob.type == 'MESH' else None
        if arm is None and ob.type == 'MESH':
            arm = next((m.object for m in ob.modifiers if m.type == 'ARMATURE' and m.object), None)
        if arm is not None and arm.type == 'ARMATURE':
            return describe_rig(arm)
    return None


def describe_rig(arm):
    from . import motion_retarget
    ours = _KEY in arm
    tier = 'experimental' if rig_experimental.marked(arm) else 'standard' if ours else None
    action, channels = _channels(arm)
    span = _span(action, channels) if action is not None else None
    meshes = _rig_meshes(arm) if ours else []
    out = {'rigged': True, 'armature': arm.name, 'made_by_loopcut': ours, 'tier': tier or 'own rig',
           'bone_count': len(arm.data.bones), 'meshes': [o.name for o in meshes],
           'animated': f'frames {span[0]}-{span[1]}' if span else False}
    check = 'check_deformation checks ' + ('the whole animation' if span else 'the current pose')
    if tier == 'experimental':
        text = (f'Already rigged by Loopcut (experimental, {len(arm.data.bones)} bones): no ready-made motions, so '
                f'animate it with bone keyframes (free); {check}.')
    elif tier == 'standard':
        walk = motion_price()
        text = (f'Already rigged by Loopcut (standard, {len(arm.data.bones)} bones): ready-made walks and runs work'
                + (f' ({walk}; tell the user first)' if walk else '') + f'; preview pose stress tests poses; {check}.')
    else:
        moves = motion_retarget.rig_of(arm) is not None
        text = (f'Already has its own skeleton ({len(arm.data.bones)} bones): '
                + ('ready-made motions can target it' if moves else 'animate it with bone keyframes')
                + "; Loopcut's test poses and bend check need a Loopcut rig.")
    out['message'] = text + ' It needs no new rig.'
    return out


def tool(action, targets=None, rig=None, pose='gentle', side='both'):
    from .tools import ToolResult, _editor_context
    try:
        with _editor_context('VIEW_3D'):
            if action == 'inspect':
                described = rigged(targets)
                if described is not None:
                    return ToolResult(json.dumps(described))
                report = inspect(targets)[3]
                note = price_note()
                if note:
                    report['price_note'] = note
                return ToolResult(json.dumps(report))
            if action == 'remove':
                return ToolResult(remove(rig))
            if action == 'check_deformation':
                return ToolResult(check_deformation(rig))
            if action in ('preview', 'reset_preview'):
                return ToolResult(preview(rig, action == 'reset_preview', pose or 'gentle', side or 'both'))
            raise RigError('Choose Check character, Try a free test pose, Check how it bends, Reset test pose or '
                           'Remove character rig.', 'invalid_input')
    except RigError as ex:
        return ToolResult(str(ex), ok=ex.code in EXPECTED)


DISPATCH = {'character_rig': tool}


def describe(name, arguments):
    if name != 'character_rig':
        return None
    labels = {'inspect': 'Check character (free)', 'preview': 'Try a free test pose',
              'check_deformation': 'Check how it bends (free)', 'reset_preview': 'Reset test pose',
              'remove': 'Remove character rig'}
    return labels.get(arguments.get('action'), 'Character rig'), ''
