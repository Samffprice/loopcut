"""R14 fixes from persona round 7 on v0.2.18 (runs3: ar-dog-5 20261025-191048, ar-beginner-4 20261025-195527; the free
plan's Fast model, GPT-6 Luna): a gesture-like motion_from_text on a Loopcut rig that already walks is not bought, as
the free gesture layers over the walk (R7-B1); the applied motion reads in plain words and lists the free extras
(R7-B2); the rig check's stamp says how to play it and its reply has a "Watch it" button (R7-D2/B2); captures and
inspections say the rig's bones are not in the picture (R7-D1). No Blender: Blender reads are faked."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
fake_bpy = types.ModuleType('bpy')
fake_bpy.app = types.SimpleNamespace(driver_namespace={})
sys.modules.setdefault('bpy', fake_bpy)

from loopcut import (agent, autorig, cloud, cloud_account, cloud_jobs, context, conversations, inspection,  # noqa: E402
                     llm, motion_retarget as mr, rig_experimental, rig_gate, rig_gesture, state)
from loopcut.ui import layout  # noqa: E402
from test_motion import descriptor as motion_descriptor, video_descriptor  # noqa: E402

R7_WAVE = 'A man stands still and waves with his right hand.'  # ar-beginner-4, call 45: 6 credits, the walk replaced.
FREE = ('Not bought, no credits used: a wave on Character Rig is free with character_rig gesture (gesture wave, side '
        'right), which layers it over its current motion (frames 1–120) and keeps that motion; frames [121, 168], '
        'after the motion ends, make it stop and then wave. motion_from_text would replace that motion with a new '
        'one. Only if the user wants a new full-body motion in place of it, make the same call again; it is charged '
        'then.')
CARD = 'Not bought, no credits used: a wave on Character Rig is free.'


class Rig(dict):
    """A Blender armature object, as far as the guard and the footer read one."""

    def __init__(self, name='Character Rig', tier='standard', ours=True, kind='ARMATURE', **props):
        super().__init__({autorig._KEY: json.dumps({'targets': ['Mesh']}), rig_experimental.TIER_PROPERTY: tier}
                         if ours else {}, **props)
        self.name, self.type, self.animation_data = name, kind, None
        self.pose = types.SimpleNamespace(bones=[])
        self.show_in_front, self.visible = True, True
        self.data = types.SimpleNamespace(display_type='STICK')

    def visible_get(self):
        return self.visible


def turn_session(busy=True):
    session = state.new_session()
    session['messages'] = [{'role': 'user', 'content': 'can he wave too? like stop and wave at the end. how much'}]
    session['busy'] = busy
    return session


def scene_bpy(objects=(), overlays=True, bones=True):
    fake = types.ModuleType('bpy')
    overlay = types.SimpleNamespace(show_overlays=overlays, show_bones=bones)
    area = types.SimpleNamespace(type='VIEW_3D', spaces=types.SimpleNamespace(active=types.SimpleNamespace(
        overlay=overlay)))
    window = types.SimpleNamespace(screen=types.SimpleNamespace(areas=[area]))
    fake.context = types.SimpleNamespace(
        scene=types.SimpleNamespace(objects=list(objects), render=types.SimpleNamespace(fps=24, fps_base=1.)),
        window_manager=types.SimpleNamespace(windows=[window]))
    fake.data = types.SimpleNamespace(objects={o.name: o for o in objects})
    return fake


def curve(*modifiers):
    return (None, 'rotation_quaternion', 0, types.SimpleNamespace(modifiers=[types.SimpleNamespace(type=m)
                                                                           for m in modifiers]))


class GesturePrompts(unittest.TestCase):
    """rig_gesture.asked: a wave, a point or a raised arm alone; anything the body does besides is a motion."""

    def test_round_sevens_prompt_and_close_variants_are_gestures(self):
        cases = {R7_WAVE: ('wave', 'right'), 'wave hello': ('wave', None), 'Waving goodbye with the left hand': (
            'wave', 'left'), 'stops and waves at the camera': ('wave', None), 'points forward': ('point', None),
            'a woman pointing at something with her right hand': ('point', 'right'),
            'raises his left arm': ('raise_arm', 'left'), 'lifts a hand': ('raise_arm', None),
            'hands up': ('raise_arm', None), 'raise both arms in the air': ('raise_arm', None)}
        for prompt, found in cases.items():
            with self.subTest(prompt):
                self.assertEqual(rig_gesture.asked(prompt), found)

    def test_whole_body_actions_and_other_prompts_are_not(self):
        for prompt in ('jump', 'dance', 'A man jumps and waves', 'walks forward and waves', 'he ran and waved',
                       'sitting and waving', 'turns around and points', 'a man stepped forward and pointed',
                       'holds a cup in his hand', 'picks up a box with his hands', 'shakes hands', 'a man stands idle',
                       'run and wave right and left', '', None, 42):
            with self.subTest(prompt):
                self.assertIsNone(rig_gesture.asked(prompt))


class FreeGesture(unittest.TestCase):
    """R7-B1: the guard itself, from faked Blender reads."""

    def guard(self, arm, prompt=R7_WAVE, session=None, span=(1, 120), channels=None, refusal=None):
        session = session if session is not None else turn_session()
        action = object() if span else None
        real = autorig._gesture_refusal
        with patch.dict(sys.modules, {'bpy': scene_bpy([arm])}), patch.object(state, 'session', return_value=session), \
                patch.object(autorig, '_channels', return_value=(action, channels or [curve()])), \
                patch.object(autorig, '_span', return_value=span), \
                patch.object(autorig, '_gesture_refusal', side_effect=lambda a: refusal or real(a)):
            return autorig.free_gesture(arm, prompt), session

    def test_a_wave_on_a_walking_standard_rig_is_not_bought(self):
        (text, card), session = self.guard(Rig())
        self.assertEqual((text, card), (FREE, CARD))
        mark = session['messages'][0][autorig.GUARDED]
        self.assertIsInstance(mark, str, 'conversations.load takes only scalars')
        self.assertEqual(json.loads(mark), ['Character Rig'])
        self.assertTrue(autorig.GUARDED.startswith(context.PRIVATE_PREFIX), 'never sent to the model')
        for jargon in ('NLA', 'root motion', 'credits a second', 'quote'):
            self.assertNotIn(jargon, text)

    def test_the_same_call_again_in_the_turn_is_bought_and_a_new_turn_asks_again(self):
        session = turn_session()
        self.assertEqual(self.guard(Rig(), session=session)[0], (FREE, CARD))
        self.assertEqual(self.guard(Rig(), session=session)[0], ('', ''), 'the escape hatch: bought as asked')
        self.assertEqual(self.guard(Rig(), prompt='raises his right arm', session=session)[0], ('', ''),
                         'reworded, the same rig and turn: bought too')
        self.assertTrue(self.guard(Rig('Rig 2'), session=session)[0][0].startswith(
            'Not bought, no credits used: a wave on Rig 2'), 'another rig is its own offer')
        session['messages'].append({'role': 'user', 'content': 'and point at the door'})
        self.assertTrue(self.guard(Rig(), prompt='points at the door', session=session)[0][0].startswith(
            'Not bought, no credits used: pointing on Character Rig'), 'a new user turn')
        self.assertEqual(json.loads(session['messages'][-1][autorig.GUARDED]), ['Character Rig'], 'on its own message')

    def test_it_never_fires_for_experimental_still_own_or_layered_rigs_or_whole_body_prompts(self):
        cases = {'experimental': dict(arm=Rig(tier='experimental')),
                 'still rig': dict(arm=Rig(), span=None),
                 'not a Loopcut rig': dict(arm=Rig(ours=False)),
                 'a mesh': dict(arm=Rig(kind='MESH')),
                 'NLA or controls': dict(arm=Rig(), refusal=autorig.RigError('layers', 'already_animated')),
                 'jump': dict(arm=Rig(), prompt='a man jumps up high'),
                 'dance': dict(arm=Rig(), prompt='dance happily'),
                 'walk and wave': dict(arm=Rig(), prompt='walks forward and waves'),
                 'outside a turn': dict(arm=Rig(), session=turn_session(busy=False))}
        for name, kwargs in cases.items():
            with self.subTest(name):
                (text, card), session = self.guard(**kwargs)
                self.assertEqual((text, card), ('', ''))
                self.assertNotIn(autorig.GUARDED, session['messages'][0])
        self.assertEqual(autorig.free_gesture(None, R7_WAVE), ('', ''))

    def test_the_real_refusals_keep_it_off(self):
        """add_gesture's own refusals decide (_gesture_refusal): the guard never offers a gesture that would refuse."""
        layered = Rig()
        layered.animation_data = types.SimpleNamespace(drivers=[], nla_tracks=[types.SimpleNamespace(mute=False,
                                                       strips=[1])], action_blend_type='REPLACE', action_influence=1.)
        posed = Rig(loopcut_rig_preview='stress')
        for arm in (layered, posed):
            self.assertIsNotNone(autorig._gesture_refusal(arm))
        self.assertIsNone(autorig._gesture_refusal(Rig()))
        self.assertEqual(autorig._gesture_refusal(Rig(tier='experimental')).code, 'experimental_rig')

    def test_a_looping_animation_gets_no_stop_hint(self):
        (text, _), _ = self.guard(Rig(), channels=[curve('CYCLES')])
        self.assertNotIn('make it stop', text)
        self.assertIn('which layers it over its current motion (frames 1–120) and keeps that motion. ', text)

    def test_a_broken_read_buys_as_asked(self):
        with patch.object(state, 'session', side_effect=RuntimeError('no session')):
            self.assertEqual(autorig.free_gesture(Rig(), R7_WAVE), ('', ''))

    def test_the_mark_saves_loads_and_never_reaches_the_wire(self):
        (_, _), session = self.guard(Rig())
        session['items'] = [state.item_user(session['messages'][0]['content'])]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'LOOPCUT_DATA_DIR': tmp}), \
                patch.object(conversations, 'rename_merged_tools'):
            conversations.save(session)
            loaded = conversations.load(session['id'])
        self.assertEqual(json.loads(loaded['messages'][0][autorig.GUARDED]), ['Character Rig'])
        self.assertNotIn(autorig.GUARDED, conversations.wire_messages(loaded['id'], loaded['messages'])[0])


