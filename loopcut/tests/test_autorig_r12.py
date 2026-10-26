"""R12 fixes from persona round 5 on rc/0.2.16 (runs3: ar-dog-3 20261024-154435, ar-beginner-2 20261024-160209,
the free plan's Fast model): "verified" over a noodle-stretched gallop and "fixed" over a surfboard boot, with the
bend check refusing the experimental dog and never run on the walk (R5-D2, R5-B2); facts that came only after the
purchase (R5-D1, R5-B1); refusals drawn as red X cards (R5-D3); and small hints (R5-D5, R5-B4). No Blender:
rig_deform, rig_parts and the wording are pure Python, and the Blender reads are faked."""
import json
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))

from loopcut import autorig, motion_retarget, rig_deform, rig_experimental, rig_inspect, rig_parts  # noqa: E402
from loopcut.rig_inspect import RigError  # noqa: E402
from test_autorig_r10 import bend, tube  # noqa: E402


def row(name, bent, stretch=1.5, squash=.5, volume=.9, spikes=0, bend=None):
    return {'name': name, 'bent': bent, 'bend': bend, 'vertices': 100, 'spikes': spikes, 'spike_cm': 1.,
            'stretch': stretch, 'squash': squash, 'volume': volume}


ANIMATION = {'frames': 14, 'first': 1, 'last': 120}


def walk(ankle=(2.0, 7.2), knee=(1.5, 1.8)):
    """Two frames of four joints: the left ankle's worst at frame 59, the right ankle never turned."""
    return [(1, [row('left knee', 30, knee[0], bend=30.), row('left ankle', 20, ankle[0]), row('right knee', 25),
                 row('right ankle', 2)]),
            (59, [row('left knee', 40, knee[1], bend=40.), row('left ankle', 30, ankle[1], squash=.3),
                  row('right knee', 20), row('right ankle', 3)])]


