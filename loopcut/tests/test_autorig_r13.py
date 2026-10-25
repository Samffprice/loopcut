"""R13 fixes from persona round 6 on rc/0.2.17 (runs3: ar-dog-4 20261024-223635, ar-beginner-3 20261024-225759, the
free plan's Fast model, which acts neither on prose rules nor on facts in tool results): Loopcut runs the rig check
itself when a turn that changed a Loopcut rig ends (R6-D1, R6-B1), with the motion coverage that catches a frozen
gallop (R6-D2); a free `show` (R6-B2/D3) and built-in gestures (R6-B3); plain-word facts and a balance-aware price
note (R6-D4, R6-D5, R6-B4). No Blender: rig_motion, rig_gesture and the gate's wording are pure Python, and the
Blender reads are faked."""
import json
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
fake_bpy = types.ModuleType('bpy')
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault('bpy', fake_bpy)

from loopcut import (agent, autorig, completion_guard, context, llm, motion_retarget, orchestrator,  # noqa: E402
                     rig_experimental, rig_gate, rig_gesture, rig_inspect, rig_motion, rig_pose, state)
from loopcut.ui import layout, theme as T  # noqa: E402
from test_autorig_r10 import skeleton  # noqa: E402

FRAMES = list(range(1, 121))
FROZEN = [True] * 24 + [False] * 95  # ar-dog-4: keys 1-25, played to 120.


def motion(ok=False, line=None, unplayed=False):
    line = line or rig_motion.line(rig_motion.coverage(FROZEN if not ok else [True] * 119, FRAMES, 24))
    return {'ok': ok, 'kind': 'motion', 'line': line, 'detail': line}


def bend(ok=True):
    if ok:
        return {'ok': True, 'kind': 'bend', 'line': 'it bends without tearing or spikes in the 16 frames checked',
                'detail': 'Worst at frame 46: left knee, stretch 1.90x, squash 0.40x, no spikes or torn edges.'}
    return {'ok': False, 'kind': 'bend', 'line': 'the front right leg pulls into spikes at frame 87',
            'detail': 'Worst at frame 87: front right leg, 136 vertices pulled out into spikes, up to 259.0 cm.'}


def verdicts(*checks, unplayed=False):
    return [{'rig': 'Character Rig', 'checks': list(checks), 'unplayed': unplayed}]


class Coverage(unittest.TestCase):
    """R6-D2: frames 36, 56, 109 and 112 held one pose; only 1-25 moved, and nothing deforms in a frozen stretch."""

    def test_the_frozen_gallop_reads_as_moving_only_at_the_start(self):
        found = rig_motion.coverage(FROZEN, FRAMES, 24)
        self.assertEqual(found['still'], [(25, 120)])
        self.assertTrue(rig_motion.problem(found))
        self.assertEqual(rig_motion.line(found), 'it moves only in frames 1–25 of 1–120, then stands still to frame 120')

    def test_a_cycle_that_keeps_moving_and_short_holds_pass(self):
        self.assertEqual(rig_motion.line(rig_motion.coverage([True] * 119, FRAMES, 24)),
                         'it keeps moving from frame 1 to 120')
        beat = [True] * 60 + [False] * 20 + [True] * 39  # A 20-frame beat between a walk and a wave: under 1/4.
        self.assertFalse(rig_motion.problem(rig_motion.coverage(beat, FRAMES, 24)))

    def test_nothing_moving_middle_holds_and_starts_are_named(self):
        self.assertEqual(rig_motion.line(rig_motion.coverage([False] * 119, FRAMES, 24)),
                         'nothing moves from frame 1 to 120')
        middle = rig_motion.coverage([True] * 40 + [False] * 40 + [True] * 39, FRAMES, 24)
        self.assertEqual(rig_motion.line(middle), 'it stands still from frame 41 to 81 of 1–120')
        start = rig_motion.coverage([False] * 50 + [True] * 69, FRAMES, 24)
        self.assertEqual(rig_motion.line(start), 'it stands still from frame 1 to 51, then moves to frame 120')

    def test_a_hold_after_loopcuts_own_motion_is_not_a_freeze(self):
        """Coordinator's review: a wave on a still rig keys 1-49 of a 1-250 scene; that hold is the scene's range."""
        found = rig_motion.coverage([True] * 48 + [False] * 201, list(range(1, 251)), 24)
        self.assertEqual(found['still'], [(49, 250)])
        self.assertTrue(rig_motion.problem(found))
        self.assertFalse(rig_motion.problem(found, deliberate=True))
        self.assertEqual(rig_motion.line(found, deliberate=True), 'it moves in frames 1–49, then holds still to frame 250')
        never = rig_motion.coverage([False] * 249, list(range(1, 251)), 24)
        self.assertTrue(rig_motion.problem(never, deliberate=True), 'never moving always counts')

    def test_a_long_range_read_every_few_frames_keeps_frame_numbers(self):
        frames = list(range(1, 3001, 2))
        moves = [k < 100 for k in range(len(frames) - 1)]
        self.assertEqual(rig_motion.coverage(moves, frames, 24)['still'], [(201, 2999)])


