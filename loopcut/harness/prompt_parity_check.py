"""V1 byte parity: actual old add-on -> fake_model vs new -> prompt_proxy -> fake_model.

V1 keeps a1b09694021 as the reference. Only the worker replacements recorded in
the platform's SOURCE.json are applied to old prompts/bodies at comparison time;
each must match exactly once. Captured bytes remain untouched. Other roles and
skill bodies still require exact parity with the current platform files.

Private artifacts stay in SCRATCH, outside the repository. No Blender or external model is used.
Only scene/tool execution, UI/persistence and nondeterminism are stubbed in the
production scenarios. The wire fixture is a synthetic unit-test transport check.
"""
import argparse
from contextlib import ExitStack
import copy
import dataclasses
import hashlib
from http.server import ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import traceback
import types
from unittest import mock
from urllib.parse import urlsplit
import uuid

import fake_model
import prompt_assembly as assembly
import prompt_proxy

REPO = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).resolve()
OLD_REV = 'a1b09694021'
ADDON = Path('scripts/addons_core/loopcut')
PLATFORM = Path('/Volumes/BackupFiles/MasterCoding/BlenderCursor/loopcut-web-platform')
# /private/tmp on macOS; Linux (the open repository's CI) has no /private, so its temp folder.
SCRATCH = Path('/private/tmp') if sys.platform == 'darwin' else Path(tempfile.gettempdir()).resolve()
SCENE = '<scene_context>\nfile: unsaved\nobjects: Camera (CAMERA), Cube (MESH)\n</scene_context>'
ADDONS = 'Installed add-ons: Parity Probe [parity_probe] bpy.ops.parity_probe.say_* (2)'
IMAGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9WQAAAAASUVORK5CYII='
LEGACY_CONSTANTS = (('agent', 'SYSTEM_PROMPT'), ('orchestrator', 'PROMPT'), ('workers', 'PROMPT'), ('router', 'PROMPT'))


class IncompleteParity(ValueError):
    """Missing baseline evidence cannot certify parity."""


def load_server_edits(path):
    """Load private replacements at runtime; diagnostics never include their text."""
    try:
        metadata = json.loads(path.read_bytes())
    except (OSError, ValueError) as ex:
        raise IncompleteParity('Cannot read server-edits SOURCE.json') from ex
    if not isinstance(metadata, dict) or not isinstance(metadata.get('server_edits'), list):
        raise IncompleteParity('Invalid server_edits metadata')
    pairs = []
    for edit in metadata['server_edits']:
        if not isinstance(edit, dict) or not isinstance(edit.get('prompt'), str):
            raise IncompleteParity('Invalid server edit record')
        if edit['prompt'] != 'worker':
            continue
        replacements = edit.get('replace')
        if not isinstance(replacements, list) or not replacements:
            raise IncompleteParity('Invalid worker replacements')
        for pair in replacements:
            if (not isinstance(pair, list) or len(pair) != 2 or
                    not all(isinstance(value, str) for value in pair) or not pair[0] or pair[0] == pair[1]):
                raise IncompleteParity('Invalid worker replacement pair')
            pairs.append(tuple(pair))
    if not pairs:
        raise IncompleteParity('No recorded worker replacements')
    return pairs


def apply_server_edits(text, role, edits):
    """The only permitted baseline changes are exact, once-only worker edits."""
    if role != 'worker':
        return text
    for number, (old, new) in enumerate(edits, 1):
        count = text.count(old)
        if count != 1:
            raise ValueError(f'Worker replacement {number} matched {count} times; expected exactly once')
        text = text.replace(old, new, 1)
    return text


def rebaseline_body(raw, role, edits):
    """Replace only the system string's JSON token, preserving all other wire bytes."""
    if role != 'worker' or not edits:
        return raw
    body = json.loads(raw)
    messages = body.get('messages', [])
    system = [m.get('content') for m in messages if m.get('role') == 'system']
    if len(system) != 1 or not isinstance(system[0], str):
        raise ValueError('Expected exactly one worker system string')
    changed = apply_server_edits(system[0], role, edits)
    before, after = (json.dumps(text).encode('utf-8') for text in (system[0], changed))
    # llm.stream_chat uses json.dumps defaults. Do not normalize a different
    # serialization into a false pass, or change matching user/tool text.
    if raw.count(before) != 1:
        raise ValueError('Worker system JSON token is not unique or has unexpected serialization')
    return raw.replace(before, after, 1)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def private_dir(path):
    path = Path(path).resolve()
    if not path.is_relative_to(SCRATCH) or path == SCRATCH or path.is_relative_to(REPO):
        raise ValueError(f'Parity artifacts must be under {SCRATCH}, outside the repository')
    return path