class AnimationCheck(unittest.TestCase):
    """R5-D2 and R5-B2: the check now samples the animation and leads with its worst frame and region."""

    def test_the_frames_follow_what_plays(self):
        self.assertEqual(rig_deform.span((1., 25.), (1, 96), cyclic=True), (1, 96), "ar-dog-3's cycled gallop")
        self.assertEqual(rig_deform.span((1., 120.), (1, 250)), (1, 120), 'the walk inside a longer scene range')
        self.assertEqual(rig_deform.span((200., 300.), (1, 100)), (200, 300), 'an action outside the scene range')
        self.assertIsNone(rig_deform.span((1., 1.), (1, 250)), 'a single keyed frame is a pose, not an animation')
        picked = rig_deform.frames(1, 120, lambda f: -abs(f - 61))
        self.assertEqual(len(picked), rig_deform.ANIMATION_FRAMES + rig_deform.EXTREMES)
        self.assertIn(1, picked)
        self.assertIn(120, picked)
        self.assertIn(61, picked, 'the frame where the bones turn furthest is checked')
        self.assertEqual(rig_deform.frames(1, 8, lambda f: 0), list(range(1, 9)), 'a short one: every frame')

    def test_the_worst_frame_and_region_come_first(self):
        rows = rig_deform.worst(walk())
        self.assertEqual([r['frame'] for r in rows], [59, 59, 1, 59], 'a tie keeps the first; never turned: most')
        text = rig_deform.lines(rows, animation=ANIMATION)
        self.assertEqual(text[0], 'Worst at frame 59: left ankle, skin torn: edges stretched to 7.2x.')
        self.assertEqual(text[1], 'Deformation check over the animation (free; 14 frames from 1 to 120, each against '
                                  'rest): problems at left ankle.')
        self.assertIn('- left ankle, worst at frame 59, turned 30° from rest: PROBLEM: skin torn: edges stretched to '
                      '7.2x (stretch 7.20x, squash 0.30x, volume 90%).', text)
        self.assertIn('- left knee, worst at frame 59, bent 40° from straight: no spikes or torn edges', text[2])
        self.assertIn('Not tested, since never turned: right ankle.', text)
        self.assertTrue(text[-1].endswith('It checked 14 frames, not every one, so look at the worst one too.'))
        self.assertIn('cannot see skin pushed through skin', text[-1], "r11's footer stays")

    def test_a_clean_animation_still_names_its_worst_frame(self):
        text = rig_deform.lines(rig_deform.worst(walk(ankle=(1.2, 1.3), knee=(1.5, 1.9))), animation=ANIMATION)
        self.assertEqual(text[0], 'Worst at frame 59: left ankle, stretch 1.30x, squash 0.30x, no spikes or torn '
                                  'edges.', 'squashed to 0.30x is nearer its limit than stretched to 1.90x')
        self.assertIn('no spikes or torn edges at the turned joints.', text[1])

    def test_an_animations_strays_need_more_spikes_to_be_a_problem(self):
        """Clean rigs reached 7 spikes in the dance's worst frame (a mild armpit pinch); broken ones 25 to 136."""
        dance = [(44, [row('right shoulder', 85, 4.2, spikes=7), row('left knee', 30)])]
        text = rig_deform.lines(rig_deform.worst(dance), animation=ANIMATION)
        self.assertIn('- right shoulder, worst at frame 44, turned 85° from rest: minor: 7 stray vertices', text[2])
        self.assertNotIn('PROBLEM', '\n'.join(text))
        torn = [(98, [row('right elbow', 53, 1.7, spikes=28, bend=53.), row('left knee', 30)])]
        text = rig_deform.lines(rig_deform.worst(torn), animation=ANIMATION)
        self.assertEqual(text[0], 'Worst at frame 98: right elbow, 28 vertices pulled out into spikes, up to 1.0 cm.')
        still = rig_deform.lines([row('right shoulder', 85, spikes=7)])
        self.assertIn('PROBLEM: 7 vertices pulled out into spikes', still[1], "a still pose keeps r10's limit")

    def test_an_animation_that_never_turns_tests_nothing(self):
        still = [(f, [row('left knee', 2), row('left ankle', 1)]) for f in (1, 60)]
        text = rig_deform.lines(rig_deform.worst(still), animation=ANIMATION)
        self.assertEqual(text, [rig_deform.nothing(ANIMATION)])
        self.assertIn('no joint is turned from rest in the 14 frames checked from 1 to 120', text[0])
        self.assertIn('do not call any joint clean', text[0])

    def test_one_plan_measures_every_frame_as_measure_does(self):
        points, edges, tris = tube()
        centre, radius = (0., 0., .3), .12
        planned = rig_deform.plan([{'rest': points, 'edges': edges, 'tris': tris, 'scale': 1.,
                                    'joints': [(centre, centre, radius)]}])
        for degrees in (30, 90, 140):
            posed = bend(points, degrees, blend=.01)
            joints = [{'name': 'left knee', 'bent': degrees}]
            alone = rig_deform.measure([{'rest': points, 'posed': posed, 'edges': edges, 'tris': tris, 'scale': 1.,
                                         'joints': [(centre, centre, radius)]}], joints)
            self.assertEqual(rig_deform.measure_pose(planned, joints, [(posed, [centre])]), alone)

    def test_a_part_border_counts_for_both_parts(self):
        """An experimental rig's parts are regions by dominant bone, so the joint sits on their border."""
        points, edges, tris = tube(rings=24)
        region = [0 if p[2] < .3 else 1 for p in points]
        posed = bend(points, 120, joint=.3, blend=.005)
        joints = [{'name': 'body', 'bent': 0}, {'name': 'tail', 'bent': 120}]
        centres = [((0., 0., .15), (0., 0., .15), .2), ((0., 0., .45), (0., 0., .45), .2)]
        mesh = {'rest': points, 'posed': posed, 'edges': edges, 'tris': tris, 'scale': 1., 'joints': centres,
                'region': region}
        inside = rig_deform.measure([mesh], joints)
        border = rig_deform.measure([dict(mesh, boundary=True)], joints)
        self.assertGreater(border[1]['stretch'], inside[1]['stretch'] + .2, 'the bend at the border is measured')
        self.assertEqual([r['vertices'] for r in border], [region.count(0), region.count(1)])


