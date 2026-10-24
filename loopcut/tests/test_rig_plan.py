"""Shared planner policy: observed digits, invariant mapping and typed refusals."""
from copy import deepcopy
import json
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/addons_core'))
from loopcut import rig_plan
from test_rig_canonicalize import humanoid


def partial(count, depth=3, clavicles=True):
    joints, vertices, roles = humanoid(clavicles=clavicles)
    fingers = ('Thumb', 'Index', 'Middle', 'Ring', 'Pinky')[:count] if count != 1 else ('Middle',)
    keep = [i for i in range(len(joints)) if not any(
        index == i and 'Hand' in role and not role.endswith('Hand') and
        not any(role.endswith(finger + str(k)) for finger in fingers for k in range(1, depth + 1))
        for role, index in roles.items())]
    remap = {old: new for new, old in enumerate(keep)}
    return ([dict(joints[i], parent=None if joints[i]['parent'] is None else remap[joints[i]['parent']]) for i in keep],
            vertices, {r: remap[i] for r, i in roles.items() if i in remap})


class PartialDigits(unittest.TestCase):
    def test_sideways_bow_legs_bend_backwards_at_full_and_tiny_scale(self):
        for scale in (1., .01, .003, .001):
            joints, vertices, expected = humanoid()
            for side, sign in [('Left', 1), ('Right', -1)]:
                for role, point in [('Leg', (sign * .13, .01, .55)),
                                    ('Foot', (sign * .17, .02, .21)),
                                    ('ToeBase', (sign * .17, -.13, .135))]:
                    joints[expected[side + role]]['head'] = point
            joints = [dict(j, head=rig_plan.mul(j['head'], scale)) for j in joints]
            vertices = [rig_plan.mul(p, scale) for p in vertices]
            report = rig_plan.plan(joints, vertices)
            labels = {row['role']: row for row in report['labels']}
            backward = rig_plan.mul(report['frame']['front'], -1)
            for side in ('Left', 'Right'):
                a, b, c = [joints[expected[side + role]]['head'] for role in ('UpLeg', 'Leg', 'Foot')]
                u, v = rig_plan.sub(b, a), rig_plan.sub(c, b)
                axis = labels[side + 'Leg']['x_axis']
                angle = math.atan2(rig_plan.dot(rig_plan.cross(u, v), axis), rig_plan.dot(u, v))
                turn = math.pi / 2 - angle
                rotated = rig_plan.add(rig_plan.mul(v, math.cos(turn)),
                                       rig_plan.mul(rig_plan.cross(axis, v), math.sin(turn)))
                self.assertAlmostEqual(rig_plan.dot(rig_plan.unit(u), rig_plan.unit(rotated)), 0., places=6)
                self.assertGreater(rig_plan.dot(rig_plan.sub(rotated, v), backward),
                                   rig_plan.length(v) * .5)

    def test_two_three_four_five_digits_and_short_chains_preserve_every_joint(self):
        for count in (2, 3, 4, 5):
            for depth in (1, 2, 3):
                with self.subTest(count=count, depth=depth):
                    joints, vertices, expected = partial(count, depth)
                    original = deepcopy(joints)
                    report = rig_plan.plan(joints, vertices)
                    labels = {r['role']: r['index'] for r in report['labels']}
                    self.assertEqual(joints, original)
                    self.assertEqual(len(labels), len(joints))
                    self.assertEqual(set(labels.values()), set(range(len(joints))))
                    for role, index in expected.items():
                        if not role.startswith('torso'):
                            self.assertEqual(labels[role], index, role)
                    self.assertEqual(report['hands']['Right']['digits'], count)

    def test_single_digit_continuation_has_real_wrist_and_middle_slot(self):
        for clavicles in (True, False):
            joints, vertices, expected = partial(1, clavicles=clavicles)
            report = rig_plan.plan(joints, vertices)
            roles = {r['role']: r['index'] for r in report['labels']}
            self.assertEqual(len(roles), len(joints))
            for side in ('Left', 'Right'):
                self.assertEqual(roles[side + 'Hand'], expected[side + 'Hand'])
                for k in range(1, 4):
                    self.assertEqual(roles[side + 'HandMiddle' + str(k)], expected[side + 'HandMiddle' + str(k)])
                self.assertNotIn(side + 'HandThumb1', roles)
            self.assertTrue(any('no missing bones' in n for n in report['notes']))

    def test_short_single_digit_cannot_be_misread_as_a_clavicle_and_shifted_wrist(self):
        for depth in (1, 2):
            for clavicles in (False, True):
                joints, vertices, _ = partial(1, depth, clavicles)
                with self.assertRaises(rig_plan.CanonicalizationError) as error:
                    rig_plan.plan(joints, vertices)
                self.assertEqual(error.exception.anatomy_reason, 'finger_count')

    def test_farther_thumb_uses_opposition_not_nearest_base(self):
        joints, vertices, expected = partial(2)
        for side, sign in [('Left', 1), ('Right', -1)]:
            hand = joints[expected[side + 'Hand']]['head']
            for k in range(1, 4):
                joints[expected[side + 'HandThumb' + str(k)]]['head'] = (
                    hand[0] + sign * (.03 + (k - 1) * .02), hand[1] - .055 - (k - 1) * .03, hand[2])
                joints[expected[side + 'HandIndex' + str(k)]]['head'] = (
                    hand[0] + sign * (.04 + (k - 1) * .025), hand[1], hand[2])
        report = rig_plan.plan(joints, vertices)
        self.assertEqual({r['role']: r['index'] for r in report['labels']}['RightHandThumb1'], expected['RightHandThumb1'])

    def test_ambiguous_thumb_and_unclear_wrist_refuse(self):
        joints, vertices, roles = partial(2)
        for side, sign in [('Left', 1), ('Right', -1)]:
            hand = joints[roles[side + 'Hand']]['head']
            for finger, offset in [('Thumb', .04), ('Index', .06)]:
                for k in range(1, 4):
                    joints[roles[side + 'Hand' + finger + str(k)]]['head'] = (
                        hand[0] + sign * (offset + k * .02), hand[1], hand[2])
        with self.assertRaises(rig_plan.CanonicalizationError) as error:
            rig_plan.plan(joints, vertices)
        self.assertEqual(error.exception.anatomy_reason, 'finger_count')

        joints, vertices, roles = partial(1)
        joints[roles['LeftHandMiddle1']]['head'] = (3, 0, 1.5)
        with self.assertRaises(rig_plan.CanonicalizationError) as error:
            rig_plan.plan(joints, vertices)
        self.assertEqual(error.exception.anatomy_reason, 'finger_count')

    def test_tied_knuckle_axes_with_different_orders_refuse_in_every_joint_order(self):
        joints, vertices, roles = partial(4)
        for side, sign in [('Left', 1), ('Right', -1)]:
            joints[roles[side + 'Hand']]['head'] = (sign * .74, 0., 1.48)
            for finger, offset in [('Thumb', (.04, -.04, 0)), ('Index', (.10, 0, 0)),
                                   ('Middle', (.12, .01, .005)), ('Ring', (.12, .01, -.005))]:
                for k in range(1, 4):
                    joints[roles[side + 'Hand' + finger + str(k)]]['head'] = (
                        sign * (.74 + offset[0] + (k - 1) * .025), offset[1], 1.48 + offset[2])
        n = len(joints)
        reversed_joints = [dict(j, parent=None if j['parent'] is None else n - 1 - j['parent'])
                           for j in reversed(joints)]
        for value in (joints, reversed_joints):
            with self.assertRaises(rig_plan.CanonicalizationError) as error:
                rig_plan.plan(value, vertices)
            self.assertEqual(error.exception.anatomy_reason, 'finger_count')

    def test_partial_labels_are_invariant_under_order_rotation_translation_and_tiny_scale(self):
        for count in (1, 2, 3, 4):
            joints, vertices, _ = partial(count)
            baseline = rig_plan.plan(joints, vertices)
            count_j = len(joints)
            transform = lambda p: (.01 * -p[1] + 3, .01 * p[2] - 2, .01 * -p[0] + 1)
            changed = [dict(j, name='new' + str(i), parent=None if j['parent'] is None else count_j - 1 - j['parent'],
                            head=transform(j['head'])) for i, j in enumerate(reversed(joints))]
            result = rig_plan.plan(changed, list(map(transform, vertices)))
            self.assertEqual({r['role']: r['index'] for r in result['labels']},
                             {r['role']: count_j - 1 - r['index'] for r in baseline['labels']})


