"""
Track segment builder for OverFlight.

Groups raw state_vectors rows into per-aircraft track segments with
simplified polylines. Supports both full rebuild and incremental builds.
"""

import json
import logging
import math
import time

from overflight.config import (
    RETENTION_HOURS,
    SIMPLIFICATION_EPSILON,
    TRACK_GAP_THRESHOLD_SECONDS,
)
from overflight.database.queries import EARTH_RADIUS_MILES, haversine_distance
from overflight.utils.simplify import simplify_track

logger = logging.getLogger(__name__)


def _classify_phase(points):
    """
    Classify a track segment's flight phase based on on_ground transitions.

    Args:
        points: List of dicts with at least 'on_ground' and 'altitude' keys.

    Returns:
        One of 'ground', 'departure', 'arrival', 'enroute'.
    """
    if not points:
        return "enroute"

    first_on_ground = bool(points[0]["on_ground"])
    last_on_ground = bool(points[-1]["on_ground"])

    all_on_ground = all(p["on_ground"] for p in points)
    all_airborne = all(not p["on_ground"] for p in points)

    if all_on_ground:
        return "ground"
    if all_airborne:
        return "enroute"
    if first_on_ground and not last_on_ground:
        return "departure"
    if not first_on_ground and last_on_ground:
        return "arrival"

    # Mixed — default to enroute
    return "enroute"


def _segment_points(rows):
    """
    Split a sorted list of state vector rows for a single aircraft
    into segments based on time gaps and ground/air transitions.

    Args:
        rows: List of dicts sorted by timestamp for one icao24.

    Yields:
        Lists of consecutive row dicts forming one segment each.
    """
    if not rows:
        return

    current_segment = [rows[0]]

    for i in range(1, len(rows)):
        prev = rows[i - 1]
        curr = rows[i]

        time_gap = curr["timestamp"] - prev["timestamp"]
        ground_change = bool(curr["on_ground"]) != bool(prev["on_ground"])

        if time_gap > TRACK_GAP_THRESHOLD_SECONDS or ground_change:
            yield current_segment
            current_segment = [curr]
        else:
            current_segment.append(curr)

    if current_segment:
        yield current_segment


def _build_segment_record(icao24, points):
    """
    Build a track_segments record from a list of state vector points.

    Args:
        icao24: Aircraft ICAO24 hex address.
        points: List of state vector row dicts for one segment.

    Returns:
        A tuple ready for database insertion, or None if too few points.
    """
    if len(points) < 2:
        return None

    phase = _classify_phase(points)

    # Build polyline as [(timestamp, lat, lon, altitude), ...]
    raw_polyline = [
        (p["timestamp"], p["latitude"], p["longitude"], p.get("altitude"))
        for p in points
    ]

    # Simplify the polyline
    simplified = simplify_track(raw_polyline, epsilon=SIMPLIFICATION_EPSILON)

    # Serialize as JSON
    polyline_json = json.dumps(simplified)

    # Time range
    start_time = points[0]["timestamp"]
    end_time = points[-1]["timestamp"]

    # Compute bounding box
    lats = [p["latitude"] for p in points]
    lons = [p["longitude"] for p in points]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    # Altitude range (filter out None values)
    altitudes = [p["altitude"] for p in points if p.get("altitude") is not None]
    min_altitude = min(altitudes) if altitudes else None
    max_altitude = max(altitudes) if altitudes else None

    # Average velocity
    velocities = [p["velocity"] for p in points if p.get("velocity") is not None]
    avg_velocity = sum(velocities) / len(velocities) if velocities else None

    # Average heading (circular mean)
    headings = [p["heading"] for p in points if p.get("heading") is not None]
    if headings:
        sin_sum = sum(math.sin(math.radians(h)) for h in headings)
        cos_sum = sum(math.cos(math.radians(h)) for h in headings)
        avg_heading = math.degrees(math.atan2(sin_sum, cos_sum)) % 360
    else:
        avg_heading = None

    # Callsign: use the most common non-null callsign
    callsigns = [p["callsign"] for p in points if p.get("callsign")]
    callsign = max(set(callsigns), key=callsigns.count) if callsigns else None

    return (
        icao24,
        callsign,
        phase,
        polyline_json,
        len(simplified),
        start_time,
        end_time,
        min_altitude,
        max_altitude,
        min_lat,
        max_lat,
        min_lon,
        max_lon,
        avg_velocity,
        avg_heading,
        int(time.time()),
    )


