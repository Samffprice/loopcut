"""Topology labelling, ambiguous anatomy and coordinate-frame tests without bpy."""
from copy import deepcopy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut.rig_canonicalize import CanonicalizationError, dot, plan, sub


def humanoid(fingers=True, toes=True, clavicles=True, spine_count=3):
    joints, expected = [], {}
    def joint(role, parent, xyz):
        i = len(joints)
        joints.append({'name': f'opaque-{i * 17 + 11}', 'parent': expected.get(parent), 'head': xyz})
        expected[role] = i
        return role
    joint('Hips', None, (0., 0., 1.))
    parent = 'Hips'
    for k in range(spine_count):
        parent = joint('torso' + str(k), parent, (0., 0., 1.12 + k * .33 / max(1, spine_count - 1)))
    chest = parent
    joint('Neck', chest, (0., 0., 1.60))
    joint('Head', 'Neck', (0., 0., 1.72))
    for side, sign in [('Left', 1), ('Right', -1)]:
        p = chest
        if clavicles:
            p = joint(side + 'Shoulder', p, (sign * .06, 0., 1.53))
        p = joint(side + 'Arm', p, (sign * .2, 0., 1.5))
        p = joint(side + 'ForeArm', p, (sign * .48, 0., 1.48))
        hand = joint(side + 'Hand', p, (sign * .74, -.01, 1.47))
        if fingers:
            for f, y in [('Thumb', -.06), ('Index', -.045), ('Middle', -.01), ('Ring', .025), ('Pinky', .06)]:
                p = hand
                for k in range(1, 4):
                    x = (.77 if f == 'Thumb' else .84) + (k - 1) * .025
                    p = joint(side + 'Hand' + f + str(k), p, (sign * x, y, 1.45 if f == 'Thumb' else 1.47))
        p = joint(side + 'UpLeg', 'Hips', (sign * .10, 0., .95))
        p = joint(side + 'Leg', p, (sign * .10, -.01, .52))
        p = joint(side + 'Foot', p, (sign * .10, .02, .10))
        if toes:
            joint(side + 'ToeBase', p, (sign * .10, -.13, .025))
    vertices = [j['head'] for j in joints] + [(0., 0., 1.95)]
    for sign in (-1, 1):
        vertices += [(sign * .10 + dx, y, z) for dx in (-.04, 0., .04)
                     for y in (-.17, -.12, -.06, 0., .055) for z in (.02, .09)]
    return joints, vertices, expected


