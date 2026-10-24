"""Fail-closed rig sidecar and artifact checks before any scene mutation."""
import math
import re
from pathlib import Path

from . import rig_experimental, rig_transport as transport
from .rig_inspect import RigError
from .rig_plan import canonical_plan

LIMITS = {'rigged.glb': (50_000_000, 'model/gltf-binary'),
          'rig.json': (32_000_000, 'application/json'),
          'validation.json': (2_000_000, 'application/json')}
HASH = re.compile(r'[0-9a-f]{64}\Z')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')


def require(condition, message):
    if not condition:
        raise RigError(message + ' Your character has not been changed.', 'invalid_bundle')


def finite_array(value, size):
    return (isinstance(value, (list, tuple)) and len(value) == size
            and all(type(v) in (float, int) and math.isfinite(v) and abs(v) <= 1e6 for v in value))


def fields(value, names):
    require(isinstance(value, dict) and set(value) == set(names.split()),
            'A returned rig file has missing or unreadable information.')


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def match_plan(rig, report):
    require(rig['canonical_plan'] == canonical_plan(report),
            'The returned body-part names disagree with the checks on this computer.')


def match_metadata(rig, outputs):
    """Contract 0.9 puts tier/reasons on every artifact, not the public Job."""
    if 'tier' not in rig:  # A completed legacy standard job may still be applied.
        return
    experimental = rig['tier'] == 'experimental'
    reasons = rig['experimental']['reasons'] if experimental else []
    for output in outputs:
        meta = output.get('meta', {})
        require(isinstance(meta, dict) and meta.get('tier') == rig['tier']
                and isinstance(meta.get('reasons'), list)
                and all(isinstance(reason, str) for reason in meta['reasons'])
                and sorted(meta['reasons']) == sorted(reasons)
                and meta.get('canonicalization') == ('not_applicable' if experimental else 'local_pending')
                and (meta.get('experimental') == rig['experimental'] if experimental else 'experimental' not in meta),
                'The returned rig type disagrees with the job result.')


