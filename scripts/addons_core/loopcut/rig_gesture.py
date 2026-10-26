"""Built-in arm gestures for standard rigs, layered over an animation; pure Python, so tests need no Blender.

R6-B3 (ar-beginner-3): a hand-keyed wave on top of a text-to-motion walk took 6+ minutes of failing code. The walk
keys every frame, and the model fought Blender 5's layered actions ("no action.fcurves", "Keyframe not in F-Curve")
and rotation axes (it searched for "rotations that truly raise the arm"); the result was barely visible.

autorig.gesture reads, per frame of the gesture's range, every bone's basis from the action's curves (the walk) and
hands them here as rig_pose bones; pose() aims the arm in the body's frame at that moment, the way rig_pose aims test
poses in armature space: the chest's turn since rest carries the body's up, left and front, so a wave stays beside
the head while the walk turns and sways the chest. blend() eases the gesture in and out over the walk's own
rotations, so the walk is unchanged at both ends of the range and everywhere outside it.

R7-B1 (ar-beginner-4): "can he wave too? like stop and wave at the end" bought a 6-credit motion_from_text ("A man
stands still and waves with his right hand."), which replaced the walk. asked() reads such a prompt as one of these
gestures, so autorig.free_gesture can offer the free one over the walk instead; a whole-body action stays a motion.
"""
import math
import re

from . import rig_pose
from .rig_pose import add, apply3, mul, rotation, unit

GESTURES = ('wave', 'raise_arm', 'point')
SIDES = ('right', 'left')
WHAT = {'wave': 'waves', 'raise_arm': 'raises the arm', 'point': 'points forward'}
SECONDS = 2.0         # The default length.
SHORTEST = 12         # Frames.
EASE_SECONDS = .33    # Into the gesture and back to the walk, each at most a quarter of the range.
WAVE_PERIOD = .6      # Seconds per side-to-side swing of the forearm,
WAVE_SWING = 22       # degrees each way about its middle,
WAVE_MIDDLE = 12      # which leans this far out from straight up.
WAVE_ARM = 35         # The upper arm, this far above level, out to the side,
WAVE_FORWARD = .3     # and this far forward (a share of the side direction).
RAISE_OUT = 12        # Raised arm: this far out from straight up.
POINT_OUT = 15        # Pointing arm: this far out from straight ahead, at shoulder height.
CHAIN = ('Arm', 'ForeArm', 'Hand')


def body(rig, chest):
    """(up, out-of-side axes at rest, the chest's rotation since rest): up and left from rest heads, turned with the
    chest bone (the shoulder's parent) the way it is posed now."""
    up, left, front = rig_pose.frame(rig)
    posed = rotation(rig.pose(chest))
    rest = rotation(rig.bones[chest][1])
    turn = [[sum(posed[i][k] * rest[j][k] for k in range(3)) for j in range(3)] for i in range(3)]  # posed @ rest^T
    return apply3(turn, up), apply3(turn, left), apply3(turn, front)


def _tilt(a, b, degrees):
    r = math.radians(degrees)
    return unit(add(mul(a, math.cos(r)), mul(b, math.sin(r))))


def pose(bones, gesture, side, t, fps):
    """{bone name: (w, x, y, z)} for the side's arm chain at time t seconds into the gesture. bones: rig_pose's,
    with the bases of this frame (the walk's pose); the shoulder keeps its own."""
    if gesture not in GESTURES:
        raise rig_pose.PoseError('Choose a gesture: ' + ', '.join(GESTURES) + '.')
    if side not in SIDES:
        raise rig_pose.PoseError('Choose the right or left arm.')
    rig = rig_pose.Rig(bones)
    s = side.capitalize()
    required = ['Hips', 'Neck', 'Head', 'LeftArm', 'RightArm'] + [s + r for r in CHAIN]
    if not all(rig.has(r) for r in required):
        raise rig_pose.PoseError('This rig is missing the arm bones a gesture needs.')
    chest = rig.bones[rig_pose.PREFIX + s + ('Shoulder' if rig.has(s + 'Shoulder') else 'Arm')][0]
    if chest is None:
        raise rig_pose.PoseError('This rig is missing the arm bones a gesture needs.')
    up, left, front = body(rig, chest)
    out = left if side == 'left' else mul(left, -1.)
    if gesture == 'wave':
        arm = unit(add(_tilt(out, up, WAVE_ARM), mul(front, WAVE_FORWARD)))
        swing = WAVE_MIDDLE + WAVE_SWING * math.sin(2 * math.pi * t / WAVE_PERIOD)
        fore = _tilt(up, out, swing)
    elif gesture == 'raise_arm':
        arm = fore = _tilt(up, out, RAISE_OUT)
    else:
        arm = fore = _tilt(front, out, POINT_OUT)
    rig.aim(s + 'Arm', arm)
    rig.aim(s + 'ForeArm', fore)
    rig.set(s + 'Hand', (1., 0., 0., 0.))
    return dict(rig.solved)


