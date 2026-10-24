"""Portable humanoid label/axis planner, vendored byte-for-byte by the worker.

No Blender, NumPy, network, filesystem or package imports. ``plan_json`` is the
versioned JSON boundary; ``plan`` is the raising Python API used by the add-on.
All coordinates share one metric frame. Names/indices never infer anatomy.
"""

import math

PLANNER_VERSION = '1.1.0'
POLICY = 'mixamo-tokenrig-v1'
PREFIX = 'mixamorig:'


class CanonicalizationError(ValueError):
    def __init__(self, message, code='unsupported_skeleton', anatomy_reason='not_humanoid'):
        super().__init__(message)
        self.code = code
        self.anatomy_reason = anatomy_reason


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def mul(a, s):
    return tuple(x * s for x in a)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def length(a):
    return math.sqrt(dot(a, a))


def unit(a):
    n = length(a)
    if n < 1e-10:
        raise CanonicalizationError('degenerate direction; orientation is ambiguous')
    return mul(a, 1 / n)


def reject_axis(a, axis):
    return sub(a, mul(axis, dot(a, axis)))


def mean(points):
    return tuple(sum(p[k] for p in points) / len(points) for k in range(3))


def quantile(sorted_values, q):
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def plan(joints, vertices):
    """Return one role, confidence, tail and hinge axis per input joint.

    Coordinates are in a common metric space. No original names, gold facts,
    skin weights, fixture IDs or assumed world-facing direction enter inference.
    Confidence is an explainable heuristic, not a calibrated probability.
    """
    if not isinstance(joints, list) or not 15 <= len(joints) <= 256:
        reason = 'too_few_joints' if isinstance(joints, list) and len(joints) < 15 else 'not_humanoid'
        raise CanonicalizationError('expected a bounded humanoid joint list (15–256)', anatomy_reason=reason)
    if any(not isinstance(j, dict) or set(j) != {'name', 'parent', 'head'} for j in joints):
        raise CanonicalizationError('each joint requires name, parent and head')
    names = [j['name'] for j in joints]
    if (not all(isinstance(n, str) and n and len(n.encode('utf-8', errors='replace')) <= 63
                and all(ord(c) >= 32 and ord(c) != 127 and not 0xd800 <= ord(c) <= 0xdfff for c in n)
                for n in names) or len(set(names)) != len(names)):
        raise CanonicalizationError('joint identifiers must be unique safe strings of at most 63 UTF-8 bytes')
    raw_points = [j['head'] for j in joints]
    if (not isinstance(vertices, (list, tuple)) or not 1 <= len(vertices) <= 300000
            or any(not isinstance(p, (list, tuple)) or len(p) != 3
                   or not all(type(v) in (float, int) and abs(v) <= 1e6 and math.isfinite(v) for v in p)
                   for p in raw_points + list(vertices))):
        raise CanonicalizationError('finite 3D joints and mesh vertices are required')
    points = [tuple(p) for p in raw_points]
    children = [[] for _ in joints]
    roots = []
    for i, j in enumerate(joints):
        parent = j['parent']
        if parent is None:
            roots.append(i)
        elif type(parent) is not int or not 0 <= parent < len(joints) or parent == i:
            raise CanonicalizationError('invalid parent index')
        else:
            children[parent].append(i)
    if len(roots) != 1:
        raise CanonicalizationError('humanoid requires one pelvis root')
    root = roots[0]
    visited = set()
    def visit(i):
        if i in visited:
            raise CanonicalizationError('joint graph contains a cycle')
        visited.add(i)
        for c in children[i]:
            visit(c)
    visit(root)
    if len(visited) != len(joints):
        raise CanonicalizationError('joint graph is disconnected or cyclic')
    if len(children[root]) != 3:
        raise CanonicalizationError(f'pelvis must branch into spine and two legs; found {len(children[root])} children',
                                    anatomy_reason='pelvis_branches')
    def chain(i):
        result = [i]
        while len(children[result[-1]]) == 1:
            result.append(children[result[-1]][0])
        return result
    branches = [chain(i) for i in children[root]]
    spines = [c for c in branches if len(children[c[-1]]) == 3]
    if len(spines) != 1:
        raise CanonicalizationError('cannot identify a unique torso with neck and two arm branches')
    spine = spines[0]
    legs = [c for c in branches if c != spine]
    if any(len(c) not in (3, 4) or children[c[-1]] for c in legs):
        raise CanonicalizationError('legs must be unbranched thigh–shin–foot with optional toe')
    chest = spine[-1]
    trunk_up = unit(sub(points[chest], points[root]))
    upper = [chain(i) for i in children[chest]]
    candidates = [(dot(unit(sub(points[c[-1]], points[chest])), trunk_up), c)
                  for c in upper if not children[c[-1]] and 2 <= len(c) <= 4]
    candidates.sort(key=lambda row: row[0], reverse=True)
    if not candidates or candidates[0][0] < .75:
        raise CanonicalizationError('no head/neck chain aligned above torso')
    if len(candidates) > 1 and candidates[0][0] - candidates[1][0] < .25:
        raise CanonicalizationError('ambiguous head versus arm branches')
    neck_head = candidates[0][1]
    arms = [c for c in upper if c != neck_head]
    # A single digit has no fork: chain() follows it through the wrist. Only a
    # complete 3-joint continuation with a clear limb-to-hand scale transition
    # is recognized. Short ambiguous chains stay refused instead of guessing a
    # wrist or synthesizing a clavicle/digit. All original nodes remain present.
    for k, arm in enumerate(arms):
        if len(arm) == 4 and not children[arm[-1]]:
            steps = [length(sub(points[b], points[a])) for a, b in zip(arm, arm[1:])]
            # Four nodes could also be an unclavicled arm plus one short digit.
            # Without a hand fork, require a shorter proximal clavicle and two
            # substantial limb segments; never silently move the wrist to a tip.
            if steps[0] > .75 * steps[1] or steps[2] < .6 * steps[1]:
                raise CanonicalizationError('unbranched arm has an ambiguous wrist or short digit',
                                            'unsupported_hands', 'finger_count')
        if len(arm) in (6, 7) and not children[arm[-1]]:
            wrist, forearm = arm[-4], arm[-5]
            limb = sub(points[wrist], points[forearm])
            digit = sub(points[arm[-1]], points[wrist])
            steps = [length(sub(points[b], points[a])) for a, b in zip(arm[-4:], arm[-3:])]
            if (max(steps) > .6 * length(limb) or length(digit) > 1.25 * length(limb)
                    or dot(unit(limb), unit(digit)) < .5):
                raise CanonicalizationError('single digit does not clearly continue a wrist',
                                            'unsupported_hands', 'finger_count')
            arms[k] = arm[:-3]
    if len(arms) != 2 or any(len(c) not in (3, 4) or len(children[c[-1]]) > 5 for c in arms):
        raise CanonicalizationError('arms require optional clavicle, upper arm, forearm and a distinguishable hand',
                                    'unsupported_hands', 'finger_count')
    head = neck_head[-1]
    up = unit(sub(points[head], points[root]))
    height = max(dot(p, up) for p in vertices) - min(dot(p, up) for p in vertices)
    if height < 1e-6:
        raise CanonicalizationError('mesh height is zero')
    if any(dot(sub(points[c[-1]], points[root]), up) > -.25 * height for c in legs):
        raise CanonicalizationError('leg terminals do not lie below pelvis: nonhumanoid anatomy')
    lateral = unit(reject_axis(sub(points[arms[0][-2]], points[arms[1][-2]]), up))
    try:
        toe_directions = [unit(reject_axis(sub(points[c[3]], points[c[2]]), up)) for c in legs if len(c) == 4]
    except CanonicalizationError as ex:
        raise CanonicalizationError('toe direction is degenerate; orientation is ambiguous',
                                    'ambiguous_front', 'front_ambiguous') from ex
    if toe_directions:
        # Outward-pointing feet may disagree with one another while both clearly
        # point forward. Test each against the body's sagittal axis instead of
        # imposing a pairwise toe-angle limit. Near-sideways/opposed evidence is
        # still ambiguous; this confidence is a disclosed heuristic, not a model
        # probability. Arm order only changes the axis sign, never the result.
        normal = unit(cross(up, lateral))
        projections = [dot(direction, normal) for direction in toe_directions]
        alignment = min(abs(value) for value in projections)
        if min(projections) * max(projections) <= 0 or alignment <= .5:
            raise CanonicalizationError('toe directions do not agree on body forward; orientation is ambiguous',
                                        'ambiguous_front', 'front_ambiguous')
        front = mul(normal, 1 if sum(projections) > 0 else -1)
        bilateral = len(projections) == 2
        front_confidence = .78 + (.19 if bilateral else .10) * alignment
        front_source = ('bilateral toe projections' if bilateral else 'single toe projection') + ' on body sagittal axis'
    else:
        # Reduced rigs have ankle joints only. Use asymmetric shoe volume around
        # the ankles, not world -Y or inherited bone axes, to resolve front/back.
        normal = unit(cross(up, lateral))
        ankle_points = [points[c[2]] for c in legs]
        extents = []
        for ankle in ankle_points:
            samples = [dot(sub(v, ankle), normal) for v in vertices
                       if abs(dot(sub(v, ankle), up)) < height * .07
                       and abs(dot(sub(v, ankle), lateral)) < height * .065]
            if not samples:
                raise CanonicalizationError('no foot surface to resolve front on reduced rig',
                                            'ambiguous_front', 'front_ambiguous')
            samples.sort()
            extents.append(quantile(samples, .98) + quantile(samples, .02))
        if min(extents) * max(extents) <= 0 or abs(sum(extents) / 2) < height * .012:
            raise CanonicalizationError('foot mesh has ambiguous front/back without toe joints',
                                        'ambiguous_front', 'front_ambiguous')
        front = mul(normal, 1 if sum(extents) > 0 else -1)
        front_confidence, front_source = .78, 'bilateral foot mesh extent (no toe joints)'
    front = unit(reject_axis(front, up))
    left = unit(cross(up, front))
    roles, evidence = {}, {}
    def assign(index, role, confidence=.98, reason='humanoid chain topology'):
        if index in evidence or role in roles:
            raise CanonicalizationError('non-bijective semantic assignment')
        roles[role] = index
        evidence[index] = {'index': index, 'source': names[index], 'role': role,
                           'name': PREFIX + role, 'confidence': min(confidence, front_confidence), 'reason': reason}
    assign(root, 'Hips')
    notes = []
    # Recognized anchors are distributed over longer spines. Additional joints
    # remain weighted ancestors, named explicitly, and inherit retarget motion.
    anchor_names = {0: 'Spine', len(spine) - 1: 'Spine2'} if len(spine) > 1 else {0: 'Spine'}
    if len(spine) >= 3:
        anchor_names[(len(spine) - 1) // 2] = 'Spine1'
    for k, i in enumerate(spine):
        assign(i, anchor_names.get(k, f'SpineExtra{k:02d}'))
    for k, i in enumerate(neck_head[:-1]):
        assign(i, 'Neck' if k == 0 else ('Neck1' if k == len(neck_head) - 2 else f'NeckExtra{k:02d}'))
    assign(head, 'Head')
    notes.append(f'{len(spine)} torso bones retained; recognized anchors: {[anchor_names[k] for k in sorted(anchor_names)]}')
    if len(spine) > 3 or len(neck_head) > 3:
        notes.append('extra spine/neck joints retain their weights and inherit nearest mapped ancestor motion')
    side_chains = {}
    for kind, chains in [('arm', arms), ('leg', legs)]:
        signed = [(dot(sub(points[c[-1] if kind == 'arm' else c[0]], points[root]), left), c) for c in chains]
        if min(x for x, _ in signed) > -.025 * height or max(x for x, _ in signed) < .025 * height:
            raise CanonicalizationError(f'{kind} chains do not straddle body midplane')
        for x, c in signed:
            side = 'Left' if x > 0 else 'Right'
            side_chains[side + kind] = c
            if kind == 'arm':
                parts = (['Shoulder'] if len(c) == 4 else []) + ['Arm', 'ForeArm', 'Hand']
            else:
                parts = ['UpLeg', 'Leg', 'Foot'] + (['ToeBase'] if len(c) == 4 else [])
            for i, part in zip(c, parts):
                assign(i, side + part)
    finger_axes, hand_evidence = {}, {}
    for side in ('Left', 'Right'):
        hand = roles[side + 'Hand']
        fingers = [chain(c) for c in children[hand]]
        if not fingers:
            notes.append(side + ': no finger joints; no synthetic fingers added')
            continue
        if any(not 1 <= len(c) <= 3 or children[c[-1]] for c in fingers):
            raise CanonicalizationError('digits require one to three unbranched joints',
                                        'unsupported_hands', 'finger_count')
        axis = unit(sub(points[hand], points[roles[side + 'ForeArm']]))
        if len(fingers) == 1:
            # No opposition evidence can identify a thumb. A lone continuation
            # receives the central Middle slot, a documented retarget surrogate,
            # not a claim that four missing anatomical digits were recovered.
            for k, i in enumerate(fingers[0]):
                assign(i, side + 'HandMiddle' + str(k + 1), .74,
                       'single central continuation; Middle slot by partial-digit policy')
            notes.append(side + ': one continuing digit mapped to Middle; no missing bones or weights invented')
            hand_evidence[side] = {'digits': 1, 'policy': 'central continuation uses Middle', 'confidence': .74}
            continue
        # An opposed thumb diverges from the forearm/central digit direction at
        # both its base and tip. Proximity alone fails on generated two-finger
        # hands whose thumb base is farther from the wrist than the other digit.
        scores = [(sum(1 - dot(unit(sub(points[i], points[hand])), axis) for i in (c[0], c[-1])), c)
                  for c in fingers]
        scores.sort(key=lambda row: row[0], reverse=True)
        separation = scores[0][0] - scores[1][0]
        if scores[0][0] < .2 or separation < .12:
            raise CanonicalizationError(side + ': thumb opposition is ambiguous',
                                        'unsupported_hands', 'finger_count')
        thumb = scores[0][1]
        others = [c for c in fingers if c != thumb]
        if len(others) == 1:
            across = unit(reject_axis(sub(points[others[0][0]], points[thumb[0]]), axis))
        else:
            pairs = [(length(sub(points[a[0]], points[b[0]])), a, b)
                     for k, a in enumerate(others) for b in others[k + 1:]]
            width = max(row[0] for row in pairs)
            centre = mean([points[c[0]] for c in others])
            axes, order = [], None
            for distance, a, b in pairs:
                if width - distance > width * 1e-6:
                    continue
                direction = unit(sub(points[b[0]], points[a[0]]))
                if dot(sub(points[thumb[0]], centre), direction) > 0:
                    direction = mul(direction, -1)
                candidate = sorted(others, key=lambda c: dot(points[c[0]], direction))
                if (-dot(sub(points[thumb[0]], centre), direction) < width * .08
                        or any(dot(sub(points[b[0]], points[a[0]]), direction) < width * .02
                               for a, b in zip(candidate, candidate[1:]))
                        or order is not None and candidate != order):
                    raise CanonicalizationError(side + ': knuckle order or thumb side is ambiguous',
                                                'unsupported_hands', 'finger_count')
                order = candidate
                axes.append(direction)
            # Tied widest axes may agree on labels; average those orientations.
            # A tie that changes digit order refuses instead of using joint IDs.
            across, others = unit(mean(axes)), order
        # Compact spatial ordering is a mapping policy, not reconstruction of
        # which anatomical digit is absent. Two/four-digit hands use Thumb plus
        # the first one/three slots; no holes, synthetic bones or lost weights.
        for finger, c in [('Thumb', thumb), *zip(('Index', 'Middle', 'Ring', 'Pinky'), others)]:
            for k, i in enumerate(c):
                assign(i, side + 'Hand' + finger + str(k + 1), .88,
                       'thumb opposition; spatial order away from thumb; retained chain depth')
        finger_axes[side] = across
        hand_evidence[side] = {'digits': len(fingers), 'thumb_opposition': scores[0][0],
                              'opposition_margin': separation, 'policy': 'Thumb then compact Index/Middle/Ring/Pinky'}
        if len(fingers) != 5 or any(len(c) != 3 for c in fingers):
            notes.append(side + f': {len(fingers)} observed digits, {list(map(len, fingers))} joints; '
                         'compact spatial names do not infer missing anatomical digits')
    if len(evidence) != len(joints):
        raise CanonicalizationError('unlabelled branches remain; refusing partial humanoid conversion')
    # X is the hinge; Y follows the bone. Sign is chosen so +X flexion bends
    # wrists forward and ankles backward, with the same rule on both sides.
    hinge = {}
    for side in ('Left', 'Right'):
        for a, b, c, desired in [('Arm', 'ForeArm', 'Hand', front), ('UpLeg', 'Leg', 'Foot', mul(front, -1))]:
            u = sub(points[roles[side + b]], points[roles[side + a]])
            v = sub(points[roles[side + c]], points[roles[side + b]])
            # Compute directional evidence on unit vectors, never squared or
            # cubed lengths that cross the degeneracy bound on tiny characters.
            u_direction, v_direction = unit(u), unit(v)
            bend = cross(u_direction, v_direction)
            sine = length(bend)
            method = 'measured three-joint bend plane'
            if sine < .04:
                bend = cross(v_direction, desired)
                method = 'near-straight chain: body-front bend plane'
            elif abs(dot(unit(cross(unit(bend), v_direction)), desired)) < .5:
                # Bowed legs can provide a measurable plane that flexes mostly
                # sideways. It is not evidence of a knee/elbow hinge. Require
                # the measured tangent within 60 degrees of body flexion;
                # otherwise use the independent body-front plane, keeping heads.
                bend = cross(v_direction, desired)
                method = 'sideways joint curvature: body-front bend plane'
            bend = unit(bend)
            if dot(cross(bend, v_direction), desired) < 0:
                bend = mul(bend, -1)
            for part in (a, b):
                hinge[side + part] = (bend, method)
    for role, i in roles.items():
        p = points[i]
        reason = 'child joint'
        if role == 'Hips':
            tail = points[spine[0]]
        elif role == 'Head':
            tail = add(p, mul(up, max(dot(sub(v, p), up) for v in vertices)))
            reason = 'head to mesh top along body up'
        elif role.endswith('Foot'):
            toe = roles.get(role[:-4] + 'ToeBase')
            direction = reject_axis(sub(points[toe], p), up) if toe is not None else mul(front, height * .075)
            tail = add(p, direction)
            reason = 'ankle toward ball, projected onto ground plane'
        elif role.endswith('ToeBase'):
            parent = joints[i]['parent']
            direction = reject_axis(sub(p, points[parent]), up)
            tail = add(p, mul(unit(direction), max(length(direction) * .35, height * .015)))
            reason = 'toe continues forward along ground'
        elif role.endswith('Hand'):
            middle = roles.get(role + 'Middle1')
            if middle is not None:
                tail = points[middle]
                reason = 'hand to middle-finger base'
            else:
                direction = unit(sub(p, points[joints[i]['parent']]))
                samples = [dot(sub(v, p), direction) for v in vertices if length(sub(v, p)) < height * .12]
                extent = max(samples, default=height * .045)
                tail = add(p, mul(direction, max(height * .01, min(extent, height * .075))))
                reason = 'no fingers: continue forearm into local hand surface'
        elif children[i]:
            # Chest has branches: the central neck is the continuation, not an arm.
            child = neck_head[0] if i == chest else children[i][0]
            tail = points[child]
        else:
            parent = joints[i]['parent']
            tail = add(p, mul(sub(p, points[parent]), .75))
            reason = 'fingertip continues last phalanx'
        y = unit(sub(tail, p))
        side = 'Left' if role.startswith('Left') else 'Right'
        x, roll_reason = hinge.get(role, (left, 'body lateral axis'))
        if 'Hand' in role and side in finger_axes:
            x, roll_reason = finger_axes[side], 'knuckle line'
        x = reject_axis(x, y)
        if length(x) < 1e-6:
            x = reject_axis(front, y)
            roll_reason += '; perpendicular body-front fallback'
        evidence[i].update(tail=tail, x_axis=unit(x), orientation=reason, roll=roll_reason)
    return {'schema_version': 1, 'planner_version': PLANNER_VERSION,
            'status': 'planned', 'method': 'topology_geometry_mixamo_v1',
            'uses_source_names': False, 'uses_gold': False, 'height': height,
            'frame': {'up': up, 'front': front, 'left': left, 'front_source': front_source,
                      'front_confidence': front_confidence,
                      'front_margin': alignment - .5 if toe_directions else abs(sum(extents) / 2) / height - .012},
            'hands': hand_evidence,
            'labels': [evidence[i] for i in range(len(joints))], 'notes': notes,
            'confidence_min': min(row['confidence'] for row in evidence.values())}


def canonical_plan(report):
    """The closed contract 0.8 projection; planner provenance is kept separately."""
    return {'policy': POLICY, 'labels': [{k: row[k] for k in ('index', 'source', 'role', 'name')}
                                        for row in sorted(report['labels'], key=lambda row: row['index'])]}


def plan_json(payload):
    """JSON in/out v1: {schema_version:1, joints:[...], vertices:[[x,y,z],...]}.

    Returns the accepted contract projection and full diagnostic report, or a
    typed unsupported_anatomy refusal. No exception text from external libraries
    or names is exposed. The raising API remains useful for atomic Blender apply.
    """
    result = {'schema_version': 1, 'planner_version': PLANNER_VERSION}
    try:
        if (not isinstance(payload, dict) or set(payload) != {'schema_version', 'joints', 'vertices'}
                or type(payload['schema_version']) is not int or payload['schema_version'] != 1):
            raise CanonicalizationError('expected planner input schema_version 1 with joints and vertices')
        report = plan(payload['joints'], payload['vertices'])
    except CanonicalizationError as ex:
        return {**result, 'status': 'refused', 'code': 'unsupported_anatomy',
                'anatomy_reason': ex.anatomy_reason, 'message': str(ex)}
    return {**result, 'status': 'planned', 'canonical_plan': canonical_plan(report), 'report': report}