class GuardPlacement(unittest.TestCase):
    """R7-B1: in cloud.execute's main-thread part (cloud_jobs.prepare_inputs), so before the quote, the consent card and
    the charge, which all wait in finish(); a neutral card."""

    def execute(self, d, arguments, free=(FREE, CARD), target=None):
        target = target or Rig()
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Mock(cache_dir=Path(tmp))
            runtime.descriptors.return_value = [d]
            consent = Mock(return_value=True)
            with patch.object(cloud, '_runtime', runtime), patch.object(cloud, 'sync_settings'), \
                    patch.object(cloud, '_progress'), patch.object(cloud_jobs, 'resolve_files', return_value={}), \
                    patch.object(cloud_jobs, 'motion_target', return_value=target), \
                    patch.object(autorig, 'free_gesture', return_value=free) as guard, \
                    patch.object(cloud_account, 'authorize') as authorize, \
                    patch.object(cloud_jobs, 'run') as run, \
                    patch.object(cloud_jobs.blobs, 'upload_missing') as upload, \
                    patch.object(cloud.cloud_http, 'Client'), cloud_account.approval_context(consent):
                result = cloud.execute(cloud.PREFIX + d['name'], arguments)
            return result, guard, (authorize, run, upload, consent, runtime.client.request)

    def test_a_guarded_wave_is_a_neutral_result_and_nothing_is_quoted_asked_or_charged(self):
        for threshold in (100, 0):  # 0: the server asks before every priced call (a per-call ask).
            d = motion_descriptor(backend='managed', approval={'confirm_over_credits': threshold})
            with self.subTest(threshold=threshold):
                self.assertEqual(cloud_account.approval_details(d)['required'], threshold == 0)
                result, guard, untouched = self.execute(d, {'prompt': R7_WAVE, 'target_armature': 'Character Rig'})
                self.assertTrue(result.ok, 'a condition, not a failure: no red card')
                self.assertIsNone(result.then, 'no job is prepared')
                self.assertEqual((result.text, result.outcome), (FREE, CARD), 'the card shows its first line')
                guard.assert_called_once()
                self.assertEqual(guard.call_args.args[1], R7_WAVE)
                for call in untouched:
                    call.assert_not_called()

    def test_no_card_comes_before_the_guard(self):
        """agent._approval gives a cloud tool no Run/Reject card before execute: its file and cost consent are both
        in finish(), after prepare_inputs."""
        self.assertEqual(agent._approval(cloud.PREFIX + 'motion_from_text', json.dumps({'prompt': R7_WAVE}), ()), '')

    def test_an_unguarded_call_goes_on_to_its_quote_as_before(self):
        d = motion_descriptor(backend='managed')
        result, guard, (authorize, run, *_), = self.execute(d, {'prompt': 'jump'}, free=('', ''))
        self.assertTrue(callable(result.then), 'the job is prepared; finish() quotes and asks as before')
        guard.assert_called_once()
        authorize.assert_not_called()  # Not yet: that is finish(), on the agent thread.

    def test_motion_from_video_is_never_guarded(self):
        d = video_descriptor()
        d['backend'] = 'managed'
        _, guard, _ = self.execute(d, {'video': 'Clip'})
        guard.assert_not_called()