def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith('LOOPCUT_')}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONHASHSEED='0', PYTHONPATH='', TZ='UTC')
    return env


def validate_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError('At least one scenario is required')
    names = set()
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get('name'), str)
                or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', case['name'])
                or case['name'] in names
                or case.get('role') not in (*assembly.PROMPT_IDS, 'summary', 'reference')
                or case.get('kind') not in ('loop', 'worker', 'router', 'summary', 'reference', 'wire')):
            raise ValueError('Invalid or duplicate scenario')
        names.add(case['name'])


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def first_path(old, new, path='$'):
    """First content difference in traversal order; never expose private values."""
    if type(old) is not type(new):
        return path
    if isinstance(old, dict):
        for key in dict.fromkeys([*old, *new]):
            child = path + '[' + json.dumps(key) + ']'
            if key not in old or key not in new:
                return child
            found = first_path(old[key], new[key], child)
            if found:
                return found
    elif isinstance(old, list):
        for n, (a, b) in enumerate(zip(old, new)):
            found = first_path(a, b, f'{path}[{n}]')
            if found:
                return found
        if len(old) != len(new):
            return f'{path}[{min(len(old), len(new))}]'
    elif old != new:
        return path
    return None


def compare_bytes(old, new):
    if old == new:
        return None
    offset = next((n for n, (a, b) in enumerate(zip(old, new)) if a != b), min(len(old), len(new)))
    try:
        path = first_path(json.loads(old), json.loads(new))
        kind = 'content' if path else 'serialization'
    except (ValueError, UnicodeError):
        path, kind = '$', 'invalid_json'
    return {'kind': kind, 'path': path, 'byte_offset': offset,
            'old_bytes': len(old), 'new_bytes': len(new), 'old_sha256': digest(old), 'new_sha256': digest(new)}


def brief(craft, words, identity='probe', aspects=()):
    return {'id': identity, 'deliverable': 'Probe', 'prefix': 'Probe', 'role': craft,
            'owns': [], 'aspects': list(aspects), 'requirements': [{'quote': words, 'do': words}],
            'style': '', 'checks': []}


def scenarios():
    cases = []
    for role in ('single', 'orchestrator'):
        replies = [fake_model.tool_reply('run_python', {'code': 'pass', 'summary': 'Apply fixed edit'}),
                   fake_model.text_reply('First turn complete.'), fake_model.text_reply('Second turn complete.')]
        if role == 'orchestrator':
            replies.insert(0, fake_model.tool_reply('delegate', {'wave': {
                'shared': {'units': 'm'}, 'workers': [brief('modeling', 'Build a prop', 'shape'),
                                                   brief('materials', 'Finish a prop', 'finish')]}}))
        for addons in (False, True):
            cases.append({'name': role + ('-addons' if addons else '-plain'), 'role': role,
                          'kind': 'loop', 'addons': addons, 'replies': replies})
    # Ids assert coverage; expected prompt text is never reconstructed by the checker.
    workers = [
        ('modeling-default', 'modeling', 'Build a prop', [], ()),
        ('modeling-swap-1', 'modeling', 'Build one joined mesh', ['craft/modeling/swap-1'], ()),
        ('modeling-swap-2', 'modeling', 'Build a flat-shaded low-poly prop', ['craft/modeling/swap-2'], ()),
        ('modeling-swap-3', 'modeling', 'Build a prop; create no new materials', ['craft/modeling/swap-3'], ()),
        ('modeling-painterly', 'modeling', 'Build painterly low poly furniture; create no new materials',
         ['craft/modeling/swap-4', 'craft/modeling/painterly', 'craft/modeling/stylised'], ()),
        ('modeling-toon', 'modeling', 'Build toon stylised wooden furniture with metal bolts',
         ['craft/modeling/swap-4', 'craft/modeling/toon', 'craft/modeling/stylised', 'craft/modeling/furniture'], ()),
        ('modeling-swaps-3-4', 'modeling', 'Build a painterly prop; create no new materials',
         ['craft/modeling/swap-3', 'craft/modeling/swap-4'], ()),
        ('modeling-hard-surface', 'modeling', 'Build a hard-surface mechanical prop with bolts', [], ()),
        ('materials-glass', 'materials', 'Make glass with refraction', [], ()),
        ('materials-metal', 'materials', 'Make brushed metal steel', [], ()),
        ('lighting', 'lighting', 'Light a studio product', [], ('lighting',)),
        ('lighting-shot', 'lighting', 'Frame and light a studio product',
         ['craft/lighting/shot-camera'], ('lighting', 'camera')),
        ('camera', 'camera', 'Frame the whole product with perspective', [], ('camera',)),
        ('animation', 'animation', 'Animate a looping turntable with keyframes', [], ()),
    ]
    worker_replies = [fake_model.tool_reply('run_python', {'code': 'pass', 'summary': 'Build fixed prop'}),
                      fake_model.tool_reply('report_back', {'summary': 'Fixed prop complete.'})]
    for name, craft, words, required, aspects in workers:
        cases.append({'name': 'worker-' + name, 'kind': 'worker', 'role': 'worker',
                      'brief': brief(craft, words, aspects=aspects), 'required_skills': required,
                      'replies': worker_replies})
    cases.append({'name': 'worker-role-change', 'kind': 'worker', 'role': 'worker',
                  'brief': brief('modeling', 'Build a prop'),
                  'continuation': brief('materials', 'Make glass and metal'), 'replies': worker_replies * 2})
    for attached in (False, True):
        cases.append({'name': 'router-' + ('image' if attached else 'plain'), 'kind': 'router',
                      'role': 'router', 'attached': attached,
                      'replies': [fake_model.text_reply('{"level":1,"deliverables":[],"why":"Fixed routing"}')] * 2})
    for role in ('summary', 'reference'):
        cases.append({'name': 'control-' + role, 'kind': role, 'role': role,
                      'replies': [fake_model.text_reply('Fixed control result.')]})
    return cases


