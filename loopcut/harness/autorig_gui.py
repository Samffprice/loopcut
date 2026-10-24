"""Real Loopcut GUI/agent dispatch against the recorded local HTTP rig service.

--server URL instead opens an interactive branch session against that server;
no task is started automatically in that mode. Use an isolated launch profile.
"""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
parser = argparse.ArgumentParser()
parser.add_argument('--m1', type=Path)
parser.add_argument('--out', type=Path, required=True)
parser.add_argument('--server')
parser.add_argument('--file', type=Path)
parser.add_argument('--fixture', default='B2', help='Recorded fixture under --m1 for offline checks')
ARGS = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
ARGS.out.mkdir(parents=True, exist_ok=True)
import autorig_fake as fake


def factory(task):
    review = ARGS.m1 / 'review' / ARGS.fixture / 'review.json'
    inside = json.loads(review.read_bytes())['raw_bones_inside']['fraction'] if review.is_file() else None
    return mainthread.run_on_main(lambda: fake.bundle(task, autorig._prepared[task['task_id']],
        ARGS.m1 / 'results' / ARGS.fixture / 'native/rigged.glb', ARGS.out / 'fake-result',
        inside_fraction=inside)).result(timeout=120)


if not ARGS.server and ARGS.m1 is None:
    parser.error('--m1 is required for recorded GUI checks')
SERVER = None if ARGS.server else fake.Server(factory, seconds=2)
if SERVER:
    SERVER.age_required = True
SERVICE_URL = ARGS.server or SERVER.url
os.environ['LOOPCUT_SERVICE_URL'] = SERVICE_URL
# Account/settings modules cache this URL on import. Set it before loading the
# branch package, so sign-in and tools point at the same selected test server.
import bpy
import checkout
checkout.use()
import loopcut
from loopcut import agent, autorig, cloud, cloud_account, cloud_jobs, config, credentials, mainthread, state, tools
from loopcut.ui import host

CARD, TURN = None, None
SHOTS, QUEUE = set(), []
RESULT, FAILED = [], []
ORIGINAL = None
SCENARIO, HINGE_FAILURE = 'success', None
DEADLINE = time.monotonic() + 240


def guarded(function):
    def run():
        try:
            if not ARGS.server and time.monotonic() > DEADLINE:
                raise TimeoutError('Offline GUI check exceeded four minutes')
            return function()
        except BaseException:
            traceback.print_exc()
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)
    return run


def screenshot(name):
    if SCENARIO != 'success':
        name = SCENARIO + '-' + name
    if name in SHOTS:
        return
    window = bpy.context.window_manager.windows[0]
    for area in window.screen.areas:
        area.tag_redraw()
    # Local checking/apply stages execute on main; explicitly swap buffers so
    # screenshots record their real card text, not a staged mock conversation.
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        bpy.ops.screen.screenshot(filepath=str(ARGS.out / (name + '.png')))
    display_path = ARGS.out / (name + '.json')
    host.dump_display(str(display_path))
    if CARD is not None and CARD in state.session()['items']:
        from autorig_card_check import check
        display = json.loads(display_path.read_text())
        check(display['prims'], f"item{state.session()['items'].index(CARD)}.tool.")
    from autorig_card_check import check
    display = json.loads(display_path.read_text())
    for job in {p['id'].split('.')[1] for p in display['prims'] if p.get('id', '').startswith('cloud.')} :
        check(display['prims'], f'cloud.{job}.')
    SHOTS.add(name)
    print('SCREENSHOT', name, flush=True)


def setup():
    global ORIGINAL
    cloud_http = __import__('loopcut.cloud_http', fromlist=['validate_url'])
    cloud_http.validate_url(SERVICE_URL)
    if ARGS.server:
        token = os.environ.pop('LOOPCUT_AUTORIG_TEST_KEY', '')
        if token:
            credentials.store(config.SERVICE_BASE_URL, token)
    else:
        credentials.store(config.SERVICE_BASE_URL, 'lc_offline_gui')
    file = ARGS.file if ARGS.server else ARGS.m1 / 'prepared' / ARGS.fixture / 'original.blend'
    if file:
        bpy.ops.wm.open_mainfile(filepath=str(file), load_ui=False, use_scripts=False)
    bpy.context.preferences.view.show_splash = False
    loopcut.register()
    # This is a private test profile, not the user's onboarding/preferences.
    from loopcut import onboarding
    if host.NATIVE:
        onboarding.unregister()
    objects = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    with tools._editor_context('VIEW_3D'):
        autorig._select(objects)
        ORIGINAL = autorig.fingerprint(objects)
    window = bpy.context.window_manager.windows[0]
    for area in window.screen.areas:
        if area.type == 'VIEW_3D':
            # M1 files contain evaluation lights/cameras. Hide their viewport
            # wires in this isolated profile so they do not obscure the character.
            area.spaces.active.overlay.show_extras = False
            region = next(r for r in area.regions if r.type == 'WINDOW')
            with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
                bpy.ops.view3d.view_axis(type='FRONT')
                bpy.ops.view3d.view_selected(use_all_regions=False)
    if not host.has_panel(window):
        host.dock(window)
    if ARGS.server:
        print('AUTORIG INTERACTIVE: branch add-on loaded; sign into the chosen test server if needed.', flush=True)
        return None
    bpy.app.timers.register(guarded(begin), first_interval=1.5)
    return None


