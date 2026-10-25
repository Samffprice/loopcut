"""A free numeric check of a posed character against its rest shape, per joint region; pure Python, so
tests need no Blender. autorig.check_deformation reads the evaluated meshes (posed, and with the rig at
rest) and words the result for the model, which judges deformation badly from images (ar-artist-1 said
"no tearing" and "chest smooth, no lumps" three times over a pinch, sleeves left behind and spikes).

Per region, a sphere around the joint at rest: edge stretch and squash (posed over rest length, the 99th
and 1st percentiles, leaving out a spike's own edges), the volume of the cone from the joint to the region's triangles (posed over rest),
and spikes: vertices whose posed displacement is far from their neighbours' mean (a Laplacian outlier)
with an edge stretched as well, since a rigid turn moves a vertex off that mean but stretches nothing.

Calibrated 2026-10-23 on ar-artist-1's hand-edited final rig and 15 clean automatic rigs in every named
pose (out/autorig/m2/client-ui/r10): only spikes separate them. Linear skinning loses volume at strong
bends on every rig (clean elbows at 130° kept 18-71%, knees at 90° 17-93%), so volume is a number, not a
verdict; stretch and squash count only past anything a clean rig did (4.6x, 0.17x).

R11 (ar-artist-2 passed a torn shoulder as "no spikes or torn skin, volume 54%", 97% before the model's
"fix"): remember() compares each joint with the last check of the same pose, so a joint that got worse leads
the result, and moved() names rest joints moved since rigging. Nothing measured locally told that rig from a
clean one (out/autorig/m2/client-ui/r11): faces turned over against their bone, skin crossing skin, slivers
and weight jumps all ran as high or higher on clean rigs in the stronger named poses, so the clean verdict
says only what was measured.
"""
import math
from bisect import bisect_left, bisect_right
from itertools import compress, repeat
from operator import and_, eq, ge, gt, itemgetter, mul, not_, or_, truediv

# Region: (name, the bone whose head is the joint, radius as a share of that bone's length).
REGIONS = (('shoulder', 'Arm', .55), ('elbow', 'ForeArm', .4), ('wrist', 'Hand', .6),
           ('hip', 'UpLeg', .45), ('knee', 'Leg', .4), ('ankle', 'Foot', 1.))  # R12: ar-beginner-2's boots.
SPIKE = 3.0          # A spike sits this many local edge lengths off its neighbours' mean displacement,
SPIKE_STRETCH = 2.0  # has an edge at least this many times its rest length,
SPIKE_SHARE = .1     # and is off by at least this share of the region's radius.
SPIKES = 3           # A region needs this many to be a problem; one or two are listed as minor.
# An animation's worst frame of ~16 picks up more strays: clean rigs reached 7 in the GEM-X dance (a mild armpit
# pinch on screen), broken ones 25-136 (ar-dog-3's noodle, ar-beginner-2's surfboard boot and torn sleeve).
ANIMATION_SPIKES = 12
STRETCH = 6.0        # 99th-percentile edge length over rest past this: torn.
SQUASH = .1          # 1st percentile under this: crushed.
BENT = 10            # Degrees: a joint turned less is reported only when something is found.
CHANGE_VOLUME = .15  # Since the last check, a joint is clearly worse or better by this much volume,
CHANGE_RATIO = 1.3   # or stretch changed by this factor
CHANGE_STRETCH = .5  # and this much, or spikes by SPIKES.
MOVED = .005         # Metres: a rest joint moved this far since rigging is reported,
TURNED = 3           # or a bone turned this many degrees.
POSE_TOLERANCE = 5   # Degrees: a joint whose bones are this close to a remembered check is in the same pose.
REMEMBERED = 40      # Joint checks kept per rig (ten joints in four poses).
ANIMATION_FRAMES = 12  # An animation check measures this many evenly spaced frames, both ends included,
EXTREMES = 2           # the frames where the bones turn furthest from rest, and two halfway to the worst's neighbours.


