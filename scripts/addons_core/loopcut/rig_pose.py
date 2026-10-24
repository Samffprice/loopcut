"""Named test poses for standard rigs, aimed in armature space; pure Python, so tests need no Blender.

autorig.preview reads the rig into `bones` ({name: (parent, rest 4x4 rows, basis 4x4 rows, length)},
armature space), applies solve()'s quaternions as rotation_quaternion, and reports measure() on the result.
Hips, Neck, Head, the arms (Arm, ForeArm, Hand) and legs (UpLeg, Leg) are required; shoulder and finger
bones are used when the rig has them (19-bone standard rigs have no fingers, 22-bone ones may lack
shoulders, and some hands have one joint per finger).
"""
import math

PREFIX = 'mixamorig:'
POSES = ('gentle', 'arms_up', 'shoulder_raise', 'elbows_bent', 'knees_bent', 'forearm_twist', 'fists', 'stress')
SIDES = ('both', 'left', 'right')
GENTLE = 25         # Degrees on knees and elbows, the original test pose.
ARMS_UP = 10        # Upper arms this far out from straight up: beside the ears...
ARMS_UP_WIDEST = 40  # ...or wider, up to this, until the arm clears the head (a big-headed character's would not).
CLEAR = 1.15        # Head-sphere radii the arm chain keeps from the head's centre.
SHOULDER_LIFT = 15  # The shoulder bones' lift that goes with arms up.
SHRUG = 30
ELBOW = 130         # Bend from straight.
KNEE = 90
TWIST = 90          # Forearm roll about its own length, mirrored between sides.
CURL = (70, 90, 60)   # Finger joints, base to tip.
THUMB = (25, 35, 35)
FINGERS = ('Index', 'Middle', 'Ring', 'Pinky')
STEPS = {'stress': ('arms_up', 'elbows_bent', 'knees_bent', 'fists')}
WHAT = {'gentle': 'knees and elbows bent a little', 'arms_up': 'arms up beside the ears',
        'shoulder_raise': 'shoulders raised, arms hanging', 'elbows_bent': f'elbows bent {ELBOW}°',
        'knees_bent': f'knees bent {KNEE}°', 'forearm_twist': f'forearms twisted {TWIST}°',
        'fists': 'hands closed into fists',
        'stress': f'arms up beside the ears, elbows bent {ELBOW}°, knees bent {KNEE}°, hands in fists'}


class PoseError(ValueError):
    pass


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def mul(a, k):
    return tuple(x * k for x in a)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def unit(a):
    n = math.sqrt(dot(a, a))
    if n < 1e-12:
        raise PoseError('degenerate direction')
    return mul(a, 1 / n)


def reject(a, axis):
    return sub(a, mul(axis, dot(a, axis)))


def angle(a, b):
    return math.degrees(math.acos(max(-1., min(1., dot(unit(a), unit(b))))))


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def rigid_inverse(m):
    r = [[m[j][i] for j in range(3)] for i in range(3)]
    t = [-sum(r[i][k] * m[k][3] for k in range(3)) for i in range(3)]
    return [r[0] + [t[0]], r[1] + [t[1]], r[2] + [t[2]], [0., 0., 0., 1.]]


def column(m, j):
    return (m[0][j], m[1][j], m[2][j])


def rotation(m):
    """The orthonormal 3x3 rows of a 4x4's rotation; a scaled parent keeps its directions."""
    x, y = unit(column(m, 0)), column(m, 1)
    y = unit(reject(y, x))
    z = cross(x, y)
    return [[x[i], y[i], z[i]] for i in range(3)]


def apply3(r, v):
    return tuple(dot(row, v) for row in r)


def transpose3(r):
    return [[r[j][i] for j in range(3)] for i in range(3)]


def axis_angle(axis, degrees):
    """(w, x, y, z)."""
    half = math.radians(degrees) / 2
    return (math.cos(half),) + mul(unit(axis), math.sin(half))