class MotionText(unittest.TestCase):
    """R7-B2: "root motion in world", "resampled from 30", "Mixamo, 22 bones mapped" and "scrub or play frames 1–120"
    reached the user word for word."""

    def test_the_round_seven_walk_reads_in_plain_words(self):
        walk = mr.result_text('Motion: A man walks forward at a relaxed pace…', "on 'Character Rig'", 1, 120, 119 / 24,
                              24, travel=mr.travel_note(6.38, mr.way((-.26, -6.37), (0., -1.)), 6.38), scene_set=True,
                              fingers='Its fingers hold a relaxed pose; this motion does not move them',
                              warnings=[mr.plain_warning('fingers_static')], extras=True)
        self.assertEqual(walk, "Motion 'Motion: A man walks forward at a relaxed pace…' now plays on 'Character "
                         "Rig' over frames 1–120 (5.0 s at 24 fps). It moves 6.4 m forward from where it starts. The "
                         "scene now plays frames 1–120. Its fingers hold a relaxed pose; this motion does not move them. "
                         "Free on this rig: a wave, point or raised arm over this motion (character_rig gesture), and a "
                         "side camera that follows it (character_rig show)")
        for jargon in ('root motion', 'resampled', 'bones mapped', 'Mixamo', 'NLA', 'fingers_static', 'scrub'):
            self.assertNotIn(jargon, walk)
        summary = cloud_jobs.result_summary('apply_motion', {'id': 'job_1sUj'}, [walk])
        self.assertTrue(summary.startswith("Applied motion: Motion '"))
        self.assertIn('(character_rig show). Job job_1sUj; provenance recorded.', summary, 'one full stop')

    def test_a_replaced_motion_says_so_and_where_it_is_kept(self):
        wave = mr.result_text('Motion: A man stands still and waves with his right…', "on 'Character Rig'", 1, 72,
                              71 / 24, 24, in_place=True, stashed='Motion: A man walks forward at a relaxed pace…',
                              scene_set=True)
        self.assertIn("It stays on the spot. It replaces the earlier motion 'Motion: A man walks forward at a relaxed "
                      "pace…', which is kept, switched off, in the Nonlinear Animation editor. The scene now plays "
                      "frames 1–72", wave)
        self.assertNotIn('Free on this rig', wave, 'the extras are for standard Loopcut rigs only')

    def test_which_way_and_how_far(self):
        facing = (0., -1.)  # A Loopcut rig faces -Y.
        self.assertEqual([mr.way(m, facing) for m in ((0, -3), (0, 3), (3, 0), (-3, 0), (0, 0))],
                         ['forward', 'backward', 'to its left', 'to its right', ''])
        self.assertEqual(mr.travel_note(6.38, 'forward'), 'it moves 6.4 m forward from where it starts')
        self.assertEqual(mr.travel_note(.3, '', 2.3), 'it moves up to 2.3 m from where it starts and ends near it')
        self.assertEqual(mr.travel_note(.3, '', .5), '')
        still = mr.result_text('Motion: dance', "on 'Hero' (a Mixamo rig)", 1, 48, 2., 24)
        self.assertIn('It stays within 1 m of where it starts', still)
        outside = mr.result_text('Motion: walk', "on 'Hero'", 1, 120, 5., 24, outside=(1, 72),
                                 warnings=['mixamorig:Hips is connected to its parent, so the character cannot travel',
                                           'feet slide'])
        self.assertIn('The scene plays frames 1–72, so part of this motion is outside it. mixamorig:Hips is connected '
                      'to its parent, so the character cannot travel. Feet slide', outside, 'a bone name keeps its case')

    def test_the_services_warning_codes_in_words(self):
        self.assertEqual([mr.plain_warning(w) for w in ('fingers_static', 'resampled_from_25_fps',
                                                         'downscaled_to_1280x720', 'prompt_split_into_3_segments',
                                                         'multiple_people_tracked_largest', 'feet slide')],
                         ['', '', '', 'the prompt was made as 3 parts, one after another',
                          'the video shows more than one person; it follows the largest', 'feet slide'])