class Gate(unittest.TestCase):
    """R6-D1/B1: 9+ "Animation changed … run check_deformation" facts, 0 checks. The turn's end runs it."""

    def session(self, changed=None, **marks):
        session = state.new_session()
        request = {'role': 'user', 'content': 'can u rig my dog so he can run', **marks}
        if changed is not False:
            request[rig_gate.CHANGED] = json.dumps(changed or {'Character Rig': ['animation']})
        session['messages'] = [request, {'role': 'assistant', 'content': 'He runs, verified.'}]
        session['items'] = [state.item_user('can u rig my dog so he can run'),
                            {'kind': 'assistant', 'text': 'He runs, verified.', 'streaming': False}]
        return session, request

    def turn(self, outcome='running'):
        turn = agent.Turn()
        turn.outcome = outcome
        return turn

    def run_gate(self, session, found, turn=None, role='single', text='He runs, verified.'):
        calls = []

        def on_main(fn):
            calls.append(fn)
            return fn()
        with patch.object(rig_gate, 'check', return_value=found):
            out = rig_gate.finish_gate(session, turn or self.turn(), llm.Completion(text=text), role, on_main)
        return out, calls

    def test_no_marked_rig_runs_nothing(self):
        session, _ = self.session(changed=False)
        out, calls = self.run_gate(session, verdicts(motion()))
        self.assertEqual((out, calls), (None, []))
        self.assertEqual(session['items'][-1]['text'], 'He runs, verified.')

    def test_a_helper_and_a_stopped_turn_are_left_alone(self):
        session, _ = self.session()
        self.assertEqual(self.run_gate(session, verdicts(motion()), role='worker'), (None, []))
        turn = self.turn()
        turn.cancel.set()
        self.assertEqual(self.run_gate(session, verdicts(motion()), turn=turn)[0], None)

    def test_a_problem_sends_the_model_back_once_from_the_shared_budget(self):
        session, request = self.session()
        self.assertEqual(completion_guard.nudges_left(request), 2)
        out, _ = self.run_gate(session, verdicts(motion(), bend()))
        self.assertTrue(out.startswith('[Loopcut] Loopcut checked the rig before your reply could end.'))
        self.assertIn('- Character Rig: The animation moves only in frames 1–25 of 1–120, then stands still to frame '
                      '120.', out)
        self.assertIn('do not say it works', out)
        self.assertTrue(request[completion_guard.RIG_CHECKED])
        self.assertEqual(completion_guard.nudges_left(request), 1, 'one of the two nudges is spent')
        self.assertEqual(session['items'][-1]['kind'], 'notice')
        self.assertEqual(session['items'][-1]['text'], 'Loopcut checked the rig: the animation moves only in frames '
                         '1–25 of 1–120, then stands still to frame 120. Asked the assistant to fix it.')
        out, _ = self.run_gate(session, verdicts(motion(), bend()))
        self.assertIsNone(out, 'never twice')
        reply = session['items'][-1]
        self.assertEqual(reply['verification'], 'Verified by Loopcut on this scene: 1 of 2 checks.\n'
                         'Failed: the animation moves only in frames 1–25 of 1–120, then stands still to frame 120.\n'
                         'It bends without tearing or spikes in the 16 frames checked.')
        self.assertTrue(completion_guard._UI_RECORD.fullmatch(reply['verification'].split('\n')[0]))

    def test_no_nudge_left_no_turn_left_an_honest_stop_or_a_waiting_card_only_stamps(self):
        cases = {'budget': ({completion_guard.CONTINUED: 2}, 'running', 'He runs.', False),
                 'corrected and continued': ({completion_guard.CONTINUED: 1, completion_guard.CORRECTED: True},
                                             'running', 'He runs.', False),
                 'step limit': ({}, 'step_limit', 'He runs.', False),
                 'honest stop': ({}, 'running', "I'm stopping here because the loop will not play.", False),
                 'card waiting': ({}, 'running', 'He runs.', True)}
        for name, (marks, outcome, text, card) in cases.items():
            with self.subTest(name):
                session, request = self.session(**marks)
                if card:
                    item = state.item_tool('loopcut_cloud__auto_rig', 'Rig', '')
                    item['status'] = 'awaiting'
                    session['items'].insert(1, item)
                out, _ = self.run_gate(session, verdicts(motion(), bend(False)), turn=self.turn(outcome), text=text)
                self.assertIsNone(out)
                self.assertNotIn(completion_guard.RIG_CHECKED, request)
                block = next(i for i in reversed(session['items']) if i['kind'] == 'assistant')['verification']
                self.assertTrue(block.startswith('Verified by Loopcut on this scene: 0 of 2 checks.\nFailed: '), block)
                self.assertIn('Failed: the front right leg pulls into spikes at frame 87.', block)

    def test_a_clean_check_is_one_short_stamp(self):
        session, request = self.session()
        out, _ = self.run_gate(session, verdicts(motion(ok=True), bend()))
        self.assertIsNone(out)
        self.assertEqual(session['items'][-1]['verification'], 'Verified by Loopcut on this scene: 2 of 2 checks.\n'
                         'The animation keeps moving from frame 1 to 120.\n'
                         'It bends without tearing or spikes in the 16 frames checked.')
        self.assertEqual(session['messages'][-1]['content'].split('\n\n')[-1], session['items'][-1]['verification'])
        self.assertEqual(completion_guard.nudges_left(request), 2)

    def test_a_stamp_after_another_block_this_turn_says_it_checked_the_rig(self):
        """2026-10-25: two "Verified by Loopcut" heads with different counts in one turn read as a glitch."""
        session, request = self.session()
        orchestrator._append_to_reply(session, llm.Completion(text='He runs, verified.'),
                                      'Verified by Loopcut on this scene: 1 of 1 checks.')
        self.run_gate(session, verdicts(motion(ok=True), bend()))
        self.assertEqual(session['items'][-1]['verification'].split('\n\n')[1].split('\n')[0],
                         'Rig checked this turn: 2 of 2 checks.')
        self.assertEqual(request[orchestrator.BLOCKS], 2)

    def test_a_stamp_is_not_repeated_when_nothing_new_was_marked(self):
        """Coordinator's note 2: contact sends the turn on after the rig stamp; the next final reply has nothing new."""
        session, request = self.session(**{rig_gate.MARKS: 1})
        self.run_gate(session, verdicts(motion(ok=True), bend()))
        self.assertEqual(request[rig_gate.STAMPED], 1)
        session['items'].append({'kind': 'assistant', 'text': 'Moved the dog onto the floor.', 'streaming': False})
        session['messages'].append({'role': 'assistant', 'content': 'Moved the dog onto the floor.'})
        out, calls = self.run_gate(session, verdicts(motion(ok=True), bend()), text='Moved the dog onto the floor.')
        self.assertEqual((out, calls), (None, []))
        self.assertNotIn('verification', session['items'][-1])
        request[rig_gate.MARKS] = 2  # The model changed the rig again.
        out, calls = self.run_gate(session, verdicts(motion(ok=True), bend()), text='Moved the dog onto the floor.')
        self.assertEqual(len(calls), 1)
        self.assertIn('verification', session['items'][-1])

    def test_errors_fail_open(self):
        session, request = self.session()
        with patch.object(rig_gate, 'check', side_effect=RuntimeError('no scene')):
            out = rig_gate.finish_gate(session, self.turn(), llm.Completion(text='Done.'), 'single', lambda fn: fn())
        self.assertIsNone(out)
        self.assertEqual(session['items'][-1]['text'], 'He runs, verified.')
        out = rig_gate.finish_gate(session, self.turn(), llm.Completion(text='Done.'), 'single',
                                   lambda fn: (_ for _ in ()).throw(RuntimeError('main thread gone')))
        self.assertIsNone(out)
        self.assertNotIn(completion_guard.RIG_CHECKED, request)

    def test_several_rigs_name_themselves(self):
        found = verdicts(motion(ok=True)) + [{'rig': 'Dog Rig', 'checks': [motion(ok=True)], 'unplayed': False}]
        self.assertEqual(rig_gate.stamp(found).split('\n')[1:], ["Character Rig's animation keeps moving from frame 1 to "
                                                                 "120.", "Dog Rig's animation keeps moving from frame 1 "
                                                                 "to 120."])

    def test_bend_failures_name_the_worst_places_plainly(self):
        def row(name, frame, spikes=0, stretch=1.5, squash=.5):
            return {'name': name, 'frame': frame, 'bent': 30, 'spikes': spikes, 'spike_cm': 2., 'stretch': stretch,
                    'squash': squash, 'volume': .8, 'vertices': 50}
        rows = [row('front right leg', 87, spikes=136), row('head', 40, stretch=7.), row('tail', 12, squash=.05),
                row('front left leg', 60, spikes=30)]
        self.assertEqual(rig_gate._bend(rows, rows, 12), 'the front right leg pulls into spikes at frame 87; the front '
                         'left leg pulls into spikes at frame 60; and 2 more places')
        self.assertEqual(rig_gate._bend(rows[1:3], rows[1:3], 12), 'the head tears at frame 40; the tail is crushed at '
                         'frame 12')

    def test_a_still_rigs_weight_check_reads_as_a_test_pose(self):
        line = rig_gate._sentence('it', 'in a test pose with arms up and elbows and knees bent, the right elbow pulls '
                                  'into spikes')
        self.assertEqual(line, 'In a test pose with arms up and elbows and knees bent, the right elbow pulls into spikes.')