def animated(drivers=(), constraint=False, nla=None, shape_keys=None):
    ns = types.SimpleNamespace
    bone = ns(constraints=[ns(influence=1., mute=False)] if constraint else [])
    ad = ns(drivers=list(drivers), action_influence=1., action_blend_type='REPLACE',
            nla_tracks=[ns(mute=nla == 'muted', strips=[1])] if nla else [])
    arm = ns(animation_data=ad, pose=ns(bones=[bone]), constraints=[], data=ns(animation_data=None))
    key = ns(animation_data=ns(action=object(), drivers=[], nla_tracks=[])) if shape_keys else None
    mesh = ns(animation_data=None, data=ns(animation_data=None, shape_keys=key))
    return arm, [mesh]


class FrameSetting(unittest.TestCase):
    """Frames come from the action's curves only when that is what Blender would evaluate; else frame_set."""

    def test_constraints_drivers_nla_and_animated_meshes_fall_back_to_frame_set(self):
        curves = ['rotation']
        cases = {'plain keys': ({}, True), 'a muted stash of an older action': ({'nla': 'muted'}, True),
                 'a bone constraint': ({'constraint': True}, False), 'a driver': ({'drivers': ['d']}, False),
                 'an NLA track that plays': ({'nla': 'playing'}, False),
                 'driven or keyed shape keys': ({'shape_keys': True}, False)}
        for label, (options, direct) in cases.items():
            arm, meshes = animated(**options)
            with self.subTest(label):
                self.assertEqual(autorig._curves_only(arm, meshes, curves, ['channel']), direct)
        arm, meshes = animated()
        self.assertFalse(autorig._curves_only(arm, meshes, curves + ['custom'], ['channel']),
                         'a curve the check would skip (a rig switch) falls back too')


class AnimationMemory(unittest.TestCase):
    """R5-B2: fix 2 (the surfboard boot) against the check after fix 1 of the same walk."""

    def test_worse_still_better_and_no_change(self):
        def rows(stretch, volume=.9):
            return [row('left ankle', 30, stretch, volume=volume), row('right ankle', 2)]
        lead, memory = rig_deform.remember_animation([], 'Walk', rows(1.62))
        self.assertEqual(lead, [])
        lead, memory = rig_deform.remember_animation(memory, 'Walk', rows(3.91))
        self.assertEqual(lead, ['Worse since the last check of this animation: left ankle stretch 1.62x → 3.91x; '
                                'undo the last change.'])
        lead, memory = rig_deform.remember_animation(memory, 'Walk', rows(2.7))
        self.assertEqual(lead, ['Still worse than the first check of this animation: left ankle stretch 1.62x → '
                                '2.70x.'])
        lead, memory = rig_deform.remember_animation(memory, 'Walk', rows(1.5))
        self.assertEqual(lead, ['Better since the last check of this animation: left ankle stretch 2.70x → 1.50x.'])
        lead, memory = rig_deform.remember_animation(memory, 'Walk', rows(1.52))
        self.assertEqual(lead, ['No clear change since the last check of this animation.'])
        lead, _ = rig_deform.remember_animation(memory, 'Wave', rows(4.0))
        self.assertEqual(lead, [], 'another animation starts its own record')

    def test_a_fix_that_mends_one_joint_and_hurts_another_says_both(self):
        """Fix 3 mended both boots (79 spikes to none) and stretched the sleeves; "undo the last change" would have
        put the surfboard boots back."""
        def rows(ankle_spikes, elbow):
            return [row('left ankle', 30, 4.7 if ankle_spikes else 1.7, spikes=ankle_spikes),
                    row('right elbow', 40, elbow, bend=40.)]
        _, memory = rig_deform.remember_animation([], 'Walk', rows(79, 2.04))
        lead, _ = rig_deform.remember_animation(memory, 'Walk', rows(0, 4.97))
        self.assertEqual(lead, ['Since the last check of this animation, better: left ankle stretch 4.70x → 1.70x, '
                                'spikes 79 → 0; worse: right elbow stretch 2.04x → 4.97x.'])
        # Fix 2 against fix 1: a joint's volume came back, but nothing flagged was mended, so it is still an undo.
        _, memory = rig_deform.remember_animation([], 'Walk', [row('left ankle', 30, 1.0, volume=2.28)])
        lead, _ = rig_deform.remember_animation(memory, 'Walk', [row('left ankle', 30, 4.85, volume=1.04, spikes=88)])
        self.assertEqual(lead, ['Worse since the last check of this animation: left ankle stretch 1.00x → 4.85x, spikes '
                                '0 → 88; undo the last change.'])