def load_addon(build):
    sys.path.insert(0, str(build / 'scripts/addons_core'))
    bpy = types.ModuleType('bpy')
    bpy.app = types.SimpleNamespace(driver_namespace={})
    sys.modules['bpy'] = bpy
    modules = types.SimpleNamespace(**{name: importlib.import_module('loopcut.' + name) for name in (
        'agent', 'orchestrator', 'workers', 'router', 'context', 'state', 'config', 'conversations', 'waves', 'llm', 'skills')})
    for module in vars(modules).values():
        if not Path(module.__file__).resolve().is_relative_to((build / ADDON).resolve()):
            raise RuntimeError('Imported an add-on from the wrong build')
    return modules


def snapshot(build, out):
    modules = load_addon(build)
    out.mkdir(parents=True)
    texts = {module: getattr(getattr(modules, module), name) for module, name in LEGACY_CONSTANTS}
    prompts = dict(single=texts['agent'], orchestrator=texts['agent'] + '\n\n' + texts['orchestrator'],
                   worker=texts['workers'], router=texts['router'])
    for identity, text in prompts.items():
        (out / identity).write_bytes(text.encode('utf-8'))


def session_for(m, name):
    session = m.state.new_session()
    session['id'] = digest(name.encode())[:32]
    return session


def add_user(m, session, text, attached=False):
    message = {'role': 'user', 'content': SCENE + '\n\n' + text}
    if attached:
        message.update({m.context.ATTACHED: True, 'content': [
            {'type': 'text', 'text': message['content']}, {'type': 'image_url', 'image_url': {'url': IMAGE}}]})
    session['messages'].append(message)
    session['items'].append(m.state.item_user(text))
    session['turn_number'] = session.get('turn_number', 0) + 1
    return message


def run_turn(m, session, cfg, spec):
    turn = m.agent.Turn()
    turn.config = cfg
    ran = []

    def execute(name, arguments):
        ran.append(name)
        return types.SimpleNamespace(text='Canned delegate result: two workers merged.' if name == 'delegate'
                                     else 'Canned tool result: success.', ok=True, image_path=None, changed=False)

    session['busy'], session['turn'] = True, turn
    m.agent.run_loop(session, turn, spec, run_tool=execute, ensure_checkpoint=lambda _: None)
    errors = [i for i in session['items'] if i.get('kind') == 'error']
    if turn.outcome != 'completed' or errors:
        raise RuntimeError(f'Loop outcome {turn.outcome}: {errors}')  # Private process log only.
    return ran