def begin():
    global CARD, TURN
    if host.find_panel(bpy.context.window_manager.windows[0]) is None:
        return .2
    # Docking changes the viewport's aspect ratio; fit the complete character now.
    with tools._editor_context('VIEW_3D'):
        bpy.ops.view3d.view_selected(use_all_regions=False)
    runtime = cloud._snapshot()
    original_progress = cloud._progress
    def capture_progress(name, value):
        original_progress(name, value)
        text = value.get('message', '') if isinstance(value, dict) else value
        stages = {autorig.STAGES[k]: filename for k, filename in {
            'checking': 'checking', 'uploading': 'uploading', 'quoting': 'quoting', 'queued': 'waiting',
            'running': 'rigging', 'applying': 'applying', 'validating': 'validating',
            'validating_experimental': 'validating'}.items()}
        for marker, filename in stages.items():
            if marker.lower() in text.lower():
                capture = guarded(lambda n=filename: screenshot(n))
                if threading.current_thread() is threading.main_thread():
                    capture()
                else:
                    # Hold this test step until its real frame is captured. A fast
                    # refusal can otherwise advance to age approval before the draw.
                    mainthread.run_on_main(capture).result(timeout=30)
    cloud._progress = capture_progress
    session = state.session()
    session['items'].clear()
    session['items'].append({'kind': 'user', 'text': 'Rig my character.'})
    CARD = state.item_tool(cloud.PREFIX + 'auto_rig', 'Rig my character', '')
    CARD.update(status='running', expanded=True)
    session['items'].append(CARD)
    TURN = agent.Turn()
    session.update(busy=True, turn=TURN)
    consent = agent._Consent(session, TURN, CARD)
    def run():
        try:
            runtime.refresh()
            with cloud_account.approval_context(consent):
                result = agent._run_tool_on_main(cloud.PREFIX + 'auto_rig', json.dumps({**fake.inputs(),
                    'targets': mainthread.run_on_main(lambda: [o.name for o in bpy.context.scene.objects if o.type == 'MESH']).result()}),
                    TURN.cancel.is_set)
            RESULT.append(result)
        except BaseException:
            FAILED.append(traceback.format_exc())
    TURN.thread = threading.Thread(target=run, daemon=True)
    TURN.thread.start()
    bpy.app.timers.register(guarded(tick), first_interval=.15)
    return None


def check_preview_ownership():
    from loopcut import scene_diff, ownership
    from loopcut.ui import draw
    original = state.session()
    state.replace(state.new_session())
    user = bpy.data.images.new('.user-image.png', width=8, height=8)
    user.use_fake_user = True
    before = scene_diff._images([])
    path = str(ARGS.out / 'quote.png')
    preview = state.item_tool('capture_viewport', 'Chat preview', '')
    preview.update(status='done', expanded=True, image_path=path)
    state.session()['items'].append(preview)
    screenshot('chat-preview')
    if draw._image_cache.get(path) is None:
        raise AssertionError('Actual panel draw did not load its thumbnail')
    image = draw._image_cache[path][0]
    name = image.name
    if not image.get(ownership.TEMP) or scene_diff._images([]) != before:
        raise AssertionError('A chat preview leaked into scene changes')
    saved = ARGS.out / 'preview-ownership.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(saved), check_existing=False)
    if draw._image_cache or name in bpy.data.images:
        raise AssertionError('save_pre did not release the chat preview')
    # Blender refuses libraries.load on the current file. Inspect identical saved
    # bytes under a temporary path, without opening or changing the user's scene.
    with tempfile.TemporaryDirectory(prefix='autorig-saved-images-') as tmp:
        inspect = Path(tmp) / 'saved.blend'
        shutil.copyfile(saved, inspect)
        with bpy.data.libraries.load(str(inspect)) as (data, _):
            if name in data.images or user.name not in data.images:
                raise AssertionError('Saved image ownership was incorrect')
    (ARGS.out / 'preview-ownership.json').write_text(json.dumps({
        'actual_panel_preview_drawn': True,
        'temporary_excluded_from_changes': True, 'temporary_excluded_from_saved_blend': True,
        'user_image_preserved': user.name}, indent=2))
    state.session()['items'].remove(preview)
    bpy.data.images.remove(user)
    state.replace(original)