def regions(points, joints):
    """Each point's region index (nearest joint inside its radius) or -1. joints: [(centre, radius)]. Only the
    points in each joint's x-slab are measured (66k-vertex characters)."""
    best, region = [math.inf] * len(points), [-1] * len(points)
    x = list(map(itemgetter(0), points))
    order = sorted(range(len(points)), key=x.__getitem__)
    xs = list(map(x.__getitem__, order))
    for j, (centre, radius) in enumerate(joints):
        near = order[bisect_left(xs, centre[0] - radius):bisect_right(xs, centre[0] + radius)]
        distance = list(map(math.dist, map(points.__getitem__, near), repeat(centre)))
        for k in compress(range(len(near)), map(gt, repeat(radius), distance)):
            i = near[k]
            if distance[k] < best[i]:
                best[i], region[i] = distance[k], j
    return region


def _cone(points, tris):
    """(sum of det(a, b, c), sum of a x b + b x c + c x a) over tris: six times the cone volume from any
    apex q is the first minus q . the second."""
    total, nx, ny, nz = 0., 0., 0., 0.
    for a, b, c in tris:
        ax, ay, az = points[a]
        bx, by, bz = points[b]
        cx, cy, cz = points[c]
        kx, ky, kz = by * cz - bz * cy, bz * cx - bx * cz, bx * cy - by * cx  # b x c
        total += ax * kx + ay * ky + az * kz
        nx += ay * bz - az * by + kx + cy * az - cz * ay
        ny += az * bx - ax * bz + ky + cz * ax - cx * az
        nz += ax * by - ay * bx + kz + cx * ay - cy * ax
    return total, (nx, ny, nz)


def _percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, max(0, round(q * (len(values) - 1))))] if values else None


def plan(meshes):
    """The half of measure() that needs only the rest shape, so an animation check does it once for all its frames.
    meshes: [{'rest': [(x, y, z)], 'edges': [(a, b)], 'tris': [(a, b, c)], 'scale': metres per unit, 'joints':
    [(rest centre, posed centre, radius)] in that mesh's space}], each optionally with 'region' (a region index
    per vertex, -1 for none: an experimental rig's parts by dominant bone, rig_parts) and 'boundary' (an edge
    between two regions counts for both: a part's joint is on its border, while a joint sphere holds its own)."""
    out = []
    for mesh in meshes:
        rest, centres = mesh['rest'], mesh['joints']
        region = mesh.get('region') or regions(rest, [(c, r) for c, _, r in centres])
        counts = {}
        for j in region:
            if j >= 0:
                counts[j] = counts.get(j, 0) + 1
        # Edges at a region, their rest and posed lengths: map and compress keep the loops in C.
        touch = [e for e in mesh['edges'] if region[e[0]] >= 0 or region[e[1]] >= 0]
        first, second = list(map(itemgetter(0), touch)), list(map(itemgetter(1), touch))
        was = list(map(math.dist, map(rest.__getitem__, first), map(rest.__getitem__, second)))
        if 0. in was:
            keep = [k for k, w in enumerate(was) if w]
            first, second, was = [first[k] for k in keep], [second[k] for k in keep], [was[k] for k in keep]
        at, to = list(map(region.__getitem__, first)), list(map(region.__getitem__, second))
        cross = [k for k, (a, b) in enumerate(zip(at, to)) if a != b] if mesh.get('boundary') else []
        tris = {}
        for t in mesh['tris']:
            j = region[t[0]]
            if j >= 0 and region[t[1]] == j and region[t[2]] == j:
                tris.setdefault(j, []).append(t)
        out.append({'rest': rest, 'scale': mesh['scale'], 'region': region, 'counts': counts, 'first': first,
                    'second': second, 'was': was, 'at': at, 'to': to, 'cross': cross, 'tris': tris,
                    'centres': [(c, r) for c, _, r in centres]})
    return out


def measure(meshes, joints):
    """meshes: [{'rest', 'posed': [(x, y, z)], 'edges': [(a, b)], 'tris': [(a, b, c)], 'scale': metres per
    unit, 'joints': [(rest centre, posed centre, radius)] in that mesh's space, in `joints` order}].
    joints: [{'name', 'bent'}, optionally 'bend' (degrees from straight)]. Returns one row per joint: name,
    bent, bend, vertices, stretch, squash, volume, spikes, spike_cm, and spiked: (mesh number, vertex index)
    of each spike. A spike in a region whose joint is not bent counts at the nearest bent one, whose turn
    moved it (raised arms drag stray vertices at the resting elbow)."""
    return measure_pose(plan(meshes), joints, [(m['posed'], [p for _, p, _ in m['joints']]) for m in meshes])