# ar-dog-3's experimental rig as the run printed it (world space, metres; transcript call 8), parent first.
DOG = [('bone_0', None, (.01, .62, 2.02)), ('bone_1', 'bone_0', (.01, .62, 2.01)), ('bone_2', 'bone_1', (.01, 1.04, 2.)),
       ('bone_3', 'bone_2', (.01, 1.38, 2.38)), ('bone_4', 'bone_1', (.01, .27, 1.85)),
       ('bone_5', 'bone_4', (.01, -.07, 1.82)), ('bone_6', 'bone_5', (.01, -.42, 1.83)),
       ('bone_7', 'bone_6', (.01, -.99, 2.14)), ('bone_8', 'bone_7', (.01, -1.34, 2.55)),
       ('bone_9', 'bone_8', (.02, -1.87, 2.52)), ('bone_10', 'bone_6', (.01, -1.13, 1.64)),
       ('bone_11', 'bone_6', (.11, -.8, 2.26)), ('bone_12', 'bone_11', (.31, -1.02, 1.8)),
       ('bone_13', 'bone_12', (.3, -.92, 1.18)), ('bone_14', 'bone_13', (.28, -.96, .44)),
       ('bone_15', 'bone_14', (.28, -1.05, .09)), ('bone_16', 'bone_6', (-.1, -.8, 2.26)),
       ('bone_17', 'bone_16', (-.3, -1.02, 1.8)), ('bone_18', 'bone_17', (-.3, -.92, 1.18)),
       ('bone_19', 'bone_18', (-.27, -.96, .44)), ('bone_20', 'bone_19', (-.27, -1.05, .09)),
       ('bone_21', 'bone_5', (.01, -.2, 1.41)), ('bone_22', 'bone_1', (.01, .62, 2.02)),
       ('bone_23', 'bone_22', (.3, .72, 1.71)), ('bone_24', 'bone_23', (.33, .78, 1.1)),
       ('bone_25', 'bone_24', (.34, 1.19, .68)), ('bone_26', 'bone_25', (.33, 1.07, .13)),
       ('bone_27', 'bone_22', (.01, .62, 2.02)), ('bone_28', 'bone_27', (-.28, .72, 1.71)),
       ('bone_29', 'bone_28', (-.31, .78, 1.1)), ('bone_30', 'bone_29', (-.33, 1.19, .68)),
       ('bone_31', 'bone_30', (-.31, 1.07, .13)), ('bone_32', 'bone_27', (.01, .62, 2.02))]


def skin(layout, empty=(), heavy=None):
    """rig_parts input: each bone dominating some vertices around its head, except the empty ones."""
    heavy = heavy or {}
    out = {}
    for name, parent, head in layout:
        count = 0 if name in empty else heavy.get(name, 60)
        out[name] = {'parent': parent, 'head': head, 'vertices': count, 'low': head[2] - .05 if count else None,
                     'centroid': head if count else None}
    return out