def check_provider_recovery():
    original = state.session()
    session = state.new_session()
    state.replace(session)
    session['items'].append(dict(state.item_error('No API key yet. Add one in Preferences > Add-ons > Loopcut.'),
                                 config_error='provider_setup'))
    screenshot('provider-before')
    cfg = config.Config(api_key='synthetic-local-provider', base_url=SERVER.url + '/offline', model='offline-provider',
                        reasoning_effort='low', auto_run=True, auto_look=False, max_steps=1, context_budget=40000)
    reply = agent._request(session, agent.Turn(), cfg)
    if reply.text != 'The selected provider is working.' or any(i.get('config_error') for i in session['items']):
        raise AssertionError('A successful local provider request did not clear its setup error')
    screenshot('provider-after')
    (ARGS.out / 'provider-recovery.json').write_text(json.dumps({'successful_local_sse_request': True,
        'setup_error_cleared': True, 'external_requests': 0}, indent=2))
    state.replace(original)


def check_short_card_variants():
    """Real GUI rendering of offline approval, preflight and receipt edge cases."""
    global CARD
    from loopcut import rig_text, rig_refund, rig_inspect, cloud_http
    original, original_card = state.session(), CARD
    session = state.new_session()
    state.replace(session)
    turn = agent.Turn()
    CARD = state.item_tool(cloud.PREFIX + 'auto_rig', 'Rig your character', '')
    session['items'].append(CARD)
    consent = agent._Consent(session, turn, CARD)
    details = dict(text='20 credits', credits=20, balance=1000, threshold=0, required=True,
                   rig_notice=rig_text.QUOTE)
    with patch.object(agent, '_wait_for_approval', side_effect=lambda _: screenshot('plain-quote') or True):
        consent(details)
    for kind in ('budget', 'burst', 'daily_limit_reached', 'rate_limited'):
        with patch.object(agent, '_wait_for_approval', side_effect=lambda _, n=kind: screenshot(n) or True):
            consent._ask(kind, {**details, 'message':'Limit reached', 'url':''})
    try:
        rig_inspect.geometry([(0,0,0), (1,0,0), (0,1,0)], [(0,1,2)] * 100001)
    except rig_inspect.RigError as ex:
        CARD.update(status='failed', cost=rig_text.price(0,1000), output=cloud.error_text(ex, rig=True))
    screenshot('preflight-refusal')
    sys.path.insert(0, str(HERE.parent / 'tests'))
    from test_rig_refund import Service, receipt, rig_job
    for name, response, failures in [('refund-review', receipt(status='review', review_reason='late', credits_refunded=0), 0),
                                     ('refund-limit', receipt(status='review', review_reason='limit', credits_refunded=0), 0),
                                     ('refund-unconfirmed', receipt(), 2)]:
        service = Service(response)
        service.failures = failures
        report = rig_refund.ApplyFailure(rig_job(), 'validation_failed', service.client(), lambda _: None)
        report.start()
        outcome = report.wait()
        CARD.update(status='failed', cost=rig_text.price(outcome['job']['credits']['charged'],1000, settled=True),
                    output=outcome['message'])
        screenshot(name)
    # Jobs uses its own render path; capture actual progress_rows and layout.
    rows = cloud_jobs.progress_rows({'job_copy': {'job': {**rig_job(), 'status':'running'},
                                                'title':'Rig your character', 'ready':False}})
    with patch.object(cloud_jobs, 'ui_rows', return_value=rows):
        session['items'].clear()
        screenshot('jobs-progress')
    state.replace(original)
    CARD = original_card


