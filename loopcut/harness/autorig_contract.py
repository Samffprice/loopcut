"""Optional private-platform oracle for offline rig HTTP tests; never shipped.

Load the platform's own validator, schemas and fixtures, without copying them.
The regression gate requires LOOPCUT_CONTRACT_DIR; ordinary public unit tests
can run without access to the private repository.
"""
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path


class Contract:
    def __init__(self, root):
        self.root = Path(root)
        path = self.root / 'conformance/validate.py'
        spec = importlib.util.spec_from_file_location('autorig_platform_validate', path)
        self.validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.validator)
        self.schemas = self.validator.load_schemas(self.root / 'schemas')
        self.descriptor = self.fixture('tool-descriptor/auto_rig.json')
        self.schemas['autorig-input'] = self.descriptor['input_schema']
        self.validator.check_schema(self.schemas['autorig-input'], 'autorig-input', self.schemas)
        assert self.descriptor['version'] == 3, 'Review the client before accepting a new tool version'
        self.records = []
        self.quotes = {}
        self.bindings = []

    def fixture(self, name):
        return json.loads((self.root / 'fixtures' / name).read_bytes())

    def check(self, value, schema):
        errors = self.validator.validate(value, schema, self.schemas)
        self.records.append({'schema': schema, 'errors': errors,
                             'sha256': hashlib.sha256(json.dumps(value, sort_keys=True,
                                 separators=(',', ':'), allow_nan=False).encode()).hexdigest()})
        assert not errors, f'{schema}: {errors}'

    def request(self, value, schema, blobs):
        self.check(value, schema)
        self.check(value['input'], 'autorig-input')
        assert value['tool'] == 'auto_rig' and value['tool_version'] == 3
        assert set(value['files']) == {'model', 'rig_task'}
        for digest in value['files'].values():
            assert hashlib.sha256(blobs[digest]).hexdigest() == digest
        task = json.loads(blobs[value['files']['rig_task']])
        self.check(task, 'rig-task')
        assert task.get('accept_experimental', False) is value['input'].get('accept_experimental', False)
        assert task['model_sha256'] == value['files']['model']
        if schema == 'job-create-request':
            self.bind_quote(value, value['quote_id'])

    def bind_quote(self, request, quote_id, *, create=False):
        # Match the platform runner's quote identity, including exact input
        # shape: omission and explicit false have different request hashes.
        identity = {k: request[k] for k in ('tool', 'tool_version', 'input', 'files')}
        identity['bundle'] = request.get('bundle')
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if create:
            self.quotes[quote_id] = digest
        else:
            assert self.quotes.get(quote_id) == digest, 'The job differs from its approved quote'
            self.bindings.append({'quote_id': quote_id, 'identity_sha256': digest,
                                  'files': request['files'],
                                  'accept_experimental': request['input'].get('accept_experimental', False)})

    def bundle(self, data, task):
        rig, report = [json.loads(data[name]) for name in ('rig.json', 'validation.json')]
        self.check(rig, 'rig')
        self.check(report, 'rig-validation')
        assert rig['task_id'] == report['task_id'] == task['task_id']
        assert rig['source_model_sha256'] == task['model_sha256']
        assert rig['source_snapshot_sha256'] == task['source_snapshot_sha256']
        experimental = rig['tier'] == 'experimental'
        assert not experimental or task.get('accept_experimental') is True
        assert any(c['id'] == 'anatomy' and c['status'] == 'fail' for c in report['checks']) is experimental
        result = {'schema': 'loopcut.rig-result/0.1', 'task_id': task['task_id'], 'status': 'succeeded',
                  'tier': rig['tier'], 'reasons': rig.get('experimental', {}).get('reasons', []),
                  'skeleton_version': rig['skeleton_version'], 'bone_count': len(rig['joints']),
                  'vertex_count': sum(m['source_vertex_count'] for m in rig['meshes']), 'warnings': rig['warnings'],
                  'artifacts': [{'name': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                                 'mime': 'model/gltf-binary' if name.endswith('.glb') else 'application/json'}
                                for name, raw in data.items()]}
        self.check(result, 'rig-result')

    def proof(self):
        sources = [self.root / 'conformance/validate.py', *sorted((self.root / 'schemas').glob('*.json')),
                   self.root / 'fixtures/tool-descriptor/auto_rig.json', self.root / 'fixtures/quote/auto_rig-v3.json']
        return {'contract_dir': str(self.root), 'contract_version': '0.9', 'tool_version': 3,
                'schemas_checked': dict(Counter(r['schema'] for r in self.records)),
                'records': self.records, 'quote_job_bindings': self.bindings,
                'source_sha256': {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}


def from_environment(*, required=False):
    root = os.environ.get('LOOPCUT_CONTRACT_DIR')
    if not root:
        if required:
            raise RuntimeError('Set LOOPCUT_CONTRACT_DIR to the fetched platform contract for this gate')
        return None
    return Contract(root)  # An explicitly configured missing contract is always a failure.
