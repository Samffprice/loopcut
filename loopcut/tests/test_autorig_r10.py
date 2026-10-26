"""R10 fixes from the ar-artist-1 persona run: named stress poses aimed in armature space, a numeric
deformation check, and a card that says what the rig contains. No Blender: rig_pose and rig_deform are
pure Python, and the add-on's glue is checked against their contracts."""
import json
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'addons_core'))
sys.modules.setdefault('bpy', types.ModuleType('bpy'))

from loopcut import autorig, rig_deform, rig_pose, rig_text, tools  # noqa: E402
from loopcut.rig_pose import add, cross, dot, mul, sub, unit  # noqa: E402

P = rig_pose.PREFIX
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
from autorig_fake import PLATFORM_NOTICE, PLATFORM_NOTICES  # noqa: E402,F401  The platform's lines; tests accept
# them or the card's.
UP, LEFT, FRONT = (0., 0., 1.), (1., 0., 0.), (0., -1., 0.)  # Facing -Y, as Blender's front view.


def skeleton(fingers=True, shoulders=True, roll=0.):
    """An A-pose standard rig the way canonicalization leaves it: +X bends elbows forward and knees back.
    Finger and hand bones get an arbitrary roll (the poses must not depend on it)."""
    joints = {'Hips': (None, (0, 0, 1.0), (0, 0, 1.1)), 'Spine': ('Hips', (0, 0, 1.1), (0, 0, 1.4)),
              'Neck': ('Spine', (0, 0, 1.5), (0, 0, 1.6)), 'Head': ('Neck', (0, 0, 1.6), (0, 0, 1.82))}
    a = math.radians(45)
    for side, k in (('Left', 1), ('Right', -1)):
        start = (k * .04, 0, 1.45)
        if shoulders:
            joints[side + 'Shoulder'] = ('Spine', start, (k * .18, 0, 1.45))
        shoulder = (k * .18, 0, 1.45)
        down = (k * math.cos(a), 0, -math.sin(a))
        elbow, wrist = add(shoulder, mul(down, .28)), add(shoulder, mul(down, .54))
        knuckles = add(wrist, mul(down, .08))
        joints[side + 'Arm'] = (side + 'Shoulder' if shoulders else 'Spine', shoulder, elbow)
        joints[side + 'ForeArm'] = (side + 'Arm', elbow, wrist)
        joints[side + 'Hand'] = (side + 'ForeArm', wrist, knuckles)
        if fingers:
            # Thumb at the front (-Y), pinky at the back, as a relaxed A-pose hand palm-in.
            for f, y in zip(rig_pose.FINGERS, (-.03, -.01, .01, .03)):
                base = add(knuckles, (0, y, 0))
                for n in (1, 2, 3):
                    joints[side + 'Hand' + f + str(n)] = (side + 'Hand' if n == 1 else side + 'Hand' + f + str(n - 1),
                                                         add(base, mul(down, .025 * (n - 1))), add(base, mul(down, .025 * n)))
            thumb = add(wrist, (k * .02, -.035, -.01))
            out = unit(add(down, (0, -.8, 0)))
            for n in (1, 2, 3):
                joints[side + 'HandThumb' + str(n)] = (side + 'Hand' if n == 1 else side + 'HandThumb' + str(n - 1),
                                                       add(thumb, mul(out, .025 * (n - 1))), add(thumb, mul(out, .025 * n)))
        hip, knee, ankle = (k * .1, 0, 1.0), (k * .1, 0, .55), (k * .1, 0, .1)
        joints[side + 'UpLeg'] = ('Hips', hip, knee)
        joints[side + 'Leg'] = (side + 'UpLeg', knee, ankle)
        joints[side + 'Foot'] = (side + 'Leg', ankle, (k * .1, -.12, .02))
    bones = {}
    for role, (parent, head, tail) in joints.items():
        y = unit(sub(tail, head))
        if role.endswith(('Arm', 'ForeArm')):
            z = unit(rig_pose.reject(FRONT, y))  # +X turns Y toward +Z: the forearm comes forward.
        elif role.endswith(('UpLeg', 'Leg')):
            z = unit(rig_pose.reject(mul(FRONT, -1), y))  # The shin goes back.
        else:
            hint = (math.cos(roll), math.sin(roll), .3)
            z = unit(rig_pose.reject(hint, y)) if abs(dot(unit(hint), y)) < .95 else unit(rig_pose.reject(FRONT, y))
        x = cross(y, z)
        rest = [[x[i], y[i], z[i], head[i]] for i in range(3)] + [[0., 0., 0., 1.]]
        bones[P + role] = (P + parent if parent else None, rest, [[float(i == j) for j in range(4)] for i in range(4)],
                           math.dist(head, tail))
    return bones


