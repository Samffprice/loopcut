"""R11 fixes from the ar-artist-2 persona run (runs3 20261023-231833, the free plan's Fast model): the
built-in poses went unused (R4-A1), an unposed check passed every joint (R4-A2), and the check passed a
torn shoulder after the model's "fix" (R4-A3). No Blender: rig_pose and rig_deform are pure Python."""
import json
import math
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))

from loopcut import autorig, rig_deform, rig_pose  # noqa: E402
from test_autorig_r10 import skeleton, tube  # noqa: E402

P = rig_pose.PREFIX


def row(name, bent, stretch=1.5, squash=.5, volume=.9, spikes=0, bend=None):
    return {'name': name, 'bent': bent, 'bend': bend, 'vertices': 100, 'spikes': spikes, 'spike_cm': 1.,
            'stretch': stretch, 'squash': squash, 'volume': volume}


def quaternion(degrees, axis=(1., 0., 0.)):
    return list(rig_pose.axis_angle(axis, degrees))


class BuiltInPoses(unittest.TestCase):
    """R4-A1: asked for "set test poses", the model hand-wrote one in run_python twice; its elbows reached 13°
    and it reported 120°."""

    def test_the_schema_points_test_poses_at_preview_stress(self):
        description = autorig.SCHEMAS[0]['function']['description']
        self.assertIn('preview pose stress, even for other angles; not run_python', description)
        self.assertLess(len(json.dumps(autorig.SCHEMAS)), 1100, 'the model pays for every word')

    def test_a_pose_made_by_hand_is_told_from_the_test_pose(self):
        bones = skeleton()
        turns, _ = rig_pose.solve(bones, 'stress')
        now = {name: (1., 0., 0., 0.) for name in bones}
        now.update(turns)
        self.assertFalse(rig_pose.by_hand(turns, now), 'the built-in stress pose as preview left it')
        twisted = dict(now, **{P + 'LeftHand': tuple(quaternion(90, (0., 1., 0.)))})
        self.assertTrue(rig_pose.by_hand(turns, twisted), 'a hand twisted on top of it')
        self.assertTrue(rig_pose.by_hand({}, now), 'no test pose was set: posed in run_python')
        self.assertFalse(rig_pose.by_hand({}, {name: (1., 0., 0., 0.) for name in bones}), 'at rest')
        self.assertIn('pose stress', autorig.BY_HAND)
        self.assertIn('by hand', autorig.BY_HAND)

    def test_the_verdict_line_states_the_angles_reached(self):
        rows = [row('left shoulder', 80), row('left elbow', 13, bend=60.2), row('right shoulder', 81),
                row('right elbow', 14, bend=61.4), row('left knee', 90, bend=88.)]
        text = rig_deform.lines(rows, notes=[autorig.BY_HAND])
        self.assertEqual(text[0], 'Deformation check (free; the posed mesh against rest) at shoulders turned left 80°, '
                                  'right 81° from rest; elbows bent left 60°, right 61° from straight; left knee bent 88° '
                                  'from straight: no spikes or torn edges at the turned joints.')
        self.assertIn('- left elbow, bent 60° from straight: no spikes or torn edges', text[2])
        self.assertEqual(text[-2], autorig.BY_HAND, 'the hint sits just above the footer')
        self.assertIn('Report these numbers and angles', text[-1])