class GateVerdict(unittest.TestCase):
    """verdict(): which checks run for a marked rig, from faked Blender reads."""

    def arm(self, tier='standard'):
        arm = types.SimpleNamespace(name='Character Rig', type='ARMATURE')
        arm.__contains__ = None
        ours = {autorig._KEY: '{}', rig_experimental.TIER_PROPERTY: tier}
        return type('Arm', (dict,), {'name': 'Character Rig', 'type': 'ARMATURE'})(ours)

    def measured(self, flagged=(), animation=True):
        rows = [{'name': 'left knee', 'bent': 40, 'spikes': 0, 'stretch': 1.9, 'squash': .4, 'volume': .8,
                 'vertices': 10, 'spike_cm': 0., 'frame': 46}] + list(flagged)
        return {'text': 'Worst at frame 46: left knee, stretch 1.90x.\n- left knee, worst at frame 46: no spikes',
                'rows': rows, 'flagged': list(flagged), 'animation': {'frames': 16, 'first': 1, 'last': 120}
                if animation else None, 'tested': True}

    def test_an_animated_rig_gets_motion_and_bend(self):
        found = rig_motion.coverage([True] * 119, FRAMES, 24)
        with patch.object(autorig, '_channels', return_value=(object(), ['c'])), \
                patch.object(autorig, '_span', return_value=(1, 120)), \
                patch.object(autorig, 'coverage', return_value={**found, 'unplayed': False}), \
                patch.object(autorig, 'measure', return_value=self.measured()):
            v = rig_gate.verdict(self.arm(), ['animation'])
        self.assertEqual([c['kind'] for c in v['checks']], ['motion', 'bend'])
        self.assertTrue(all(c['ok'] for c in v['checks']))

    def coverage_verdict(self, moves, frames, kinds, unplayed=False, again=None):
        found = {**rig_motion.coverage(moves, frames, 24), 'unplayed': unplayed}
        fake = types.ModuleType('bpy')
        fake.context = types.SimpleNamespace(view_layer=types.SimpleNamespace(update=lambda: None))
        with patch.dict(sys.modules, {'bpy': fake}), \
                patch.object(autorig, '_channels', return_value=(object(), ['c'])), \
                patch.object(autorig, '_span', return_value=(frames[0], frames[-1])), \
                patch.object(autorig, 'coverage', side_effect=[found, again or found]), \
                patch.object(autorig, 'refresh_animation'), \
                patch.object(autorig, 'measure', return_value=self.measured()):
            return rig_gate.verdict(self.arm(), kinds)

    def test_a_gesture_on_a_still_rig_passes_and_a_hand_keyed_freeze_fails(self):
        """Coordinator's review: a standard rig with no action, "make it wave", scene 1-250 at 24 fps: the gesture keys 1-49.
        The same hold after the model's own 24-frame loop (ar-dog-4) is the freeze the check is for."""
        frames = list(range(1, 251))
        wave = [True] * 48 + [False] * 201
        for kinds in (['gesture'], ['motion'], ['motion', 'weights']):
            with self.subTest(kinds=kinds):
                v = self.coverage_verdict(wave, frames, kinds)
                self.assertTrue(v['checks'][0]['ok'])
                self.assertEqual(v['checks'][0]['line'], 'it moves in frames 1–49, then holds still to frame 250')
        for kinds in (['animation'], ['gesture', 'animation']):
            with self.subTest(kinds=kinds):
                v = self.coverage_verdict(FROZEN, FRAMES, kinds)
                self.assertFalse(v['checks'][0]['ok'])
                self.assertEqual(v['checks'][0]['line'], 'it moves only in frames 1–25 of 1–120, then stands still to '
                                 'frame 120')
        never = self.coverage_verdict([False] * 249, frames, ['gesture'])
        self.assertFalse(never['checks'][0]['ok'], 'never moving always fails')
        stale = self.coverage_verdict(wave, frames, ['motion'], unplayed=True)
        self.assertFalse(stale['checks'][0]['ok'], 'Blender not playing the curves always fails')
        self.assertTrue(stale['unplayed'])

    def test_a_gesture_on_a_still_rig_does_not_send_the_model_back(self):
        frames = list(range(1, 251))
        session = state.new_session()
        request = {'role': 'user', 'content': 'make it wave',
                   rig_gate.CHANGED: json.dumps({'Character Rig': ['gesture']}), rig_gate.MARKS: 1}
        session['messages'] = [request, {'role': 'assistant', 'content': 'He waves.'}]
        session['items'] = [{'kind': 'assistant', 'text': 'He waves.', 'streaming': False}]
        found = self.coverage_verdict([True] * 48 + [False] * 201, frames, ['gesture'])
        with patch.object(rig_gate, 'check', return_value=[found]):
            out = rig_gate.finish_gate(session, agent.Turn(), llm.Completion(text='He waves.'), 'single', lambda fn: fn())
        self.assertIsNone(out)
        self.assertNotIn(completion_guard.RIG_CHECKED, request)
        self.assertEqual(session['items'][-1]['verification'].split('\n')[:2],
                         ['Verified by Loopcut on this scene: 2 of 2 checks.',
                          'The animation moves in frames 1–49, then holds still to frame 250.'])

    def test_an_animation_blender_does_not_play_is_refreshed_by_loopcut(self):
        """ar-dog-4: Cycles modifiers added a step after the keys; fcurve.evaluate cycled, the viewport froze."""
        frozen = {**rig_motion.coverage(FROZEN, FRAMES, 24), 'unplayed': True}
        playing = {**rig_motion.coverage([True] * 119, FRAMES, 24), 'unplayed': False}
        fake = types.ModuleType('bpy')
        fake.context = types.SimpleNamespace(view_layer=types.SimpleNamespace(update=lambda: None))
        with patch.dict(sys.modules, {'bpy': fake}), \
                patch.object(autorig, '_channels', return_value=(object(), ['c'])), \
                patch.object(autorig, '_span', return_value=(1, 120)), \
                patch.object(autorig, 'coverage', side_effect=[frozen, playing]), \
                patch.object(autorig, 'refresh_animation') as refresh, \
                patch.object(autorig, 'measure', return_value=self.measured()):
            v = rig_gate.verdict(self.arm(), ['animation'])
        refresh.assert_called_once()
        self.assertTrue(v['checks'][0]['ok'])
        self.assertEqual(v['checks'][0]['line'], 'it keeps moving from frame 1 to 120 (Loopcut refreshed it, so '
                         'Blender now plays its loop)')

    def test_a_still_rig_with_new_weights_is_checked_in_pose_stress_and_nothing_else_is(self):
        spiky = {'name': 'right elbow', 'bent': 120, 'spikes': 9, 'stretch': 2., 'squash': .4, 'volume': .5,
                 'vertices': 10, 'spike_cm': 3.}
        with patch.object(autorig, '_channels', return_value=(None, [])), \
                patch.object(autorig, 'stress_measure', return_value=self.measured([spiky], animation=False)) as stress:
            v = rig_gate.verdict(self.arm(), ['weights'])
            self.assertIsNone(rig_gate.verdict(self.arm(), ['animation']), 'a still pose keyed: nothing to check')
        stress.assert_called_once()
        self.assertEqual(v['checks'][0]['line'], 'in a test pose with arms up and elbows and knees bent, the right '
                         'elbow pulls into spikes')
        self.assertIsNone(rig_gate.verdict(types.SimpleNamespace(type='MESH'), ['animation']))