def posed(bones, turns):
    rig = rig_pose.Rig(bones)
    for name, q in turns.items():
        rig.set(name[len(P):], q)
    return {name: rig.pose(name) for name in bones}


def direction(matrices, role):
    return unit(rig_pose.column(matrices[P + role], 1))


def head(matrices, role):
    return rig_pose.column(matrices[P + role], 3)


class NamedPoses(unittest.TestCase):
    """character_rig preview's stress poses are aimed by the add-on, so a weak model never solves bones."""

    def test_arms_up_puts_both_upper_arms_beside_the_ears(self):
        bones = skeleton()
        turns, notes = rig_pose.solve(bones, 'arms_up')
        m = posed(bones, turns)
        for side, k in (('Left', 1), ('Right', -1)):
            arm = direction(m, side + 'Arm')
            self.assertAlmostEqual(rig_pose.angle(arm, UP), rig_pose.ARMS_UP, places=6)
            self.assertGreater(dot(arm, mul(LEFT, k)), 0, 'out to the side, not across the face')
            self.assertGreater(head(m, side + 'Hand')[2], head(m, 'Head')[2], 'the hands end above the head')
            self.assertAlmostEqual(rig_pose.angle(direction(m, side + 'Shoulder'), unit(sub((k * .18, 0, 1.45), (k * .04, 0, 1.45)))),
                                   rig_pose.SHOULDER_LIFT, places=6)
        self.assertEqual(notes, [])
        self.assertEqual(set(turns), {P + s + r for s in ('Left', 'Right') for r in ('Shoulder', 'Arm')})

    def test_arms_widen_together_until_both_clear_a_big_head(self):
        bones = skeleton()
        for name, (parent, rest, basis, length) in list(bones.items()):
            if name.startswith(P + 'Right') and 'Shoulder' not in name and 'Leg' not in name and 'Foot' not in name:
                rest = [row[:3] + [row[3] + (.08 if i == 0 else 0)] for i, row in enumerate(rest)]  # Nearer the head.
            bones[name] = (parent, rest, basis, .5 if name == P + 'Head' else length)
        turns, notes = rig_pose.solve(bones, 'arms_up')
        m, rig = posed(bones, turns), rig_pose.Rig(bones)
        for name, q in turns.items():
            rig.set(name[len(P):], q)
        left, right = (rig_pose.angle(direction(m, s + 'Arm'), UP) for s in ('Left', 'Right'))
        self.assertAlmostEqual(left, right, places=6)
        self.assertGreater(left, rig_pose.ARMS_UP, 'widened for the head')
        self.assertTrue(all(rig.clearance(s) >= rig_pose.CLEAR for s in ('Left', 'Right')), notes)

    def test_each_pose_measures_what_it_names_on_both_sides_and_one_side_when_asked(self):
        bones = skeleton(roll=1.1)
        expected = {'elbows_bent': ('Arm', 'ForeArm', rig_pose.ELBOW), 'knees_bent': ('UpLeg', 'Leg', rig_pose.KNEE)}
        for pose, (parent, child, degrees) in expected.items():
            m = posed(bones, rig_pose.solve(bones, pose)[0])
            for side in ('Left', 'Right'):
                self.assertAlmostEqual(rig_pose.angle(direction(m, side + parent), direction(m, side + child)), degrees, places=5)
        m = posed(bones, rig_pose.solve(bones, 'elbows_bent')[0])
        self.assertTrue(all(dot(sub(head(m, s + 'Hand'), head(m, s + 'ForeArm')), FRONT) > 0 for s in ('Left', 'Right')))
        m = posed(bones, rig_pose.solve(bones, 'knees_bent')[0])
        self.assertTrue(all(dot(sub(head(m, s + 'Foot'), head(m, s + 'Leg')), FRONT) < 0 for s in ('Left', 'Right')))
        turns, _ = rig_pose.solve(bones, 'stress', 'left')
        self.assertTrue(turns and all(name.startswith(P + 'Left') for name in turns))
        m = posed(bones, turns)
        self.assertLess(rig_pose.angle(direction(m, 'LeftArm'), UP), 15)
        self.assertGreater(rig_pose.angle(direction(m, 'RightArm'), UP), 100, 'the right arm stays down')
        text = rig_pose.measure(m, bones, 'stress', 'left')
        self.assertIn('upper arms from straight up: left 10°', text)
        self.assertIn('elbows bent left 130°', text)
        self.assertIn('knees bent left 90°', text)

    def test_shoulder_raise_lifts_the_shoulders_and_keeps_the_arms_hanging(self):
        bones = skeleton()
        m = posed(bones, rig_pose.solve(bones, 'shoulder_raise')[0])
        for side in ('Left', 'Right'):
            rest_arm = unit(rig_pose.column(bones[P + side + 'Arm'][1], 1))
            self.assertLess(rig_pose.angle(direction(m, side + 'Arm'), rest_arm), 1e-6)
            self.assertGreater(head(m, side + 'Arm')[2], 1.45 + .05, 'the shoulder joint rises')
        with self.assertRaisesRegex(rig_pose.PoseError, 'no shoulder bones'):
            rig_pose.solve(skeleton(shoulders=False), 'shoulder_raise')

    def test_forearm_twist_rolls_about_the_forearm_mirrored(self):
        bones = skeleton()
        turns, _ = rig_pose.solve(bones, 'forearm_twist')
        m = posed(bones, turns)
        self.assertEqual(rig_pose.measure(m, bones, 'forearm_twist'), ['forearms twisted left 90°, right 90°'])
        left, right = turns[P + 'LeftForeArm'], turns[P + 'RightForeArm']
        self.assertAlmostEqual(left[2], -right[2])
        self.assertLess(rig_pose.angle(direction(m, 'LeftForeArm'), unit(rig_pose.column(bones[P + 'LeftForeArm'][1], 1))), 1e-6)

    def test_fists_close_toward_the_palm_whatever_the_bone_roll(self):
        a = math.radians(45)
        for roll in (0., 2.0, 4.0):
            bones = skeleton(roll=roll)
            m = posed(bones, rig_pose.solve(bones, 'fists')[0])
            for side, k in (('Left', 1), ('Right', -1)):
                # Thumb forward, pinky back, hand down and out: this palm faces the body.
                down = (k * math.cos(a), 0, -math.sin(a))
                palm = unit((-k * math.sin(a), 0, -math.cos(a)))
                for f in rig_pose.FINGERS:
                    tip = add(head(m, side + 'Hand' + f + '3'), mul(direction(m, side + 'Hand' + f + '3'), .025))
                    offset = sub(tip, head(m, side + 'Hand' + f + '1'))
                    self.assertGreater(dot(offset, palm), .01, (roll, side, f))
                    self.assertLess(dot(offset, down), 0, 'the tip tucks back toward the wrist')
            self.assertIn('fingers curled left 220°, right 220°', rig_pose.measure(m, bones, 'fists'))
            self.assertIn('fingers curled', ' '.join(rig_pose.measure(posed(bones, rig_pose.solve(bones, 'stress')[0]),
                                                                     bones, 'stress')))
        with self.assertRaisesRegex(rig_pose.PoseError, 'no finger bones'):
            rig_pose.solve(skeleton(fingers=False), 'fists')
        turns, notes = rig_pose.solve(skeleton(fingers=False), 'stress')
        self.assertEqual(notes, ['no finger bones, so no fists'])
        self.assertTrue(turns)

    def test_a_pose_starts_from_the_original_pose_and_gentle_is_unchanged(self):
        bones = skeleton()
        turns, _ = rig_pose.solve(bones, 'gentle')
        q = rig_pose.axis_angle((1., 0., 0.), 25)
        self.assertEqual(turns, {P + s + r: q for s in ('Left', 'Right') for r in ('Leg', 'ForeArm')})
        bent = rig_pose.Rig(bones)
        bent.set('Spine', rig_pose.axis_angle((1., 0., 0.), 30))  # The user's own lean stays; arms still aim up.
        lean = {n: (p, r, bent.basis[n], k) for n, (p, r, _, k) in bones.items()}
        turns, notes = rig_pose.solve(lean, 'arms_up')
        m = posed(lean, {**turns, P + 'Spine': bent.solved[P + 'Spine']})
        arm = direction(m, 'LeftArm')
        self.assertAlmostEqual(dot(arm, FRONT), 0, places=6, msg='aimed in armature space, not along the lean')
        self.assertLessEqual(rig_pose.angle(arm, UP), rig_pose.ARMS_UP_WIDEST)
        clear = rig_pose.Rig(lean)
        for name, q in turns.items():
            clear.set(name[len(P):], q)
        self.assertGreaterEqual(clear.clearance('Left'), rig_pose.CLEAR, 'widened until it clears the leaning head')
        self.assertEqual(notes, [])
        with self.assertRaisesRegex(rig_pose.PoseError, 'Choose a test pose'):
            rig_pose.solve(bones, 'tpose')