def measure_pose(planned, joints, posed):
    """measure() for one pose of plan()'s meshes. posed: per mesh, (posed points, posed joint centres)."""
    rows = [{'name': j['name'], 'bent': j['bent'], 'bend': j.get('bend'), 'vertices': 0, 'ratios': [],
             'rest_volume': 0., 'posed_volume': 0., 'spikes': 0, 'spike_cm': 0., 'spiked': []} for j in joints]
    bent = [k for k, j in enumerate(joints) if j['bent'] >= BENT]
    for number, (mesh, (points, _)) in enumerate(zip(planned, posed)):
        rest, region, centres, scale = mesh['rest'], mesh['region'], mesh['centres'], mesh['scale']
        first, second, was = mesh['first'], mesh['second'], mesh['was']
        for j, n in mesh['counts'].items():
            rows[j]['vertices'] += n
        ratio = list(map(truediv, map(math.dist, map(points.__getitem__, first), map(points.__getitem__, second)), was))
        # Only a vertex with a stretched edge can be a spike; its neighbours' mean displacement decides.
        long = list(map(ge, ratio, repeat(SPIKE_STRETCH)))
        candidates = {i for i in (*compress(first, long), *compress(second, long)) if region[i] >= 0}
        sums = {i: [0., 0., 0., 0., 0] for i in candidates}
        incident = list(map(or_, map(sums.__contains__, first), map(sums.__contains__, second)))
        for a, b, w in compress(zip(first, second, was), incident):
            for i, other in ((a, b), (b, a)):
                if i in sums:
                    row = sums[i]
                    p, q = points[other], rest[other]
                    row[0] += p[0] - q[0]; row[1] += p[1] - q[1]; row[2] += p[2] - q[2]; row[3] += w; row[4] += 1
        spiked = set()
        for i, (x, y, z, length, k) in sums.items():
            j = region[i]
            moved = (points[i][0] - rest[i][0], points[i][1] - rest[i][1], points[i][2] - rest[i][2])
            off = math.dist(moved, (x / k, y / k, z / k))
            if off > SPIKE * length / k and off > SPIKE_SHARE * centres[j][1]:
                if joints[j]['bent'] < BENT and bent:
                    j = min(bent, key=lambda b: math.dist(rest[i], centres[b][0]) / centres[b][1])
                rows[j]['spikes'] += 1
                rows[j]['spike_cm'] = max(rows[j]['spike_cm'], off * scale * 100)
                rows[j]['spiked'].append((number, i))
                spiked.add(i)
        # A spike's own edges are reported as the spike, not as stretch.
        at, to = mesh['at'], mesh['to']
        inner = map(eq, at, to)
        if spiked:
            inner = map(and_, inner, map(not_, map(or_, map(spiked.__contains__, first), map(spiked.__contains__, second))))
        for j, r in compress(zip(at, ratio), inner):
            rows[j]['ratios'].append(r)
        for k in mesh['cross']:
            if first[k] not in spiked and second[k] not in spiked:
                for j in (at[k], to[k]):
                    if j >= 0:
                        rows[j]['ratios'].append(ratio[k])
    # Volume only where a line is shown, once every mesh's spikes are counted.
    shown = {j for j, row in enumerate(rows) if row['bent'] >= BENT or row['spikes']}
    for mesh, (points, apexes) in zip(planned, posed):
        for j, tris in mesh['tris'].items():
            if j not in shown:
                continue
            for values, key, apex in ((mesh['rest'], 'rest_volume', mesh['centres'][j][0]),
                                      (points, 'posed_volume', apexes[j])):
                total, normal = _cone(values, tris)
                rows[j][key] += (total - sum(q * n for q, n in zip(apex, normal))) / 6
    for row in rows:
        ratios = row.pop('ratios')
        row['stretch'], row['squash'] = _percentile(ratios, .99), _percentile(ratios, .01)
        rest_volume, posed_volume = row.pop('rest_volume'), row.pop('posed_volume')
        # A region whose triangles face the joint inconsistently has no meaningful cone volume.
        row['volume'] = posed_volume / rest_volume if rest_volume > 0 else None
    return rows