def weight(frame, first, last, fps):
    """0 at both ends of the range, 1 between the eases (smoothstep)."""
    ease = max(1., min(fps * EASE_SECONDS, (last - first) / 4))
    x = min(frame - first, last - frame) / ease
    x = max(0., min(1., x))
    return x * x * (3 - 2 * x)


def slerp(p, q, t):
    """(w, x, y, z) from p toward q, the short way."""
    d = sum(a * b for a, b in zip(p, q))
    if d < 0:
        q, d = tuple(-x for x in q), -d
    if d > .9995:
        out = tuple(a + (b - a) * t for a, b in zip(p, q))
    else:
        theta = math.acos(min(1., d))
        s = math.sin(theta)
        out = tuple((math.sin((1 - t) * theta) * a + math.sin(t * theta) * b) / s for a, b in zip(p, q))
    n = math.sqrt(sum(x * x for x in out))
    return tuple(x / n for x in out)


def blend(walk, gesture, w):
    """Each bone's rotation from the walk's toward the gesture's by w; bones only the walk turns are left out."""
    return {name: slerp(walk.get(name, (1., 0., 0., 0.)), q, w) for name, q in gesture.items()}


def frames(first, last, playback, fps, has_action):
    """The gesture's (first, last): as asked, else the last SECONDS of the playback range of an animated rig (the
    user's "wave at the end"), else SECONDS from the start of the playback range."""
    length = max(SHORTEST, round(fps * SECONDS))
    if first is None:
        first = playback[1] - length if has_action else playback[0]
        first = max(playback[0], first)
    if last is None:
        last = first + length
    if last - first < SHORTEST:
        raise rig_pose.PoseError(f'A gesture needs at least {SHORTEST} frames.')
    return int(first), int(last)



_WORDS = re.compile(r"[a-z]+")
_WAVE = frozenset(('wave', 'waves', 'waved', 'waving'))
_POINT = frozenset(('point', 'points', 'pointed', 'pointing'))
_RAISE = frozenset(('raise', 'raises', 'raised', 'raising', 'lift', 'lifts', 'lifted', 'lifting'))
_LIMB = frozenset(('arm', 'arms', 'hand', 'hands'))
# The body moves, not only an arm: a motion, even with a wave in it ("jumps and waves", "walks over and points").
_BODY = frozenset([w + end for w in (
    'walk', 'run', 'jog', 'sprint', 'jump', 'hop', 'leap', 'danc', 'kick', 'punch', 'squat', 'crouch', 'kneel', 'sit',
    'fall', 'spin', 'turn', 'roll', 'flip', 'crawl', 'climb', 'swim', 'bow', 'stretch', 'throw', 'clap', 'push', 'pull',
    'lung', 'skip', 'stomp', 'march', 'step', 'sneak', 'limp', 'fight', 'box', 'pick', 'lie', 'lay', 'shrug', 'nod',
    'shak', 'salut', 'cartwheel', 'backflip', 'somersault')
    for end in ('', 's', 'es', 'e', 'ed', 'ing', 'ning', 'ping', 'ting', 'ped', 'ding')]
    + ['ran', 'fell', 'sat', 'threw', 'leapt', 'swam', 'lying'])


def asked(prompt):
    """(gesture, side or None) when a motion prompt asks only for a wave, a point or a raised arm or hand; None when
    it names anything else the body does, or no gesture."""
    words = set(_WORDS.findall(prompt.lower())) if isinstance(prompt, str) else set()
    if words & _BODY:
        return None
    if words & _WAVE:
        kind = 'wave'
    elif words & _POINT:
        kind = 'point'
    elif words & _LIMB and (words & _RAISE or 'up' in words):
        kind = 'raise_arm'
    else:
        return None
    sides = [s for s in SIDES if s in words]
    return kind, sides[0] if len(sides) == 1 else None
