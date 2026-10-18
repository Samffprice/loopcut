"""Exercise agent dispatch against the local mock with real Blender imports and provenance.

    <blender> -b --factory-startup --python loopcut/harness/cloud_e2e_check.py -- --service http://127.0.0.1:8787

This script loads its own checkout through checkout.use(). Start contract/mock/server.py
with --live-tools image_generate,image_to_3d --job-seconds .5. No vendor keys are needed;
lc_demo is stored only in an isolated temporary config folder. Approval UI is bypassed.
"""

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from pathlib import Path


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print('PASS ' + message, flush=True)


def on_worker(fn, timeout=120):
    """Background Blender does not service UI timers; pump the same queue as the agent."""
    from loopcut import mainthread
    results = []
    def work():
        try:
            results.append(fn())
        except BaseException as ex:
            results.append(ex)
    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while worker.is_alive() and time.monotonic() < deadline:
        mainthread._pump()
        time.sleep(.01)
    check(not worker.is_alive(), 'worker completed within the time limit')
    mainthread._pump()
    if isinstance(results[0], BaseException):
        raise results[0]
    return results[0]


def execute(name, arguments):
    from loopcut import agent, cloud, cloud_account, state
    full_name = cloud.PREFIX + name
    card = {'kind': 'tool', 'name': full_name, 'status': 'running', 'output': ''}
    state.session()['items'].append(card)
    # This calls tools.execute on main, then its continuation on this worker, exactly as a turn does.
    def run():
        with cloud_account.approval_context(lambda details: True):
            return agent._run_tool_on_main(full_name, json.dumps(arguments))
    result = on_worker(run)
    card.update(status='done' if result.ok else 'error', output=result.text)
    print(result.text, flush=True)
    check(result.ok, name + ' succeeded through tools.execute')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--service', required=True)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    parts = urllib.parse.urlsplit(args.service)
    if (parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/')):
        raise ValueError('--service must be the local HTTP mock origin')
    os.environ['LOOPCUT_SERVICE_URL'] = args.service.rstrip('/')
    os.environ['LOOPCUT_DATA_DIR'] = tempfile.mkdtemp(prefix='loopcut-cloud-e2e-')
    os.environ['LOOPCUT_CONFIG_DIR'] = tempfile.mkdtemp(prefix='loopcut-cloud-e2e-config-')
    os.environ['LOOPCUT_NO_GENERATION'] = 'false'
    os.environ['LOOPCUT_PRIVATE_MODE'] = 'true'
    for name in ('OPENAI_API_KEY', 'FAL_KEY', 'GEMINI_API_KEY', 'ELEVENLABS_API_KEY'):
        os.environ['LOOPCUT_OWN_KEY_FIRST_' + name] = 'false'  # Credits first: a shell key must not take the route.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import checkout
    checkout.use()
    import bpy
    from loopcut import agent, chatgpt_responses, cloud, cloud_jobs, config, conversations, credentials, state

    credentials.store(config.SERVICE_BASE_URL, 'lc_demo')
    runtime = cloud._snapshot()
    check(runtime.signed_in, 'signed in with the isolated mock identity')
    on_worker(lambda: runtime.client.request('POST', '/mock/control', {
        'live_tools': ['image_generate', 'image_to_3d'], 'fail_jobs': False, 'force_error': None,
        'balance': 1840, 'flags': {'services': True, 'premium_skills': False},
        'settings': {'no_generation': False, 'private_mode': True}}))
    check(on_worker(runtime.refresh), 'manifest refreshed against the mock')
    flat = agent._tool_schemas()
    names = [row['function']['name'] for row in flat]
    namespaced = chatgpt_responses.to_tools(flat)
    remote = next(row['tools'] for row in namespaced if row['name'] == cloud.NAMESPACE)
    for name in ('image_generate', 'image_to_3d'):
        check(names.count(cloud.PREFIX + name) == 1, name + ' occurs once in chat-completions schemas')
        check(sum(row['name'] == name for row in remote) == 1, name + ' occurs once in Responses schemas')
        check(next(d for d in runtime.descriptors() if d['name'] == name)['backend'] == 'managed',
              name + ' routes to the mock managed service')
    soon = cloud.coming_soon()
    check(bool(soon), 'coming-soon tools are present in cloud.coming_soon()')
    check(all(cloud.PREFIX + row['name'] not in names and row['name'] not in {r['name'] for r in remote}
              for row in soon), 'coming-soon tools are absent from both provider schemas')

    state.reset()
    before = cloud_jobs.datablock_snapshot(bpy.data)
    execute('image_generate', {'prompt': 'A tiny prop reference'})
    images = [image for image in bpy.data.images if 'loopcut_provenance' in image
              and json.loads(image['loopcut_provenance'])['tool'] == 'image_generate']
    check(bool(images), 'generated image landed with loopcut_provenance')
    check(any(ob.type == 'EMPTY' and ob.empty_display_type == 'IMAGE' and ob.data in images for ob in bpy.data.objects),
          'generated image has a scene reference object')
    reference = state.session()['references'][0]
    check(conversations.image_path(state.session()['id'], reference['full']).is_file(),
          'generated image is pinned and readable by the model')
    execute('image_to_3d', {'image': images[0].name, 'prompt': 'A simple prop'})
    imported = [ob for ob in bpy.data.objects if ob.type == 'MESH' and 'loopcut_provenance' in ob
                and json.loads(ob['loopcut_provenance'])['tool'] == 'image_to_3d']
    check(bool(imported), 'GLB landed as mesh objects with loopcut_provenance')
    check(all('loopcut_provenance' in ob.data for ob in imported), 'imported mesh datablocks carry provenance')
    check(all(ob.select_get() for ob in imported), 'imported models are selected')
    created = cloud_jobs.created_datablocks(before, cloud_jobs.datablock_snapshot(bpy.data))
    check(all('loopcut_provenance' in block for block in created), 'every created datablock carries provenance')
    check(all('loopcut_provenance' not in block for block in before.values()), 'original datablocks retain their provenance')
    check(not cloud_jobs.ui_rows(), 'no duplicate or unfinished cloud progress rows remain')
    print('PASS cloud end-to-end check', flush=True)


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        traceback.print_exc()
        print('FAIL cloud end-to-end check', flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)  # Blender otherwise exits zero after a --python exception.