class NothingTested(unittest.TestCase):
    """R4-A2: at rest the check said "no spikes or torn skin at the turned joints", and the model told the user
    every joint came back clean; the gentle pose only bends knees and elbows 25°."""

    def test_an_unposed_check_says_it_tested_nothing(self):
        rows = [row(n, 0) for n in ('left shoulder', 'left elbow', 'left knee')]
        self.assertEqual(rig_deform.lines(rows), [rig_deform.NOTHING])
        for words in ('nothing was tested', 'do not call any joint clean', 'preview with pose stress'):
            self.assertIn(words, rig_deform.NOTHING)
        self.assertNotIn('no spikes', rig_deform.NOTHING)
        points, edges, tris = tube()
        measured = rig_deform.measure([{'rest': points, 'posed': points, 'edges': edges, 'tris': tris, 'scale': 1.,
                                        'joints': [((0, 0, .3), (0, 0, .3), .12)]}],
                                      [{'name': 'left elbow', 'bent': 4}])
        self.assertEqual(rig_deform.lines(measured, ['Rest skeleton changed since rigging: …']),
                         ['Rest skeleton changed since rigging: …', rig_deform.NOTHING])

    def test_joints_left_unturned_are_called_untested(self):
        text = rig_deform.lines([row('left elbow', 25, bend=25), row('left shoulder', 0), row('left hip', 0)])
        self.assertIn('Not tested, since not turned: left shoulder, left hip.', text)
        self.assertIn('do not call a flagged or untested joint clean', text[-1])

    def test_the_gentle_pose_says_what_it_bent_and_what_it_did_not_test(self):
        self.assertEqual(autorig.gentle('both'), 'Free test pose: both knees and elbows bent 25°. Shoulders, hips and '
                                                 'wrists were not tested; pose stress bends and checks every joint.')
        self.assertIn('the left knee and elbow bent 25°', autorig.gentle('left'))
        self.assertIn('nor the right side', autorig.gentle('left'))
        source = Path(autorig.__file__).read_text()
        self.assertIn('return gentle(side) + back', source[source.index('def preview('):source.index('\ndef _segment')])