class WatchIt(unittest.TestCase):
    """R7-D2/B2: show got 0 calls in round 7 and all of round 6; the stamp carries the button."""

    def gate(self, verdicts):
        session = state.new_session()
        request = {'role': 'user', 'content': 'make him walk', rig_gate.CHANGED: json.dumps({'Character Rig': ['motion']})}
        session['messages'] = [request, {'role': 'assistant', 'content': 'He walks.'}]
        session['items'] = [state.item_user('make him walk'), {'kind': 'assistant', 'text': 'He walks.',
                                                               'streaming': False}]
        turn = agent.Turn()
        turn.outcome = 'running'
        with patch.object(rig_gate, 'check', return_value=verdicts):
            rig_gate.finish_gate(session, turn, llm.Completion(text='He walks.'), 'single', lambda fn: fn())
        return session

    def walk(self, animated=True, watch=True):
        return [{'rig': 'Character Rig', 'unplayed': False, 'animated': animated, 'watch': watch, 'checks': [
            {'ok': True, 'kind': 'motion', 'line': 'it keeps moving from frame 1 to 120', 'detail': ''},
            {'ok': True, 'kind': 'bend', 'line': 'it bends without tearing or spikes in the 15 frames checked',
             'detail': ''}]}]

    def test_an_animated_rigs_stamp_says_how_to_play_it_and_carries_the_button(self):
        session = self.gate(self.walk())
        reply = session['items'][-1]
        self.assertEqual(reply['verification'], 'Verified by Loopcut on this scene: 2 of 2 checks.\n'
                         'The animation keeps moving from frame 1 to 120.\n'
                         'It bends without tearing or spikes in the 15 frames checked.\n'
                         'Press Space over the 3D view to play it.')
        self.assertEqual(reply[rig_gate.ACTION], {'label': 'Watch it', 'op': 'loopcut.rig_show', 'rig': 'Character Rig'})
        self.assertEqual(session['messages'][-1]['content'].split('\n\n')[-1], reply['verification'])

    def test_a_still_rig_or_one_show_cannot_frame_gets_neither(self):
        still = self.gate([{**self.walk()[0], 'animated': False, 'watch': False}])['items'][-1]
        self.assertNotIn('Press Space', still['verification'])
        self.assertNotIn(rig_gate.ACTION, still)
        unskinned = self.gate(self.walk(watch=False))['items'][-1]
        self.assertIn('Press Space', unskinned['verification'])
        self.assertNotIn(rig_gate.ACTION, unskinned)

    def test_only_an_allowlisted_op_on_a_named_loopcut_rig_is_a_button(self):
        rigs = frozenset({'Character Rig'})
        good = {rig_gate.ACTION: {'label': 'Watch it', 'op': 'loopcut.rig_show', 'rig': 'Character Rig'}}
        self.assertEqual(rig_gate.footer_action(good, rigs), ('Watch it', 'Character Rig'))
        tampered = [{'op': 'wm.quit_blender', 'rig': 'Character Rig'}, {'op': 'loopcut.rig_remove', 'rig': 'Character Rig'},
                    {'op': ['loopcut.rig_show'], 'rig': 'Character Rig'}, {'op': 'loopcut.rig_show', 'rig': 'Cube'},
                    {'op': 'loopcut.rig_show', 'rig': ['Character Rig']}, {'op': 'loopcut.rig_show'}, 'loopcut.rig_show']
        for action in tampered:
            with self.subTest(action=action):
                self.assertIsNone(rig_gate.footer_action({rig_gate.ACTION: action}, rigs))
        relabelled = {rig_gate.ACTION: {'label': 'Delete everything', 'op': 'loopcut.rig_show', 'rig': 'Character Rig'}}
        self.assertEqual(rig_gate.footer_action(relabelled, rigs)[0], 'Watch it', 'the label is never the item\'s')

    def footer(self, session, rigs=frozenset({'Character Rig'})):
        drawn = layout.build(session, 420, 900, 1, lambda font, size, text: len(text) * size * .6,
                             ui={'watch_rigs': rigs})
        hits = [h for h in drawn['hits'] if h['id'].endswith('.verification.action')]
        labels = [p['text'] for p in drawn['prims'] if p['id'].endswith('.verification.action.label')]
        return hits, labels

    def test_the_footer_draws_one_button_of_its_own_kind(self):
        session = self.gate(self.walk())
        hits, labels = self.footer(session)
        self.assertEqual(labels, ['Watch it'])
        self.assertEqual(hits[0]['action'], ('verification_action', len(session['items']) - 1))
        self.assertNotIn(hits[0]['action'][0], ('approve', 'approve_always', 'reject'), "5b's answer keys stay theirs")
        self.assertEqual(self.footer(session, frozenset()), ([], []), 'a rig no longer in the file: no button')

    def test_a_saved_conversation_loads_and_a_tampered_one_draws_and_runs_nothing(self):
        session = self.gate(self.walk())
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'LOOPCUT_DATA_DIR': tmp}), \
                patch.object(conversations, 'rename_merged_tools'):
            conversations.save(session)
            loaded = conversations.load(session['id'])
            self.assertEqual(self.footer(loaded)[1], ['Watch it'])
            path = Path(tmp) / 'conversations' / session['id'] / 'conversation.json'
            if not path.exists():
                path = next(Path(tmp).rglob('conversation.json'))
            body = json.loads(path.read_text())
            body['items'][-1][rig_gate.ACTION] = {'label': 'Watch it', 'op': 'wm.quit_blender', 'rig': 'Character Rig'}
            path.write_text(json.dumps(body))
            tampered = conversations.load(session['id'])
        self.assertEqual(self.footer(tampered), ([], []))
        with patch.object(rig_gate, 'watchable', return_value=frozenset({'Character Rig'})), \
                patch.object(autorig, 'tool') as tool:
            self.assertEqual(rig_gate.run_footer(tampered, len(tampered['items']) - 1), '')
            tool.assert_not_called()

    def test_the_click_runs_show_on_the_rig_once_checked_again(self):
        from loopcut import tools
        session = self.gate(self.walk())
        n = len(session['items']) - 1
        with patch.object(rig_gate, 'watchable', return_value=frozenset({'Character Rig'})), \
                patch.object(autorig, 'tool', return_value=tools.ToolResult('Ready to watch (free): …')) as tool:
            line = rig_gate.run_footer(session, n)
        tool.assert_called_once_with('show', rig='Character Rig')
        self.assertEqual(line, 'Watching Character Rig through Follow Camera, with its bones hidden. Press Space over '
                         'the 3D view to play it.')
        with patch.object(rig_gate, 'watchable', return_value=frozenset()), patch.object(autorig, 'tool') as tool:
            self.assertEqual(rig_gate.run_footer(session, n), '', 'the rig is gone or no longer Loopcut\'s')
            self.assertEqual(rig_gate.run_footer(session, 99), '')
            self.assertEqual(rig_gate.run_footer(session, 'x'), '')
            tool.assert_not_called()
        with patch.object(rig_gate, 'watchable', return_value=frozenset({'Character Rig'})), \
                patch.object(autorig, 'tool', return_value=tools.ToolResult('This rig has no skinned mesh to show.',
                                                                           ok=False)):
            self.assertEqual(rig_gate.run_footer(session, n), 'This rig has no skinned mesh to show.')

    def test_watchable_names_only_loopcut_armatures_in_the_file(self):
        session = {'items': [{rig_gate.ACTION: {'rig': name}} for name in ('Character Rig', 'Own Rig', 'Mesh', 'Gone')]
                   + [{rig_gate.ACTION: 'x'}, {}]}
        objects = [Rig(), Rig('Own Rig', ours=False), Rig('Mesh', kind='MESH')]
        with patch.dict(sys.modules, {'bpy': scene_bpy(objects)}):
            self.assertEqual(rig_gate.watchable(session), frozenset({'Character Rig'}))