class PreviewTool(unittest.TestCase):
    def test_schema_offers_named_poses_sides_and_a_free_check(self):
        params = autorig.SCHEMAS[0]['function']['parameters']['properties']
        self.assertEqual(params['pose']['enum'], list(rig_pose.POSES))
        self.assertEqual(params['side']['enum'], ['both', 'left', 'right'])
        self.assertIn('check_deformation', params['action']['enum'])
        self.assertTrue(tools.only_reads('character_rig', {'action': 'check_deformation'}))
        self.assertFalse(tools.only_reads('character_rig', {'action': 'preview', 'pose': 'stress'}))
        self.assertLess(len(json.dumps(autorig.SCHEMAS)), 1600, 'the model pays for every word')
        self.assertEqual(autorig.describe('character_rig', {'action': 'check_deformation'})[0], 'Check how it bends (free)')

    def test_tool_passes_pose_and_side_and_runs_the_check(self):
        import contextlib
        with patch('loopcut.tools._editor_context', lambda editor: contextlib.nullcontext()), \
                patch.object(autorig, 'preview', return_value='posed') as preview, \
                patch.object(autorig, 'check_deformation', return_value='checked') as check:
            self.assertEqual(autorig.tool('preview', rig='Rig', pose='stress', side='left').text, 'posed')
            preview.assert_called_once_with('Rig', False, 'stress', 'left')
            self.assertEqual(autorig.tool('reset_preview').text, 'posed')
            self.assertEqual(preview.call_args.args, (None, True, 'gentle', 'both'))
            self.assertEqual(autorig.tool('check_deformation', rig='Rig').text, 'checked')
            check.assert_called_once_with('Rig')


