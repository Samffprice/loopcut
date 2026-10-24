"""Bounded geometry transport, shared by local preparation and bundle verification."""
import hashlib
import json
import math
import struct
from pathlib import Path

MAX_INPUT = 50_000_000
TO_BLENDER = [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def read_json(path, limit):
    def invalid(value):
        raise ValueError('Non-finite JSON number')
    path = Path(path)
    if not 1 <= path.stat().st_size <= limit:
        raise ValueError('Rig sidecar size is outside its limit')
    return json.loads(path.read_bytes(), parse_constant=invalid)


def export(path, meshes):
    """Write only world geometry: one primitive/node per original, exact vertex order.

    No Blender exporter, modifiers, texture paths, shape keys or names leave the
    machine. Mesh entries are (opaque id, preparation-space points, triangles).
    """
    doc = {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': []}],
           'nodes': [], 'meshes': [], 'bufferViews': [], 'accessors': []}
    binary, manifest = bytearray(), []
    for mesh_id, points, triangles in meshes:
        positions = b''.join(struct.pack('<3f', p[0], p[2], -p[1]) for p in points)
        indices = b''.join(struct.pack('<3I', *t) for t in triangles)
        accessor_ids = []
        for data, count, component, kind in [(positions, len(points), 5126, 'VEC3'),
                                               (indices, len(triangles) * 3, 5125, 'SCALAR')]:
            view = len(doc['bufferViews'])
            doc['bufferViews'].append({'buffer': 0, 'byteOffset': len(binary), 'byteLength': len(data)})
            binary.extend(data)
            accessor_ids.append(len(doc['accessors']))
            acc = {'bufferView': view, 'componentType': component, 'count': count, 'type': kind}
            if kind == 'VEC3':
                xyz = [struct.unpack_from('<3f', positions, i * 12) for i in range(len(points))]
                acc.update(min=[min(p[k] for p in xyz) for k in range(3)],
                           max=[max(p[k] for p in xyz) for k in range(3)])
            doc['accessors'].append(acc)
        node = len(doc['nodes'])
        doc['nodes'].append({'mesh': node})
        doc['scenes'][0]['nodes'].append(node)
        doc['meshes'].append({'primitives': [{'attributes': {'POSITION': accessor_ids[0]},
                                            'indices': accessor_ids[1], 'mode': 4}]})
        manifest.append({'mesh_id': mesh_id, 'gltf_node': node, 'vertex_count': len(points),
                         'triangle_count': len(triangles), 'position_sha256': digest(positions),
                         'topology_sha256': digest(indices)})
    doc['buffers'] = [{'byteLength': len(binary)}]
    header = json_bytes(doc)
    header += b' ' * (-len(header) % 4)
    binary.extend(b'\0' * (-len(binary) % 4))
    data = (struct.pack('<4sII', b'glTF', 2, 28 + len(header) + len(binary))
            + struct.pack('<II', len(header), 0x4e4f534a) + header
            + struct.pack('<II', len(binary), 0x004e4942) + binary)
    if len(data) > MAX_INPUT:
        raise ValueError('The geometry upload exceeds 50 MB. Use a lower-detail copy.')
    Path(path).write_bytes(data)
    glb_json(path)
    return manifest


def skin(path):
    """Decode a bounded native skin for cross-file verification, without importing it.

    mathutils ships with Blender. Binary rows and transforms are validated before
    constructing matrices; no external paths or executable assets are imported.
    """
    from mathutils import Matrix, Quaternion, Vector
    doc = glb_json(path)
    data = Path(path).read_bytes()
    offset = 20 + struct.unpack_from('<I', data, 12)[0]
    binary = data[offset + 8:]
    def accessor(index, *, weights=False):
        if type(index) is not int or not 0 <= index < len(doc['accessors']):
            raise ValueError('Invalid skin accessor')
        acc = doc['accessors'][index]
        fmt = {5121: 'B', 5123: 'H', 5125: 'I', 5126: 'f'}.get(acc['componentType'])
        width = {'SCALAR': 1, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}.get(acc['type'])
        normalized = acc.get('normalized', False)
        if (not fmt or not width or type(normalized) is not bool
                or normalized and not (weights and acc['componentType'] in (5121, 5123))
                or weights and (width != 4 or acc['componentType'] not in (5126, 5121, 5123)
                                or acc['componentType'] != 5126 and not normalized)):
            raise ValueError('Unsupported native skin accessor')
        if acc['count'] > (256 if acc['type'] == 'MAT4' else 300_000):
            raise ValueError('Skin accessor exceeds its bounded row count')
        fmt = '<' + fmt * width
        view = doc['bufferViews'][acc['bufferView']]
        start = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
        rows = [struct.unpack_from(fmt, binary, start + i * view.get('byteStride', struct.calcsize(fmt)))
                for i in range(acc['count'])]
        if normalized:
            divisor = 255 if acc['componentType'] == 5121 else 65535
            rows = [tuple(v / divisor for v in row) for row in rows]
        if any(not all(math.isfinite(v) for v in row) for row in rows):
            raise ValueError('Non-finite skin values')
        return rows
    nodes = doc.get('nodes', [])
    parents = {c: i for i, n in enumerate(nodes) for c in n.get('children', [])}
    cache = {}
    def world(i):
        if i in cache:
            return cache[i]
        n = nodes[i]
        if 'matrix' in n:
            if len(n['matrix']) != 16:
                raise ValueError('Invalid node matrix')
            local = Matrix([n['matrix'][k:k + 4] for k in range(0, 16, 4)]).transposed()
        else:
            t, q, s = n.get('translation', [0, 0, 0]), n.get('rotation', [0, 0, 0, 1]), n.get('scale', [1, 1, 1])
            if (len(t), len(q), len(s)) != (3, 4, 3):
                raise ValueError('Invalid node transform')
            local = Matrix.LocRotScale(Vector(t), Quaternion((q[3], *q[:3])), Vector(s))
        if abs(local.determinant()) < 1e-12:
            raise ValueError('Singular node transform')
        cache[i] = world(parents[i]) @ local if i in parents else local
        return cache[i]
    skins = doc.get('skins', [])
    if len(skins) != 1:
        raise ValueError('The result must contain exactly one skeleton')
    skeleton = skins[0]
    node_ids = skeleton['joints']
    ids = {n: i for i, n in enumerate(node_ids)}
    conversion = Matrix([TO_BLENDER[k:k + 4] for k in range(0, 16, 4)])
    joints = []
    for n in node_ids:
        matrix = conversion @ world(n)
        joints.append({'name': nodes[n].get('name'), 'parent': ids.get(parents.get(n)),
                       'head': tuple(matrix.translation), 'matrix': matrix})
    inverse = accessor(skeleton['inverseBindMatrices'])
    if len(inverse) != len(joints):
        raise ValueError('Inverse bind count differs')
    points, triangles, weights = [], [], []
    for i, node in enumerate(nodes):
        if 'mesh' not in node:
            continue
        if node.get('skin') != 0:
            raise ValueError('An output mesh is not bound to the skeleton')
        if type(node['mesh']) is not int or not 0 <= node['mesh'] < len(doc['meshes']):
            raise ValueError('Invalid mesh node')
        mesh_world = world(i)
        for j, flat in enumerate(inverse):
            bind = Matrix([flat[k:k + 4] for k in range(0, 16, 4)]).transposed()
            rest = world(node_ids[j]) @ bind
            # The worker contract uses identity binds. A GLTF mesh-local bind is
            # also valid (recorded fixtures transformed as a complete scene).
            error = min(max(abs(rest[r][c] - expected[r][c]) for r in range(4) for c in range(4))
                        for expected in (Matrix.Identity(4), mesh_world))
            if error > 1e-3:
                raise ValueError('Inverse bind matrices disagree with the rest pose')
        for primitive in doc['meshes'][node['mesh']]['primitives']:
            base = len(points)
            attrs = primitive['attributes']
            positions = accessor(attrs['POSITION'])
            if len(points) + len(positions) > 300_000:
                raise ValueError('Instanced skin exceeds its vertex limit')
            points.extend(tuple(conversion @ mesh_world @ Vector(p)) for p in positions)
            influence_sets = []
            for k in (0, 1):
                if f'JOINTS_{k}' in attrs:
                    ji, we = accessor(attrs[f'JOINTS_{k}']), accessor(attrs[f'WEIGHTS_{k}'], weights=True)
                    if len(ji) != len(positions) or len(we) != len(positions):
                        raise ValueError('Skin rows do not match positions')
                    influence_sets.append((ji, we))
            for v in range(len(positions)):
                row = {}
                for ji, we in influence_sets:
                    for j, w in zip(ji[v], we[v]):
                        if w == 0:
                            continue
                        if type(j) is not int or not 0 <= j < len(joints) or not 0 < w <= 1 or j in row:
                            raise ValueError('Invalid or duplicate skin influence')
                        row[j] = w
                if not row or abs(sum(row.values()) - 1) > .001:
                    raise ValueError('A vertex has missing or unnormalized weights')
                weights.append(row)
            indices = [v[0] for v in accessor(primitive['indices'])] if 'indices' in primitive else list(range(len(positions)))
            if any(type(v) is not int or not 0 <= v < len(positions) for v in indices):
                raise ValueError('Triangle index outside its primitive')
            if len(triangles) + len(indices) // 3 > 100_000:
                raise ValueError('Instanced skin exceeds its triangle limit')
            triangles.extend(tuple(base + v for v in indices[k:k + 3]) for k in range(0, len(indices), 3))
    return joints, points, triangles, weights

def glb_json(path, max_bytes=MAX_INPUT):
    """Bound the mesh transport before Blender import; not a full glTF validator."""
    path = Path(path)
    if not 20 <= path.stat().st_size <= max_bytes:
        raise ValueError('GLB size outside bounds')
    data = path.read_bytes()
    if struct.unpack_from('<4sII', data) != (b'glTF', 2, len(data)):
        raise ValueError('invalid GLB header')
    length, kind = struct.unpack_from('<II', data, 12)
    if kind != 0x4E4F534A or length > len(data) - 20 or length % 4:
        raise ValueError('invalid GLB JSON chunk')
    doc = json.loads(data[20:20 + length])
    if doc.get('asset', {}).get('version') != '2.0':
        raise ValueError('unsupported glTF version')
    for row in doc.get('buffers', []) + doc.get('images', []):
        if 'uri' in row:
            raise ValueError('only embedded GLB buffers/images are allowed')
    if doc.get('extensionsRequired'):
        raise ValueError('compressed/extension-dependent GLB needs explicit review')
    _finite_tree(doc)
    binary_start = 20 + length
    binary = b''
    if binary_start < len(data):
        if len(data) - binary_start < 8:
            raise ValueError('truncated GLB binary header')
        size, kind = struct.unpack_from('<II', data, binary_start)
        if kind != 0x004E4942 or size % 4 or binary_start + 8 + size != len(data):
            raise ValueError('invalid GLB binary chunk')
        binary = data[binary_start + 8:]
    buffers = doc.get('buffers', [])
    if len(buffers) != 1 or not 0 <= len(binary) - buffers[0]['byteLength'] <= 3:
        raise ValueError('GLB must have one correctly sized embedded buffer')
    views = doc.get('bufferViews', [])
    for view in views:
        offset, size = view.get('byteOffset', 0), view['byteLength']
        if (view.get('buffer') != 0 or type(offset) is not int or type(size) is not int
                or min(offset, size) < 0 or offset + size > buffers[0]['byteLength']):
            raise ValueError('buffer view exceeds binary bounds')
    accessors = doc.get('accessors', [])
    widths = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
    elements = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT2': 4, 'MAT3': 9, 'MAT4': 16}
    for acc in accessors:
        count, component, kind = acc['count'], acc['componentType'], acc['type']
        if (type(count) is not int or not 1 <= count <= 3_000_000 or
                component not in widths or kind not in elements or 'sparse' in acc):
            raise ValueError('unsupported or unbounded accessor; sparse inputs need review')
        index = acc.get('bufferView')
        if type(index) is not int or not 0 <= index < len(views):
            raise ValueError('invalid accessor buffer view')
        view = views[index]
        item_size = widths[component] * elements[kind]
        stride, offset = view.get('byteStride', item_size), acc.get('byteOffset', 0)
        if (type(stride) is not int or not item_size <= stride <= 252 or
                type(offset) is not int or offset < 0 or
                offset + (count - 1) * stride + item_size > view['byteLength']):
            raise ValueError('accessor exceeds buffer view')
    def accessor(index):
        if type(index) is not int or not 0 <= index < len(accessors):
            raise ValueError('invalid accessor index')
        return accessors[index]
    triangles, vertices = 0, 0
    meshes = doc.get('meshes', [])
    if not 1 <= len(meshes) <= 64:
        raise ValueError('mesh count outside bounds')
    for mesh in meshes:
        for primitive in mesh['primitives']:
            pos = accessor(primitive['attributes']['POSITION'])
            if pos['type'] != 'VEC3' or pos['componentType'] != 5126 or primitive.get('mode', 4) != 4:
                raise ValueError('requires float positions and triangles')
            view = views[pos['bufferView']]
            start = view.get('byteOffset', 0) + pos.get('byteOffset', 0)
            stride = view.get('byteStride', 12)
            for index in range(pos['count']):
                if not all(math.isfinite(v) for v in struct.unpack_from('<3f', binary, start + index * stride)):
                    raise ValueError('non-finite binary position')
            vertices += pos['count']
            indices = accessor(primitive['indices']) if 'indices' in primitive else pos
            count = indices['count']
            if count % 3:
                raise ValueError('incomplete triangle')
            triangles += count // 3
    if vertices > 300_000 or triangles > 100_000:
        raise ValueError('mesh exceeds the rig transport limit')
    nodes = doc.get('nodes', [])
    if not 1 <= len(nodes) <= 1024:
        raise ValueError('node count outside bounds')
    parents = {}
    for i, node in enumerate(nodes):
        for child in node.get('children', []):
            if type(child) is not int or not 0 <= child < len(nodes) or child in parents:
                raise ValueError('invalid or multiply parented node')
            parents[child] = i
    for i in parents:
        seen = {i}
        while i in parents:
            i = parents[i]
            if i in seen:
                raise ValueError('node hierarchy cycle')
            seen.add(i)
    for skin in doc.get('skins', []):
        joints = skin['joints']
        if (not 1 <= len(joints) <= 256 or len(set(joints)) != len(joints)
                or any(type(i) is not int or not 0 <= i < len(nodes) for i in joints)):
            raise ValueError('invalid skin joints')
    return doc


def _finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('non-finite value')
    if isinstance(value, dict):
        for item in value.values():
            _finite_tree(item)
    elif isinstance(value, list):
        for item in value:
            _finite_tree(item)