def between(a, b):
    """The shortest rotation taking direction a to b, as (w, x, y, z)."""
    a, b = unit(a), unit(b)
    c = dot(a, b)
    if c < -1 + 1e-9:
        other = (1., 0., 0.) if abs(a[0]) < .9 else (0., 0., 1.)
        return (0.,) + unit(cross(a, other))
    w = math.sqrt((1 + c) * 2)
    return (w / 2,) + mul(cross(a, b), 1 / w)


def quat_matrix(q, translation=(0., 0., 0.), scale=(1., 1., 1.)):
    w, x, y, z = q
    r = [[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
         [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
         [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]]
    return [[r[i][0] * scale[0], r[i][1] * scale[1], r[i][2] * scale[2], translation[i]] for i in range(3)] \
        + [[0., 0., 0., 1.]]


def _segment_distance(c, p, q):
    d = sub(q, p)
    t = max(0., min(1., dot(sub(c, p), d) / dot(d, d))) if dot(d, d) else 0.
    return math.dist(c, add(p, mul(d, t)))


class Rig:
    """FK over armature-space rest matrices, with the bases set so far."""

    def __init__(self, bones):
        self.bones = bones
        self.basis = {name: [list(r) for r in row[2]] for name, row in bones.items()}
        self.solved = {}

    def has(self, role):
        return PREFIX + role in self.bones

    def rest(self, role):
        return self.bones[PREFIX + role][1]

    def head(self, role):
        return column(self.rest(role), 3)

    def length(self, role):
        return self.bones[PREFIX + role][3]

    def resting(self, role):
        return unit(column(self.rest(role), 1))

    def neutral(self, name):
        """The bone's armature-space matrix with an identity basis, under its parents' poses."""
        parent, rest = self.bones[name][:2]
        return rest if parent is None else matmul(matmul(self.pose(parent), rigid_inverse(self.bones[parent][1])), rest)

    def pose(self, name):
        return matmul(self.neutral(name), self.basis[name])

    def direction(self, role):
        return unit(column(self.pose(PREFIX + role), 1))

    def set(self, role, q):
        """The rotation of role's basis becomes q; its location and scale stay."""
        name = PREFIX + role
        m = self.basis[name]
        scale = tuple(math.sqrt(sum(m[i][j] ** 2 for i in range(3))) for j in range(3))
        self.basis[name] = quat_matrix(q, (m[0][3], m[1][3], m[2][3]), scale)
        self.solved[name] = q

    def aim(self, role, target):
        """Point role along target (armature space) by the shortest turn from its neutral direction."""
        r = rotation(self.neutral(PREFIX + role))
        self.set(role, between((0., 1., 0.), apply3(transpose3(r), target)))

    def flex(self, role, degrees):
        """Bend role about its own X (the knee/elbow hinge on standard rigs) until it is `degrees` from its
        parent's line, on the side +X flexes toward. A rest bend out of the hinge's plane (R10's elbows sit
        46° sideways) leaves the parent off that plane, so the turn is solved, not added: turning v by t
        about the hinge gives u . v(t) = r cos(t - t0)."""
        neutral = self.neutral(PREFIX + role)
        parent = self.bones[PREFIX + role][0]
        u, v, hinge = unit(column(self.pose(parent), 1)), unit(column(neutral, 1)), unit(column(neutral, 0))
        a, b = dot(u, v), dot(u, cross(hinge, v))
        r, t0 = math.hypot(a, b), math.atan2(b, a)
        turn = t0 + math.acos(max(-1., min(1., math.cos(math.radians(degrees)) / r))) if r > 1e-9 else math.radians(degrees)
        self.set(role, axis_angle((1., 0., 0.), math.degrees(turn)))

    def local(self, role, axis, degrees):
        """A turn about an armature-space axis taken at rest, expressed in role's own rest frame."""
        self.set(role, axis_angle(apply3(transpose3(rotation(self.rest(role))), axis), degrees))

    def clearance(self, side):
        """The arm chain's (shoulder joint to hand tip) nearest approach to the head, in radii of a sphere on
        the head bone's middle; the head bone runs from the neck to the top of the mesh."""
        head = self.pose(PREFIX + 'Head')
        radius = self.length('Head') / 2
        centre = add(column(head, 3), mul(unit(column(head, 1)), radius))
        points = [column(self.pose(PREFIX + side + role), 3) for role in ('Arm', 'ForeArm', 'Hand')]
        points.append(add(points[-1], mul(self.direction(side + 'Hand'), self.length(side + 'Hand'))))
        return min(_segment_distance(centre, p, q) for p, q in zip(points, points[1:])) / radius


def frame(rig):
    """Body up, the character's left and front, from rest heads (armature space)."""
    up = unit(sub(rig.head('Neck'), rig.head('Hips')))
    left = unit(reject(sub(rig.head('LeftArm'), rig.head('RightArm')), up))
    return up, left, cross(left, up)


def _digits(rig, side, finger):
    """A finger's joints from its base, as many as the rig has (some hands have one per finger)."""
    found = []
    for k in (1, 2, 3):
        if not rig.has(side + 'Hand' + finger + str(k)):
            break
        found.append(side + 'Hand' + finger + str(k))
    return found


def _hand(rig, side):
    """(fingers: [(role, curl axis, degrees)], thumb: [...]) at rest; empty when the hand cannot curl: a
    single-digit hand has no thumb to tell the palm side from the back."""
    hand = side + 'Hand'
    chains = [c for c in (_digits(rig, side, f) for f in FINGERS) if c]
    thumb = _digits(rig, side, 'Thumb')
    if not chains or not thumb:
        return [], []
    bases = [c[0] for c in chains]
    centre = mul(tuple(map(sum, zip(*(rig.head(b) for b in bases)))), 1 / len(bases))
    direction = unit(sub(centre, rig.head(hand)))
    across = sub(rig.head(bases[-1]), rig.head(bases[0])) if len(bases) > 1 else sub(rig.head(bases[0]), rig.head(thumb[0]))
    across = unit(reject(across, direction))  # Away from the thumb, along the knuckles.
    palm = mul(cross(across, direction), 1 if side == 'Left' else -1)  # Chirality: the side the fingers close on.
    curl = unit(cross(direction, palm))
    fingers = [(role, curl, CURL[k]) for c in chains for k, role in enumerate(c)]
    end = column(rig.rest(thumb[-1]), 3) if len(thumb) > 1 else add(rig.head(thumb[0]), mul(rig.resting(thumb[0]), rig.length(thumb[0])))
    pointing = unit(sub(end, rig.head(thumb[0]))) if len(thumb) > 1 else rig.resting(thumb[0])
    axis = unit(cross(pointing, palm))
    return fingers, [(role, axis, THUMB[k]) for k, role in enumerate(thumb)]


def solve(bones, pose='gentle', side='both'):
    """({bone name: (w, x, y, z)} for the bones this pose turns, plain notes on what it left out). Each pose
    starts from the bases given (the original pose), parents before children, so it never stacks on an
    earlier test pose."""
    if pose not in POSES:
        raise PoseError('Choose a test pose: ' + ', '.join(POSES) + '.')
    if side not in SIDES:
        raise PoseError('Choose both, left or right.')
    rig, notes = Rig(bones), []
    required = ['Hips', 'Neck', 'Head'] + [s + r for s in ('Left', 'Right') for r in ('Arm', 'ForeArm', 'Hand', 'UpLeg', 'Leg')]
    if not all(rig.has(role) for role in required):
        raise PoseError('This rig is missing the arm or leg bones a test pose needs.')
    up, left, _ = frame(rig)
    sides = ('Left', 'Right') if side == 'both' else (side.capitalize(),)
    steps = STEPS.get(pose, (pose,))
    if 'fists' in steps and not any(_hand(rig, s)[0] for s in sides):
        if pose == 'fists':
            raise PoseError('This rig has no finger bones that can close, so there is no fist pose.')
        steps, notes = steps[:-1], notes + ['no finger bones, so no fists']
    if pose == 'shoulder_raise' and not any(rig.has(s + 'Shoulder') for s in sides):
        raise PoseError('This rig has no shoulder bones to raise; the arms-up pose still works.')
    for s in sides:
        if 'gentle' in steps:
            for role in ('Leg', 'ForeArm'):
                rig.set(s + role, axis_angle((1., 0., 0.), GENTLE))
        if ('arms_up' in steps or 'shoulder_raise' in steps) and rig.has(s + 'Shoulder'):
            # From rest directions, so the same pose comes out whatever the arms were doing before.
            lift = math.radians(SHRUG if 'shoulder_raise' in steps else SHOULDER_LIFT)
            d = rig.resting(s + 'Shoulder')
            rig.aim(s + 'Shoulder', add(mul(d, math.cos(lift)), mul(unit(reject(up, d)), math.sin(lift))))
        if 'shoulder_raise' in steps:
            rig.aim(s + 'Arm', rig.resting(s + 'Arm'))
    if 'arms_up' in steps:
        # The persona's own first try put a forearm through the face: widen until both arms clear the head,
        # one angle for both, so the pose stays symmetric on a lopsided rig.
        for outward in range(ARMS_UP, ARMS_UP_WIDEST + 1, 2):
            a = math.radians(outward)
            for s in sides:
                rig.aim(s + 'Arm', add(mul(up, math.cos(a)), mul(left, (1 if s == 'Left' else -1) * math.sin(a))))
                if 'elbows_bent' in steps:
                    rig.flex(s + 'ForeArm', ELBOW)
            if all(rig.clearance(s) >= CLEAR for s in sides):
                break
        else:
            notes.append('the arms still pass close to the head')
    for s in sides:
        if 'elbows_bent' in steps and 'arms_up' not in steps:
            rig.flex(s + 'ForeArm', ELBOW)
        if 'knees_bent' in steps:
            rig.flex(s + 'Leg', KNEE)
        if 'forearm_twist' in steps:
            rig.set(s + 'ForeArm', axis_angle((0., 1., 0.), TWIST if s == 'Left' else -TWIST))
        if 'fists' in steps:
            fingers, thumb = _hand(rig, s)
            if not fingers:
                notes.append(f'no {s.lower()} fist: that hand has no finger bones that can close')
            for role, axis, degrees in fingers + thumb:
                rig.local(role, axis, degrees)
    return rig.solved, notes


def measure(posed, bones, pose, side='both'):
    """What the pose came to, from armature-space pose matrices ({name: 4x4 rows}): plain phrases with
    numbers, for the result the model reports from."""
    rig = Rig(bones)
    up = frame(rig)[0]

    def direction(role):
        return unit(column(posed[PREFIX + role], 1))

    def curl(s):  # The smallest total bend along a finger, from the hand to its last joint.
        chains = [[s + 'Hand'] + c for c in (_digits(rig, s, f) for f in FINGERS) if c]
        return min((sum(angle(direction(a), direction(b)) for a, b in zip(c, c[1:])) for c in chains), default=None) \
            if _hand(rig, s)[0] else None

    def roll(s):
        neutral = matmul(matmul(posed[PREFIX + s + 'Arm'], rigid_inverse(rig.rest(s + 'Arm'))), rig.rest(s + 'ForeArm'))
        return angle(column(neutral, 0), column(posed[PREFIX + s + 'ForeArm'], 0))
    sides = ('Left', 'Right') if side == 'both' else (side.capitalize(),)
    steps = {**STEPS, 'gentle': ('elbows_bent', 'knees_bent')}.get(pose, (pose,))
    rows = [('arms_up', 'upper arms from straight up:', lambda s: angle(direction(s + 'Arm'), up)),
            ('shoulder_raise', 'shoulders raised by',
             lambda s: angle(direction(s + 'Shoulder'), rig.resting(s + 'Shoulder')) if rig.has(s + 'Shoulder') else None),
            ('elbows_bent', 'elbows bent', lambda s: angle(direction(s + 'Arm'), direction(s + 'ForeArm'))),
            ('knees_bent', 'knees bent', lambda s: angle(direction(s + 'UpLeg'), direction(s + 'Leg'))),
            ('forearm_twist', 'forearms twisted', roll),
            ('fists', 'fingers curled', curl)]
    found = []
    for step, what, value in rows:
        values = [(s, value(s)) for s in sides] if step in steps else []
        values = [(s, v) for s, v in values if v is not None]
        if values:
            found.append(what + ' ' + ', '.join(f'{s.lower()} {v:.0f}°' for s, v in values))
    return found