def worker_case(m, case, cfg, folder):
    parent = session_for(m, case['name'] + '-parent')
    add_user(m, parent, 'Build the requested prop.')
    turn = m.agent.Turn()
    turn.roles = m.config.Roles(cfg, cfg, True)
    job = types.SimpleNamespace(folder=folder, absorb=lambda spawn: None)
    details, previous = [], None
    for number, original in enumerate([case['brief']] + ([case['continuation']] if 'continuation' in case else [])):
        current = copy.deepcopy(original)
        if previous is not None:
            current['continues'] = 'prior-thread'
            parent['worker_threads'] = {'prior-thread': {
                'role': previous.brief['role'], 'messages': previous.session['messages'],
                'folder': str(previous.folder), 'references': [], 'merged': True}}
        plan = {'shared': {'units': 'm', 'notes': 'Keep the original composition.'}, 'workers': [current]}
        with mock.patch.object(m.waves, 'job_for', return_value=job), \
                mock.patch.object(m.waves, '_owned_keys', return_value=[]), \
                mock.patch.object(m.waves, '_wave_checkpoint', return_value='fixed-checkpoint'):
            wave = m.waves.prepare(parent, turn, plan, len(parent['messages']),
                                   save_spawn=lambda _: types.SimpleNamespace(wave=f'fixed-wave-{number}'),
                                   scene_context=lambda _: SCENE)
        worker = m.waves.Worker(wave, current, 0)
        worker.folder.mkdir(parents=True, exist_ok=True)
        # Retain the real worker LoopSpec/schemas; these hooks normally talk to the child/budget.
        spec = dataclasses.replace(worker.spec, save=lambda _: None, before_request=None, on_usage=None,
                                   after_step=lambda s, t: any(c.get('function', {}).get('name') == 'report_back'
                                       for msg in s['messages'][-2:] for c in msg.get('tool_calls', [])))
        ran = run_turn(m, worker.session, worker.turn.config, spec)
        if ran != ['run_python', 'report_back']:
            raise RuntimeError('Worker did not execute both scripted steps')
        opening = wave.openings[current['id']]
        ids = [identity for match in assembly.PLACEHOLDER.finditer(opening) for identity in match[1].split(',')]
        required = case.get('required_skills', [])
        if getattr(spec, 'prompt_id', None):
            if not ids or not set(required).issubset(ids):
                raise RuntimeError(f'Coverage missing required skills: {required}; selected: {ids}')
        elif '<role_notes>' not in opening:
            raise RuntimeError('Old worker missing local role notes')
        if number and (previous.brief['role'] == current['role'] or '<role_notes>' not in opening):
            raise RuntimeError('Continuation did not change role and refresh role notes')
        details.append({'craft': current['role'], 'skill_ids': ids, 'continued': bool(number),
                        'history_messages': len(worker.session['messages']), 'tools_executed': ran})
        previous = worker
    return details


def drive_case(m, case, url, folder, build_kind):
    cfg = m.config.Config(api_key=os.environ['LOOPCUT_PARITY_KEY'], base_url=url, model=case['name'], reasoning_effort='low',
                          auto_run=True, auto_look=False, max_steps=8, context_budget=200_000, keep_context=True,
                          max_output_tokens=2048, call_max_seconds=0)
    session = session_for(m, case['name'])
    kind = case['kind']
    if kind == 'wire':
        # Test-only synthetic messages; never used as add-on parity evidence.
        request = copy.deepcopy(case[build_kind])
        result = m.llm.stream_chat(base_url=url, api_key=cfg.api_key, model=cfg.model,
                                  on_text=lambda _: None, is_cancelled=lambda: False,
                                  headers=m.llm.correlation_headers(session['id'], case['role']), **request)
        if not result.text:
            raise RuntimeError('Wire fixture received no reply')
        return {}
    if kind == 'loop':
        session['addons_line'] = ADDONS if case['addons'] else ''
        spec = m.agent.SINGLE_SPEC if case['role'] == 'single' else m.agent.ORCH_SPEC
        spec = dataclasses.replace(spec, save=lambda _: None, asks=False, checkpoints=False, changes_card=False,
                                   on_turn_start=None, on_final=None, on_end=None)
        calls = []
        for text in ('Make a small prop — keep its silhouette.', 'Refine the prop’s finish.'):
            add_user(m, session, text)
            calls.extend(run_turn(m, session, cfg, spec))
        expected = ['delegate', 'run_python'] if case['role'] == 'orchestrator' else ['run_python']
        if calls != expected:
            raise RuntimeError(f'Unexpected tools executed: {calls}')
        return {'turns': 2, 'tools_executed': calls}
    if kind == 'worker':
        return worker_case(m, case, cfg, folder)
    if kind == 'router':
        for n, text in enumerate(('Build a prop.', 'Now make it blue.')):
            message = add_user(m, session, text, case['attached'])
            if n and m.router.previous_note(session) == 'none':
                raise RuntimeError('Router second turn has no previous note')
            decision, _ = m.router.ask(cfg, message, session, lambda: False)
            if decision is None:
                raise RuntimeError('Router returned no decision')
            session['messages'].append({'role': 'assistant', 'content': 'Previous turn finished.'})
            session['turn_levels'].append({'turn': n + 1, 'first': 1, 'final': 1})
        return {'turns': 2, 'attached_image': case['attached']}
    if kind == 'summary':
        text = m.context.summarize(cfg, [{'role': 'user', 'content': 'Make a blue prop.'},
                                        {'role': 'assistant', 'content': 'Blue prop complete.'}], lambda: False, session)
        if text != 'Fixed control result.':
            raise RuntimeError('Summary control failed')
    elif kind == 'reference':
        message = add_user(m, session, 'Match this reference.', True)
        session['references'] = [{'ref': IMAGE, 'name': 'reference.png', 'pinned': True}]
        if m.agent._reference_card(session, cfg, message, lambda: False) != ['reference.png']:
            raise RuntimeError('Reference control failed')
    else:
        raise ValueError('Unknown scenario kind')
    return {}