class TornShoulder(unittest.TestCase):
    """R4-A3: after the model moved the shoulder joints 5 cm and smoothed weights, the left shoulder read "no
    spikes or torn skin (stretch 3.77x, squash 0.57x, volume 54%)"; before it was 1.99x and 97%. No local measure
    told that rig from a clean one (r11/fold-calibrate.log), so the regression and the rest joints carry it."""

    POSE = {'left shoulder': {P + 'LeftShoulder': quaternion(0), P + 'LeftArm': quaternion(80)}}

    def check(self, memory, **numbers):
        return rig_deform.remember(memory, [row('left shoulder', 80, **numbers)], self.POSE)

    def test_a_joint_that_got_worse_since_the_last_check_leads_the_result(self):
        lead, memory = self.check([], stretch=1.99, squash=.35, volume=.97)
        self.assertEqual(lead, [], 'the first check of a pose has nothing to compare with')
        lead, memory = self.check(json.loads(json.dumps(memory)), stretch=3.77, squash=.57, volume=.54)
        self.assertEqual(lead, ['Worse since the last check: left shoulder volume 97% → 54%, stretch 1.99x → 3.77x; '
                                'undo the last change.'])
        text = rig_deform.lines([row('left shoulder', 80, stretch=3.77, squash=.57, volume=.54)], lead)
        self.assertTrue(text[0].startswith('Worse since the last check'), text)
        # The second smoothing undid the stretch, not the volume: still worse than the pose's first check.
        lead, memory = self.check(memory, stretch=1.97, squash=.47, volume=.48)
        self.assertEqual(lead, ['Still worse than the first check of this pose: left shoulder volume 97% → 48%.'])
        lead, memory = self.check(memory, stretch=1.99, squash=.35, volume=.97)
        self.assertEqual(lead, ['Better since the last check: left shoulder volume 48% → 97%.'])
        lead, memory = self.check(memory, stretch=1.99, squash=.35, volume=.97)
        self.assertEqual(lead, ['No clear change since the last check of this pose.'])
        lead, memory = self.check(memory, stretch=1.99, squash=.35, volume=1.3)
        self.assertEqual(lead, ['Worse since the last check: left shoulder volume 97% → 130%; undo the last change.'],
                         'bulging is not better')
        lead, _ = self.check(memory, stretch=1.99, squash=.35, volume=.97, spikes=5)
        self.assertEqual(lead, ['Worse since the last check: left shoulder spikes 0 → 5; undo the last change.'])

    def test_only_the_same_joint_in_the_same_pose_is_compared(self):
        _, memory = self.check([], volume=.97)
        moved = {'left shoulder': {P + 'LeftShoulder': quaternion(0), P + 'LeftArm': quaternion(92)}}
        lead, memory = rig_deform.remember(memory, [row('left shoulder', 92, volume=.5)], moved)
        self.assertEqual((lead, len(memory)), ([], 2), 'another pose starts its own record')
        near = {'left shoulder': {P + 'LeftShoulder': quaternion(2), P + 'LeftArm': quaternion(83)}}
        lead, _ = rig_deform.remember(memory, [row('left shoulder', 83, volume=.5)], near)
        self.assertEqual(lead, ['Worse since the last check: left shoulder volume 97% → 50%; undo the last change.'])
        lead, _ = rig_deform.remember(memory, [row('left shoulder', 4, volume=.5)], self.POSE)
        self.assertEqual(lead, [], 'an unturned joint is not compared')
        nearer = {'left shoulder': dict(self.POSE['left shoulder'], **{P + 'Spine2': quaternion(0)})}
        lead, _ = rig_deform.remember(memory, [row('left shoulder', 80, volume=.5)], nearer)
        self.assertEqual(lead, ['Worse since the last check: left shoulder volume 97% → 50%; undo the last change.'],
                         'a moved rest joint brought a resting bone near: the same pose')
        turned = dict(nearer['left shoulder'], **{P + 'Spine2': quaternion(30)})
        self.assertFalse(rig_deform.same_pose(self.POSE['left shoulder'], turned), 'but not when that bone is turned')
        memory = [{'joint': f'j{k}', 'pose': {}, 'first': [1, 1, 1, 0], 'last': [1, 1, 1, 0]} for k in range(45)]
        _, memory = self.check(memory, volume=.97)
        self.assertEqual(len(memory), rig_deform.REMEMBERED)
        self.assertEqual(memory[-1]['joint'], 'left shoulder', 'the newest kept')

    def test_a_clean_verdict_claims_only_what_was_measured(self):
        text = rig_deform.lines([row('left shoulder', 80, stretch=1.97, squash=.47, volume=.48)])
        self.assertIn('- left shoulder, turned 80° from rest: no spikes or torn edges (stretch 1.97x', text[1])
        self.assertNotIn('torn skin', '\n'.join(text))
        self.assertIn('cannot see skin pushed through skin or layers poking out, so look at the joint too', text[-1])

    def test_rest_joints_moved_since_rigging_are_named_in_plain_words(self):
        def bone(head, y=(1., 0., 0.), z=(0., 0., 1.)):
            return [*head, *y, *z]
        saved = {P + 'LeftShoulder': bone((.04, .048, .53)), P + 'LeftArm': bone((.175, .048, .528)),
                 P + 'LeftHand': bone((.45, .048, .3)), P + 'Hips': bone((0., 0., 0.))}
        parents = {P + 'LeftShoulder': None, P + 'LeftArm': P + 'LeftShoulder', P + 'LeftHand': None, P + 'Hips': None}
        self.assertEqual(rig_deform.moved(saved, saved, parents, 1.), '')
        a = math.radians(10)
        now = dict(saved, **{P + 'LeftArm': bone((.175, .048, .578)),
                             P + 'LeftShoulder': bone((.04, .048, .53), (math.cos(.3), 0., math.sin(.3))),
                             P + 'LeftHand': bone((.45, .048, .3), (math.cos(a), math.sin(a), 0.))})
        self.assertEqual(rig_deform.moved(saved, now, parents, 1.),
                         'Rest skeleton changed since rigging: the left upper arm joint moved 5.0 cm, the left hand bone '
                         'turned 10°; the skin was weighted to the old joints, so it can tear there.')
        self.assertIn('moved 1.0 cm', rig_deform.moved(saved, now, parents, .2), 'in metres, at the scene unit scale')
        self.assertIn('the left collarbone turned 17°', rig_deform.moved(saved, now, parents, .05), 'its child stayed')
        self.assertEqual([rig_deform.plain(P + n) for n in ('RightHandIndex2', 'LeftHandThumb1', 'Spine2', 'LeftUpLeg')],
                         ['right index finger', 'left thumb', 'chest', 'left thigh'])

    def test_the_rig_keeps_its_rest_joints_and_check_memory(self):
        source = Path(autorig.__file__).read_text()
        applying = source[source.index('def apply('):source.index('\ndef _selected_rig(')]
        self.assertIn("arm['loopcut_rig_rest'] = json.dumps(_rest(arm))", applying)
        checking = source[source.index('def check_deformation('):source.index('\nSCHEMAS = ')]
        for key in ("'loopcut_rig_rest'", "'loopcut_rig_checks'", "'loopcut_rig_test_pose'", 'rig_deform.remember',
                    'rig_deform.moved', 'BY_HAND'):
            self.assertIn(key, checking)


if __name__ == '__main__':
    unittest.main()