class ExperimentalParts(unittest.TestCase):
    """R5-D2: the check refused the experimental dog; its parts are now named by their place on the body."""

    def test_the_dog_gets_a_head_body_tail_and_four_named_legs(self):
        found = rig_parts.parts(skin(DOG, empty={'bone_0', 'bone_10', 'bone_21', 'bone_22', 'bone_27', 'bone_32'},
                                     heavy={'bone_2': 400, 'bone_3': 300, 'bone_4': 300, 'bone_5': 300}), 0., 3.1)
        self.assertEqual(found['cue'], 'head')
        self.assertGreater(found['front'][1], .99, 'the head is at +Y, as the run found')
        named = {p['name']: sorted(p['bones'], key=lambda b: int(b[5:])) for p in found['parts']}
        self.assertEqual(list(named), ['head', 'body', 'tail', 'front right leg', 'front left leg', 'back right leg',
                                       'back left leg'])
        self.assertEqual(named['front right leg'], ['bone_23', 'bone_24', 'bone_25', 'bone_26'])
        self.assertEqual(named['back left leg'], ['bone_16', 'bone_17', 'bone_18', 'bone_19', 'bone_20'])
        self.assertEqual(named['tail'], ['bone_7', 'bone_8', 'bone_9'])
        self.assertIn('bone_21', named['body'], 'a branch with no skin goes to the body')
        self.assertFalse([n for n in named if 'bone' in n], 'never bone names')

    def test_a_biped_faces_where_its_toes_point(self):
        layout = [('pelvis', None, (0., 0., 1.)), ('spine', 'pelvis', (0., 0., 1.3)), ('neck', 'spine', (0., .1, 1.6)),
                  ('skull', 'neck', (0., .15, 1.75)), ('thigh_a', 'pelvis', (.15, 0., .95)),
                  ('shin_a', 'thigh_a', (.15, 0., .5)), ('foot_a', 'shin_a', (.15, 0., .05)),
                  ('thigh_b', 'pelvis', (-.15, 0., .95)), ('shin_b', 'thigh_b', (-.15, 0., .5)),
                  ('foot_b', 'shin_b', (-.15, 0., .05))]
        found = rig_parts.parts(skin(layout), 0., 1.8, feet=(0., -.04, 0.))
        self.assertEqual(found['cue'], 'feet')
        self.assertLess(found['front'][1], -.99)
        named = {p['name']: p['bones'] for p in found['parts']}
        self.assertIn('foot_a', named['left leg'], 'facing -Y, +X is the left side')
        self.assertIn('foot_b', named['right leg'])
        self.assertIn('skull', named['upper body'], "a biped's spine and head hang from its hips as one branch")

    def test_without_a_head_or_feet_legs_are_counted_not_sided(self):
        layout = [('seat', None, (0., 0., .5))] + [(f'leg{k}', 'seat', (x, y, .45)) for k, (x, y) in
                                                   enumerate([(.2, .2), (-.2, .2), (.2, -.2), (-.2, -.2)])]
        layout += [(f'foot{k}', f'leg{k}', (x, y, .02)) for k, (x, y) in enumerate([(.2, .2), (-.2, .2), (.2, -.2),
                                                                                    (-.2, -.2)])]
        found = rig_parts.parts(skin(layout), 0., 1.)
        self.assertIsNone(found['front'])
        self.assertEqual(sorted(p['name'] for p in found['parts']),
                         ['body', 'leg 1 of 4', 'leg 2 of 4', 'leg 3 of 4', 'leg 4 of 4'])

    def test_part_wording(self):
        rows = [row('front left leg', 40), row('tail', 3), {**row('head', 30), 'vertices': 0}]
        text = rig_deform.lines(rows, word='part')
        self.assertEqual(text[0], 'Deformation check (free; the posed mesh against rest) at front left leg turned 40° '
                                  'from rest: no spikes or torn edges at the turned parts.')
        self.assertIn('Turned, but no skin there to test: head.', text)
        self.assertIn('Not tested, since not turned: tail.', text)
        self.assertIn('do not call a flagged or untested part clean.', text[-1])
        self.assertTrue(rig_deform.nothing(word='part').endswith('Pose or animate it first, then check again.'))
        saved = {'bone_23': [0, 0, 0, 0, 1, 0, 0, 0, 1]}
        moved = rig_deform.moved(saved, {'bone_23': [.05, 0, 0, 0, 1, 0, 0, 0, 1]}, {}, 1., {'bone_23': 'front left leg'})
        self.assertEqual(moved, 'Rest skeleton changed since rigging: a front left leg joint moved 5.0 cm; the skin was '
                                'weighted to the old joints, so it can tear there.')


