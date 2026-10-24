"""Compare actual add-on preparations to every frozen eval input geometrically.

Run in Blender under heavy_lock with --root (repeatable) and --out. This checks
the exported input, unlike replaying a recorded output. No inference or network.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autorig_integration as base
from loopcut import autorig, rig_transport
import bpy
from mathutils import Matrix, Quaternion, Vector


def read_meshes(path):
    """Independent fixture decoder, allowing attributes that geometry-only uploads omit."""
    raw = path.read_bytes()
    length = struct.unpack_from('<I', raw, 12)[0]
    doc = json.loads(raw[20:20 + length])
    binary = raw[28 + length:]
    def accessor(index):
        a = doc['accessors'][index]
        view = doc['bufferViews'][a['bufferView']]
        fmt = '<' + {5121: 'B', 5123: 'H', 5125: 'I', 5126: 'f'}[a['componentType']] * {
            'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}[a['type']]
        start = view.get('byteOffset', 0) + a.get('byteOffset', 0)
        stride = view.get('byteStride', struct.calcsize(fmt))
        return [struct.unpack_from(fmt, binary, start + i * stride) for i in range(a['count'])]
    rows = []
    convert = Matrix([rig_transport.TO_BLENDER[k:k + 4] for k in range(0, 16, 4)])
    def visit(index, parent):
        node = doc['nodes'][index]
        q = node.get('rotation', [0, 0, 0, 1])
        local = (Matrix([node['matrix'][k:k + 4] for k in range(0, 16, 4)]).transposed()
                 if 'matrix' in node else Matrix.LocRotScale(Vector(node.get('translation', [0, 0, 0])),
                     Quaternion((q[3], *q[:3])), Vector(node.get('scale', [1, 1, 1]))))
        world = parent @ local
        if 'mesh' in node:
            points, triangles, attributes = [], [], set()
            for primitive in doc['meshes'][node['mesh']]['primitives']:
                assert primitive.get('mode', 4) == 4
                vertices = [convert @ world @ Vector(p) for p in accessor(primitive['attributes']['POSITION'])]
                indices = [p[0] for p in accessor(primitive['indices'])]
                triangles.extend(tuple(i + len(points) for i in indices[k:k + 3])
                                 for k in range(0, len(indices), 3))
                points.extend(vertices)
                attributes.update(primitive['attributes'])
            rows.append({'name': node.get('name'), 'points': points, 'triangles': triangles,
                         'attributes': sorted(attributes)})
        for child in node.get('children', []):
            visit(child, world)
    for index in doc['scenes'][doc.get('scene', 0)]['nodes']:
        visit(index, Matrix.Identity(4))
    return rows


def bounds(rows):
    points = [p for row in rows for p in row['points']]
    return [[min(p[k] for p in points) for k in range(3)],
            [max(p[k] for p in points) for k in range(3)]]


def compare(reference, actual, tolerance):
    """Compare oriented corners, retaining triangle order and separate mesh identity."""
    assert len(reference) == len(actual), 'mesh count differs'
    maximum = 0.0
    for expected, got in zip(reference, actual):
        assert expected['name'] == got['name'], 'mesh identity differs'
        assert len(expected['triangles']) == len(got['triangles']), 'triangle count differs'
        for a, b in zip(expected['triangles'], got['triangles']):
            for i, j in zip(a, b):
                maximum = max(maximum, (expected['points'][i] - got['points'][j]).length)
        assert maximum <= tolerance, f'ordered surface differs by {maximum:g} m (limit {tolerance:g})'
    return maximum


def facts(path, rows):
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'vertices': sum(len(r['points']) for r in rows),
            'unique_positions': len({tuple(p) for r in rows for p in r['points']}),
            'triangles': sum(len(r['triangles']) for r in rows), 'bounds': bounds(rows),
            'attributes': sorted({a for r in rows for a in r['attributes']})}


def exercise(root, fid, directory, *, persona=False):
    if persona:
        from evals import autorig_persona
        # Keep this checkout loaded: factory preferences would re-enable the
        # installed add-on. The real persona task appends into its live session.
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in list(bpy.data.meshes):
            if not mesh.users:
                bpy.data.meshes.remove(mesh)
        autorig._prepared.clear()
        autorig_persona.generated_adult()
        objects = [o for o in bpy.context.selected_objects if o.type == 'MESH']
    else:
        objects = base.load(root, fid)
    before = autorig.fingerprint(objects)
    transforms = [{'name': o.name, 'matrix_world': [list(row) for row in o.matrix_world],
                   'modifiers': [m.type for m in o.modifiers]} for o in objects]
    files = autorig.prepare({**base.fake.inputs(), 'targets': [o.name for o in objects],
                            'accept_experimental': True}, directory)
    task = json.loads(files['rig_task'].read_bytes())
    preparation = autorig._prepared[task['task_id']]
    actual = read_meshes(files['model'])
    reference = read_meshes(root / 'prepared' / fid / 'input.glb')
    for row, name in zip(actual, preparation['names']):
        row['name'] = name
    reference = sorted(reference, key=lambda row: preparation['names'].index(row['name']))
    uploaded_facts = facts(files['model'], actual)
    # Undo only the add-on's documented transport frame, never a best-fit alignment.
    for row in actual:
        row['points'] = [preparation['to_world'] @ p for p in row['points']]
    low, high = bounds(reference)
    tolerance = max(1e-8, max(b - a for a, b in zip(low, high)) * 2e-6)
    error = compare(reference, actual, tolerance)
    assert before == autorig.fingerprint(objects), 'preparation changed the original'
    assert task['seed'] == 20261021, 'inference seed differs from the live worker fixture gate'
    negative = []
    if fid == 'R10':
        import copy
        for kind in ['decimation', 'axis', 'scale', 'winding']:
            mutated = copy.deepcopy(actual)
            if kind == 'decimation':
                mutated[0]['triangles'].pop()
            elif kind == 'winding':
                mutated[0]['triangles'][0] = tuple(reversed(mutated[0]['triangles'][0]))
            else:
                for row in mutated:
                    row['points'] = [Vector((p.z, p.y, p.x)) if kind == 'axis' else p * 1.01 for p in row['points']]
            try:
                compare(reference, mutated, tolerance)
            except AssertionError:
                negative.append(kind)
            else:
                raise AssertionError('comparator accepted ' + kind)
    return {'fixture': fid, 'root': str(root), 'persona_start': persona,
            'eval': facts(root / 'prepared' / fid / 'input.glb', reference),
            'upload': uploaded_facts, 'max_corner_error_metres': error, 'tolerance_metres': tolerance,
            'seed': task['seed'], 'units': task['units'], 'up': task['up'], 'source_transforms': transforms,
            'to_world': [list(row) for row in preparation['to_world']],
            'scene_unit_scale': bpy.context.scene.unit_settings.scale_length,
            'preserved_original': True, 'negative_controls': negative, 'passed': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        for index, root in enumerate(args.root):
            for folder in sorted((root / 'prepared').iterdir()):
                if all((folder / f).is_file() for f in ['input.glb', 'original.blend']):
                    rows.append(exercise(root, folder.name, args.out / str(index) / folder.name))
                    print('PASS prepared surface', index, folder.name, flush=True)
            if (root / 'prepared/R10/input.glb').is_file():
                rows.append(exercise(root, 'R10', args.out / 'persona-R10', persona=True))
                print('PASS autorig_generated_adult task preparation', flush=True)
    finally:
        (args.out / 'results.json').write_text(json.dumps(rows, indent=2))
    print(f'PASS {len(rows)} real add-on preparations match frozen eval geometry', flush=True)


if __name__ == '__main__':
    main()