def build_tracks(conn, since_timestamp=None):
    """
    Build track segments from raw state vectors.

    Args:
        conn: SQLite connection to the flight database (must have
              both state_vectors and track_segments tables).
        since_timestamp: Only process state vectors newer than this
                        Unix timestamp. If None, process all rows.

    Returns:
        Number of track segments created.
    """
    cursor = conn.cursor()

    if since_timestamp is not None:
        cursor.execute(
            "SELECT * FROM state_vectors WHERE timestamp >= ? ORDER BY icao24, timestamp",
            (since_timestamp,),
        )
    else:
        cursor.execute(
            "SELECT * FROM state_vectors ORDER BY icao24, timestamp"
        )

    # Group rows by icao24
    aircraft_rows = {}
    for row in cursor.fetchall():
        row_dict = dict(row)
        icao24 = row_dict["icao24"]
        if icao24 not in aircraft_rows:
            aircraft_rows[icao24] = []
        aircraft_rows[icao24].append(row_dict)

    segments = []
    for icao24, rows in aircraft_rows.items():
        for segment_points in _segment_points(rows):
            record = _build_segment_record(icao24, segment_points)
            if record is not None:
                segments.append(record)

    if segments:
        conn.execute("BEGIN")
        try:
            conn.executemany("""
                INSERT INTO track_segments
                    (icao24, callsign, phase, polyline, point_count,
                     start_time, end_time, min_altitude, max_altitude,
                     min_lat, max_lat, min_lon, max_lon,
                     avg_velocity, avg_heading, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, segments)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    logger.info("Built %d track segments from %d aircraft", len(segments), len(aircraft_rows))
    return len(segments)


def build_tracks_incremental(conn):
    """
    Build track segments only for state vectors added since the last build.

    Uses the track_build_meta table to track the last-processed timestamp.

    Args:
        conn: SQLite connection to the flight database.

    Returns:
        Number of new track segments created.
    """
    cursor = conn.cursor()

    # Get last build timestamp
    cursor.execute(
        "SELECT value FROM track_build_meta WHERE key = 'last_build_timestamp'"
    )
    row = cursor.fetchone()
    last_build = int(row[0]) if row else None

    # Delete existing segments that overlap with the rebuild window
    # to avoid duplicates from partial segments being completed
    if last_build is not None:
        cursor.execute(
            "DELETE FROM track_segments WHERE end_time >= ?",
            (last_build,),
        )
        conn.commit()

    count = build_tracks(conn, since_timestamp=last_build)

    # Update last build timestamp
    now = int(time.time())
    cursor.execute(
        "INSERT OR REPLACE INTO track_build_meta (key, value) VALUES (?, ?)",
        ("last_build_timestamp", str(now)),
    )
    conn.commit()

    return count


def get_tracks_near(conn, lat, lon, radius_miles, start_time=None, end_time=None):
    """
    Query track segments that overlap a spatial and temporal window.

    Args:
        conn: SQLite connection to the flight database.
        lat: Center latitude in decimal degrees.
        lon: Center longitude in decimal degrees.
        radius_miles: Search radius in miles.
        start_time: Unix timestamp for window start. Defaults to 24h ago.
        end_time: Unix timestamp for window end. Defaults to now.

    Returns:
        List of track segment dicts with parsed polylines.
    """
    if start_time is None:
        start_time = int(time.time()) - (RETENTION_HOURS * 3600)
    if end_time is None:
        end_time = int(time.time())

    # Compute bounding box for the spatial filter
    lat_delta = radius_miles / 69.0
    lon_delta = radius_miles / (69.0 * math.cos(math.radians(lat)))
    bbox_min_lat = lat - lat_delta
    bbox_max_lat = lat + lat_delta
    bbox_min_lon = lon - lon_delta
    bbox_max_lon = lon + lon_delta

    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM track_segments
        WHERE end_time >= ? AND start_time <= ?
          AND max_lat >= ? AND min_lat <= ?
          AND max_lon >= ? AND min_lon <= ?
    """, (start_time, end_time,
          bbox_min_lat, bbox_max_lat,
          bbox_min_lon, bbox_max_lon))

    results = []
    for row in cursor.fetchall():
        track = dict(row)
        track["polyline"] = json.loads(track["polyline"])
        results.append(track)

    return results


def get_track_density(conn, lat, lon, radius_miles, start_time=None, end_time=None):
    """
    Count track segments matching spatial and temporal criteria.

    Args:
        conn: SQLite connection to the flight database.
        lat: Center latitude.
        lon: Center longitude.
        radius_miles: Search radius in miles.
        start_time: Unix timestamp for window start. Defaults to 24h ago.
        end_time: Unix timestamp for window end. Defaults to now.

    Returns:
        Count of matching track segments.
    """
    if start_time is None:
        start_time = int(time.time()) - (RETENTION_HOURS * 3600)
    if end_time is None:
        end_time = int(time.time())

    lat_delta = radius_miles / 69.0
    lon_delta = radius_miles / (69.0 * math.cos(math.radians(lat)))
    bbox_min_lat = lat - lat_delta
    bbox_max_lat = lat + lat_delta
    bbox_min_lon = lon - lon_delta
    bbox_max_lon = lon + lon_delta

    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM track_segments
        WHERE end_time >= ? AND start_time <= ?
          AND max_lat >= ? AND min_lat <= ?
          AND max_lon >= ? AND min_lon <= ?
    """, (start_time, end_time,
          bbox_min_lat, bbox_max_lat,
          bbox_min_lon, bbox_max_lon))

    return cursor.fetchone()[0]