class UpFrontFacts(unittest.TestCase):
    """R5-D1 and R5-B1: what the rig and the walk cost, and what an experimental rig cannot do, before buying."""

    def runtime(self, motion=10, rig=20):
        descriptors = [{'name': 'auto_rig', 'cost': {'credits_estimate': rig}},
                       {'name': 'motion_from_text', 'cost': {'credits_estimate': motion}}]
        return types.SimpleNamespace(descriptors=lambda: descriptors)

    def test_the_prices_come_from_the_catalog(self):
        from loopcut import cloud
        with patch.object(cloud, '_runtime', self.runtime()):
            self.assertEqual(autorig.motion_price(), '2 credits a second (10 for 5 s)')
            self.assertEqual(autorig.price_note(), 'Before rigging, tell the user the total: the rig is 20 credits and '
                                                   'a ready-made walk or run is 2 credits a second (10 for 5 s).')
        with patch.object(cloud, '_runtime', self.runtime(motion=15)):
            self.assertIn('3 credits a second (15 for 5 s)', autorig.price_note())
        with patch.object(cloud, '_runtime', None):
            self.assertEqual(autorig.price_note(), '', 'no catalog, no made-up price')

    def test_inspect_carries_the_price_note_and_the_experimental_facts(self):
        report = {'eligible': True, 'warnings': [rig_inspect.SHAPE_WARNING]}
        from loopcut import cloud
        with patch.object(autorig, 'rigged', return_value=None), \
                patch.object(autorig, 'inspect', return_value=(None, None, None, report)), \
                patch.object(cloud, '_runtime', self.runtime()), patch('loopcut.tools._editor_context'):
            result = json.loads(autorig.tool('inspect').text)
        self.assertTrue(result['price_note'].startswith('Before rigging, tell the user the total'))
        for words in ('person rig', 'no ready-made walks', 'keyframes', 'for free'):  # R13: the user's words.
            self.assertIn(words, rig_inspect.SHAPE_WARNING)

    def test_the_rig_results_tell_the_model_what_comes_next(self):
        steps = rig_experimental.describe([{'name': 'bone_0', 'parent': None}, {'name': 'bone_1', 'parent': 0}],
                                          'Character Rig')['next_steps']
        # R13: the turn's end runs the check (rig_gate), and show hides the bones.
        self.assertIn('hides Character Rig (its bones draw over the mesh)', steps[2])
        self.assertEqual(steps[1], 'Loopcut checks how it moves and bends when your reply ends.')
        self.assertIn('Loopcut checks how it moves and bends when your reply ends', rig_experimental.MOTION_NOTE)
        description = autorig.SCHEMAS[0]['function']['description']
        self.assertIn('bend check (a whole animation too)', description)
        self.assertLess(len(json.dumps(autorig.SCHEMAS)), 1100, 'the model pays for every word')


class FakeRig(dict):
    def __init__(self, name, bones, tier=None, ours=True, targets=()):
        super().__init__()
        self.name, self.type, self.animation_data = name, 'ARMATURE', None
        self.data = types.SimpleNamespace(bones=[types.SimpleNamespace(name=f'b{k}') for k in range(bones)])
        if ours:
            self[autorig._KEY] = json.dumps({'targets': list(targets)})
        if tier:
            self[rig_experimental.TIER_PROPERTY] = tier


class NeutralResults(unittest.TestCase):
    """R5-D3: inspect, the bend check and the motion refused the rigged dog with three red X cards in a row."""

    def test_expected_conditions_are_not_failures(self):
        for code, ok in (('experimental_rig', True), ('already_animated', True), ('not_loopcut_rig', True),
                         ('invalid_mode', False), ('invalid_input', False)):
            with patch.object(autorig, 'preview', side_effect=RigError('Nope.', code)), \
                    patch('loopcut.tools._editor_context'):
                result = autorig.tool('preview')
            with self.subTest(code=code):
                self.assertEqual(result.ok, ok)
                self.assertEqual(result.text, 'Nope.')

    def test_inspect_on_a_rigged_character_describes_the_rig(self):
        fake = types.ModuleType('bpy')
        fake.data = types.SimpleNamespace(objects={'Cube.001': types.SimpleNamespace(name='Cube.001', type='MESH')})
        dog = FakeRig('Character Rig', 33, 'experimental', targets=['Cube.001'])
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(autorig, 'motion_price', return_value=''):
            described = autorig.describe_rig(dog)
        self.assertEqual((described['tier'], described['bone_count'], described['meshes']),
                         ('experimental', 33, ['Cube.001']))
        self.assertEqual(described['message'], 'Already rigged by Loopcut (33 bones, not a person rig, so no '
                         'ready-made walks): animate it with keyframes, for free; check_deformation checks the current '
                         'pose. It needs no new rig.')  # R13: plain words.
        robe = FakeRig('Character Rig', 34, 'standard')
        with patch.dict(sys.modules, {'bpy': fake}), \
                patch.object(autorig, 'motion_price', return_value='2 credits a second (10 for 5 s)'), \
                patch.object(autorig, '_channels', return_value=(types.SimpleNamespace(name='Walk'), ['curve'])), \
                patch.object(autorig, '_span', return_value=(1, 120)):
            described = autorig.describe_rig(robe)
        self.assertEqual(described['animated'], 'frames 1-120')
        self.assertIn('ready-made walks and runs work (2 credits a second (10 for 5 s); tell the user first)',
                      described['message'])
        self.assertIn('check_deformation checks the whole animation', described['message'])
        own = FakeRig('Armature', 52, ours=False)
        with patch.dict(sys.modules, {'bpy': fake}), patch.object(motion_retarget, 'rig_of', return_value=object()):
            described = autorig.describe_rig(own)
        self.assertEqual(described['message'], "Already has its own skeleton (52 bones): ready-made motions can target "
                         "it; Loopcut's test poses and bend check need a Loopcut rig. It needs no new rig.")
        with patch.object(autorig, 'rigged', return_value={'rigged': True}), patch('loopcut.tools._editor_context'):
            result = autorig.tool('inspect', targets=['Cube.001'])
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.text), {'rigged': True})


