"""Body parts of an experimental rig, from its bone tree and where its bones sit; pure Python, so tests need no
Blender. autorig.check_deformation measures each part like a standard rig's joint, and names it in plain words by
its place on the body ("front left leg", "tail"), never by bone name: ar-dog-3's check refused the experimental
dog ("needs a standard rig") and the model then called a noodle-stretched gallop verified.

A leg is the largest branch of the tree that reaches the ground with no fork splitting it into two branches that
both do. The body is the bones from the legs' attachments up to the root. Every other branch off the body is an
appendage: the head is the biggest one on the body's midline, which also says where the front is (else the feet:
toes reach forward of the ankles), a tail is one on the midline behind, and paired ones are arms. Branches with
under MINOR of the skin go to the body.
"""
import math

LEG_REACH = .12   # A branch whose lowest skin or joint is within this share of the height of the ground reaches it.
MINOR = .02       # A branch with less of the mesh's vertices than this goes to the body.
PAIRED = .35      # Off the midline by this share of the legs' half-width (or of a tenth of the height): paired.
CLEAR = .1        # The head this share of the height ahead of the body's centre says which way is front.
UPPER = .3        # A head branch rising this share of the height above the body is an upper body.
ORDINALS = ('front', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth')


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    n = math.sqrt(_dot(a, a))
    return _scale(a, 1 / n) if n > 1e-12 else None


def _mean(points):
    points = list(points)
    return tuple(sum(p[k] for p in points) / len(points) for k in range(3)) if points else None


def _flat(v, up):
    return _sub(v, _scale(up, _dot(v, up)))


def _axis(points, up):
    """The horizontal direction the points spread along most (2D principal axis), or None."""
    if len(points) < 2:
        return None
    centre = _mean(points)
    a = _unit(_cross(up, (1., 0., 0.))) or _unit(_cross(up, (0., 1., 0.)))
    b = _cross(up, a)
    xs = [(_dot(_sub(p, centre), a), _dot(_sub(p, centre), b)) for p in points]
    sxx, syy, sxy = (sum(x * x for x, _ in xs), sum(y * y for _, y in xs), sum(x * y for x, y in xs))
    if sxx + syy < 1e-12:
        return None
    angle = .5 * math.atan2(2 * sxy, sxx - syy)
    return _unit(tuple(a[k] * math.cos(angle) + b[k] * math.sin(angle) for k in range(3)))


def _names(items, kind, front, right, width):
    """Plain names for paired items (legs or arms): [(item, position)] -> {item: name}, by side, then front to back."""
    if front is None:
        order = sorted(items, key=lambda item: _dot(item[1], right))
        return {item: f'{kind} {k + 1} of {len(order)}' for k, (item, _) in enumerate(order)}
    sides = {}
    for item, position in items:
        offset = _dot(position, right)
        side = 'right' if offset > .1 * width else 'left' if offset < -.1 * width else 'middle'
        sides.setdefault(side, []).append((item, position))
    names = {}
    for side, found in sides.items():
        found.sort(key=lambda pair: -_dot(pair[1], front))
        for k, (item, _) in enumerate(found):
            if len(found) == 1:
                names[item] = f'{side} {kind}'
            else:
                where = 'back' if k == len(found) - 1 else ORDINALS[min(k, len(ORDINALS) - 1)]
                names[item] = f'{where} {side} {kind}'
    return names


def parts(bones, ground, height, up=(0., 0., 1.), feet=None):
    """bones: {name: {'parent': name or None, 'head': (x, y, z), 'vertices': how many vertices it dominates,
    'low': the lowest of them along up or None, 'centroid': their mean or None}}, in one space (the armature's).
    ground: the mesh's lowest point along up there; height: its extent along up. feet: the mesh's lowest band's
    horizontal shift against the band above it, or None.
    Returns {'parts': [{'name', 'kind', 'bones', 'apex'}], 'front': unit vector or None, 'cue': what set it}; kind
    is 'head', 'body', 'tail', 'leg' or 'arm'; apex: the bones whose heads are the part's centre."""
    up = _unit(up) or (0., 0., 1.)
    children = {n: [] for n in bones}
    roots = []
    for name, bone in bones.items():
        (children[bone['parent']] if bone['parent'] in children else roots).append(name)
    below = {}

    def subtree(n):
        if n not in below:
            found, stack = [], [n]
            while stack:
                b = stack.pop()
                found.append(b)
                stack.extend(children[b])
            below[n] = found
        return below[n]

    def lowest(n):
        return min(min(_dot(bones[b]['head'], up), bones[b]['low'] if bones[b].get('low') is not None else math.inf)
                   for b in subtree(n))

    floor = ground + LEG_REACH * height
    reaches = {n: lowest(n) <= floor for n in bones}
    legs = []

    def find(n):
        if not reaches[n]:
            return
        if not any(sum(reaches[c] for c in children[b]) >= 2 for b in subtree(n)):
            legs.append(n)
            return
        last = n
        while len(children[last]) == 1:
            last = children[last][0]
        for c in children[last]:
            find(c)

    for root in roots:
        find(root)
    in_leg = {b for n in legs for b in subtree(n)}
    trunk = set(roots) - in_leg
    for n in legs:
        b = bones[n]['parent']
        while b is not None and b not in trunk:
            trunk.add(b)
            b = bones[b]['parent']
    if not legs:  # Nothing reaches the ground: the body is the root's chain, up to its first fork.
        for root in roots:
            b = root
            while len(children[b]) == 1:
                b = children[b][0]
                trunk.add(b)
    branches = [c for b in sorted(trunk) for c in children[b] if c not in trunk and c not in legs]
    total = sum(bone.get('vertices', 0) for bone in bones.values()) or 1

    def centroid(members):
        weighted = [(bones[b]['centroid'], bones[b].get('vertices', 0)) for b in members
                    if bones[b].get('centroid') is not None and bones[b].get('vertices', 0)]
        count = sum(w for _, w in weighted)
        if count:
            return tuple(sum(p[k] * w for p, w in weighted) / count for k in range(3))
        return _mean(bones[b]['head'] for b in members)

    centre = _mean(bones[b]['head'] for b in trunk) or _mean(bone['head'] for bone in bones.values())
    # Where each leg is: its skin's centroid (a leg can start with a short bone on the midline, as ar-dog-3's did).
    tops = {n: centroid(subtree(n)) for n in legs}
    # The body's long axis: between the girdles the legs hang from, else across the legs' spread, else the bones'.
    girdles = {}
    for n in legs:
        girdles.setdefault(bones[n]['parent'], []).append(tops[n])
    hips = [_mean(points) for points in girdles.values()]
    far = max(((a, b) for a in hips for b in hips), key=lambda pair: _dot(*(_flat(_sub(*pair), up),) * 2),
              default=None)
    axis = None
    if far and math.sqrt(_dot(_flat(_sub(*far), up), _flat(_sub(*far), up))) > .1 * height:
        axis = _unit(_flat(_sub(*far), up))
    elif len(legs) >= 2:
        across = _axis([tops[n] for n in legs], up)
        axis = _unit(_cross(across, up)) if across else None
    axis = axis or _axis([bone['head'] for bone in bones.values()], up) or _unit(_cross(up, (0., 1., 0.)))
    lateral = _unit(_cross(up, axis))
    width = max((abs(_dot(_sub(tops[n], centre), lateral)) for n in legs), default=0.) or .1 * height
    found = []
    for c in branches:
        members = subtree(c)
        count = sum(bones[b].get('vertices', 0) for b in members)
        place = _sub(centroid(members), centre)
        found.append({'root': c, 'bones': members, 'count': count, 'place': place,
                      'paired': abs(_dot(place, lateral)) >= PAIRED * width, 'minor': count < MINOR * total})
    middle = [a for a in found if not a['paired'] and not a['minor']]
    head = max(middle, key=lambda a: a['count'], default=None)
    front, cue = None, None
    if head is not None and abs(_dot(head['place'], axis)) > CLEAR * height:
        front, cue = _scale(axis, 1 if _dot(head['place'], axis) > 0 else -1), 'head'
    elif feet is not None and abs(_dot(feet, axis)) > .005 * height:
        front, cue = _scale(axis, 1 if _dot(feet, axis) > 0 else -1), 'feet'
    if cue == 'feet':  # The biggest midline branch is the head unless it is clearly behind (a heavy tail).
        head = max((a for a in middle if _dot(a['place'], front) > -CLEAR * height), key=lambda a: a['count'],
                   default=None)
    right = _cross(front, up) if front else lateral
    out = {'head': [], 'body': sorted(trunk), 'tail': []}
    apex = {'head': [head['root']] if head else [], 'body': sorted(trunk), 'tail': []}
    arms = []
    for a in found:
        if a['minor']:
            kind = 'body'
        elif a is head:
            kind = 'head'
        elif a['paired']:
            arms.append(a)
            continue
        elif front is None:
            kind = 'body'
        else:  # On the midline: ahead of the centre with the head (a jaw or neck of its own), else a tail.
            kind = 'head' if _dot(a['place'], front) > 0 else 'tail'
        out[kind] += a['bones']
        if kind == 'tail' and (not apex['tail'] or a['count'] > apex['tail'][1]):
            apex['tail'] = [a['root'], a['count']]
    apex['tail'] = apex['tail'][:1]
    rows = [{'name': kind, 'kind': kind, 'bones': out[kind], 'apex': apex[kind] or out[kind][:1]}
            for kind in ('head', 'body', 'tail') if out[kind]]
    if head is not None and max(_dot(_sub(bones[b]['head'], centre), up) for b in head['bones']) > UPPER * height:
        rows[0]['name'] = 'upper body'  # A biped's spine, arms and head hang from its hips as one branch.
    legs_named = _names([(n, _sub(tops[n], centre)) for n in legs], 'leg', front, right, width)
    for n in sorted(legs, key=lambda n: (-_dot(_sub(tops[n], centre), front) if front else 0, legs_named[n])):
        rows.append({'name': legs_named[n], 'kind': 'leg', 'bones': subtree(n), 'apex': [n]})
    arm_width = max((abs(_dot(a['place'], right)) for a in arms), default=width)
    arms_named = _names([(a['root'], a['place']) for a in arms], 'arm', front, right, arm_width)
    for a in arms:
        rows.append({'name': arms_named[a['root']], 'kind': 'arm', 'bones': a['bones'], 'apex': [a['root']]})
    return {'parts': rows, 'front': front, 'cue': cue}
