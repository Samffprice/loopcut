"""Experimental-rig disclosure and hierarchy information; no anatomy inference."""

TIER_PROPERTY = 'loopcut_rig_tier'
from .rig_text import QUOTE

MOTION_REFUSAL = 'Ready-made motions need a person-shaped character, so ask Loopcut to animate this one instead.'
MOTION_NOTE = ('No credits used. This rig cannot use ready-made motions: animate it with keyframes instead, for free. '
               'Loopcut checks how it moves and bends when your reply ends.')
REASONS = frozenset(('pelvis_branches', 'too_few_joints', 'not_humanoid', 'front_ambiguous', 'finger_count'))


def marked(armature):
    return armature is not None and armature.get(TIER_PROPERTY) == 'experimental'


def result_message(bone_count):
    # The user's card, so plain words (ar-dog-1: the model echoed "standard humanoid rigs" to a
    # beginner); bone names and chains go to the model in its separate note.
    return (f'Added {bone_count} bones so your character can bend. Loopcut can animate it with keyframes; '
            'ready-made motions need a person-shaped character.')


def euler_pose(armature):
    """Every pose bone rotates by XYZ Euler, so ordinary keyframe code works: ar-dog-1 keyed
    rotation_euler on a native rig's default quaternion bones and nothing moved. Nothing in the
    native path reads quaternions; standard rigs keep theirs for the motion tools."""
    for bone in armature.pose.bones:
        bone.rotation_mode = 'XYZ'


def describe(joints, armature):
    """Partition the validated parent-first hierarchy into unambiguous chains.

    Each bone appears once. A chain ends at a fork; its children start new
    chains. Native names are data, never guessed limb roles or executable code.
    """
    children = [[] for _ in joints]
    root = next(i for i, joint in enumerate(joints) if joint['parent'] is None)
    for i, joint in enumerate(joints):
        if joint['parent'] is not None:
            children[joint['parent']].append(i)
    starts = [root] + [i for i, joint in enumerate(joints)
                       if joint['parent'] is not None and len(children[joint['parent']]) != 1]
    chains = []
    for i in starts:
        chain = [joints[i]['name']]
        while len(children[i]) == 1:
            i = children[i][0]
            chain.append(joints[i]['name'])
        chains.append(chain)
    return {'tier': 'experimental', 'armature': armature, 'bone_count': len(joints),
            'root': joints[root]['name'], 'bone_chains': chains, 'rotation_mode': 'XYZ',
            'motion_tools_supported': False,
            # ar-dog-3: the bones left in view read as "still broken", and the gallop was called verified unchecked.
            'next_steps': ['Keyframe bones with ordinary Blender tools for a simple cycle, such as a four-leg walk.',
                           'Loopcut checks how it moves and bends when your reply ends.',
                           f'Then run character_rig show: it hides {armature} (its bones draw over the mesh) and sets '
                           "up the user's view to watch it.",
                           'Rename bones, fix weights or delete stray bones only if asked.']}
