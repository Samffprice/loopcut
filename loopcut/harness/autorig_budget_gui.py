"""R7 native GUI evidence with the real consent/jobs path and in-memory HTTP only.

Run in an isolated profile under autorig_launch's heavy lock. No rig generation,
model calls, production credentials or network service is used by this scenario.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import traceback
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'tests'))
parser = argparse.ArgumentParser()
parser.add_argument('--out', type=Path, required=True)
ARGS = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
ARGS.out.mkdir(parents=True, exist_ok=True)
import bpy
import checkout
checkout.use()
import loopcut
from loopcut import cloud, state, onboarding
from loopcut.ui import host
from test_autorig_r7 import BudgetNotice

CHECK = None


def guarded(function):
    def run():
        try:
            return function()
        except BaseException:
            traceback.print_exc()
            sys.stdout.flush()
            os._exit(1)
    return run


def setup():
    loopcut.register()
    onboarding.unregister()
    bpy.context.preferences.view.show_splash = False
    window = bpy.context.window_manager.windows[0]
    # A fresh Blender profile can already have Quick Setup open. Disabling
    # future splashes does not dismiss that modal; let its Escape event run.
    window.event_simulate(type='ESC', value='PRESS')
    window.event_simulate(type='ESC', value='RELEASE')
    # A wide but ordinary dock makes the complete short sentence visible on one row.
    host.DOCK_FACTOR = .35
    host.dock(window)
    bpy.app.timers.register(guarded(scenario), first_interval=1.5)


def scenario():
    global CHECK
    if host.find_panel(bpy.context.window_manager.windows[0]) is None:
        return .2
    CHECK = BudgetNotice()
    CHECK.setUp()
    CHECK.session = state.session()
    CHECK.session['items'].clear()
    CHECK.session['items'].append({'kind': 'user', 'text': 'Rig my character, then add an 8-second walk.'})
    _, rig, _ = CHECK.run_call('auto_rig', 20, 0)
    rig.update(status='done', rig_stage='Your rig is ready.', cost='Used 20 credits ($0.20)', expanded=False)
    _, motion, _ = CHECK.run_call()
    motion.update(summary='Create an 8-second walk', expanded=False)
    CHECK.session.update(busy=True, turn=CHECK.turn, scroll=0)
    assert len(CHECK.approvals) == 1
    assert CHECK.notices()[0]['text'] == "Used 16 credits from this chat's budget · Balance after: 964"
    # The same HTTP response used by the notice supplies the UI's account summary.
    runtime = cloud.Runtime(CHECK.client, CHECK.directory.name, signed_in=True)
    runtime.manifest['account'] = {'credits': {'balance': CHECK.balance}, 'settings': {}}
    cloud._runtime = runtime
    patcher = patch.object(cloud, '_snapshot', return_value=runtime)
    patcher.start()
    CHECK.addCleanup(patcher.stop)
    bpy.app.timers.register(guarded(capture), first_interval=1)


def capture():
    window = bpy.context.window_manager.windows[0]
    for area in window.screen.areas:
        area.tag_redraw()
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
        bpy.ops.screen.screenshot(filepath=str(ARGS.out / 'budget-notice.png'))
    host.dump_display(str(ARGS.out / 'display.json'))
    display = json.loads((ARGS.out / 'display.json').read_text())
    index = CHECK.session['items'].index(CHECK.notices()[0])
    prims = [p for p in display['prims'] if p.get('id', '').startswith(f'item{index}.notice') and p.get('text')]
    assert len(prims) == 1, prims
    assert prims[0]['text'] == CHECK.notices()[0]['text'], prims
    (ARGS.out / 'result.json').write_text(json.dumps({
        'successful': True, 'offline': True, 'spend_usd': 0,
        'approvals': CHECK.approvals, 'notice': CHECK.notices()[0],
        'rendered_lines': len(prims), 'spend': CHECK.session['spend'],
        'requests': CHECK.calls, 'addon_source': str(Path(loopcut.__file__).resolve())}, indent=2))
    CHECK.doCleanups()
    print('R7 GUI PASS: one rig approval; motion budget notice on one rendered line.', flush=True)
    bpy.ops.wm.quit_blender()


bpy.app.timers.register(guarded(setup), first_interval=1)
