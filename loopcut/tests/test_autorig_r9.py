"""R9 fixes from the ar-dog-1 persona run: native rigs key in Euler, a plain inspect and result."""
import json
import math
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from test_autorig import boxes, descriptor, valid_sidecars
from loopcut import autorig, cloud, cloud_account, rig_experimental, rig_inspect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'harness'))
from autorig_card_check import check

N = types.SimpleNamespace


class Vec:
    def __init__(self, values):
        self.v = [float(x) for x in values]

    def __sub__(self, other):
        return Vec(a - b for a, b in zip(self.v, other.v))

    def __add__(self, other):
        return Vec(a + b for a, b in zip(self.v, other.v))

    def __mul__(self, k):
        return Vec(a * k for a in self.v)

    @property
    def length(self):
        return math.sqrt(sum(a * a for a in self.v))

    @property
    def z(self):
        return self.v[2]

    def __iter__(self):
        return iter(self.v)

    def normalized(self):
        return self * (1 / self.length)


class Mat:
    def __init__(self, rows):
        self.rows = [list(r) for r in rows]

    def to_3x3(self):
        return N(col=[Vec(self.rows[r][c] for r in range(3)) for c in range(3)])


def fake_blender():
    """Just what autorig._armature touches: leaving Edit Mode builds quaternion pose bones, as Blender does."""
    edit_bones = []
    data = N(edit_bones=N(new=lambda name: edit_bones.append(N(name=name, align_roll=lambda axis: None))
                          or edit_bones[-1]))
    arm = N(name='Character Rig', data=data, mode='OBJECT', pose=N(bones=[]), select_set=lambda value: None)

    def mode_set(mode):
        if mode == 'OBJECT' and arm.mode == 'EDIT':
            arm.pose.bones = [N(name=b.name, rotation_mode='QUATERNION') for b in edit_bones]
        arm.mode = mode
    bpy = N(data=N(armatures=N(new=lambda name: data), objects=N(new=lambda name, d: arm)),
            context=N(object=None, selected_objects=[], view_layer=N(objects=N(active=None))),
            ops=N(object=N(mode_set=mode_set)))
    return bpy, N(Vector=Vec, Matrix=Mat)


class NativeRigsKeyInEuler(unittest.TestCase):
    def build(self, tier):
        rig = valid_sidecars()[0]
        rig['tier'] = tier
        bpy, mathutils = fake_blender()
        with patch.dict(sys.modules, {'bpy': bpy, 'mathutils': mathutils}):
            return autorig._armature(rig, N(objects=N(link=lambda o: None)), 'back')

    def test_experimental_bones_rotate_in_xyz_and_standard_bones_keep_quaternions(self):
        # ar-dog-1: rotation_euler keys on the native rig's default quaternion bones did nothing.
        native = self.build('experimental')
        self.assertEqual({b.rotation_mode for b in native.pose.bones}, {'XYZ'})
        self.assertEqual(len(native.pose.bones), 15)
        self.assertEqual(native.matrix_world, 'back')
        # Motion tools retarget onto standard rigs, which keep Blender's default.
        self.assertEqual({b.rotation_mode for b in self.build('standard').pose.bones}, {'QUATERNION'})

    def test_the_model_is_told_the_rotation_mode(self):
        joints = [{'name': 'bone_0', 'parent': None}, {'name': 'bone_1', 'parent': 0}]
        self.assertEqual(rig_experimental.describe(joints, 'Character Rig')['rotation_mode'], 'XYZ')


def dog():
    """A standing four-legged body, long along Y: no arms, so no T-pose or A-pose to find."""
    return boxes([((-.1, -.3, .25), (.1, .3, .45)), ((-.07, .3, .4), (.07, .45, .55)),
                  ((-.1, -.3, 0), (-.04, -.24, .25)), ((.04, -.3, 0), (.1, -.24, .25)),
                  ((-.1, .24, 0), (-.04, .3, .25)), ((.04, .24, 0), (.1, .3, .25))])


class InspectSaysWhatIsHandled(unittest.TestCase):
    def inspect(self):
        points, triangles = dog()
        # The dog was uploaded Y-up at 100x scale: the object's local up is world +Y (old: "lying_down": true).
        back = Mat([[1, 0, 0, 0], [0, 0, 1, 0], [0, 1, 0, 0], [0, 0, 0, 1]])
        mesh = N(name='Cube.001', scale=(100.0, 100.0, 100.0), rotation_euler=(0.0, 0.0, 0.0))
        with patch.object(autorig, 'targets', return_value=[mesh]), \
                patch.object(autorig, 'geometry', return_value=([('mesh_0', points, triangles)], points, triangles, back)):
            return autorig.inspect(['Cube.001'])[3]

    def test_a_standing_dog_is_not_reported_lying_down_and_its_pose_is_no_question(self):
        report = self.inspect()
        self.assertFalse({'lying_down', 'world_up'} & set(report), 'the upload orientation is handled')
        self.assertEqual(report['pose'], 'other')
        self.assertIn('do not ask the user about the pose', report['pose_handling'])
        self.assertNotIn('uncertain', json.dumps(report))

    def test_the_message_says_size_and_rotation_need_no_fixing_first(self):
        report = self.inspect()
        self.assertTrue(report['message'].startswith(
            'Ready for a rigging quote: 72 triangles. Checking was free. Size and rotation are handled, so nothing '
            'needs resizing or rotating first.'), report['message'])
        # What the quote card and the upload read stays.
        for field in ('eligible', 'warnings', 'shape_hints', 'height_metres', 'transforms',
                      'orientation_handling', 'transform_handling'):
            self.assertIn(field, report)
        self.assertEqual(rig_inspect.geometry(*dog())['message'], report['message'])


class PlainRigResult(unittest.TestCase):
    JARGON = ('experimental', 'humanoid', 'armature', 'agent', 'motion tool', 'bone_', 'skeleton', 'rig ')

    def test_result_and_motion_refusal_are_plain_words_within_the_card_budget(self):
        for text in (rig_experimental.result_message(29), rig_experimental.MOTION_REFUSAL):
            with self.subTest(text=text):
                self.assertFalse([w for w in self.JARGON if w in text.lower()], text)
                check([{'id': 'card.output', 'text': text}], 'card.')
        self.assertEqual(rig_experimental.result_message(29),
                         'Added 29 bones so your character can bend. Loopcut can animate it with keyframes; '
                         'ready-made motions need a person-shaped character.')

    def test_the_model_keeps_bone_names_for_code_and_the_user_gets_plain_words(self):
        from loopcut import scene_diff
        def apply(descriptor, job, *, details):
            details['rig'] = rig_experimental.describe([{'name': 'bone_0', 'parent': None},
                                                        {'name': 'bone_1', 'parent': 0}], 'Character Rig')
            return rig_experimental.result_message(2)
        with patch.object(scene_diff, 'snapshot', return_value={}), patch.object(cloud, '_runtime', None), \
                patch.object(cloud_account, 'receipt', return_value='20 credits'):
            result = cloud._apply(types.SimpleNamespace(apply_result=apply), descriptor(), {})
        head, data = result.model_note.split('\n', 1)
        self.assertIn('Use bone names only inside your code; tell the user what changed in plain words', head)
        self.assertEqual(json.loads(data)['bone_chains'], [['bone_0', 'bone_1']])
        self.assertNotIn('bone_', result.text)


if __name__ == '__main__':
    unittest.main()