class Marks(unittest.TestCase):
    def test_marks_land_on_the_running_turns_message_and_never_raise(self):
        session = state.new_session()
        session['messages'] = [{'role': 'user', 'content': 'make him walk'},
                               {'role': 'user', 'content': 'capture', context.CAPTURE: True}]
        with patch.object(state, 'session', return_value=session):
            rig_gate.mark(['Character Rig'], 'animation')
            self.assertNotIn(rig_gate.CHANGED, session['messages'][0], 'no turn is running')
            session['busy'] = True
            rig_gate.mark(['Character Rig'], 'animation')
            rig_gate.mark(['Character Rig', 'Character Rig'], 'weights')
            rig_gate.mark(['Character Rig'], 'animation')
        self.assertEqual(rig_gate.changed(session['messages'][0]), {'Character Rig': ['animation', 'weights']})
        self.assertEqual(session['messages'][0][rig_gate.MARKS], 3, 'every mark counts, a repeated one too')
        self.assertIsInstance(session['messages'][0][rig_gate.CHANGED], str, 'conversations.load takes only scalars')
        self.assertTrue(rig_gate.CHANGED.startswith(context.PRIVATE_PREFIX), 'never sent to the model')
        self.assertEqual(rig_gate.changed({rig_gate.CHANGED: '{broken'}), {})
        with patch.object(state, 'session', side_effect=RuntimeError('no session')):
            rig_gate.mark(['x'], 'animation')

    def test_a_marked_conversation_saves_and_loads(self):
        import os
        import tempfile
        from loopcut import conversations
        session = state.new_session()
        session['items'] = [state.item_user('make him walk')]
        session['messages'] = [{'role': 'user', 'content': 'make him walk', 'loopcut_rig_check_sent': True}]
        session['busy'] = True
        with patch.object(state, 'session', return_value=session):
            rig_gate.mark(['Character Rig'], 'animation')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'LOOPCUT_DATA_DIR': tmp}), \
                patch.object(conversations, 'rename_merged_tools'):
            conversations.save(session)
            loaded = conversations.load(session['id'])
        self.assertEqual(rig_gate.changed(loaded['messages'][0]), {'Character Rig': ['animation']})
        self.assertEqual(completion_guard.nudges_left(loaded['messages'][0]), 1)
        self.assertEqual(conversations.wire_messages(loaded['id'], loaded['messages'])[0],
                         {'role': 'user', 'content': 'make him walk'})

    def test_run_pythons_lines_are_shorter_and_mark_the_rig(self):
        fake = types.ModuleType('bpy')
        fake.data = types.SimpleNamespace(objects={})
        marks = []
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(rig_gate, 'mark', side_effect=lambda n, k: marks.append(
                (sorted(n), k))):
            text = autorig.rig_changes(({'Mesh': ('a', 'Character Rig')}, {'Character Rig': None}),
                                       ({'Mesh': ('b', 'Character Rig')}, {'Character Rig': 'gallop'}))
        self.assertEqual(text, 'Weights changed on Mesh: Loopcut checks how it bends when your reply ends.\n'
                         'Animation changed on Character Rig: Loopcut checks how it moves and bends when your reply ends.')
        self.assertEqual(marks, [(['Character Rig'], 'weights'), (['Character Rig'], 'animation')])


