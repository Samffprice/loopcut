"""Regression: coincident original vertices are not necessarily export seams."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "addons_core"))
from loopcut.rig_correspondence import transfer_weights


class CorrespondenceChecks(unittest.TestCase):
    def test_triangle_context_resolves_nearby_classes_even_when_nearest_is_wrong(self):
        tolerance = 1.
        points = [(0, 0, 0), (1.01, 0, 0), (0, 10, 0), (0, 0, 10), (0, -10, 0), (0, 0, -10)]
        faces = [(0, 2, 3), (1, 4, 5)]
        candidate = [(1., 0, 0), *points[1:]]  # Nearer to source 1; its triangle proves source 0.
        weights = [{'a': 1.}, {'b': 1.}, {'a': 1.}, {'a': 1.}, {'b': 1.}, {'b': 1.}]
        for triangles in (faces, list(reversed(faces))):
            self.assertEqual(transfer_weights(points, faces, candidate, triangles, weights, tolerance), weights)

    def test_shared_triangle_context_does_not_guess_between_nearby_classes(self):
        points = [(0, 0, 0), (1.01, 0, 0), (0, 10, 0), (0, 0, 10)]
        faces = [(0, 2, 3), (1, 2, 3)]
        candidate = [(.5, 0, 0), (.51, 0, 0), *points[2:]]
        with self.assertRaisesRegex(ValueError, 'ambiguous nearby position classes'):
            transfer_weights(points, faces, candidate, faces, [{'a': 1.}] * 4, 1.)

    def test_m2_tiny_character_ambiguous_nearby_classes_refuse(self):
        # B8's eval transport has two distinct representatives whose tolerance
        # spheres overlap. Choosing the nearest candidate would hide ambiguity.
        points = [(7.681134450857312e-05, -.0008397981336757954, .014202971438997125),
                  (7.68113631350242e-05, -.0008397981336757954, .014202992896668765)]
        candidate = [(7.681136048631743e-05, -.0008397981291636825, .014202992431819439)]
        with self.assertRaisesRegex(ValueError, 'ambiguous nearby position classes'):
            transfer_weights(points, [], candidate, [], [{'a': 1.}], 2.1181742294645564e-8)

    def test_sub_tolerance_import_drift_across_rounding_boundary(self):
        points = [(1.9999999e-6, 0, 0), (1, 0, 0), (0, 1, 0)]
        candidate = [(2.0000001e-6, 0, 0), (1, 0, 0), (0, 1, 0)]
        faces, weights = [(0, 1, 2)], [{'a': 1.}] * 3
        self.assertEqual(transfer_weights(points, faces, candidate, faces, weights, 1e-6), weights)
        candidate[0] = (4e-6, 0, 0)
        with self.assertRaisesRegex(ValueError, 'position'):
            transfer_weights(points, faces, candidate, faces, weights, 1e-6)

    def test_distinct_coincident_vertices_survive_reorder(self):
        points = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (-1, 0, 0)]
        triangles = [(0, 1, 2), (3, 2, 4)]
        weights = [{'left': 1.}, {'left': 1.}, {'left': 1.}, {'right': 1.}, {'right': 1.}]
        order = [4, 3, 2, 1, 0]
        mapping = {old: new for new, old in enumerate(order)}
        result = transfer_weights(points, triangles, [points[i] for i in order],
                                  [tuple(mapping[i] for i in t) for t in reversed(triangles)],
                                  [weights[i] for i in order], 1e-6)
        self.assertEqual(result, weights)

    def test_split_requires_agreement_and_topology(self):
        points = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (-1, 0, 0)]
        triangles = [(0, 1, 2), (0, 2, 3)]
        split = points + [points[0]]
        faces = [(0, 1, 2), (4, 2, 3)]
        weights = [{'a': 1.} for _ in split]
        self.assertEqual(transfer_weights(points, triangles, split, faces, weights, 1e-6), weights[:4])
        weights[4] = {'b': 1.}
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            transfer_weights(points, triangles, split, faces, weights, 1e-6)
        with self.assertRaisesRegex(ValueError, 'topology'):
            transfer_weights(points, triangles, split, faces[:1], weights, 1e-6)

    def test_identical_overlapping_faces_remain_ambiguous(self):
        points = [(0, 0, 0), (1, 0, 0), (0, 1, 0)] * 2
        faces = [(0, 1, 2), (3, 4, 5)]
        weights = [{'a': 1.}] * 3 + [{'b': 1.}] * 3
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            transfer_weights(points, faces, points, faces, weights, 1e-6)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            transfer_weights(points, faces * 65, points, faces * 65, weights, 1e-6)


if __name__ == '__main__':
    unittest.main()