def drive(args):
    url = urlsplit(args.url)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.path != '/v1'
            or url.username or url.password or url.query or url.fragment):
        raise ValueError('Driver only sends to the loopback HTTP stand-in at /v1')
    os.environ.update(LOOPCUT_SERVICE_URLS=args.url, LOOPCUT_BASE_URL=args.url,
                      LOOPCUT_PROMPT_INDEX=str(args.index), LOOPCUT_DATA_DIR=str(args.out / 'data'),
                      LOOPCUT_ENV_FILE=str(args.out / 'empty.env'), LOOPCUT_ROUTER='off',
                      LOOPCUT_ORCHESTRATE='false', LOOPCUT_AUTO_RUN='true', LOOPCUT_AUTO_LOOK='false')
    (args.out / 'empty.env').touch()
    m = load_addon(args.build)
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    validate_cases(cases)
    outcomes = {}
    counter = iter(range(1, 100_000))
    with ExitStack() as stack:
        # Application clocks only: HTTP timeout clocks must remain live.
        stack.enter_context(mock.patch.object(m.state, 'time', types.SimpleNamespace(time=lambda: 1_700_000_000.0)))
        stack.enter_context(mock.patch.object(m.state.uuid, 'uuid4', side_effect=lambda: uuid.UUID(int=next(counter))))
        for name, replacement in {'_redraw': lambda: None, '_scene_snapshot': lambda: None,
                                  '_alike_on_main': lambda a, b: False, '_project_roots': lambda: ()}.items():
            stack.enter_context(mock.patch.object(m.agent, name, replacement))
        for case in cases:
            folder = args.out / 'work' / case['name']
            folder.mkdir(parents=True)
            try:
                details = drive_case(m, case, args.url, folder, args.build_kind)
                outcomes[case['name']] = {'ok': True, 'details': details}
            except Exception as ex:  # Scenario boundary: fail this case, still exercise other roles.
                traceback.print_exc()
                outcomes[case['name']] = {'ok': False, 'error_type': type(ex).__name__}
            write_json(args.out / 'drive.json', outcomes)
    return int(any(not entry['ok'] for entry in outcomes.values()))