class SharedFinishGates(unittest.TestCase):
    """The three shared touches (agent._final_reply, completion_guard.nudges_left, orchestrator._append_to_reply)."""

    def test_final_reply_runs_the_rig_gate_after_the_specs_and_before_contacts(self):
        order = []
        spec = agent.LoopSpec(role='single', system=lambda s: '', schemas=lambda: [], save=lambda s: None,
                              on_final=lambda s, t, c: order.append('spec'))
        turn = agent.Turn()
        with patch.object(rig_gate, 'finish_gate', side_effect=lambda *a: order.append('rig')), \
                patch('loopcut.contact.finish_gate', side_effect=lambda *a: order.append('contact')):
            agent._final_reply({}, turn, None, spec)
        self.assertEqual(order, ['spec', 'rig', 'contact'])
        with patch.object(rig_gate, 'finish_gate', return_value='[Loopcut] fix it') as gate, \
                patch('loopcut.contact.finish_gate') as contact:
            self.assertEqual(agent._final_reply({}, turn, None, spec), '[Loopcut] fix it')
        contact.assert_not_called()
        self.assertEqual(gate.call_args[0][3], 'single')

    def test_the_agent_loop_goes_back_once_then_ends_on_the_stamp(self):
        """End to end through agent.run_loop: ar-dog-4's "verified" reply is sent back once with Loopcut's finding, and
        the next reply ends the turn with the stamp; never a third request."""
        import test_agent_loop
        helper = test_agent_loop.UnrunCompletionTest('run_replies')
        found = [verdicts(motion(), bend()), verdicts(motion(ok=True), bend())]
        with patch.object(rig_gate, 'changed', return_value={'Character Rig': ['animation']}), \
                patch.object(rig_gate, 'check', side_effect=found), \
                patch.object(agent, '_on_main', side_effect=lambda fn, *a, **k: fn()):
            session, turn, requests, ran = helper.run_replies(
                [llm.Completion(text='He runs, verified: frames 1 and 25 match.'),
                 llm.Completion(text='Fixed: he runs the whole time.')], 'can u rig my dog so he can run')
        self.assertEqual((requests, turn.outcome), (2, 'completed'))
        sent = [m['content'] for m in session['messages'] if m.get(context.STEP_LIMIT)]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith('[Loopcut] Loopcut checked the rig'))
        final = session['items'][-1]
        self.assertEqual(final['kind'], 'assistant')
        self.assertTrue(final['text'].startswith('Fixed: he runs the whole time.\n\nVerified by Loopcut on this scene: '
                                                 '2 of 2 checks.'), final['text'])

    def test_the_rig_bounce_spends_the_shared_budget(self):
        self.assertEqual(completion_guard.nudges_left({completion_guard.RIG_CHECKED: True}), 1)
        self.assertEqual(completion_guard.nudges_left({completion_guard.RIG_CHECKED: True,
                                                       completion_guard.CONTINUED: 1}), 0)

    def test_two_gates_stamps_render_as_one_grey_footer(self):
        """5b's check: the rig stamp, then contact's, in the one muted block layout.py draws (ui/layout.py:254)."""
        rig = 'Verified by Loopcut on this scene: 2 of 2 checks.\nThe animation keeps moving from frame 1 to 120.'
        contact = 'Verified by Loopcut on this scene: 1 of 1 checks.\nThe dog rests on Ground.'
        session = state.new_session()
        session['items'] = [{'kind': 'assistant', 'text': 'He runs.', 'streaming': False}]
        session['messages'] = [{'role': 'assistant', 'content': 'He runs.'}]
        completion = llm.Completion(text='He runs.')
        orchestrator._append_to_reply(session, completion, rig)
        orchestrator._append_to_reply(session, completion, contact)
        item = session['items'][0]
        self.assertEqual(item['verification'], f'{rig}\n\n{contact}')
        self.assertEqual(session['messages'][0]['content'], f'He runs.\n\n{rig}\n\n{contact}')
        drawn = layout.build(session, 420, 900, 1, lambda font, size, text: len(text) * size * .6)
        prose = [p for p in drawn['prims'] if p['id'].startswith('item0.p')]
        footer = [p for p in drawn['prims'] if p['id'].startswith('item0.verification.l')]
        self.assertEqual(' '.join(p['text'] for p in prose), 'He runs.')
        self.assertEqual(' '.join(p['text'] for p in footer), f'{rig} {contact}'.replace('\n', ' '))
        self.assertTrue(footer and all(p['color'] == T.TEXT_MUTED for p in footer))
        self.assertEqual(sum(p['id'] == 'item0.verification.rule' for p in drawn['prims']), 1)

    def test_model_text_after_a_stamp_is_not_joined(self):
        session = state.new_session()
        session['items'] = [{'kind': 'assistant', 'text': 'He runs.', 'streaming': False}]
        session['messages'] = [{'role': 'assistant', 'content': 'He runs.'}]
        orchestrator._append_to_reply(session, llm.Completion(text='He runs.'), 'Verified by Loopcut on this scene: '
                                      '1 of 1 checks.')
        session['items'][0]['text'] += '\n\nAnd more prose.'
        orchestrator._append_to_reply(session, llm.Completion(text='He runs.'), 'Verified by Loopcut on this scene: '
                                      '2 of 2 checks.')
        self.assertEqual(session['items'][0]['verification'], 'Verified by Loopcut on this scene: 2 of 2 checks.')

    def test_a_copied_two_head_block_is_one_copy(self):
        """5b's check: a reply that copies the joined footer is one imitation (one correction), not two segments."""
        block = ('Verified by Loopcut on this scene: 2 of 2 checks.\nThe animation keeps moving from frame 1 to 120.\n\n'
                 'Verified by Loopcut on this scene: 1 of 1 checks.\nThe dog rests on Ground.')
        self.assertTrue(completion_guard.imitates_records(block, ['run_python']))
        self.assertEqual(context.copied_records(block, set(), []), (block, []))
        import test_agent_loop
        helper = test_agent_loop.UnrunCompletionTest('run_replies')
        session, _, requests, ran = helper.run_replies(
            [llm.Completion(text=block), llm.Completion(text='Nothing was done.')], 'Make the dog run')
        self.assertEqual((requests, ran), (2, 0))
        self.assertEqual(len(helper.bounces(session)), 1)


