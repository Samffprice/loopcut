"""Factory-scene controller plumbing check. Scripted responses only; no network or paid calls.

Blender --background --factory-startup --disable-autoexec --python loopcut/harness/controller_check.py
"""

import json
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo / 'scripts' / 'addons_core'))
import bpy
from loopcut import agent, config, controller, inspection, llm, state, tools

session = state.new_session()
session['items'].append(state.item_user('Make a red cube.'))
session['messages'].append({'role': 'user', 'content': 'Make a red cube.'})
turn = agent.Turn()
session['turn'] = turn
cfg = config.Config('unused', 'https://example.invalid/v1', 'fast', 'low', True, False, 0, 32000)
turn.config = cfg
inspection.register()
inspected = []
calls = []

def inspect(name, args):
    inspected.append(name)
    assert name == 'review_objects'
    return tools.get_object_info(args['names']).text, None

def stream(**kwargs):
    packet = json.loads(kwargs['messages'][1]['content'])
    calls.append(packet)
    if len(calls) == 1:
        return llm.Completion(tool_calls=[llm.ToolCall('read', 'review_objects', '{"names":["Cube"]}')])
    return llm.Completion(text=json.dumps({
        'event': 'finish', 'action': 'present',
        'scene_version': packet['scene_version'], 'instruction_version': packet['instruction_version'],
        'reason': 'Scripted decision for the controller plumbing smoke check.',
        'guidance': '', 'findings': [], 'advice': ''}))

review = controller.Controller(session, turn, config.Controller(cfg), snapshot=inspection.live_token,
                               inspect=inspect, stream=stream)
turn.controller = review
decision = review.review('finish', 'Scripted candidate; this is not a quality evaluation.')
assert review.current(decision)
assert inspected == ['review_objects']
bpy.data.objects['Cube'].location.x = 2
bpy.context.view_layer.update()
assert not review.current(decision), 'A real Blender edit must invalidate the review.'

def edited_during_review(**kwargs):
    completion = stream(**kwargs)
    bpy.data.objects['Cube'].location.y += 1
    bpy.context.view_layer.update()
    return completion

review.stream = edited_during_review
try:
    review.review('finish', 'Stale candidate')
except controller.StaleReview:
    pass
else:
    raise AssertionError('A decision returned after a real edit must be rejected.')
inspection.unregister()
print('CONTROLLER_SMOKE ' + json.dumps({'blender': bpy.app.version_string, 'object_inspection': 'pass',
      'later_edit_invalidates': 'pass', 'edit_during_review_rejected': 'pass', 'paid_model_calls': 0}))