def tube(rings=24, segments=16, length=.6, radius=.06):
    """A capped-less tube along Z with outward-wound triangles: (points, edges, tris)."""
    points = [(radius * math.cos(2 * math.pi * s / segments), radius * math.sin(2 * math.pi * s / segments),
               length * r / (rings - 1)) for r in range(rings) for s in range(segments)]
    index = lambda r, s: r * segments + s % segments  # noqa: E731
    edges, tris = [], []
    for r in range(rings):
        for s in range(segments):
            edges.append((index(r, s), index(r, s + 1)))
            if r + 1 < rings:
                edges += [(index(r, s), index(r + 1, s)), (index(r, s), index(r + 1, s + 1))]
                tris += [(index(r, s), index(r, s + 1), index(r + 1, s + 1)), (index(r, s), index(r + 1, s + 1), index(r + 1, s))]
    return points, edges, tris


def bend(points, degrees, joint=.3, blend=.08):
    """A smooth elbow: points above the joint turn about X through it, weight easing in over `blend`."""
    out = []
    for x, y, z in points:
        w = min(1., max(0., (z - joint + blend) / (2 * blend)))
        w = w * w * (3 - 2 * w)
        a = math.radians(degrees) * w
        y0, z0 = y, z - joint
        out.append((x, y0 * math.cos(a) - z0 * math.sin(a), joint + y0 * math.sin(a) + z0 * math.cos(a)))
    return out