class Show(unittest.TestCase):
    def test_the_schema_offers_show_and_gestures_and_stays_small(self):
        function = autorig.SCHEMAS[0]['function']
        params = function['parameters']['properties']
        self.assertIn('show', params['action']['enum'])
        self.assertIn('gesture', params['action']['enum'])
        self.assertEqual(params['gesture']['enum'], list(rig_gesture.GESTURES))
        self.assertEqual(params['frames']['items'], {'type': 'integer'})
        self.assertIn("show (hides bones, follow camera in the user's view)", function['description'])
        self.assertLess(len(json.dumps(autorig.SCHEMAS)), 1100, 'the model pays for every word')
        self.assertEqual(autorig.describe('character_rig', {'action': 'show'})[0], 'Set up the view to watch it (free)')
        self.assertEqual(autorig.describe('character_rig', {'action': 'gesture'})[0], 'Add a gesture (free)')

    def test_the_tool_routes_show_and_gesture(self):
        with patch.object(autorig, 'show', return_value='Ready to watch') as show, \
                patch.object(autorig, 'add_gesture', return_value='Free, no credits used') as gesture, \
                patch('loopcut.tools._editor_context'):
            self.assertEqual(autorig.tool('show', rig='Character Rig').text, 'Ready to watch')
            self.assertEqual(autorig.tool('gesture', rig='Character Rig', gesture='wave', side='left',
                                          frames=[90, 140]).text, 'Free, no credits used')
        show.assert_called_once_with('Character Rig', 'both')
        gesture.assert_called_once_with('Character Rig', 'wave', 'left', [90, 140])

    def test_the_camera_stands_on_the_side_of_the_raised_hand(self):
        """ar-beginner-3: the camera showed the back of a man waving his right arm."""
        tops = {'mixamorig:RightHand': .9, 'mixamorig:LeftHand': .1}
        self.assertEqual(autorig._show_side('both', tops, 1.8), 'right')
        self.assertEqual(autorig._show_side('left', tops, 1.8), 'left', 'the side asked wins')
        self.assertEqual(autorig._show_side(None, {'mixamorig:RightHand': .2, 'mixamorig:LeftHand': .15}, 1.8), 'left')
        self.assertEqual(autorig._show_side(None, {}, 1.), 'left', 'an experimental rig has no named hands')

    def test_travel_and_the_rig_results_point_at_show(self):
        self.assertEqual(motion_retarget.travel_note(5.5), "it travels 5.5 m from where it starts; character_rig show "
                         "(free) sets up the user's view to follow it")
        self.assertEqual(motion_retarget.travel_note(.4), '')
        steps = rig_experimental.describe([{'name': 'bone_0', 'parent': None}, {'name': 'bone_1', 'parent': 0}],
                                          'Character Rig')['next_steps']
        self.assertEqual(len(steps), 4)
        self.assertIn('character_rig show', steps[2])
        self.assertIn('Loopcut checks how it moves and bends when your reply ends.', steps)
        self.assertNotIn('numpad', ' '.join(steps).lower())