def problems(row, limit=SPIKES):
    """(problems, minor findings), each a plain phrase; limit: the spikes that make a problem."""
    found, minor = [], []
    if row['spikes'] >= limit:
        found.append(f"{row['spikes']} vertices pulled out into spikes, up to {row['spike_cm']:.1f} cm")
    elif row['spikes']:
        minor.append(f"{row['spikes']} stray {'vertex' if row['spikes'] == 1 else 'vertices'}, up to "
                     f"{row['spike_cm']:.1f} cm")
    if row['stretch'] is not None and row['stretch'] > STRETCH:
        found.append(f"skin torn: edges stretched to {row['stretch']:.1f}x")
    if row['squash'] is not None and row['squash'] < SQUASH:
        found.append(f"skin crushed: edges squashed to {row['squash']:.2f}x")
    return found, minor


CLEAN = 'no spikes or torn edges'  # Only what it measures (ar-artist-2 saw shards it passed as "no torn skin").
HINGES = ('elbow', 'knee')


def _angle(row):
    """'bent 60° from straight' (elbows and knees, when measured) or 'turned 80° from rest'."""
    if row.get('bend') is not None and row['name'].endswith(HINGES):
        return f"bent {row['bend']:.0f}° from straight"
    return f"turned {row['bent']:.0f}° from rest"


def reached(rows):
    """The angles the tested joints reached, grouped: 'elbows bent left 60°, right 61° from straight; left
    shoulder turned 80° from rest'. A name without a leading side (an experimental rig's 'front left leg', 'tail')
    stands alone."""
    groups = {}
    for row in rows:
        side, _, region = row['name'].partition(' ')
        if side not in ('left', 'right') or not region:
            side, region = '', row['name']
        groups.setdefault(region, []).append((side, _angle(row).split(' ', 2)))
    out = []
    for region, found in groups.items():
        if len(found) == 1 or not found[0][0]:
            out += [f"{(side + ' ' + region).strip()} {' '.join(words)}" for side, words in found]
        else:
            verb, where = found[0][1][0], found[0][1][2]
            out.append(f'{region}s {verb} ' + ', '.join(f'{side} {words[1]}' for side, words in found) + f' {where}')
    return '; '.join(out)


def span(action, playback, cyclic=False):
    """(first, last) frame an animation check samples: the playback range within the action's frame range, all of
    the playback range when a curve cycles (ar-dog-3's 24-frame gallop played to 96), the action's own range when
    they do not overlap; None for an action that keys a single frame."""
    (a, b), (start, end) = action, playback
    if b - a < 1:
        return None
    first, last = (start, end) if cyclic else (max(start, math.ceil(a)), min(end, math.floor(b)))
    if last <= first:
        first, last = math.ceil(a), math.floor(b)
    return (first, last) if last > first else None


def frames(first, last, amount):
    """The frames an animation check measures: ANIMATION_FRAMES evenly spaced, both ends included, then the
    EXTREMES others where amount(frame) (the bones' total turn from rest) is largest; every frame of a short one."""
    every = list(range(first, last + 1))
    if len(every) <= ANIMATION_FRAMES:
        return every
    even = sorted({round(first + k * (last - first) / (ANIMATION_FRAMES - 1)) for k in range(ANIMATION_FRAMES)})
    ranked = sorted((f for f in every if f not in even), key=amount, reverse=True)
    return sorted(even + ranked[:EXTREMES])


def severity(row, limit=ANIMATION_SPIKES):
    """(past a problem limit, the share of the nearest limit reached, their sum): picks an animation's worst
    frame."""
    stretch = row['stretch'] if row['stretch'] is not None else 1.
    squash = row['squash'] if row['squash'] is not None else 1.
    shares = ((stretch - 1) / (STRETCH - 1), (1 - squash) / (1 - SQUASH), row['spikes'] / limit)
    return bool(problems(row, limit)[0]), max(shares), sum(shares)


def worst(frames):
    """frames: [(frame, rows)], one measure() per frame over the same joints. Per joint, its worst row with that
    'frame', among the frames where it was turned or something was found there; a joint with neither keeps the
    row where it turned most."""
    out = []
    for k in range(len(frames[0][1])):
        rows = [dict(found[k], frame=frame) for frame, found in frames]
        tested = [r for r in rows if r['bent'] >= BENT or r['spikes']]
        out.append(max(tested, key=severity) if tested else max(rows, key=itemgetter('bent')))
    return out