class WeightFact(unittest.TestCase):
    """R5-B2: three weight "fixes" told to the user without a check; the third turned a boot into a surfboard."""

    def changes(self, before, after, objects=None):
        fake = types.ModuleType('bpy')
        fake.data = types.SimpleNamespace(objects=objects or {})
        with patch.dict(sys.modules, {'bpy': fake}):
            return autorig.rig_changes(before, after)

    def test_changed_weights_point_at_the_check(self):
        before = ({'Mesh': ('a', 'Character Rig')}, {'Character Rig': 'walk'})
        # R13: the turn's end runs the check itself (rig_gate); the line says so.
        self.assertEqual(self.changes(before, ({'Mesh': ('b', 'Character Rig')}, {'Character Rig': 'walk'})),
                         'Weights changed on Mesh: Loopcut checks how it bends when your reply ends.')
        still = ({'Mesh': ('a', 'Character Rig')}, {'Character Rig': None})
        self.assertIn('Loopcut checks how it bends when your reply ends',
                      self.changes(still, ({'Mesh': ('b', 'Character Rig')}, {'Character Rig': None}),
                                   {'Character Rig': FakeRig('Character Rig', 34, 'standard')}))
        self.assertIn('pose its bones and run character_rig check_deformation',
                      self.changes(still, ({'Mesh': ('b', 'Character Rig')}, {'Character Rig': None}),
                                   {'Character Rig': FakeRig('Character Rig', 33, 'experimental')}))
        self.assertEqual(self.changes(before, before), '', 'nothing changed, nothing said')

    def test_new_or_changed_keys_point_at_the_animation_check(self):
        before = ({}, {'Character Rig': None})
        self.assertEqual(self.changes(before, ({}, {'Character Rig': 'gallop'})),
                         'Animation changed on Character Rig: Loopcut checks how it moves and bends when your reply '
                         'ends.')  # R13: rig_gate runs it.
        self.assertEqual(self.changes(({}, {'Character Rig': 'gallop'}), ({}, {'Character Rig': None})), '',
                         'animation cleared: nothing to check')

    def test_reading_a_rig_never_fails_the_code(self):
        from loopcut import tools
        with patch.object(autorig, 'rig_state', side_effect=RuntimeError('no scene')):
            self.assertIsNone(tools._rig_state())
        self.assertEqual(tools._rig_changes(None), '')


class SmallHints(unittest.TestCase):
    def test_root_travel_says_how_far_and_how_to_keep_it_in_view(self):
        self.assertEqual(motion_retarget.travel_note(5.5, 'forward'), 'it moves 5.5 m forward from where it starts')  # R14
        self.assertEqual(motion_retarget.travel_note(.4), '', 'a dance that drifts a little says nothing')


if __name__ == '__main__':
    unittest.main()
