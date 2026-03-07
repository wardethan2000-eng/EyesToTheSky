"""
Ramer-Douglas-Peucker polyline simplification for geographic track data.

Reduces the number of points in a track while preserving significant
shape features like turns and altitude changes.
"""

import math


def _perpendicular_distance(point, line_start, line_end):
    """
    Calculate the perpendicular distance from a point to a line segment
    defined by line_start and line_end, in lat/lon space.

    Points are (timestamp, lat, lon, altitude) tuples. Distance is
    computed using lat/lon coordinates only.
    """
    # Extract lat/lon (indices 1 and 2)
    px, py = point[1], point[2]
    sx, sy = line_start[1], line_start[2]
    ex, ey = line_end[1], line_end[2]

    dx = ex - sx
    dy = ey - sy

    # If the line segment has zero length, return distance to the start point
    length_sq = dx * dx + dy * dy
    if length_sq == 0:
        return math.sqrt((px - sx) ** 2 + (py - sy) ** 2)

    # Project point onto the line and compute perpendicular distance
    t = max(0, min(1, ((px - sx) * dx + (py - sy) * dy) / length_sq))
    proj_x = sx + t * dx
    proj_y = sy + t * dy

    return math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)


def simplify_track(points, epsilon=0.001):
    """
    Simplify a track polyline using the Ramer-Douglas-Peucker algorithm.

    Args:
        points: List of (timestamp, lat, lon, altitude) tuples, ordered by time.
        epsilon: Tolerance in degrees (~0.001 deg ≈ 111 meters). Points
                 closer than this to the simplified line are removed.

    Returns:
        Simplified list retaining only significant points.
        Always retains the first and last points.
    """
    if len(points) <= 2:
        return list(points)

    # Find the point with the maximum distance from the line between first and last
    max_dist = 0
    max_idx = 0
    for i in range(1, len(points) - 1):
        dist = _perpendicular_distance(points[i], points[0], points[-1])
        if dist > max_dist:
            max_dist = dist
            max_idx = i

    # If the max distance exceeds epsilon, recursively simplify
    if max_dist > epsilon:
        left = simplify_track(points[: max_idx + 1], epsilon)
        right = simplify_track(points[max_idx:], epsilon)
        # Combine, avoiding duplicate of the split point
        return left[:-1] + right
    else:
        # All intermediate points are within tolerance — keep only endpoints
        return [points[0], points[-1]]
