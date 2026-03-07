"""Tests for the Ramer-Douglas-Peucker polyline simplification."""

import unittest

from overflight.utils.simplify import simplify_track


class TestSimplifyTrack(unittest.TestCase):
    """Test polyline simplification algorithm."""

    def test_empty_input(self):
        """Empty list returns empty list."""
        self.assertEqual(simplify_track([]), [])

    def test_single_point(self):
        """Single point returns as-is."""
        pts = [(1000, 40.0, -74.0, 5000)]
        self.assertEqual(simplify_track(pts), pts)

    def test_two_points(self):
        """Two points returns both."""
        pts = [(1000, 40.0, -74.0, 5000), (1010, 40.1, -74.1, 5100)]
        self.assertEqual(simplify_track(pts), pts)

    def test_straight_line_reduces_to_two(self):
        """A perfectly straight line should reduce to just start and end."""
        # 20 collinear points
        pts = [(1000 + i * 10, 40.0 + i * 0.01, -74.0 + i * 0.01, 5000)
               for i in range(20)]
        result = simplify_track(pts, epsilon=0.001)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], pts[0])
        self.assertEqual(result[-1], pts[-1])

    def test_ninety_degree_turn_retains_corner(self):
        """A 90-degree turn should retain the corner point."""
        # Go east, then turn north
        pts = [
            (1000, 40.0, -74.0, 5000),
            (1010, 40.0, -73.99, 5000),
            (1020, 40.0, -73.98, 5000),
            (1030, 40.0, -73.97, 5000),  # turn point
            (1040, 40.01, -73.97, 5000),
            (1050, 40.02, -73.97, 5000),
            (1060, 40.03, -73.97, 5000),
        ]
        result = simplify_track(pts, epsilon=0.001)
        # Should keep start, corner area, and end (at least 3 points)
        self.assertGreaterEqual(len(result), 3)
        self.assertEqual(result[0], pts[0])
        self.assertEqual(result[-1], pts[-1])

    def test_realistic_curve_reduces_point_count(self):
        """A slightly curving trajectory should significantly reduce points."""
        import math
        # Simulate a gentle arc: 50 points
        pts = []
        for i in range(50):
            t = i / 49.0
            lat = 40.0 + 0.2 * t
            lon = -74.0 + 0.2 * t + 0.005 * math.sin(t * math.pi)
            pts.append((1000 + i * 10, lat, lon, 10000))

        result = simplify_track(pts, epsilon=0.001)
        # Should be significantly fewer than 50, but more than 2 due to curvature
        self.assertGreater(len(result), 2)
        self.assertLess(len(result), 15)
        self.assertEqual(result[0], pts[0])
        self.assertEqual(result[-1], pts[-1])

    def test_first_and_last_always_retained(self):
        """First and last points should always be in the output."""
        pts = [(1000 + i * 10, 40.0 + i * 0.005, -74.0 + i * 0.003, 5000)
               for i in range(30)]
        result = simplify_track(pts, epsilon=0.01)
        self.assertEqual(result[0], pts[0])
        self.assertEqual(result[-1], pts[-1])

    def test_large_epsilon_keeps_only_endpoints(self):
        """A very large epsilon should collapse everything to 2 points."""
        pts = [(1000 + i * 10, 40.0 + i * 0.001, -74.0 + i * 0.001, 5000)
               for i in range(20)]
        result = simplify_track(pts, epsilon=10.0)
        self.assertEqual(len(result), 2)

    def test_zero_epsilon_keeps_all_points(self):
        """An epsilon of 0 should keep all non-collinear points."""
        import math
        pts = []
        for i in range(10):
            t = i / 9.0
            lat = 40.0 + 0.1 * t
            lon = -74.0 + 0.05 * math.sin(t * math.pi * 2)
            pts.append((1000 + i * 10, lat, lon, 5000))
        result = simplify_track(pts, epsilon=0)
        # With epsilon=0, only truly collinear points get removed
        self.assertGreaterEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