class JsonBoundary(unittest.TestCase):
    def test_json_contract_projection_and_refusal_reasons(self):
        joints, vertices, roles = humanoid()
        data = {'schema_version': 1, 'joints': joints, 'vertices': vertices}
        result = json.loads(json.dumps(rig_plan.plan_json(data), allow_nan=False))
        self.assertEqual(result['planner_version'], rig_plan.PLANNER_VERSION)
        self.assertEqual(result['canonical_plan']['policy'], 'mixamo-tokenrig-v1')
        self.assertEqual(set(result['canonical_plan']['labels'][0]), {'index', 'source', 'role', 'name'})
        cases = [(dict(data, joints=[]), 'too_few_joints'), ({}, 'not_humanoid')]
        broken = deepcopy(data)
        broken['joints'][roles['RightUpLeg']]['parent'] = roles['LeftUpLeg']
        cases.append((broken, 'pelvis_branches'))
        broken = deepcopy(data)
        broken['joints'][roles['RightToeBase']]['head'] = (-.10, .17, .025)
        cases.append((broken, 'front_ambiguous'))
        for value, reason in cases:
            response = rig_plan.plan_json(value)
            self.assertEqual((response['status'], response['code'], response['anatomy_reason']),
                             ('refused', 'unsupported_anatomy', reason))
            self.assertNotIn('canonical_plan', response)

    def test_malformed_input_is_bounded_and_never_raises_untyped_errors(self):
        joints, vertices, roles = humanoid()
        mutations = [lambda d: d.update(schema_version=True), lambda d: d.update(extra=True),
                     lambda d: d.update(joints=[None] * 15), lambda d: d.update(vertices='bad'),
                     lambda d: d['joints'][0].update(name=[]), lambda d: d['joints'][0].update(name='x' * 64),
                     lambda d: d['joints'][0].update(name='bad\ud800'),
                     lambda d: d['joints'][0].update(head=None), lambda d: d['joints'][0].pop('parent'),
                     lambda d: d['joints'][1].update(parent=True),
                     lambda d: d['joints'][roles['LeftForeArm']].update(head=d['joints'][roles['LeftArm']]['head'])]
        for bad in (True, '1', None, float('nan'), float('inf'), 10 ** 1000):
            mutations.append(lambda d, value=bad: d['joints'][0].update(head=[value, 0, 0]))
        for mutate in mutations:
            data = {'schema_version': 1, 'joints': deepcopy(joints), 'vertices': deepcopy(vertices)}
            mutate(data)
            result = rig_plan.plan_json(data)
            self.assertEqual(result['status'], 'refused')
            json.dumps(result, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