def validate(rig, report, task):
    require(isinstance(rig, dict), 'A returned rig file has missing or unreadable information.')
    versioned = 'tier' in rig or task.get('accept_experimental') is True
    fields(rig, 'schema task_id source_model_sha256 source_snapshot_sha256 model skeleton_version '
                'coordinates gltf_to_blender joints meshes confidence warnings canonical_plan'
                + (' tier' if versioned else '') + (' experimental' if 'experimental' in rig else ''))
    tier = rig.get('tier', 'standard')
    require(tier in ('standard', 'experimental'), 'The returned rig type is not supported.')
    experimental = tier == 'experimental'
    require(not experimental or task.get('accept_experimental') is True,
            'An experimental rig was returned without your agreement.')
    require((rig['canonical_plan'] is None) == experimental,
            'The returned rig type disagrees with its body-part names.')
    require(experimental or 'experimental' not in rig,
            'A standard rig includes unexpected experimental information.')
    fields(report, 'schema task_id phase status checks unweighted_vertices correspondence_valid preservation timings '
                   'peak_vram_bytes exit_code warnings visual_approval release_accepted')
    require(rig.get('schema') == 'loopcut.rig/0.1' and report.get('schema') == 'loopcut.rig-validation/0.1',
            'This rig result uses an unsupported format.')
    require(rig.get('task_id') == report.get('task_id') == task['task_id'], 'The result belongs to another rig job.')
    require(rig.get('source_model_sha256') == task['model_sha256']
            and rig.get('source_snapshot_sha256') == task['source_snapshot_sha256'],
            'The result does not match the character that was submitted.')
    require(rig.get('coordinates') == {'units': 'm', 'up': 'Z', 'space': 'blender_world'}
            and rig.get('gltf_to_blender') == transport.TO_BLENDER
            and rig.get('skeleton_version') == task['skeleton_version'],
            'The returned rig uses a size, direction or skeleton format this version cannot use.')
    model = rig.get('model', {})
    fields(model, 'code_commit weights_revision weights_sha256')
    require(isinstance(model, dict) and isinstance(model.get('code_commit'), str)
            and re.fullmatch('[0-9a-f]{40}', model['code_commit']) is not None
            and isinstance(model.get('weights_revision'), str) and 1 <= len(model['weights_revision']) <= 128
            and isinstance(model.get('weights_sha256'), list) and 1 <= len(model['weights_sha256']) <= 16
            and all(isinstance(s, str) and HASH.fullmatch(s) for s in model['weights_sha256']),
            'The rig is missing information needed to check where it came from.')
    require(rig['confidence'] == {'value': None, 'calibration': None},
            'The rig includes a reliability rating this version cannot verify.')
    for value in (rig['warnings'], report['warnings']):
        require(isinstance(value, list) and len(value) <= 64
                and all(isinstance(w, str) and 1 <= len(w) <= 300 for w in value), 'The rig includes unreadable warnings.')
    require(report.get('phase') == 'worker' and report.get('status') == 'pass'
            and report.get('correspondence_valid') is True and report.get('unweighted_vertices') == 0
            and report.get('exit_code') == 0, 'The cloud rig did not pass its checks.')
    checks = report.get('checks')
    require(isinstance(checks, list) and 1 <= len(checks) <= 256
            and all(isinstance(c, dict) and (c.get('status') in ('pass', 'not_run')
                    or experimental and c.get('id') == 'anatomy' and c.get('status') == 'fail') for c in checks),
            'The cloud rig contains failed or missing checks.')
    require(not experimental or any(c.get('id') == 'anatomy' and c.get('status') == 'fail' for c in checks),
            'The experimental rig is missing its anatomy check.')
    for c in checks:
        fields(c, 'id status measured threshold detail mesh_id vertex_indices')
        require(isinstance(c['id'], str) and IDENTIFIER.fullmatch(c['id'])
                and all(c[k] is None or number(c[k], -1e12, 1e12) for k in ('measured', 'threshold'))
                and isinstance(c['detail'], str) and len(c['detail']) <= 300
                and (c['mesh_id'] is None or isinstance(c['mesh_id'], str) and IDENTIFIER.fullmatch(c['mesh_id']))
                and isinstance(c['vertex_indices'], list) and len(c['vertex_indices']) <= 256
                and all(type(i) is int and 0 <= i < 300000 for i in c['vertex_indices']),
                'One of the cloud rig checks could not be read.')
    fields(report['preservation'], 'geometry uvs materials shape_keys independent_drivers')
    fields(report['timings'], 'load_s preprocess_s inference_s export_s total_s')
    require(all(v is None for v in report['preservation'].values())
            and all(number(v, 0, 3600) for v in report['timings'].values())
            and type(report['peak_vram_bytes']) is int and 0 <= report['peak_vram_bytes'] <= 1099511627776
            and report['visual_approval'] == 'not_reviewed' and report['release_accepted'] is False,
            'The cloud rig report could not be read.')
    joints = rig.get('joints')
    require(isinstance(joints, list) and (2 if experimental else 15) <= len(joints) <= 256,
            'The skeleton has too many or too few bones.')
    if experimental:
        info = rig.get('experimental')
        fields(info, 'reasons joint_count inside_fraction unweighted_vertices')
        reasons = info['reasons']
        require(isinstance(reasons, list) and 1 <= len(reasons) <= len(rig_experimental.REASONS)
                and all(isinstance(r, str) and r in rig_experimental.REASONS for r in reasons)
                and len(set(reasons)) == len(reasons)
                and type(info['joint_count']) is int and info['joint_count'] == len(joints)
                and number(info['inside_fraction'], 0, 1)
                and type(info['unweighted_vertices']) is int and info['unweighted_vertices'] == 0,
                'The experimental rig report could not be verified.')
    names, roots = set(), 0
    for i, j in enumerate(joints):
        fields(j, 'name parent head tail rest_matrix deform semantic_role')
        require(isinstance(j, dict) and isinstance(j.get('name'), str) and 1 <= len(j['name']) <= 63
                and len(j['name'].encode('utf-8')) <= 63 and all(ord(c) >= 32 for c in j['name'])
                and j['name'] not in names, 'The skeleton has invalid or duplicate bone names.')
        names.add(j['name'])
        require(j['semantic_role'] is None or isinstance(j['semantic_role'], str)
                and 1 <= len(j['semantic_role']) <= 64, 'A bone has an unreadable body-part name.')
        parent = j.get('parent')
        roots += parent is None
        require(parent is None or type(parent) is int and 0 <= parent < i, 'The bones are not connected correctly.')
        require(type(j.get('deform')) is bool and finite_array(j.get('head'), 3)
                and finite_array(j.get('tail'), 3) and finite_array(j.get('rest_matrix'), 16),
                'The skeleton contains unusable bone positions.')
        m = j['rest_matrix']
        require(max(abs(m[12 + k] - (1 if k == 3 else 0)) for k in range(4)) <= 1e-6
                and max(abs(m[k * 4 + 3] - j['head'][k]) for k in range(3)) <= 1e-5
                and math.dist(j['head'], j['tail']) > 1e-7, 'A bone has an unusable starting position or direction.')
        determinant = (m[0] * (m[5] * m[10] - m[6] * m[9]) - m[1] * (m[4] * m[10] - m[6] * m[8])
                       + m[2] * (m[4] * m[9] - m[5] * m[8]))
        # Native GLTF matrices can contain scale. Blender uses the finite head,
        # tail and roll direction; the canonicalizer reconstructs anatomical axes.
        require(abs(determinant) > 1e-12, 'A bone has an unusable starting size.')
    require(roots == 1, 'The bones must form one connected skeleton.')
    plan = rig['canonical_plan']
    if not experimental:
        fields(plan, 'policy labels')
        require(plan['policy'] == 'mixamo-tokenrig-v1' and isinstance(plan['labels'], list)
                and len(plan['labels']) == len(joints), 'The rig uses body-part names this version cannot use.')
    roles = set()
    for i, label in enumerate(plan['labels'] if not experimental else []):
        fields(label, 'index source role name')
        require(type(label['index']) is int and label['index'] == i and label['source'] == joints[i]['name']
                and isinstance(label['role'], str) and re.fullmatch('[A-Za-z][A-Za-z0-9]{0,63}', label['role'])
                and label['role'] not in roles and label['name'] == 'mixamorig:' + label['role']
                and len(label['name'].encode('utf-8')) <= 63, 'The rig has unreadable or repeated body-part names.')
        roles.add(label['role'])
    meshes = rig.get('meshes')
    expected = {m['mesh_id']: m for m in task['meshes']}
    require(isinstance(meshes, list) and len(meshes) == len(expected)
            and all(isinstance(m, dict) for m in meshes)
            and {m.get('mesh_id') for m in meshes} == set(expected), 'The rig is missing a character part.')
    for mesh in meshes:
        fields(mesh, 'mesh_id source_vertex_count source_vertex_weights correspondence')
        source = expected[mesh['mesh_id']]
        rows, correspondence = mesh.get('source_vertex_weights'), mesh.get('correspondence', {})
        fields(correspondence, 'method source_position_sha256 source_topology_sha256 max_error_metres')
        require(mesh.get('source_vertex_count') == source['vertex_count'] and isinstance(rows, list)
                and len(rows) == source['vertex_count'], 'The returned shape does not match your original character.')
        require(isinstance(correspondence, dict) and correspondence.get('method') in ('exact', 'exact_seam_collapse')
                and correspondence.get('source_position_sha256') == source['position_sha256']
                and correspondence.get('source_topology_sha256') == source['topology_sha256']
                and type(correspondence.get('max_error_metres')) in (float, int)
                and 0 <= correspondence['max_error_metres'] <= 1e-5,
                'The returned shape could not be matched exactly to your original character.')
        for row in rows:
            require(isinstance(row, list) and 1 <= len(row) <= 8, 'Part of the character is not attached to the skeleton.')
            used, total = set(), 0
            for pair in row:
                require(isinstance(pair, list) and len(pair) == 2, 'A connection between the character and skeleton could not be read.')
                j, w = pair
                require(type(j) is int and 0 <= j < len(joints) and joints[j]['deform'] and j not in used
                        and type(w) in (int, float) and math.isfinite(w) and 0 < w <= 1,
                        'Part of the character has unusable connections to the skeleton.')
                used.add(j)
                total += w
            require(abs(total - 1) <= .001, 'The strength of the connections to the skeleton does not add up correctly.')
    return rig


def artifacts(outputs):
    require(isinstance(outputs, list) and len(outputs) == 3
            and all(isinstance(o, dict) for o in outputs)
            and {o.get('name') for o in outputs} == set(LIMITS), 'Some returned rig files are missing.')
    paths = {}
    for o in outputs:
        limit, mime = LIMITS[o['name']]
        require(o.get('mime') == mime and type(o.get('size')) is int and 0 < o['size'] <= limit
                and isinstance(o.get('sha256'), str) and HASH.fullmatch(o['sha256']),
                'A returned rig file has an unexpected format, size or contents.')
        require(isinstance(o.get('path'), str) and bool(o['path']), 'A downloaded rig file is missing.')
        path = Path(o['path'])
        require(path.is_file() and path.stat().st_size == o['size']
                and transport.digest(path.read_bytes()) == o['sha256'], 'The downloaded rig is damaged or changed.')
        paths[o['name']] = path
    transport.glb_json(paths['rigged.glb'])
    return paths