class UnseenBones(unittest.TestCase):
    """R7-D1: "no spikes" twice, about bones the model's pictures never showed."""

    def note(self, objects, **views):
        with patch.dict(sys.modules, {'bpy': scene_bpy(objects, **views)}):
            return autorig.unseen_bones()

    def test_a_visible_rig_drawn_in_front_is_named(self):
        self.assertEqual(self.note([Rig()]), "Not in this picture: Character Rig's bones, which the user's viewport "
                         "draws over the body as grey sticks; they never render.")
        octahedral = Rig('Dog Rig')
        octahedral.data.display_type = 'OCTAHEDRAL'
        self.assertEqual(self.note([Rig(), octahedral]), "Not in this picture: the bones of Character Rig and Dog Rig, "
                         "which the user's viewport draws over the body as grey bone shapes; they never render.")

    def test_nothing_when_the_user_sees_no_bones_either(self):
        hidden, behind, own = Rig(), Rig(), Rig(ours=False)
        hidden.visible, behind.show_in_front = False, False
        for objects, views in (([hidden], {}), ([behind], {}), ([own], {}), ([], {}), ([Rig()], {'overlays': False}),
                               ([Rig()], {'bones': False})):
            with self.subTest(objects=objects, views=views):
                self.assertEqual(self.note(objects, **views), '')
        with patch.dict(sys.modules, {'bpy': types.ModuleType('bpy')}):
            self.assertEqual(autorig.unseen_bones(), '', 'never raises')

    def test_an_inspection_gives_it_to_the_model_not_the_card(self):
        row = {'state': 'complete', 'live_token': 'tok', 'output': '/x', 'scene_revision': 'a' * 64, 'id': 'i'}
        meta = {'sheet_sha256': 'sha', 'scene_revision': 'a' * 64, 'views': []}
        with patch.object(inspection.jobs, 'status', return_value=row), \
                patch.object(inspection, 'live_token', return_value='tok'), \
                patch.object(inspection.jobs, 'read_json', return_value=meta), \
                patch.object(inspection.jobs, 'digest', return_value='sha'), \
                patch.object(autorig, 'unseen_bones', return_value='Not in this picture: …'):
            result = inspection.finish(Path('/x'))
        self.assertEqual(result.model_note, 'Not in this picture: …')
        self.assertNotIn('Not in this picture', result.text)


if __name__ == '__main__':
    unittest.main()