class Gestures(unittest.TestCase):
    """R6-B3: a wave over the walk took 6+ minutes of failing code."""

    def posed(self, bones, gesture, side, t=0.):
        rig = rig_pose.Rig(bones)
        for name, q in rig_gesture.pose(bones, gesture, side, t, 24).items():
            rig.set(name[len(rig_pose.PREFIX):], q)
        return rig

    def test_each_gesture_aims_the_arm_in_the_bodys_frame(self):
        bones = skeleton()
        up, left, front = (0, 0, 1), (1, 0, 0), (0, -1, 0)
        rig = self.posed(bones, 'wave', 'right')
        arm, fore = rig.direction('RightArm'), rig.direction('RightForeArm')
        self.assertGreater(rig_pose.dot(arm, up), .4, 'upper arm raised above level')
        self.assertLess(rig_pose.dot(arm, left), -.6, 'out to the right side')
        self.assertGreater(rig_pose.dot(fore, up), .8, 'forearm up')
        swings = [rig_pose.dot(self.posed(bones, 'wave', 'right', t).direction('RightForeArm'), left)
                  for t in (0, .15, .3, .45)]
        self.assertGreater(max(swings) - min(swings), .3, 'the forearm swings side to side')
        rig = self.posed(bones, 'raise_arm', 'left')
        self.assertGreater(rig_pose.dot(rig.direction('LeftArm'), up), .95)
        self.assertGreater(rig_pose.dot(rig.direction('LeftForeArm'), up), .95)
        rig = self.posed(bones, 'point', 'right')
        self.assertGreater(rig_pose.dot(rig.direction('RightArm'), front), .95)
        self.assertLess(abs(rig_pose.dot(rig.direction('RightArm'), up)), .05, 'at shoulder height')
        self.assertEqual(set(rig_gesture.pose(bones, 'point', 'right', 0, 24)),
                         {rig_pose.PREFIX + 'Right' + r for r in rig_gesture.CHAIN}, 'the arm only')

    def test_the_aim_follows_the_chests_turn(self):
        """A walk that turns the body: the wave stays beside the head, not fixed in armature space."""
        bones = skeleton()
        name = rig_pose.PREFIX + 'Spine'
        parent, rest, basis, length = bones[name]
        turned = rig_pose.quat_matrix(rig_pose.axis_angle((0, 1, 0), 90))  # About the spine's own length: a yaw.
        bones = {**bones, name: (parent, rest, turned, length)}
        rig = self.posed(bones, 'point', 'right')
        straight = self.posed(skeleton(), 'point', 'right')
        self.assertLess(rig_pose.dot(rig.direction('RightArm'), straight.direction('RightArm')), .2)
        self.assertGreater(rig_pose.dot(rig.direction('RightArm'), (0, 0, 1)), -.05)

    def test_rigs_without_shoulders_or_fingers_still_wave(self):
        for kwargs in ({'shoulders': False}, {'fingers': False}):
            with self.subTest(**kwargs):
                rig = self.posed(skeleton(**kwargs), 'wave', 'left')
                self.assertGreater(rig_pose.dot(rig.direction('LeftForeArm'), (0, 0, 1)), .8)

    def test_it_eases_in_and_out_of_the_walk(self):
        self.assertEqual([round(rig_gesture.weight(f, 100, 148, 24), 2) for f in (100, 104, 108, 124, 144, 148)],
                         [0., .51, 1., 1., .51, 0.])
        walk = {'b': (1., 0., 0., 0.)}
        target = {'b': rig_pose.axis_angle((1, 0, 0), 90)}
        self.assertEqual(rig_gesture.blend(walk, target, 0.)['b'], (1., 0., 0., 0.))
        self.assertTrue(all(abs(a - b) < 1e-9 for a, b in zip(rig_gesture.blend(walk, target, 1.)['b'], target['b'])))
        half = rig_gesture.blend(walk, target, .5)['b']
        self.assertAlmostEqual(math.degrees(2 * math.acos(half[0])), 45., places=6)

    def test_default_frames_and_refusals(self):
        self.assertEqual(rig_gesture.frames(None, None, (1, 120), 24, True), (72, 120), 'the last 2 s of the walk')
        self.assertEqual(rig_gesture.frames(None, None, (1, 250), 24, False), (1, 49))
        self.assertEqual(rig_gesture.frames(90, None, (1, 120), 24, True), (90, 138))
        with self.assertRaisesRegex(rig_pose.PoseError, 'at least 12 frames'):
            rig_gesture.frames(10, 15, (1, 120), 24, True)
        with self.assertRaisesRegex(rig_pose.PoseError, 'Choose a gesture'):
            rig_gesture.pose(skeleton(), 'dab', 'right', 0, 24)

    def test_an_experimental_rig_is_told_to_keyframe(self):
        arm = type('Arm', (dict,), {'name': 'Character Rig', 'type': 'ARMATURE'})(
            {autorig._KEY: '{}', rig_experimental.TIER_PROPERTY: 'experimental'})
        with patch.object(autorig, '_main'), patch.object(autorig, '_selected_rig', return_value=arm), \
                patch('loopcut.tools._editor_context'):
            result = autorig.tool('gesture', rig='Character Rig')
        self.assertTrue(result.ok, 'a condition, not a red card')
        self.assertIn('keyframe', result.text)


