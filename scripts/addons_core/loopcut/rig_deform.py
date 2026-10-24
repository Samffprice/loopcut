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
"""
import math
from bisect import bisect_left, bisect_right
from itertools import compress, repeat
from operator import and_, eq, ge, gt, itemgetter, not_, or_, truediv

# Region: (name, the bone whose head is the joint, radius as a share of that bone's length).
REGIONS = (('shoulder', 'Arm', .55), ('elbow', 'ForeArm', .4), ('wrist', 'Hand', .6),
           ('hip', 'UpLeg', .45), ('knee', 'Leg', .4))
SPIKE = 3.0          # A spike sits this many local edge lengths off its neighbours' mean displacement,
SPIKE_STRETCH = 2.0  # has an edge at least this many times its rest length,
SPIKE_SHARE = .1     # and is off by at least this share of the region's radius.
SPIKES = 3           # A region needs this many to be a problem; one or two are listed as minor.
STRETCH = 6.0        # 99th-percentile edge length over rest past this: torn.
SQUASH = .1          # 1st percentile under this: crushed.
BENT = 10            # Degrees: a joint turned less is reported only when something is found.


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


def measure(meshes, joints):
    """meshes: [{'rest', 'posed': [(x, y, z)], 'edges': [(a, b)], 'tris': [(a, b, c)], 'scale': metres per
    unit, 'joints': [(rest centre, posed centre, radius)] in that mesh's space, in `joints` order}].
    joints: [{'name', 'bent'}]. Returns one row per joint: name, bent, vertices, stretch, squash, volume,
    spikes, spike_cm, and spiked: (mesh number, vertex index) of each spike. A spike in a region whose joint
    is not bent counts at the nearest bent one, whose turn moved it (raised arms drag stray vertices at the
    resting elbow)."""
    rows = [{'name': j['name'], 'bent': j['bent'], 'vertices': 0, 'ratios': [], 'rest_volume': 0.,
             'posed_volume': 0., 'spikes': 0, 'spike_cm': 0., 'spiked': []} for j in joints]
    bent = [k for k, j in enumerate(joints) if j['bent'] >= BENT]
    regioned = []
    for number, mesh in enumerate(meshes):
        rest, posed, scale = mesh['rest'], mesh['posed'], mesh['scale']
        centres = mesh['joints']
        region = regions(rest, [(c, r) for c, _, r in centres])
        for j in region:
            if j >= 0:
                rows[j]['vertices'] += 1
        # Edges at a region, their rest and posed lengths: map and compress keep the loops in C.
        touch = [e for e in mesh['edges'] if region[e[0]] >= 0 or region[e[1]] >= 0]
        first, second = list(map(itemgetter(0), touch)), list(map(itemgetter(1), touch))
        was = list(map(math.dist, map(rest.__getitem__, first), map(rest.__getitem__, second)))
        if 0. in was:
            keep = [k for k, w in enumerate(was) if w]
            first, second, was = [first[k] for k in keep], [second[k] for k in keep], [was[k] for k in keep]
        ratio = list(map(truediv, map(math.dist, map(posed.__getitem__, first), map(posed.__getitem__, second)), was))
        # Only a vertex with a stretched edge can be a spike; its neighbours' mean displacement decides.
        long = list(map(ge, ratio, repeat(SPIKE_STRETCH)))
        candidates = {i for i in (*compress(first, long), *compress(second, long)) if region[i] >= 0}
        sums = {i: [0., 0., 0., 0., 0] for i in candidates}
        incident = list(map(or_, map(sums.__contains__, first), map(sums.__contains__, second)))
        for a, b, w in compress(zip(first, second, was), incident):
            for i, other in ((a, b), (b, a)):
                if i in sums:
                    row = sums[i]
                    p, q = posed[other], rest[other]
                    row[0] += p[0] - q[0]; row[1] += p[1] - q[1]; row[2] += p[2] - q[2]; row[3] += w; row[4] += 1
        spiked = set()
        for i, (x, y, z, length, k) in sums.items():
            j = region[i]
            moved = (posed[i][0] - rest[i][0], posed[i][1] - rest[i][1], posed[i][2] - rest[i][2])
            off = math.dist(moved, (x / k, y / k, z / k))
            if off > SPIKE * length / k and off > SPIKE_SHARE * centres[j][2]:
                if joints[j]['bent'] < BENT and bent:
                    j = min(bent, key=lambda b: math.dist(rest[i], centres[b][0]) / centres[b][2])
                rows[j]['spikes'] += 1
                rows[j]['spike_cm'] = max(rows[j]['spike_cm'], off * scale * 100)
                rows[j]['spiked'].append((number, i))
                spiked.add(i)
        # A spike's own edges are reported as the spike, not as stretch.
        at, to = list(map(region.__getitem__, first)), list(map(region.__getitem__, second))
        inner = map(eq, at, to)
        if spiked:
            inner = map(and_, inner, map(not_, map(or_, map(spiked.__contains__, first), map(spiked.__contains__, second))))
        for j, r in compress(zip(at, ratio), inner):
            rows[j]['ratios'].append(r)
        regioned.append((mesh, region))
    # Volume only where a line is shown, once every mesh's spikes are counted.
    shown = {j for j, row in enumerate(rows) if row['bent'] >= BENT or row['spikes']}
    for mesh, region in regioned:
        inside = {}
        for t in mesh['tris']:
            j = region[t[0]]
            if j in shown and region[t[1]] == j and region[t[2]] == j:
                inside.setdefault(j, []).append(t)
        for j, tris in inside.items():
            for points, key, apex in ((mesh['rest'], 'rest_volume', mesh['joints'][j][0]),
                                      (mesh['posed'], 'posed_volume', mesh['joints'][j][1])):
                total, normal = _cone(points, tris)
                rows[j][key] += (total - sum(q * n for q, n in zip(apex, normal))) / 6
    for row in rows:
        ratios = row.pop('ratios')
        row['stretch'], row['squash'] = _percentile(ratios, .99), _percentile(ratios, .01)
        rest_volume, posed_volume = row.pop('rest_volume'), row.pop('posed_volume')
        # A region whose triangles face the joint inconsistently has no meaningful cone volume.
        row['volume'] = posed_volume / rest_volume if rest_volume > 0 else None
    return rows


def problems(row):
    """(problems, minor findings), each a plain phrase."""
    found, minor = [], []
    if row['spikes'] >= SPIKES:
        found.append(f"{row['spikes']} vertices pulled out into spikes, up to {row['spike_cm']:.1f} cm")
    elif row['spikes']:
        minor.append(f"{row['spikes']} stray {'vertex' if row['spikes'] == 1 else 'vertices'}, up to "
                     f"{row['spike_cm']:.1f} cm")
    if row['stretch'] is not None and row['stretch'] > STRETCH:
        found.append(f"skin torn: edges stretched to {row['stretch']:.1f}x")
    if row['squash'] is not None and row['squash'] < SQUASH:
        found.append(f"skin crushed: edges squashed to {row['squash']:.2f}x")
    return found, minor


def lines(rows):
    """The model's report: a header, one line per bent or flagged region, and the regions left out."""
    shown, quiet, flagged = [], [], []
    for row in rows:
        found, minor = problems(row)
        if found:
            flagged.append(row['name'])
        if not row['vertices'] and not row['spikes'] or (row['bent'] < BENT and not found and not minor):
            quiet.append(row['name'])
            continue
        numbers = (f"stretch {row['stretch']:.2f}x, squash {row['squash']:.2f}x" if row['stretch'] is not None
                   else 'no edges') + (f", volume {row['volume']:.0%}" if row['volume'] is not None else '')
        verdict = ('PROBLEM: ' + '; '.join(found + minor) if found else 'minor: ' + '; '.join(minor) if minor
                   else 'no spikes or torn skin')
        shown.append(f"- {row['name']}, turned {row['bent']:.0f}° from rest: {verdict} ({numbers}).")
    head = ('Deformation check (free; the posed mesh against rest): '
            + (f"problems at {', '.join(flagged)}." if flagged else 'no spikes or torn skin at the turned joints.'))
    out = [head] + shown
    if quiet:
        out.append('Not turned, nothing found: ' + ', '.join(quiet) + '.')
    out.append(f"Spikes: vertices over {SPIKE:g} edge lengths off their neighbours with a stretched edge; torn or "
               f"crushed: edges past {STRETCH:g}x or under {SQUASH:g}x. Volume is the joint's against rest; automatic "
               "weights lose some at strong bends. Report these numbers; do not call a flagged joint clean.")
    return out