def run_build(build, kind, cases, source, index_path, out, timeout=120):
    """One isolated add-on process, scripted model, and (new only) the real proxy."""
    private_dir(out)
    validate_cases(cases)
    out.mkdir(parents=True)
    cases_path = out / 'cases.json'
    write_json(cases_path, [{k: v for k, v in case.items() if k != 'replies'} for case in cases])
    model = fake_model.Server(usage=False).start()
    proxy_server = None
    try:
        for case in cases:
            model.script(case['name'], case['replies'])
        url = model.base_url
        # Ephemeral local transport credential, excluded from artifacts and body comparison.
        env = clean_env()
        env['LOOPCUT_PARITY_KEY'] = secrets.token_hex(24)
        if kind == 'new':
            proxy = prompt_proxy.Proxy(source, url, env['LOOPCUT_PARITY_KEY'], record=out / 'proxy')
            proxy_server = ThreadingHTTPServer(('127.0.0.1', 0), prompt_proxy.make_handler(proxy))
            threading.Thread(target=proxy_server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True).start()
            url = f'http://127.0.0.1:{proxy_server.server_port}/v1'
        command = [sys.executable, str(SCRIPT), 'drive', '--build', str(build), '--build-kind', kind,
                   '--url', url, '--index', str(index_path), '--cases', str(cases_path), '--out', str(out)]
        error = None
        with (out / 'process.log').open('wb') as log:
            try:
                result = subprocess.run(command, env=env, cwd=out, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=timeout, check=False)
                returncode = result.returncode
            except subprocess.TimeoutExpired:
                returncode, error = -1, 'Driver timed out'
        drive_path = out / 'drive.json'
        outcomes = json.loads(drive_path.read_text()) if drive_path.exists() else {}
        records = {case['name']: [] for case in cases}
        directory = out / 'bodies'
        directory.mkdir()
        transport_errors = []
        # Save every model request in arrival order, including an unexpected model name.
        for n, row in enumerate(model.log, 1):
            path = directory / f'{n:04d}.json'
            path.write_bytes(row.raw_body)
            if row.model not in records:
                transport_errors.append('Model received an unexpected scenario/model name')
            records.setdefault(row.model, []).append({
                'file': str(path), 'status': row.status, 'sha256': digest(row.raw_body)})
        if kind == 'new':
            proxy_paths = sorted((out / 'proxy').glob('[0-9][0-9][0-9][0-9].json'))
            if len(proxy_paths) != len(model.log):
                transport_errors.append('Proxy and model request counts differ')
            by_name = {case['name']: case for case in cases}
            for path, row in zip(proxy_paths, model.log):
                if path.read_bytes() != row.raw_body:
                    transport_errors.append('Proxy recorded bytes differ from model-received bytes')
                meta = json.loads(path.with_suffix('.meta.json').read_text())
                case = by_name.get(row.model)
                if case is None:
                    continue  # Already recorded as an unexpected model above.
                expected = case['role'] if case['role'] in assembly.PROMPT_IDS else None
                if meta['prompt_id'] != expected:
                    transport_errors.append(f"{row.model}: expected prompt header {expected}")
        return {'returncode': returncode, 'error': error, 'outcomes': outcomes, 'records': records,
                'model_errors': model.errors, 'transport_errors': transport_errors, 'remaining': [
                    {'scenario': name, 'count': count} for (name, _), count in model.remaining().items()]}
    finally:
        if proxy_server:
            proxy_server.shutdown()
            proxy_server.server_close()
        model.stop()


def compare_runs(cases, old, new, edits=()):
    rows, differences, errors = [], [], []
    for case in cases:
        name = case['name']
        a, b = old['records'].get(name, []), new['records'].get(name, [])
        identical = 0
        for n in range(max(len(a), len(b))):
            if n >= len(a) or n >= len(b):
                diff = {'kind': 'missing_request', 'path': '$', 'missing': 'old' if n >= len(a) else 'new'}
            else:
                baseline = rebaseline_body(Path(a[n]['file']).read_bytes(), case['role'], edits)
                diff = compare_bytes(baseline, Path(b[n]['file']).read_bytes())
            if diff:
                differences.append({'scenario': name, 'request': n + 1, **diff})
            else:
                identical += 1
        for kind, run, records in (('old', old, a), ('new', new, b)):
            outcome = run['outcomes'].get(name)
            if not outcome or not outcome['ok']:
                errors.append({'scenario': name, 'build': kind, 'error': 'Driver scenario failed or did not finish'})
            if len(records) != len(case['replies']):
                errors.append({'scenario': name, 'build': kind, 'error': 'Unexpected request count',
                               'expected': len(case['replies']), 'actual': len(records)})
            if any(r['status'] != 200 for r in records):
                errors.append({'scenario': name, 'build': kind, 'error': 'Model request failed'})
        rows.append({'scenario': name, 'role': case['role'], 'requests': min(len(a), len(b)),
                     'old_requests': len(a), 'new_requests': len(b), 'expected': len(case['replies']),
                     'worker_edits_per_request': len(edits) if case['role'] == 'worker' else 0,
                     'identical': identical, 'differing': max(len(a), len(b)) - identical})
    for kind, run in (('old', old), ('new', new)):
        if run['returncode'] or run['model_errors'] or run['remaining'] or run['transport_errors']:
            errors.append({'build': kind, 'error': 'Incomplete or unsuccessful build run', 'returncode': run['returncode']})
    return {'scenarios': rows, 'differences': differences, 'errors': errors}