class PlainFacts(unittest.TestCase):
    """R6-D4/D5/B4: Fast repeats tool facts word for word; the plan cost 30 with 20 in the account."""

    def runtime(self, balance=None, motion=10, rig=20):
        import threading
        descriptors = [{'name': 'auto_rig', 'cost': {'credits_estimate': rig}},
                       {'name': 'motion_from_text', 'cost': {'credits_estimate': motion}}]
        manifest = {'account': {'credits': {'balance': balance}}} if balance is not None else {}
        return types.SimpleNamespace(descriptors=lambda: descriptors, manifest=manifest, lock=threading.Lock())

    def test_the_price_note_says_the_balance_and_whether_the_plan_fits(self):
        from loopcut import cloud
        base = ('Before rigging, tell the user the total: the rig is 20 credits and a ready-made walk or run is 2 '
                'credits a second (10 for 5 s).')
        cases = {34: base + ' Balance: 34 credits, enough for the rig and a 5 s walk (30 in all).',
                 24: base + ' Balance: 24 credits, enough for the rig (20) but not a 5 s walk too (30 in all).',
                 4: base + ' Balance: 4 credits, not enough for the rig (20).',
                 None: base}
        for balance, expected in cases.items():
            with self.subTest(balance=balance), patch.object(cloud, '_runtime', self.runtime(balance)):
                self.assertEqual(autorig.price_note({'shape_hints': ['leg_gap']}), expected)

    def test_a_likely_experimental_shape_is_animated_by_hand_for_free(self):
        from loopcut import cloud
        with patch.object(cloud, '_runtime', self.runtime(20)):
            note = autorig.price_note({'shape_hints': ['upright_body', 'leg_gap']})
        self.assertEqual(note, 'Before rigging, tell the user the total: the rig is 20 credits, and Loopcut then '
                         'animates a shape like this with keyframes, for free (ready-made walks need a person shape). '
                         'Balance: 20 credits, enough for the rig.')
        self.assertNotIn('2 credits a second', note)
        with patch.object(cloud, '_runtime', None):
            self.assertEqual(autorig.price_note({'shape_hints': ['upright_body']}), '')

    def test_a_long_coat_alone_does_not_warn_of_an_experimental_rig(self):
        self.assertEqual(rig_inspect.PERSON_HINTS, frozenset({'leg_gap'}))
        self.assertFalse(autorig.likely_experimental({'shape_hints': ['leg_gap']}))
        self.assertTrue(autorig.likely_experimental({'shape_hints': ['head_and_body']}))
        # r13/hints.json: R16 (arms down, a standard rig) sets arm_separation only, so it keeps the soft warning but
        # its price note still quotes the walk.
        self.assertFalse(autorig.likely_experimental({'shape_hints': ['arm_separation']}))
        rows = {'B7': ['leg_gap'], 'R12': ['leg_gap'], 'R13': ['leg_gap'], 'R16': ['arm_separation'], 'R10': [],
                'G3 dog': ['upright_body', 'leg_gap', 'head_and_body', 'arm_separation'], 'P2': ['head_and_body', 'arm_separation']}
        warned = {k for k, hints in rows.items() if set(hints) - rig_inspect.PERSON_HINTS}
        self.assertEqual(warned, {'R16', 'G3 dog', 'P2'})
        for words in ('experimental', 'bend check', 'motion library', 'bone keyframes'):
            self.assertNotIn(words, rig_inspect.SHAPE_WARNING)
            self.assertNotIn(words, rig_experimental.MOTION_NOTE)
        self.assertIn('no ready-made walks', rig_inspect.SHAPE_WARNING)

    def test_inspect_on_rigged_characters_reads_plainly(self):
        from test_autorig_r12 import FakeRig
        fake = types.ModuleType('bpy')
        fake.data = types.SimpleNamespace(objects={})
        dog = FakeRig('Character Rig', 33, 'experimental')
        robe = FakeRig('Character Rig', 34, 'standard')
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(autorig, 'motion_price', return_value='2 credits a '
                                                                  'second (10 for 5 s)'), \
                patch.object(autorig, 'balance', return_value=4):
            self.assertEqual(autorig.describe_rig(dog)['message'], 'Already rigged by Loopcut (33 bones, not a person '
                             'rig, so no ready-made walks): animate it with keyframes, for free; check_deformation checks '
                             'the current pose. It needs no new rig.')
            text = autorig.describe_rig(robe)['message']
        self.assertIn('(2 credits a second (10 for 5 s); balance 4 credits; tell the user first)', text)
        self.assertIn('free gestures (wave, raise_arm, point)', text)


if __name__ == '__main__':
    unittest.main()