class CanonicalLabels(unittest.TestCase):
    def test_outward_toes_use_bilateral_body_forward_evidence(self):
        for degrees in (24, 29, 45):
            with self.subTest(degrees=degrees):
                joints, vertices, expected = humanoid()
                for side, sign in [('Left', 1), ('Right', -1)]:
                    ankle = joints[expected[side + 'Foot']]['head']
                    toe = (ankle[0] + sign * .15 * math.sin(math.radians(degrees)),
                           ankle[1] - .15 * math.cos(math.radians(degrees)), .025)
                    joints[expected[side + 'ToeBase']]['head'] = toe
                result = plan(joints, vertices)
                self.assertGreater(dot(result['frame']['front'], (0, -1, 0)), .999)
                self.assertGreater(result['confidence_min'], .7)
                self.assertEqual(len(result['labels']), len(joints))

    def test_opposed_sideways_and_asymmetric_arm_toe_evidence_refuses(self):
        for problem in ('opposed', 'sideways', 'asymmetric_arms'):
            with self.subTest(problem=problem):
                joints, vertices, expected = humanoid()
                for side, sign in [('Left', 1), ('Right', -1)]:
                    ankle = joints[expected[side + 'Foot']]['head']
                    dx, dy = (sign * .15, -.01) if problem == 'sideways' else (0, -.15)
                    if problem == 'opposed' and side == 'Right':
                        dy *= -1
                    joints[expected[side + 'ToeBase']]['head'] = (ankle[0] + dx, ankle[1] + dy, .025)
                if problem == 'asymmetric_arms':
                    # Forearm positions imply a sagittal direction inconsistent
                    # with the feet; don't force a front orientation from it.
                    for side, y in [('Left', -1), ('Right', 1)]:
                        i = expected[side + 'ForeArm']
                        joints[i]['head'] = (0., y, 1.48)
                with self.assertRaises(CanonicalizationError) as caught:
                    plan(joints, vertices)
                self.assertEqual(caught.exception.code, 'ambiguous_front')

    def test_single_toe_is_explicit_and_degenerate_toes_refuse(self):
        joints, vertices, expected = humanoid()
        self.assertEqual(expected['RightToeBase'], len(joints) - 1)
        result = plan(joints[:-1], vertices)
        self.assertIn('single toe projection', result['frame']['front_source'])
        for head in (joints[expected['LeftFoot']]['head'], (.10, .02, .3)):
            broken = deepcopy(joints)
            broken[expected['LeftToeBase']]['head'] = head
            with self.assertRaises(CanonicalizationError):
                plan(broken, vertices)

    def test_toe_sagittal_projection_boundary(self):
        # A 60-degree toe is the explicit ambiguity boundary; floating point
        # observations on either side must not inherit the old pairwise cutoff.
        for angle, accepted in ((59.999, True), (60.001, False)):
            joints, vertices, expected = humanoid()
            for side, sign in [('Left', 1), ('Right', -1)]:
                ankle = joints[expected[side + 'Foot']]['head']
                joints[expected[side + 'ToeBase']]['head'] = (ankle[0] + sign * .15 * math.sin(math.radians(angle)),
                    ankle[1] - .15 * math.cos(math.radians(angle)), ankle[2])
            if accepted:
                self.assertEqual(len(plan(joints, vertices)['labels']), len(joints))
            else:
                with self.assertRaises(CanonicalizationError):
                    plan(joints, vertices)

    def test_full_skeleton_ignores_names_order_and_world_axes(self):
        joints, vertices, expected = humanoid()
        # Rotate and translate, then reverse input ordering and rewrite parent IDs.
        transform = lambda p: (-p[1] + 3, p[0] - 2, p[2] + 4)
        n = len(joints)
        shuffled = [dict(j, name='arbitrary-' + str(i), head=transform(j['head']),
                        parent=None if j['parent'] is None else n - 1 - j['parent'])
                    for i, j in enumerate(reversed(joints))]
        result = plan(shuffled, [transform(v) for v in vertices])
        by = {row['role']: row for row in result['labels']}
        self.assertEqual(len(by), 52)
        for role, original in expected.items():
            if not role.startswith('torso'):
                self.assertEqual(by[role]['index'], n - 1 - original, role)
        self.assertEqual(by['LeftHand']['orientation'], 'hand to middle-finger base')
        self.assertGreater(dot(sub(by['LeftFoot']['tail'], shuffled[by['LeftFoot']['index']]['head']),
                               result['frame']['front']), 0)
        self.assertAlmostEqual(dot(sub(by['LeftFoot']['tail'], shuffled[by['LeftFoot']['index']]['head']),
                                   result['frame']['up']), 0)

    def test_reduced_no_fingers_toes_or_clavicles(self):
        joints, vertices, _ = humanoid(fingers=False, toes=False, clavicles=False, spine_count=2)
        result = plan(joints, vertices)
        roles = {row['role'] for row in result['labels']}
        self.assertEqual(len(roles), len(joints))
        self.assertNotIn('LeftShoulder', roles)
        self.assertNotIn('LeftToeBase', roles)
        self.assertNotIn('LeftHandThumb1', roles)
        self.assertEqual(result['frame']['front_source'], 'bilateral foot mesh extent (no toe joints)')
        self.assertIn('Spine2', roles)

    def test_refuses_tail_branch_and_cycles(self):
        joints, vertices, _ = humanoid()
        tail = deepcopy(joints)
        tail.append({'name': 'tail', 'parent': 0, 'head': (0., .3, 1.)})
        with self.assertRaisesRegex(CanonicalizationError, 'found 4 children'):
            plan(tail, vertices)
        joints[1]['parent'] = 2
        with self.assertRaisesRegex(CanonicalizationError, 'disconnected or cyclic'):
            plan(joints, vertices)

    def test_extra_spine_joints_are_retained_and_documented(self):
        joints, vertices, _ = humanoid(spine_count=5)
        result = plan(joints, vertices)
        roles = {row['role'] for row in result['labels']}
        self.assertEqual(len(roles), len(joints))
        self.assertTrue({'Spine', 'Spine1', 'Spine2', 'SpineExtra01', 'SpineExtra03'} <= roles)
        self.assertTrue(any('inherit' in note for note in result['notes']))

    def test_refuses_ambiguous_front_without_toes(self):
        joints, vertices, _ = humanoid(fingers=False, toes=False, spine_count=2)
        # Symmetric shoes around each ankle cannot establish front reliably.
        vertices = [p for p in vertices if p[2] > .3]
        vertices += [(s * .10, y, .05) for s in (-1, 1) for y in (-.08, .12)]
        with self.assertRaisesRegex(CanonicalizationError, 'ambiguous front/back'):
            plan(joints, vertices)


if __name__ == '__main__':
    unittest.main()
