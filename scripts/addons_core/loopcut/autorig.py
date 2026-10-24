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
from pathlib import Path

from . import (rig_bundle, rig_canonicalize, rig_correspondence, rig_deform, rig_experimental, rig_inspect,
               rig_pose, rig_transport)
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
        raise RigError('Select a character rig made by Loopcut.', 'invalid_input')
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


def preview(name=None, reset=False, pose='gentle', side='both'):
    import bpy
    from .merge import _undo
    from mathutils import Matrix, Quaternion
    arm = _selected_rig(name)
    if rig_experimental.marked(arm):
        raise RigError('This experimental rig has no standard test pose. Ask the agent to pose or animate it.',
                       'experimental_rig')
    if arm.animation_data or any(b.constraints for b in arm.pose.bones):
        raise RigError('This rig already has animation or controls. Pose it directly or play its animation.', 'already_animated')
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
    else:
        for bone_name, q in turns.items():
            bone = arm.pose.bones[bone_name]
            saved.setdefault(bone.name, {'mode': bone.rotation_mode, 'matrix': _rows(bone.matrix_basis)})
            bone.rotation_mode = 'QUATERNION'
            bone.rotation_quaternion = Quaternion(q)
        arm['loopcut_rig_preview'] = json.dumps(saved)
    _select([arm])
    bpy.ops.object.mode_set(mode='POSE')
    _undo('push', 'Free rig test pose')
    if reset:
        return 'Original pose restored.'
    # Plain words for the user (ar-artist-1's reply offered to "run reset_preview").
    back = ' To put it back, reset the test pose or undo once; tell the user it is the test pose.'
    if pose == 'gentle':
        bent = 'both knees and elbows are' if side == 'both' else f'the {side} knee and elbow are'
        return f'Free test pose: {bent} bent.' + back
    bpy.context.view_layer.update()
    posed = {b.name: _rows(b.matrix) for b in arm.pose.bones}
    measured = rig_pose.measure(posed, _skeleton(arm), pose, side)
    where = '' if side == 'both' else f', {side} side only'
    text = (f'Free test pose: {rig_pose.WHAT[pose]}{where}. Measured: {"; ".join(measured)}.'
            + ''.join(f' Left out: {note}.' for note in notes) + back)
    return text + '\n\n' + check_deformation(arm.name)


def _joints(arm):
    """(names, armature-space rest heads, posed heads, radii, bend degrees) of the joint regions this rig has."""
    found = []
    for side in ('Left', 'Right'):
        for region, role, share in rig_deform.REGIONS:
            bone = arm.data.bones.get(rig_pose.PREFIX + side + role)
            if bone is None:
                continue
            turned = [arm.pose.bones[n].matrix_basis.to_quaternion().angle for n in
                      [bone.name] + ([rig_pose.PREFIX + side + 'Shoulder'] if region == 'shoulder' else [])
                      if n in arm.pose.bones]
            found.append((f'{side.lower()} {region}', bone.head_local.copy(), arm.pose.bones[bone.name].head.copy(),
                          bone.length * share, math.degrees(max(turned))))
    return found


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