FOOTER = ("Spikes: vertices over {spike:g} edge lengths off their neighbours with a stretched edge; torn or crushed: "
          "edges past {stretch:g}x or under {squash:g}x. Volume is the {word}'s against rest; automatic weights lose "
          "some at strong bends. The check cannot see skin pushed through skin or layers poking out, so look at the "
          "{word} too. Report these numbers and angles; do not call a flagged or untested {word} clean.")


def nothing(animation=None, word='joint'):
    """The whole result when no joint is turned: R4-A2's unposed check passed every joint."""
    where = (f" in the {animation['frames']} frames checked from {animation['first']} to {animation['last']}"
             if animation else '')
    fix = ('' if animation else ' Pose it first: preview with pose stress (free) bends every joint and checks it.'
           if word == 'joint' else ' Pose or animate it first, then check again.')
    return (f'Deformation check (free): nothing was tested, because no {word} is turned from rest{where}; this says '
            f'nothing about how the rig bends, so do not call any {word} clean.{fix}')


NOTHING = nothing()


def lines(rows, lead=(), notes=(), animation=None, word='joint'):
    """The model's report: lead lines (what changed since the last check), a header with the angles reached
    and the verdict, one line per bent or flagged region, the regions not tested, then notes. animation:
    {'frames': count, 'first', 'last'} for worst() rows; the worst frame then comes first (ar-dog-3's gallop and
    ar-beginner-2's boots were called verified and fixed over broken frames). word: 'part' on an experimental
    rig, whose regions are body parts."""
    shown, quiet, bare, flagged, tested, listed = [], [], [], [], [], []
    limit = ANIMATION_SPIKES if animation else SPIKES
    for row in rows:
        found, minor = problems(row, limit)
        if found:
            flagged.append(row['name'])
        if not row['vertices'] and not row['spikes']:
            (bare if row['bent'] >= BENT else quiet).append(row['name'])
            continue
        if row['bent'] < BENT and not found and not minor:
            quiet.append(row['name'])
            continue
        if row['bent'] >= BENT:
            tested.append(row)
        numbers = (f"stretch {row['stretch']:.2f}x, squash {row['squash']:.2f}x" if row['stretch'] is not None
                   else 'no edges') + (f", volume {row['volume']:.0%}" if row['volume'] is not None else '')
        verdict = ('PROBLEM: ' + '; '.join(found + minor) if found else 'minor: ' + '; '.join(minor) if minor
                   else CLEAN)
        frame = f", worst at frame {row['frame']}" if animation else ''
        listed.append(row)
        shown.append(f"- {row['name']}{frame}, {_angle(row)}: {verdict} ({numbers}).")
    if not tested and not shown:
        return [*lead, nothing(animation, word)]
    verdict = f"problems at {', '.join(flagged)}." if flagged else f'{CLEAN} at the turned {word}s.'
    if animation:
        top = max(listed, key=severity)
        found = problems(top, limit)[0]
        first = [f"Worst at frame {top['frame']}: {top['name']}, " + ('; '.join(found) if found else
                 f"stretch {top['stretch']:.2f}x, squash {top['squash']:.2f}x, {CLEAN}" if top['stretch'] is not None
                 else CLEAN) + '.']
        head = (f"Deformation check over the animation (free; {animation['frames']} frames from {animation['first']} "
                f"to {animation['last']}, each against rest): {verdict}")
        out = [*first, *lead, head] + shown
    else:
        head = ('Deformation check (free; the posed mesh against rest)' + (f" at {reached(tested)}" if tested else '')
                + ': ' + verdict)
        out = [*lead, head] + shown
    if quiet:
        out.append(f"Not tested, since {'never ' if animation else 'not '}turned: " + ', '.join(quiet) + '.')
    if bare:
        out.append('Turned, but no skin there to test: ' + ', '.join(bare) + '.')
    out += notes
    out.append(FOOTER.format(spike=SPIKE, stretch=STRETCH, squash=SQUASH, word=word)
               + (f" It checked {animation['frames']} frames, not every one, so look at the worst one too."
                  if animation else ''))
    return out


def _numbers(row):
    return [x if x is None or isinstance(x, int) else round(x, 4) for x in
            (row['stretch'], row['squash'], row['volume'], row['spikes'])]