def report_table(rows):
    lines = ['| Scenario | Requests (old/new) | Identical | Differing |', '|---|---:|---:|---:|']
    for row in rows:
        lines.append(f"| {row['scenario']} | {row['old_requests']}/{row['new_requests']} | {row['identical']} | {row['differing']} |")
    return '\n'.join(lines)


def write_report(out, report):
    report['passed'] = bool(report['scenarios']) and not report['errors'] and not report['differences']
    roles = {}
    for row in report['scenarios']:
        roles[row['role']] = roles.get(row['role'], 0) + row['requests']
    report['requests_per_role'] = roles
    write_json(out / 'report.json', report)
    lines = ['# V1 request byte parity', '', 'INCOMPLETE' if report.get('incomplete') else
             'PASS' if report['passed'] else 'FAIL', '',
             report_table(report['scenarios']), '', 'Compared pairs per role: ' + json.dumps(roles), '',
             'Source verification: see report.json sources (exact bytes; hashes identify evidence).',
             'Old worker system strings include only the recorded SOURCE.json replacements, once each.',
             'Other roles and all bytes outside that system string are compared unchanged.',
             'Serialization differences mean parsed JSON is equal, so no differing JSON path exists.', '',
             'Differences (no prompt/skill text is printed):']
    lines.extend(json.dumps(diff) for diff in report['differences'])
    lines += ['', 'Errors:'] + [json.dumps(error) for error in report['errors']]
    lines += ['', 'Scope: local stand-in gateway only. No Blender, live model, deployed gateway, cache, BYOK or deletion',
              'verification. Tool/scene execution, UI/persistence and nondeterminism are stubbed; request builders,',
              'tool schemas, skill selection, context preparation and HTTP serialization run.',
              'Raw model bodies, proxy records and private process logs are retained beside this report.']
    (out / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def tree_digest(root):
    """Detect source edits during the run, including uncommitted changes; ignore bytecode."""
    hashes = {str(path.relative_to(root)): digest(path.read_bytes()) for path in sorted(root.rglob('*'))
              if path.is_file() and path.suffix in ('.py', '.md', '.json')}
    return digest(json.dumps(hashes, sort_keys=True).encode())


def archive_old(out):
    tree = out / 'old-tree'
    tree.mkdir()
    with subprocess.Popen(['git', 'archive', OLD_REV, str(ADDON)], cwd=REPO, stdout=subprocess.PIPE) as archive:
        result = subprocess.run(['tar', '-x', '-C', str(tree)], stdin=archive.stdout, capture_output=True)
        archive.stdout.close()
        if archive.wait() or result.returncode:
            raise RuntimeError('Could not materialise old revision with git archive / tar')
    for path in tree.rglob('*'):
        if path.is_symlink():
            raise RuntimeError('Archived add-on unexpectedly contains a symlink')
        path.chmod(0o555 if path.is_dir() else 0o444)
    tree.chmod(0o555)
    return tree


def verify_sources(old_tree, out, source, edits=()):
    """Compare legacy constants and bodies with the hosted files, never the deleted client constants."""
    directory = out / 'old-prompts'
    subprocess.run([sys.executable, str(SCRIPT), 'snapshot', '--build', str(old_tree),
                    '--out', str(directory)], env=clean_env(), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    rows = []
    for identity in assembly.PROMPT_IDS:
        old, hosted = (directory / identity).read_bytes(), source.prompts[identity].encode('utf-8')
        baseline = apply_server_edits(old.decode('utf-8'), identity, edits).encode('utf-8')
        rows.append({'source': identity, 'identical': baseline == hosted,
                     'worker_edits': len(edits) if identity == 'worker' else 0,
                     'baseline_sha256': digest(baseline), 'baseline_bytes': len(baseline),
                     'old_bytes': len(old), 'hosted_bytes': len(hosted),
                     'old_sha256': digest(old), 'hosted_sha256': digest(hosted)})
    moved, missing = [], []
    for path in sorted((old_tree / ADDON / 'skills').rglob('*.md')):
        baseline = assembly.skill_loader().read_skill(path)
        hosted = source.skills.get(baseline.id)
        if hosted is None:
            missing.append(baseline.id)
            continue
        changed = sorted(k for k in baseline.front_matter.keys() | hosted.front_matter.keys()
                         if baseline.front_matter.get(k) != hosted.front_matter.get(k))
        moved.append({'id': baseline.id, 'body_identical': baseline.body.encode() == hosted.body.encode(),
                      'changed_metadata': changed, 'sha256_body': digest(hosted.body.encode())})
    return {'assembled_prompts': rows, 'moved_skills': moved, 'missing_moved_skills': missing,
            'passed': all(r['identical'] for r in rows) and bool(moved) and not missing
                      and all(r['body_identical'] and set(r['changed_metadata']) <= {'tier'} for r in moved)}


def check(args):
    out = private_dir(args.out) if args.out else Path(tempfile.mkdtemp(prefix='loopcut-v1-parity-', dir=SCRATCH))
    if args.out:
        out.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'old_revision': OLD_REV, 'new_revision': subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(), 'scenarios': [], 'differences': [], 'errors': []}
    try:
        edits = load_server_edits(args.server_edits)
        report['server_edits'] = {'file': str(args.server_edits.resolve()),
                                  'sha256': digest(args.server_edits.read_bytes()), 'worker_replacements': len(edits)}
        report['new_addon_sha256'] = tree_digest(REPO / ADDON)
        report['server_skills_sha256'] = tree_digest(args.skills)
        report['server_prompts_sha256'] = tree_digest(args.prompts)
        old = archive_old(out)
        source = assembly.load(args.prompts, args.skills)
        report['sources'] = verify_sources(old, out, source, edits)
        if not report['sources']['passed']:
            report['errors'].append({'error': 'Source text verification failed'})
        index_path = out / 'index.json'
        write_json(index_path, assembly.index(source))
        report['source_version'] = assembly.version(source)
        cases = scenarios()
        runs = {kind: run_build(build, kind, cases, source, index_path, out / kind)
                for kind, build in (('old', old), ('new', REPO))}
        comparison = compare_runs(cases, runs['old'], runs['new'], edits)
        report['scenarios'], report['differences'] = comparison['scenarios'], comparison['differences']
        report['errors'].extend(comparison['errors'])
        report['runs'] = runs
        if (report['new_addon_sha256'] != tree_digest(REPO / ADDON)
                or report['server_skills_sha256'] != tree_digest(args.skills)
                or report['server_prompts_sha256'] != tree_digest(args.prompts)
                or report['server_edits']['sha256'] != digest(args.server_edits.read_bytes())):
            report['errors'].append({'error': 'Sources changed during the run; rerun against a stable tree'})
    except Exception as ex:  # CLI boundary: leave a failed report, never a false pass.
        (out / 'check-error.log').write_text(traceback.format_exc(), encoding='utf-8')
        report['errors'].append({'error': 'Check failed; see private check-error.log', 'error_type': type(ex).__name__})
        report['incomplete'] = isinstance(ex, IncompleteParity)
    write_report(out, report)
    print(report_table(report['scenarios']))
    print(('INCOMPLETE' if report.get('incomplete') else 'PASS' if report['passed'] else 'FAIL') + f': {out / "report.md"}')
    return 2 if report.get('incomplete') else int(not report['passed'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if len(sys.argv) > 1 and sys.argv[1] in ('drive', 'snapshot'):
        command = sys.argv.pop(1)
        parser.add_argument('--build', required=True, type=Path)
        parser.add_argument('--out', required=True, type=Path)
        if command == 'drive':
            parser.add_argument('--build-kind', choices=('old', 'new'), required=True)
            parser.add_argument('--url', required=True)
            parser.add_argument('--index', type=Path, required=True)
            parser.add_argument('--cases', type=Path, required=True)
        args = parser.parse_args()
        private_dir(args.out)
        if command == 'snapshot':
            snapshot(args.build, args.out)
            return 0
        return drive(args)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--prompts', type=Path, default=os.environ.get('LOOPCUT_PROMPTS_DIR') or PLATFORM / 'prompts')
    parser.add_argument('--skills', type=Path, default=os.environ.get('LOOPCUT_SERVER_SKILLS_DIR') or PLATFORM / 'skills',
                        help='Pin a point-in-time skill copy (e.g. reconstructed originals); deliberate later '
                             'server edits make V1 source verification fail by design.')
    parser.add_argument('--server-edits', type=Path, default=PLATFORM / 'prompts/fixtures/SOURCE.json',
                        help='Runtime baseline metadata; missing/invalid evidence is incomplete (exit 2)')
    return check(parser.parse_args())


if __name__ == '__main__':
    sys.exit(main())