def tick():
    global SCENARIO, HINGE_FAILURE
    if FAILED:
        print(FAILED[0], flush=True)
        os._exit(1)
    quote_shot = 'quote' if SCENARIO == 'success' else SCENARIO + '-quote'
    if CARD.get('status') == 'awaiting' and CARD.get('ask') == 'age_confirmation':
        if CARD.get('ask_link') != 'https://loopcut.org/account' or CARD.get('ask_label') != 'Retry':
            raise AssertionError('Age confirmation did not offer a safe link and retry')
        screenshot('age-confirmation')
        SERVER.age_required = False
        agent.decide(True)
        return .15
    if CARD.get('status') == 'awaiting' and quote_shot not in SHOTS:
        inspection = autorig.inspect([o.name for o in bpy.context.scene.objects if o.type == 'MESH'])[3]
        coat = (autorig.rig_inspect.CLOTHING_WARNING in inspection['warnings']
                and 'upright_body' not in inspection['shape_hints'])
        expected = autorig.rig_inspect.CLOTHING_WARNING if coat else autorig.rig_experimental.QUOTE
        if CARD['ask_lines'] != [expected] or '20 credits ($0.20)' not in CARD['cost']:
            raise AssertionError('Quote must show its price and exactly one warning')
        screenshot('quote')
        agent.decide(True)
    if SCENARIO == 'refund' and 'refund-pending' not in SHOTS:
        if any('requesting a refund' in row['message'] for row in cloud_jobs.ui_rows()):
            screenshot('pending')
    if RESULT:
        result = RESULT[0]
        CARD.update(status='done' if result.ok else 'failed', output=result.text, expanded=True)
        state.session()['busy'] = False
        screenshot('result')
        if SCENARIO == 'anatomy':
            if result.ok or 'finger layout' not in result.text:
                raise AssertionError('Anatomy refusal did not show its plain reason and confirmed no charge')
            if not CARD.get('cost', '').startswith('No credits used') or SERVER.charge != 20 or state.session()['spend']['credits'] != 20:
                raise AssertionError('Anatomy refusal changed card/chat charges')
            if autorig.fingerprint([o for o in bpy.context.scene.objects if o.type == 'MESH']) != ORIGINAL:
                raise AssertionError('Anatomy refusal changed the original')
            expected = {'anatomy-' + s for s in ('checking', 'uploading', 'quote', 'waiting', 'rigging', 'result')}
            if not expected <= SHOTS:
                raise AssertionError('Anatomy screenshots missing: ' + str(expected - SHOTS))
            (ARGS.out / 'gui-anatomy-results.json').write_text(json.dumps({'screenshots': sorted(expected),
                'summary': result.text, 'cost': CARD['cost'], 'original_preserved': True,
                'chat_credits': state.session()['spend']['credits'], 'fake_credits': SERVER.charge,
                'real_spend_usd': 0}, indent=2))
            print('GUI ANATOMY PASS', str(ARGS.out), flush=True)
            SERVER.close()
            bpy.ops.wm.quit_blender()
            return None
        if SCENARIO == 'refund':
            if result.ok or '20 credits returned' not in result.text:
                raise AssertionError('The GUI did not report a confirmed apply-failure refund: ' + result.text)
            from loopcut import spending
            if not CARD.get('cost', '').startswith('No credits used') or state.session()['spend']['credits'] != 20:
                raise AssertionError('Refund did not correct its original card and chat spending')
            if autorig.fingerprint([o for o in bpy.context.scene.objects if o.type == 'MESH']) != ORIGINAL:
                raise AssertionError('Refunded GUI apply failure changed the character')
            expected = {'refund-' + s for s in ('checking', 'uploading', 'quote', 'waiting', 'rigging',
                                                'applying', 'validating', 'pending', 'result')}
            if not expected <= SHOTS:
                raise AssertionError('Refund screenshots missing: ' + str(expected - SHOTS))
            (ARGS.out / 'gui-refund-results.json').write_text(json.dumps({'screenshots': sorted(expected),
                'summary': result.text, 'cost': CARD['cost'], 'original_preserved': True,
                'chat_credits': state.session()['spend']['credits'], 'fake_credits': SERVER.charge,
                'refund_requests': len(SERVER.reports), 'real_spend_usd': 0}, indent=2))
            HINGE_FAILURE.stop()
            print('GUI REFUND PASS', str(ARGS.out), flush=True)
            SCENARIO = 'anatomy'
            SERVER.failure, SERVER.anatomy_reason = True, 'finger_count'
            RESULT.clear()
            bpy.app.timers.register(guarded(begin), first_interval=.2)
            return None
        if not result.ok:
            print('GUI FAILED', result.text, flush=True)
            os._exit(1)
        rig_details = json.loads(result.model_note.split('\n', 1)[1])
        experimental = rig_details['tier'] == 'experimental'
        arm = bpy.context.view_layer.objects.active
        if rig_details['armature'] != arm.name or set(rig_details['meshes']) != {
                o.name for o in bpy.context.scene.objects if o.type == 'MESH'}:
            raise AssertionError('Model result did not identify the exact rig and skinned mesh objects')
        with tools._editor_context('VIEW_3D'):
            from loopcut.merge import _undo
            if (not _undo('undo') or any(o.type == 'ARMATURE' for o in bpy.context.scene.objects)
                    or autorig.fingerprint([o for o in bpy.context.scene.objects if o.type == 'MESH']) != ORIGINAL):
                raise AssertionError('One GUI Undo did not restore the original character')
            if not _undo('redo'):
                raise AssertionError('GUI Redo did not restore the rig')
            if experimental:
                arm = bpy.context.view_layer.objects.active
                if not autorig.rig_experimental.marked(arm):
                    raise AssertionError('Experimental marker missing after GUI Redo')
                bpy.ops.wm.save_as_mainfile(filepath=str(ARGS.out / 'gui-result.blend'), check_existing=False)
                removed = autorig.tool('remove')
                if not removed.ok or autorig.fingerprint([o for o in bpy.context.scene.objects if o.type == 'MESH']) != ORIGINAL:
                    raise AssertionError('Experimental GUI removal failed')
                check_preview_ownership()
                check_provider_recovery()
                (ARGS.out / 'gui-results.json').write_text(json.dumps({'screenshots': sorted(SHOTS),
                    'summary': result.text, 'model_note': result.model_note, 'one_undo_redo': True, 'remove': True,
                    'fake_credits': SERVER.charge, 'real_spend_usd': 0}, indent=2))
                print('GUI EXPERIMENTAL PASS', str(ARGS.out), flush=True)
                SERVER.close()
                bpy.ops.wm.quit_blender()
                return None
            preview = autorig.tool('preview')
            if not preview.ok:
                raise AssertionError(preview.text)
            pose_card = state.item_tool('character_rig', 'Try a free test pose', '')
            pose_card.update(status='done', output=preview.text, expanded=True)
            state.session()['items'].append(pose_card)
            bpy.ops.view3d.view_orbit(type='ORBITRIGHT', angle=math.radians(45))
            screenshot('free-test-pose')
            reset = autorig.tool('reset_preview')
            if not reset.ok:
                raise AssertionError(reset.text)
            bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.wm.save_as_mainfile(filepath=str(ARGS.out / 'gui-result.blend'), check_existing=False)
        removed = autorig.tool('remove')
        if not removed.ok or autorig.fingerprint([o for o in bpy.context.scene.objects if o.type == 'MESH']) != ORIGINAL:
            raise AssertionError('Selection-based removal did not restore the original character: ' + removed.text)
        expected = {'checking', 'uploading', 'quoting', 'age-confirmation', 'quote', 'waiting', 'rigging', 'applying', 'validating', 'result', 'free-test-pose'}
        if SHOTS != expected or any(not (ARGS.out / (name + '.png')).is_file() for name in expected):
            raise AssertionError('A required stage screenshot is missing: ' + str(expected - SHOTS))
        check_preview_ownership()
        check_provider_recovery()
        check_short_card_variants()
        (ARGS.out / 'gui-results.json').write_text(json.dumps({'screenshots': sorted(SHOTS),
            'summary': result.text, 'model_note': result.model_note,
            'one_undo_redo': True, 'selection_preview_reset_remove': True,
            'shape_warning_on_quote': ARGS.fixture in {'B4', 'B7', 'R12', 'R13', 'R16'},
            'fake_credits': SERVER.charge, 'real_spend_usd': 0}, indent=2))
        print('GUI PASS', str(ARGS.out), flush=True)
        SCENARIO = 'refund'
        SERVER.refund_delay = 2
        HINGE_FAILURE = patch.object(autorig, '_hinges', side_effect=autorig.RigError(
            'The rig did not bend both knees and elbows correctly. Nothing was applied.', 'validation_failed'))
        HINGE_FAILURE.start()
        RESULT.clear()
        bpy.app.timers.register(guarded(begin), first_interval=.2)
        return None
    return .15


bpy.app.timers.register(guarded(setup), first_interval=.2)
