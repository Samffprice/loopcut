"""Exact surface correspondence, including distinct coincident source vertices."""

from collections import Counter, defaultdict
from itertools import product
import math


def transfer_weights(source_points, source_triangles, points, triangles, weights, tolerance):
    """Match triangle corners, never a nearest surface or an averaged weight.

    A transport split may merge back into one original vertex only if every
    corresponding corner agrees. Distinct coincident vertices are identified by
    their incident triangles. Duplicate geometric faces with conflicting weights
    remain ambiguous and fail closed; vertex order is never used as evidence.
    """
    if not math.isfinite(tolerance) or tolerance <= 0 or len(points) != len(weights):
        raise ValueError('invalid correspondence inputs')
    if any(len(p) != 3 or not all(math.isfinite(v) for v in p)
           for p in [*source_points, *points]):
        raise ValueError('invalid correspondence coordinates')
    # Independent rounding bins can disagree across a bin boundary even when
    # the import error is below tolerance. Anchor labels in the original mesh;
    # the grid only accelerates a distance check and never decides equivalence.
    grid, representatives = defaultdict(list), []
    neighbors = list(product((-1, 0, 1), repeat=3))
    def label(point, create=False):
        cell = tuple(math.floor(v / tolerance) for v in point)
        matches = {i for delta in neighbors
                   for i in grid.get(tuple(c + d for c, d in zip(cell, delta)), [])
                   if math.dist(point, representatives[i]) <= tolerance}
        if len(matches) > 1 and create:
            raise ValueError('ambiguous nearby position classes')
        if matches:
            return next(iter(matches)) if create else matches
        if not create:
            raise ValueError('candidate position differs from original transport')
        index = len(representatives)
        representatives.append(point)
        grid[cell].append(index)
        return index
    source_keys = [label(p, create=True) for p in source_points]
    domains = [label(p) for p in points]
    def face_key(triangle, vertex_keys):
        if len(triangle) != 3 or any(type(i) is not int or i < 0 or i >= len(vertex_keys) for i in triangle):
            raise ValueError('invalid triangle index')
        return tuple(sorted(vertex_keys[i] for i in triangle))
    original_faces = Counter(face_key(t, source_keys) for t in source_triangles)
    if any(len(choices) > 1 for choices in domains):
        # Overlapping tolerance spheres do not justify choosing the closest
        # vertex. Incident triangles may uniquely identify a position class.
        # Intersect independently computed face constraints, so traversal order
        # cannot decide the answer. Unresolved/loose ambiguity remains refused.
        narrowed = [set(choices) for choices in domains]
        for triangle in triangles:
            if len(triangle) != 3 or any(type(i) is not int or not 0 <= i < len(domains) for i in triangle):
                raise ValueError('invalid triangle index')
            choices = [domains[i] for i in triangle]
            if math.prod(map(len, choices)) > 64:
                raise ValueError('excessive nearby classes prevent bounded correspondence')
            solutions = [row for row in product(*choices) if tuple(sorted(row)) in original_faces]
            if not solutions:
                raise ValueError('candidate topology differs from original transport')
            for corner, vertex in enumerate(triangle):
                narrowed[vertex].intersection_update(row[corner] for row in solutions)
        domains = narrowed
    if any(len(choices) != 1 for choices in domains):
        raise ValueError('ambiguous nearby position classes')
    candidate_keys = [next(iter(choices)) for choices in domains]
    candidates = defaultdict(list)
    for triangle in triangles:
        candidates[face_key(triangle, candidate_keys)].append(triangle)
    if any(len(rows) > 64 for rows in candidates.values()):
        raise ValueError('excessive overlapping faces prevent bounded correspondence')
    if original_faces != Counter({key: len(rows) for key, rows in candidates.items()}):
        raise ValueError('candidate topology differs from original transport')
    assignments = [None] * len(source_points)
    used = set()
    def assign(original, candidate):
        if math.dist(source_points[original], points[candidate]) > tolerance:
            raise ValueError('triangle corner position differs from original')
        actual = weights[candidate]
        previous = assignments[original]
        if previous is not None and any(abs(previous.get(k, 0) - actual.get(k, 0)) > 1e-5
                                        for k in previous.keys() | actual.keys()):
            raise ValueError('ambiguous seam/coincident surface weights')
        assignments[original] = actual
        used.add(candidate)
    for triangle in source_triangles:
        for original in triangle:
            for candidate_triangle in candidates[face_key(triangle, source_keys)]:
                for candidate in candidate_triangle:
                    if source_keys[original] == candidate_keys[candidate]:
                        assign(original, candidate)
    # Loose vertices have no incident face to disambiguate them. Preserve the
    # strict position/weight rule there, and reject added unattached geometry.
    loose = defaultdict(list)
    referenced = {i for t in triangles for i in t}
    for candidate in set(range(len(points))) - referenced:
        loose[candidate_keys[candidate]].append(candidate)
    for original, row in enumerate(assignments):
        if row is None:
            for candidate in loose[source_keys[original]]:
                assign(original, candidate)
            if assignments[original] is None:
                raise ValueError(f'correspondence failure at vertex {original}')
    if len(used) != len(points):
        raise ValueError('candidate added geometry; no exact correspondence')
    return assignments
