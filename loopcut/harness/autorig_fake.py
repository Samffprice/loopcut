"""Local-only recorded TokenRig service for client regression/persona checks.

No keys, inference or vendor network requests. Bundle creation is scheduled on
Blender's main thread by the caller; HTTP handlers never access bpy directly.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import autorig_contract

# The platform's quote line (contract 0.9 schema const). The card shows rig_text.QUOTE instead (R10).
PLATFORM_NOTICE = 'Best on humanoid characters; others get an experimental rig that may bend oddly.'


def descriptor(skeleton='tokenrig-native-273b691-v1'):
    return {'name': 'auto_rig', 'namespace': 'loopcut_cloud', 'version': 3, 'data_class': 'A',
            'title': 'Rig humanoid', 'description': 'Rig one humanoid and preserve the original character.',
            'kind': 'assist', 'status': 'beta', 'available': True, 'unavailable_reason': None,
            'input_schema': {'type': 'object', 'properties': {
                **{k: {'type': 'string', 'enum': [v]} for k, v in inputs(skeleton).items()},
                'accept_experimental': {'type': 'boolean', 'description': 'Defaults to false; must match the uploaded task.'}},
                'required': list(inputs(skeleton)), 'additionalProperties': False},
            'file_inputs': [{'field': 'model', 'accept': ['model/gltf-binary'], 'max_mb': 50, 'required': True},
                            {'field': 'rig_task', 'accept': ['application/json'], 'max_mb': 1, 'required': True}],
            'output': {'type': 'rig', 'formats': ['glb', 'json']}, 'apply': 'apply_rig',
            'apply_args': {'type': 'object', 'properties': {'targets': {'type': 'array', 'minItems': 1,
                'maxItems': 64, 'uniqueItems': True, 'items': {'type': 'string', 'minLength': 1, 'maxLength': 63}}},
                'required': ['targets'], 'additionalProperties': False},
            'cost': {'credits_estimate': 20, 'quote_required': True}, 'latency': 'minutes',
            'approval': {'confirm_over_credits': 0}, 'backends': ['managed'], 'byok': None,
            'bundle_input': None, 'display': {'title': 'Auto-rig', 'category': 'characters',
                                             'summary': 'Rig your character while keeping its original mesh.'}}


def inputs(skeleton='tokenrig-native-273b691-v1'):
    return {'category': 'humanoid', 'route': 'managed', 'controls': 'deform_only', 'skeleton_version': skeleton}


def transformed_glb(source, destination, matrix):
    from mathutils import Matrix
    from loopcut import rig_transport as rt
    raw = Path(source).read_bytes()
    length = struct.unpack_from('<I', raw, 12)[0]
    doc = json.loads(raw[20:20 + length])
    binary = raw[28 + length:]
    conversion = Matrix([rt.TO_BLENDER[k:k + 4] for k in range(0, 16, 4)])
    transform = conversion.inverted() @ matrix @ conversion
    scene = doc['scenes'][doc.get('scene', 0)]
    roots = scene['nodes']
    node = len(doc['nodes'])
    doc['nodes'].append({'matrix': [transform[r][c] for c in range(4) for r in range(4)], 'children': roots})
    scene['nodes'] = [node]
    data = rt.json_bytes(doc)
    data += b' ' * (-len(data) % 4)
    Path(destination).write_bytes(struct.pack('<4sII', b'glTF', 2, 28 + len(data) + len(binary))
        + struct.pack('<II', len(data), 0x4e4f534a) + data
        + struct.pack('<II', len(binary), 0x004e4942) + binary)


def bundle(task, preparation, native, directory, source_transform=None, *, adversarial_plan=False,
           inside_fraction=None):
    """Re-envelope genuine recorded native output. No fabricated skeleton/weights.

    source_transform is an explicitly tested uniform fixture edit (e.g. scale);
    preparation normalization then puts the recorded result into upload space.
    """
    from mathutils import Matrix
    from loopcut import rig_transport as rt, rig_correspondence, rig_canonicalize, rig_bundle, rig_plan
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'rigged.glb'
    transform = preparation['to_world'].inverted() @ (source_transform if source_transform is not None else Matrix.Identity(4))
    transformed_glb(native, target, transform)
    joints, points, triangles, weights = rt.skin(target)
    # Sidecar order is parent-first, independent of a glTF skin's joint order.
    ordered = []
    def visit(i):
        if i in ordered:
            return
        p = joints[i]['parent']
        if p is not None:
            visit(p)
        ordered.append(i)
    for i in range(len(joints)):
        visit(i)
    mapping = {old: new for new, old in enumerate(ordered)}
    source_points, source_triangles = [], []
    for _, vertices, faces in preparation['geometry']:
        source_triangles.extend(tuple(len(source_points) + i for i in f) for f in faces)
        source_points.extend(vertices)
    extent = max(max(p[k] for p in source_points) - min(p[k] for p in source_points) for k in range(3))
    tolerance = max(extent * 1e-6, 1e-8)
    result_joints = []
    for i in ordered:
        j = joints[i]
        m = j['matrix'].copy()
        head = m.translation
        distances = [math.dist(j['head'], child['head']) for child in joints if child['parent'] == i]
        length = max(min(distances) if distances else extent * .025, 1e-5)
        tail = head + m.to_3x3().col[1].normalized() * length
        result_joints.append({'name': j['name'], 'parent': mapping.get(j['parent']), 'deform': True,
                              'semantic_role': None, 'head': list(head), 'tail': list(tail),
                              'rest_matrix': [v for row in m for v in row]})
    exact = rig_correspondence.transfer_weights(source_points, source_triangles, points, triangles,
        [{mapping[j]: w for j, w in row.items()} for row in weights], tolerance)
    planned = rig_plan.plan_json({'schema_version': 1,
        'joints': [{k: j[k] for k in ('name', 'parent', 'head')} for j in result_joints],
        'vertices': [list(p) for p in points]})
    experimental = planned['status'] == 'refused' and not adversarial_plan
    if experimental:
        assert task.get('accept_experimental') is True, 'Never publish experimental artifacts to an older caller'
        assert planned['anatomy_reason'] in rig_bundle.rig_experimental.REASONS
        assert inside_fraction is not None, 'Use a measured or recorded containment fraction'
        plan = None
    elif adversarial_plan:
        # Deliberately false metadata for local rejection tests only. The native
        # skeleton and exact weights stay genuine; no finger identity is inferred.
        plan = {'policy': 'mixamo-tokenrig-v1', 'labels': [
            {'index': i, 'source': j['name'], 'role': f'Unverified{i}', 'name': f'mixamorig:Unverified{i}'}
            for i, j in enumerate(result_joints)]}
    else:
        plan = rig_bundle.canonical_plan(rig_canonicalize.plan(
            [{k: j[k] for k in ('name', 'parent', 'head')} for j in result_joints], points))
    meshes, offset = [], 0
    for entry in task['meshes']:
        count = entry['vertex_count']
        meshes.append({'mesh_id': entry['mesh_id'], 'source_vertex_count': count,
                       'source_vertex_weights': [sorted([[j, w] for j, w in row.items()]) for row in exact[offset:offset + count]],
                       'correspondence': {'method': 'exact_seam_collapse',
                                         'source_position_sha256': entry['position_sha256'],
                                         'source_topology_sha256': entry['topology_sha256'], 'max_error_metres': tolerance}})
        offset += count
    rig = {'schema': 'loopcut.rig/0.1', 'task_id': task['task_id'], 'source_model_sha256': task['model_sha256'],
           'source_snapshot_sha256': task['source_snapshot_sha256'], 'model': {
               'code_commit': '273b691d35989d71cd17ff2895fdc735097b92d1',
               'weights_revision': 'recorded-native-offline-fixture', 'weights_sha256': [hashlib.sha256(Path(native).read_bytes()).hexdigest()]},
           'skeleton_version': task['skeleton_version'], 'coordinates': {'units': 'm', 'up': 'Z', 'space': 'blender_world'},
           'gltf_to_blender': rt.TO_BLENDER, 'joints': result_joints, 'meshes': meshes,
           'confidence': {'value': None, 'calibration': None}, 'canonical_plan': plan,
           'tier': 'experimental' if experimental else 'standard',
           'warnings': ['recorded_fixture_no_inference'] + (['unverified_labels_rejection_test_only'] if adversarial_plan else [])}
    report = {'schema': 'loopcut.rig-validation/0.1', 'task_id': task['task_id'], 'phase': 'worker',
              'status': 'pass', 'checks': [{'id': 'correspondence', 'status': 'pass', 'measured': tolerance,
              'threshold': tolerance, 'detail': 'Recorded fixture; correspondence error bounded by threshold.',
              'mesh_id': None, 'vertex_indices': []}], 'unweighted_vertices': 0, 'correspondence_valid': True,
              'preservation': dict.fromkeys(['geometry', 'uvs', 'materials', 'shape_keys', 'independent_drivers']),
              'timings': dict.fromkeys(['load_s', 'preprocess_s', 'inference_s', 'export_s', 'total_s'], 0),
              'peak_vram_bytes': 0, 'exit_code': 0, 'warnings': ['recorded_fixture_no_inference'],
              'visual_approval': 'not_reviewed', 'release_accepted': False}
    if experimental:
        rig['experimental'] = {'reasons': [planned['anatomy_reason']], 'joint_count': len(result_joints),
                               'inside_fraction': inside_fraction, 'unweighted_vertices': 0}
        report['checks'].append({'id': 'anatomy', 'status': 'fail', 'measured': None, 'threshold': None,
            'detail': planned['anatomy_reason'], 'mesh_id': None, 'vertex_indices': []})
    (directory / 'rig.json').write_bytes(rt.json_bytes(rig))
    (directory / 'validation.json').write_bytes(rt.json_bytes(report))
    return {name: (directory / name).read_bytes() for name in ('rigged.glb', 'rig.json', 'validation.json')}


class Server:
    def __init__(self, factory, *, seconds=.2, skeleton='tokenrig-native-273b691-v1'):
        self.factory, self.seconds = factory, seconds
        self.calls, self.blobs, self.jobs, self.outputs = [], {}, {}, {}
        self.failure, self.corrupt, self.charge = False, False, 0
        self.anatomy_reason = None
        self.reports, self.refund_review, self.refund_unavailable = {}, None, False
        self.refund_delay = 0
        self.refund_lock = threading.Lock()
        self.contract = autorig_contract.from_environment()
        self.descriptor = (copy.deepcopy(self.contract.descriptor) if self.contract else descriptor(skeleton))
        self.descriptor.update(available=True, status='beta')
        if self.contract:
            self.contract.check(self.descriptor, 'tool-descriptor')
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200, mime='application/json'):
                data = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                digest = self.path.rsplit('/', 1)[-1]
                if hashlib.sha256(raw).hexdigest() != digest:
                    return self.send({}, 400)
                owner.blobs[digest] = raw
                self.send(b'')

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                data = json.loads(raw) if raw else {}
                owner.calls.append((self.command, self.path, data))
                if self.path == '/offline/chat/completions':
                    chunks = [{'choices': [{'delta': {'content': 'The selected provider is working.'}}]},
                              {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}]
                    body = b''.join(b'data: ' + json.dumps(c).encode() + b'\n\n' for c in chunks) + b'data: [DONE]\n\n'
                    return self.send(body, mime='text/event-stream')
                if self.path == '/v1/blobs/check':
                    return self.send({'missing': [h for h in data['sha256'] if h not in owner.blobs]})
                if self.path == '/v1/blobs/upload-urls':
                    return self.send({'items': [{'sha256': row['sha256'], 'put_url': owner.url + '/upload/' + row['sha256'],
                        'expires_at': '2099-01-01T00:00:00Z'} for row in data['items']]})
                if self.path == '/v1/quotes':
                    if getattr(owner, 'age_required', False):
                        return self.send({'error': {'code': 'age_confirmation_required', 'type': 'invalid_input',
                            'message': 'Age confirmation required', 'confirm_url': 'https://loopcut.org/account'}}, 403)
                    assert data.get('tool_version') == 3 and self.headers.get('x-loopcut-contract') == '0.9'
                    task = json.loads(owner.blobs[data['files']['rig_task']])
                    assert data['input'].get('accept_experimental', False) == task.get('accept_experimental', False)
                    if owner.contract:
                        owner.contract.request(data, 'quote-request', owner.blobs)
                    # The attached contract's line: the platform changes it (1845ae2 did) without a client release.
                    notice = (owner.contract.fixture('quote/auto_rig-v3.json')['experimental_notice']
                              if owner.contract else PLATFORM_NOTICE)
                    quote = {'quote_id': 'qt_offline', 'credits': 20, 'expires_at': '2099-01-01T00:00:00Z',
                             'experimental_notice': notice}
                    if owner.contract:
                        owner.contract.check(quote, 'quote')
                        owner.contract.bind_quote(data, quote['quote_id'], create=True)
                    return self.send(quote)
                if self.path == '/v1/jobs':
                    assert 'targets' not in data['input'] and data['quote_id'] == 'qt_offline'
                    assert self.headers.get('x-loopcut-contract') == '0.9'
                    if owner.contract:
                        owner.contract.request(data, 'job-create-request', owner.blobs)
                    task = json.loads(owner.blobs[data['files']['rig_task']])
                    assert hashlib.sha256(owner.blobs[data['files']['model']]).hexdigest() == task['model_sha256']
                    key = 'job_' + uuid.uuid4().hex
                    owner.jobs[key] = {'start': time.monotonic(), 'task': task, 'canceled': False,
                                       'failure': owner.failure, 'data': None, 'charged': False}
                    return self.send(owner.job(key))
                if self.path.endswith('/apply-failed'):
                    time.sleep(owner.refund_delay)
                    key = self.path.split('/')[-2]
                    if owner.refund_unavailable or key not in owner.jobs:
                        return self.send({}, 404)
                    if not owner.jobs[key]['charged'] or owner.jobs[key]['data'] is None:
                        return self.send({}, 422)
                    assert set(data) == {'reason'} and data['reason'] in {
                        'correspondence_ambiguous', 'canonicalize_refused', 'validation_failed', 'other'}
                    assert self.headers.get('x-loopcut-contract') == '0.9'
                    with owner.refund_lock:
                        if key not in owner.reports:
                            refunded = 0 if owner.refund_review else 20
                            owner.reports[key] = {'job_id': key, 'reason': data['reason'],
                                'status': 'review' if owner.refund_review else 'refunded',
                                'review_reason': owner.refund_review, 'credits_refunded': refunded,
                                'reported_at': '2026-10-22T20:00:00Z', 'message': 'Recorded fake receipt.',
                                'support_url': 'https://loopcut.org/beta/report-a-bug'}
                            owner.jobs[key]['refund'] = refunded
                            owner.charge -= refunded
                        receipt = owner.reports[key]
                    return self.send(receipt)
                if self.path.endswith('/cancel'):
                    key = self.path.split('/')[-2]
                    owner.jobs[key]['canceled'] = True
                    return self.send(owner.job(key))
                self.send({}, 404)

            def do_GET(self):
                path = urlsplit(self.path).path
                owner.calls.append((self.command, path, None))
                if path == '/v1/manifest':
                    manifest = {'contract': {'version': '0.9', 'min_client': '0.1'}, 'tools': [owner.descriptor], 'skills': [],
                        'flags': {'services': True, 'premium_skills': False},
                        'account': {'plan': 'pro', 'credits': {'balance': 1000 - owner.charge, 'monthly': 1000,
                                    'resets_at': '2099-01-01T00:00:00Z'},
                                    'settings': {'no_generation': False, 'private_mode': True}}}
                    if owner.contract:
                        owner.contract.check(manifest, 'manifest')
                    return self.send(manifest)
                if path.endswith('/events'):
                    # Force the existing, tested HTTP polling fallback.
                    return self.send({}, 404)
                if path.startswith('/v1/jobs/'):
                    return self.send(owner.job(path.rsplit('/', 1)[-1]))
                if path.startswith('/output/'):
                    raw, mime = owner.outputs[path]
                    return self.send(raw + b'x' if owner.corrupt else raw, mime=mime)
                self.send({}, 404)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.http.server_address[1])
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def job(self, key):
        row = self.jobs[key]
        elapsed = time.monotonic() - row['start']
        status = ('canceled' if row['canceled'] else 'queued' if elapsed < self.seconds else
                  'running' if elapsed < self.seconds * 2 else 'failed' if row['failure'] else 'succeeded')
        output = []
        if status == 'succeeded':
            if row['data'] is None:
                row['data'] = self.factory(row['task'])
                if self.contract:
                    self.contract.bundle(row['data'], row['task'])
            rig = json.loads(row['data']['rig.json'])
            meta = {'tier': rig['tier'], 'reasons': rig.get('experimental', {}).get('reasons', []),
                    'canonicalization': 'not_applicable' if rig['tier'] == 'experimental' else 'local_pending'}
            if rig['tier'] == 'experimental':
                meta['experimental'] = rig['experimental']
            for name, data in row['data'].items():
                mime = 'model/gltf-binary' if name.endswith('.glb') else 'application/json'
                path = '/output/' + key + '/' + name
                self.outputs[path] = data, mime
                output.append({'name': name, 'type': 'rig', 'mime': mime, 'size': len(data),
                               'sha256': hashlib.sha256(data).hexdigest(), 'url': self.url + path,
                               'expires_at': '2099-01-01T00:00:00Z', 'meta': meta})
            if not row['charged']:
                self.charge += 20
                row['charged'] = True
        messages = {'queued': 'queued',
                    'running': 'working', 'succeeded': 'Rig ready to apply.',
                    'failed': 'Rigging failed. Your 20 credits were refunded.',
                    'canceled': 'Rigging canceled. Your 20 credits were refunded.'}
        job = {'id': key, 'tool': 'auto_rig', 'status': status,
                'progress': {'pct': {'queued': 10, 'running': 50}.get(status, 100), 'message': messages[status]},
                'credits': {'reserved': 20, 'charged': 20 - row.get('refund', 0) if status == 'succeeded' else 0,
                            'refunded': 20 if status in ('failed', 'canceled') else row.get('refund', 0)},
                'outputs': output, 'provenance': {'job_id': key, 'tool': 'auto_rig', 'vendor': 'recorded-fixture',
                    'model': 'tokenrig-native-m1', 'kind': 'assist', 'created_at': '2026-10-22T00:00:00Z'},
                'error': {'message': messages[status], 'code': 'vendor_failed', 'type': 'vendor_failed',
                          'refunded': True,
                          **({'rig_code': 'unsupported_anatomy', 'anatomy_reason': self.anatomy_reason}
                             if self.anatomy_reason else {})} if status == 'failed' else None}
        if self.contract:
            self.contract.check(job, 'job')
        return job

    def close(self):
        self.http.shutdown()
        self.http.server_close()