def check_deformation(name=None):
    """Free: the current pose against rest, per joint region (rig_deform). Changes nothing: the rig is put
    at rest for one evaluation and back, with any subdivision after it off for both reads (the check is
    calibrated on cages, and a live 66k-vertex subdivision made it 0.8 s)."""
    import bpy
    _main()
    arm = _selected_rig(name)
    if rig_experimental.marked(arm):
        raise RigError('The deformation check needs a standard rig; look at this one in the viewport instead.',
                       'experimental_rig')
    meshes = [o for o in (bpy.data.objects.get(n) for n in json.loads(arm[_KEY])['targets'])
              if o is not None and o.type == 'MESH']
    layer = bpy.context.view_layer
    smoothing = [m for o in meshes for k, m in enumerate(o.modifiers) if m.type in SMOOTHING and m.show_viewport
                 and any(r.type == 'ARMATURE' and r.object == arm for r in list(o.modifiers)[:k])]
    position = arm.data.pose_position
    try:
        for m in smoothing:
            m.show_viewport = False
        layer.update()
        joints = _joints(arm)  # Posed heads need the update.
        if not meshes or not joints:
            raise RigError('This rig has no skinned mesh or joints to check.', 'invalid_input')
        deps = bpy.context.evaluated_depsgraph_get()
        posed = {o.name: _mesh_state(o, deps) for o in meshes}
        arm.data.pose_position = 'REST'
        layer.update()
        rest = {o.name: _mesh_state(o, bpy.context.evaluated_depsgraph_get(), topology=False)[0] for o in meshes}
    finally:
        arm.data.pose_position = position
        for m in smoothing:
            m.show_viewport = True
        layer.update()  # Pose bones read their matrices from the last evaluation: put the posed one back.
    unit = bpy.context.scene.unit_settings.scale_length or 1.
    inputs = []
    for o in meshes:
        points, edges, tris = posed[o.name]
        if len(rest[o.name]) != len(points):
            continue  # A modifier that changes the vertex count with the pose has no rest to compare.
        to_mesh = o.matrix_world.inverted() @ arm.matrix_world
        size = arm.matrix_world.to_scale()[0] / o.matrix_world.to_scale()[0]
        inputs.append({'rest': rest[o.name], 'posed': points, 'edges': edges, 'tris': tris,
                       'scale': o.matrix_world.to_scale()[0] * unit,
                       'joints': [(tuple(to_mesh @ a), tuple(to_mesh @ b), r * size) for _, a, b, r, _ in joints]})
    rows = rig_deform.measure(inputs, [{'name': n, 'bent': bent} for n, _, _, _, bent in joints])
    return '\n'.join(rig_deform.lines(rows))


SCHEMAS = [{'type': 'function', 'function': {
    'name': 'character_rig', 'title': 'Checking your character', 'description': 'Free local inspect, standard-rig test poses/reset and bend check, and '
    'rig removal. Inspect before loopcut_cloud.auto_rig, which quotes before spending. Experimental rigs need direct '
    'bone keyframes or repairs; only standard humanoid rigs support motion tools.',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['inspect', 'preview', 'check_deformation', 'reset_preview', 'remove']},
        'targets': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Mesh names to inspect; defaults to selection.'},
        'rig': {'type': 'string', 'description': 'Rig name; defaults to selection.'},
        'pose': {'type': 'string', 'enum': list(rig_pose.POSES), 'description': 'stress: arms up, elbows and knees bent, fists'},
        'side': {'type': 'string', 'enum': list(rig_pose.SIDES)}}, 'required': ['action']}}}]


def tool(action, targets=None, rig=None, pose='gentle', side='both'):
    from .tools import ToolResult, _editor_context
    try:
        with _editor_context('VIEW_3D'):
            if action == 'inspect':
                return ToolResult(json.dumps(inspect(targets)[3]))
            if action == 'remove':
                return ToolResult(remove(rig))
            if action == 'check_deformation':
                return ToolResult(check_deformation(rig))
            if action in ('preview', 'reset_preview'):
                return ToolResult(preview(rig, action == 'reset_preview', pose or 'gentle', side or 'both'))
            raise RigError('Choose Check character, Try a free test pose, Check how it bends, Reset test pose or '
                           'Remove character rig.', 'invalid_input')
    except RigError as ex:
        return ToolResult(str(ex), ok=False)


DISPATCH = {'character_rig': tool}


def describe(name, arguments):
    if name != 'character_rig':
        return None
    labels = {'inspect': 'Check character (free)', 'preview': 'Try a free test pose',
              'check_deformation': 'Check how it bends (free)', 'reset_preview': 'Reset test pose',
              'remove': 'Remove character rig'}
    return labels.get(arguments.get('action'), 'Character rig'), ''