class DeformationCheck(unittest.TestCase):
    """A numeric check, so claims about bends come from numbers (ar-artist-1 R3-A3)."""

    def run_check(self, posed_points, points, edges, tris, bent=90, joints=None):
        joints = joints or [((0, 0, .3), (0, 0, .3), .12)]
        names = [{'name': 'left elbow', 'bent': bent}, {'name': 'left wrist', 'bent': 0}][:len(joints)]
        return rig_deform.measure([{'rest': points, 'posed': posed_points, 'edges': edges, 'tris': tris, 'scale': 1.,
                                    'joints': joints}], names)

    @staticmethod
    def pull(points, indices, factor=3.):
        out = list(points)
        for i in indices:
            x, y, z = out[i]
            out[i] = (x * factor, y * factor, z)  # Off the surface, as a stray weight drags a vertex.
        return out

    def test_a_smooth_bend_passes_and_spikes_are_minor_then_a_problem(self):
        points, edges, tris = tube()
        smooth = bend(points, 60)
        (row,) = self.run_check(smooth, points, edges, tris)
        self.assertEqual(rig_deform.problems(row), ([], []), row)
        self.assertIn('left elbow, turned 90° from rest: no spikes or torn edges', '\n'.join(rig_deform.lines([row])))
        (row,) = self.run_check(self.pull(smooth, [12 * 16 + 3]), points, edges, tris)
        self.assertEqual((row['spikes'], row['spiked']), (1, [(0, 12 * 16 + 3)]))
        self.assertTrue(6 < row['spike_cm'] < 13, row['spike_cm'])  # Off its neighbours' mean, not the 12 cm pull.
        self.assertEqual(rig_deform.problems(row)[0], [], 'one stray vertex is minor')
        self.assertIn('minor: 1 stray vertex, up to', '\n'.join(rig_deform.lines([row])))
        (row,) = self.run_check(self.pull(smooth, [12 * 16 + 3, 12 * 16 + 9, 10 * 16 + 6]), points, edges, tris)
        text = '\n'.join(rig_deform.lines([row]))
        self.assertIn('left elbow, turned 90° from rest: PROBLEM: 3 vertices pulled out into spikes, up to', text)
        self.assertIn('problems at left elbow', text)
        self.assertIn('do not call a flagged or untested joint clean', text)

    def test_a_rigid_turn_is_no_spike_however_far_it_moves(self):
        # The fists' finger bones turn rigidly 200°+: their vertices leave their neighbours' mean, stretch nothing.
        points, edges, tris = tube()
        turned = bend(points, 170, blend=.0001)
        (row,) = self.run_check(turned, points, edges, tris)
        self.assertEqual(row['spikes'], 0, row)

    def test_crushed_skin_is_a_problem_and_volume_is_a_number(self):
        points, edges, tris = tube()
        crushed = [(x * .05, y * .05, z) if .25 < z < .35 else (x, y, z) for x, y, z in points]
        (row,) = self.run_check(crushed, points, edges, tris)
        found = ' '.join(rig_deform.problems(row)[0])
        self.assertIn('skin crushed', found)
        self.assertLess(row['volume'], .8)
        self.assertIn('volume', '\n'.join(rig_deform.lines([row])))
        rows = self.run_check(points, points, edges, tris, bent=0)
        self.assertIn('nothing was tested', rig_deform.lines(rows)[0])  # R11: an unposed check tests nothing.

    def test_spikes_in_an_unbent_region_count_at_the_bent_joint_that_moved_them(self):
        points, edges, tris = tube()
        pulled = self.pull(bend(points, 60), [19 * 16 + 2, 19 * 16 + 9, 20 * 16 + 5])  # Around the far joint.
        rows = self.run_check(pulled, points, edges, tris, joints=[((0, 0, .3), (0, 0, .3), .12),
                                                                   ((0, 0, .55), (0, 0, .55), .1)])
        self.assertEqual([r['spikes'] for r in rows], [3, 0])
        self.assertEqual(rig_deform.lines(rows)[-2], 'Not tested, since not turned: left wrist.')

    def test_preview_results_tell_the_model_plain_words(self):
        source = Path(autorig.__file__).read_text()
        start = source.index('def preview(')
        body = source[start:source.index('\ndef _joints(')]
        self.assertNotIn("'reset_preview'", body)
        self.assertIn('tell the user it is the test pose', body)


class RigCard(unittest.TestCase):
    def test_card_says_what_the_rig_contains_in_one_plain_sentence(self):
        import re
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
        from autorig_card_check import check
        text = rig_text.QUOTE
        self.assertIn('with fingers when it can find the hands', text, 'fingers are not on every standard rig (19-bone B2)')
        self.assertIn('made for human shapes', text)
        self.assertIn('may bend in odd places', text)
        for jargon in ('automatic weights', 'humanoid', 'bones', 'weights'):  # R7-D3
            self.assertNotIn(jargon, text)
        self.assertEqual(len(re.findall(r'[.!?](?=\s|$)', text)), 1)
        check([{'id': 'card.price', 'text': '20 credits ($0.20).'}, {'id': 'card.message', 'text': text}], 'card.')


if __name__ == '__main__':
    unittest.main()