def _changes(old, new):
    """(worse, better) plain phrases between two checks of one joint: [stretch, squash, volume, spikes]."""
    worse, better = [], []
    (s0, _, v0, k0), (s1, _, v1, k1) = old[:4], new[:4]
    # Volume against rest: lost or bulging, so nearer 100% is better.
    if v0 is not None and v1 is not None and abs(abs(v1 - 1) - abs(v0 - 1)) >= CHANGE_VOLUME:
        (worse if abs(v1 - 1) > abs(v0 - 1) else better).append(f'volume {v0:.0%} → {v1:.0%}')
    if s0 and s1 and max(s0, s1) >= CHANGE_RATIO * min(s0, s1) and abs(s1 - s0) >= CHANGE_STRETCH:
        (worse if s1 > s0 else better).append(f'stretch {s0:.2f}x → {s1:.2f}x')
    if abs(k1 - k0) >= SPIKES:
        (worse if k1 > k0 else better).append(f'spikes {k0} → {k1}')
    return worse, better


def _quaternion_angle(p, q):
    return math.degrees(2 * math.acos(min(1., abs(sum(a * b for a, b in zip(p, q))))))


def same_pose(a, b):
    """Two {bone: (w, x, y, z)} poses of a joint's bones agree within POSE_TOLERANCE on every bone they share,
    and a bone in only one of them is not turned. The set can change: a rest joint moved since the last check
    brings other bones near (ar-artist-2's raised shoulder joints)."""
    identity = (1., 0., 0., 0.)
    return bool(a.keys() & b.keys()) and all(
        _quaternion_angle(a.get(n, identity), b.get(n, identity)) <= POSE_TOLERANCE for n in a.keys() | b.keys())


def remember(memory, rows, poses):
    """(lead lines, memory to store). memory: [{'joint', 'pose': {bone: (w, x, y, z)}, 'first', 'last'}], oldest
    first; poses: {joint name: {bone: (w, x, y, z)}} of the bones near each joint now. Each turned joint is
    compared with the remembered check of it in the same pose: the last one, then the first (a later "fix"
    that undid only part of the damage still reads as worse)."""
    memory = [dict(m) for m in memory]
    worse, still, better, same = [], [], [], 0
    for row in rows:
        if row['bent'] < BENT or row['name'] not in poses:
            continue
        now = _numbers(row)
        entry = next((m for m in memory if m['joint'] == row['name'] and same_pose(m['pose'], poses[row['name']])),
                     None)
        if entry is None:
            entry = {'joint': row['name'], 'pose': poses[row['name']], 'first': now}
        else:
            memory.remove(entry)
            lost, gained = _changes(entry['last'], now)
            since = _changes(entry['first'], now)[0]
            if lost:
                worse.append(f"{row['name']} {', '.join(lost)}")
            elif since:
                still.append(f"{row['name']} {', '.join(since)}")
            elif gained:
                better.append(f"{row['name']} {', '.join(gained)}")
            else:
                same += 1
        entry['last'] = now
        memory.append(entry)
    lead = []
    if worse:
        lead.append('Worse since the last check: ' + '; '.join(worse) + '; undo the last change.')
    if still:
        lead.append('Still worse than the first check of this pose: ' + '; '.join(still) + '.')
    if better and not worse and not still:
        lead.append('Better since the last check: ' + '; '.join(better) + '.')
    elif same and not worse and not still:
        lead.append('No clear change since the last check of this pose.')
    return lead, memory[-REMEMBERED:]


def _few(items, count=4):
    return '; '.join(items[:count]) + (f'; and {len(items) - count} more' if len(items) > count else '')


def _flagged(numbers):
    """Whether remembered animation numbers [stretch, squash, volume, spikes] were past a problem limit."""
    stretch, squash, _, spikes = numbers[:4]
    return spikes >= ANIMATION_SPIKES or (stretch or 0) > STRETCH or squash is not None and squash < SQUASH


def remember_animation(memory, animation, rows):
    """(lead lines, memory to store) for an animation check: each turned joint's worst numbers (worst() rows)
    against the last and the first check of the same animation (its action's name). memory: [{'animation',
    'joint', 'first', 'last'}], oldest first. ar-beginner-2's second weight "fix" made a boot into a surfboard
    over the same walk the first one had been checked on."""
    memory = [dict(m) for m in memory]
    worse, still, better, same, cleared = [], [], [], 0, 0
    for row in rows:
        if row['bent'] < BENT:
            continue
        now = _numbers(row)
        entry = next((m for m in memory if m.get('animation') == animation and m.get('joint') == row['name']), None)
        if entry is None:
            entry = {'animation': animation, 'joint': row['name'], 'first': now}
        else:
            memory.remove(entry)
            lost, gained = _changes(entry['last'], now)
            since = _changes(entry['first'], now)[0]
            if lost:
                worse.append(f"{row['name']} {', '.join(lost)}")
            elif since:
                still.append(f"{row['name']} {', '.join(since)}")
            if gained:
                better.append(f"{row['name']} {', '.join(gained)}")
            if not lost and not since and not gained:
                same += 1
            cleared += _flagged(entry['last']) and not _flagged(now)
        entry['last'] = now
        memory.append(entry)
    lead = []
    if worse and cleared:  # ar-beginner-2's third fix mended both boots and hurt the sleeves: say both, undo neither.
        lead.append('Since the last check of this animation, better: ' + _few(better) + '; worse: ' + _few(worse) + '.')
    elif worse:
        lead.append('Worse since the last check of this animation: ' + _few(worse) + '; undo the last change.')
    if still:
        lead.append('Still worse than the first check of this animation: ' + _few(still) + '.')
    if better and not worse and not still:
        lead.append('Better since the last check of this animation: ' + _few(better) + '.')
    elif same and not worse and not still and not better:
        lead.append('No clear change since the last check of this animation.')
    return lead, memory[-REMEMBERED:]


PLAIN = {'Hips': 'hips', 'Spine': 'lower back', 'Spine1': 'middle back', 'Spine2': 'chest', 'Neck': 'neck',
         'Head': 'head', 'Shoulder': 'collarbone', 'Arm': 'upper arm', 'ForeArm': 'forearm', 'Hand': 'hand',
         'UpLeg': 'thigh', 'Leg': 'shin', 'Foot': 'foot', 'ToeBase': 'toes'}


def plain(name, prefix='mixamorig:'):
    """'mixamorig:LeftArm' -> 'left upper arm'; fingers -> 'left index finger'."""
    role = name[len(prefix):] if name.startswith(prefix) else name
    side = next((s for s in ('Left', 'Right') if role.startswith(s)), '')
    role = role[len(side):]
    if role.startswith('Hand') and role != 'Hand':
        role = role[4:].rstrip('0123456789').lower()
        role = role if role == 'thumb' else role + ' finger'
    else:
        role = PLAIN.get(role, role)
    return (side.lower() + ' ' + role).strip()


def moved(saved, current, parents, scale, parts=None):
    """Plain phrases for rest joints moved since rigging. saved/current: {bone: (head xyz, y axis, z axis)}
    in armature space; parents: {bone: parent}; scale: metres per unit; parts: {bone: body part} on an
    experimental rig, whose bone names are not words. A bone turned only because a child joint moved is not
    listed again."""
    def joint(n):
        return f'a {parts.get(n, "body")} joint' if parts is not None else f'the {plain(n)} joint'

    def bone(n):
        if parts is not None:
            return f'a {parts.get(n, "body")} bone'
        return f"the {plain(n)}{'' if plain(n).endswith('bone') else ' bone'}"
    heads = {n for n in saved.keys() & current.keys() if math.dist(saved[n][:3], current[n][:3]) * scale >= MOVED}
    out = [(-round(math.dist(saved[n][:3], current[n][:3]) * scale, 3), n, f'{joint(n)} moved '
            f'{math.dist(saved[n][:3], current[n][:3]) * scale * 100:.1f} cm') for n in heads]
    for n in saved.keys() & current.keys() - heads:
        if any(parents.get(c) == n for c in heads):
            continue
        turn = max(math.degrees(math.acos(max(-1., min(1., sum(map(mul, saved[n][k:k + 3],
                                                                    current[n][k:k + 3])))))) for k in (3, 6))
        if turn >= TURNED:
            out.append((0, n, f"{bone(n)} turned {turn:.0f}°"))
    out = [text for *_, text in sorted(out)]  # Farthest first, then left before right.
    if not out:
        return ''
    listed = ', '.join(out[:4]) + (f' and {len(out) - 4} more' if len(out) > 4 else '')
    return (f'Rest skeleton changed since rigging: {listed}; the skin was weighted to the old joints, so it can tear '
            'there.')
