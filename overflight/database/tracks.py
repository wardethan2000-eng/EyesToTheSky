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
from overflight.database.behavior import segment_indicates_flight
from overflight.utils.simplify import simplify_track

logger = logging.getLogger(__name__)

MIN_GROUND_POINTS_FOR_TRANSITION = 2
MIN_DEPARTURE_CLIMB_FPM = 500.0


def _safe_lon_delta(lat, radius_miles):
    """Return longitude delta while avoiding divide-by-zero near poles."""
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-6:
        cos_lat = 1e-6 if cos_lat >= 0 else -1e-6
    return radius_miles / (69.0 * cos_lat)


def _estimate_heading(point_a, point_b):
    """Estimate heading in degrees from point_a to point_b in lat/lon space."""
    if not point_a or not point_b:
        return None
    d_lon = point_b["longitude"] - point_a["longitude"]
    d_lat = point_b["latitude"] - point_a["latitude"]
    if abs(d_lon) < 1e-9 and abs(d_lat) < 1e-9:
        return None
    heading = math.degrees(math.atan2(d_lon, d_lat)) % 360
    return heading


def _max_climb_rate_fpm(points, liftoff_idx):
    """
    Estimate the strongest post-liftoff climb rate in feet per minute.

    Altitudes in state_vectors are meters, so we convert m/s to ft/min.
    """
    if liftoff_idx < 0 or liftoff_idx >= len(points):
        return None

    start_point = points[liftoff_idx]
    start_alt = start_point.get("altitude")
    start_ts = start_point.get("timestamp")
    if start_alt is None or start_ts is None:
        return None

    max_rate = None
    max_scan_idx = min(len(points), liftoff_idx + 6)
    for idx in range(liftoff_idx + 1, max_scan_idx):
        p = points[idx]
        if p.get("on_ground"):
            break
        alt = p.get("altitude")
        ts = p.get("timestamp")
        if alt is None or ts is None or ts <= start_ts:
            continue

        rate_m_per_sec = (alt - start_alt) / float(ts - start_ts)
        rate_fpm = rate_m_per_sec * 196.850394
        if max_rate is None or rate_fpm > max_rate:
            max_rate = rate_fpm

    return max_rate


def _classify_phase_and_metadata(points):
    """
    Classify a track segment's flight phase based on on_ground transitions.

    Args:
        points: List of dicts with at least 'on_ground' and 'altitude' keys.

    Returns:
        Tuple of (phase, metadata_dict).
    """
    metadata = {
        "liftoff_lat": None,
        "liftoff_lon": None,
        "liftoff_heading": None,
        "liftoff_time": None,
        "touchdown_lat": None,
        "touchdown_lon": None,
        "approach_heading": None,
        "touchdown_time": None,
    }

    if not points:
        return "enroute", metadata

    first_on_ground = bool(points[0]["on_ground"])
    last_on_ground = bool(points[-1]["on_ground"])

    all_on_ground = all(p["on_ground"] for p in points)
    all_airborne = all(not p["on_ground"] for p in points)

    if all_on_ground:
        return "ground", metadata
    if all_airborne:
        return "enroute", metadata
    def _first_last_altitude(rows):
        alts = [p.get("altitude") for p in rows if p.get("altitude") is not None]
        if len(alts) < 2:
            return None, None
        return alts[0], alts[-1]

    start_alt, end_alt = _first_last_altitude(points)

    # Detect transitions once so we can enrich the chosen phase.
    dep_idx = None
    arr_idx = None
    for i in range(1, len(points)):
        prev_ground = bool(points[i - 1]["on_ground"])
        curr_ground = bool(points[i]["on_ground"])
        if dep_idx is None and prev_ground and not curr_ground:
            dep_idx = i
        if arr_idx is None and (not prev_ground) and curr_ground:
            arr_idx = i

    if first_on_ground and not last_on_ground and dep_idx is not None:
        ground_prefix = points[:dep_idx]
        if len(ground_prefix) >= MIN_GROUND_POINTS_FOR_TRANSITION and all(p["on_ground"] for p in ground_prefix):
            climb_rate_fpm = _max_climb_rate_fpm(points, dep_idx)
            if climb_rate_fpm is not None and climb_rate_fpm >= MIN_DEPARTURE_CLIMB_FPM:
                liftoff = points[dep_idx]
                heading = liftoff.get("heading")
                if heading is None and dep_idx + 1 < len(points):
                    heading = _estimate_heading(liftoff, points[dep_idx + 1])
                metadata.update({
                    "liftoff_lat": liftoff["latitude"],
                    "liftoff_lon": liftoff["longitude"],
                    "liftoff_heading": heading,
                    "liftoff_time": liftoff["timestamp"],
                })
                return "departure", metadata

        # Fallback keeps backward-compatible departure classification.
        if start_alt is None or end_alt is None or end_alt >= start_alt:
            return "departure", metadata

    if (not first_on_ground) and last_on_ground and arr_idx is not None:
        ground_suffix = points[arr_idx:]
        airborne_prefix = points[:arr_idx]
        if (
            len(ground_suffix) >= MIN_GROUND_POINTS_FOR_TRANSITION
            and airborne_prefix
            and all(p["on_ground"] for p in ground_suffix)
        ):
            touchdown = points[arr_idx]
            approach_heading = None
            if arr_idx - 1 >= 0:
                prior = points[arr_idx - 1]
                approach_heading = prior.get("heading")
                if approach_heading is None:
                    approach_heading = _estimate_heading(prior, touchdown)

            metadata.update({
                "touchdown_lat": touchdown["latitude"],
                "touchdown_lon": touchdown["longitude"],
                "approach_heading": approach_heading,
                "touchdown_time": touchdown["timestamp"],
            })

            # Require a net descent when altitude data is available.
            if start_alt is None or end_alt is None or end_alt <= start_alt:
                return "arrival", metadata

        # Fallback keeps backward-compatible arrival classification.
        if start_alt is None or end_alt is None or end_alt <= start_alt:
            return "arrival", metadata

    # Mixed — default to enroute
    return "enroute", metadata


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
    transition_count = 0

    for i in range(1, len(rows)):
        prev = rows[i - 1]
        curr = rows[i]

        time_gap = curr["timestamp"] - prev["timestamp"]
        ground_change = bool(curr["on_ground"]) != bool(prev["on_ground"])

        if time_gap > TRACK_GAP_THRESHOLD_SECONDS:
            yield current_segment
            current_segment = [curr]
            transition_count = 0
            continue

        # Keep a single ground/air transition in one segment so phases like
        # departure/arrival can be represented. If a second transition appears,
        # start a new segment that includes both sides of the new transition.
        if ground_change:
            if transition_count >= 1:
                yield current_segment
                current_segment = [prev, curr]
                transition_count = 1
            else:
                current_segment.append(curr)
                transition_count = 1
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

    # Ignore powered-on ground traffic that never shows flight behavior.
    if not segment_indicates_flight(points):
        return None

    phase, phase_meta = _classify_phase_and_metadata(points)

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
        phase_meta.get("liftoff_lat"),
        phase_meta.get("liftoff_lon"),
        phase_meta.get("liftoff_heading"),
        phase_meta.get("liftoff_time"),
        phase_meta.get("touchdown_lat"),
        phase_meta.get("touchdown_lon"),
        phase_meta.get("approach_heading"),
        phase_meta.get("touchdown_time"),
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

    if since_timestamp is None:
        # A full rebuild must replace the existing derived table rather than
        # append to it, otherwise repeated maintenance rebuilds accumulate
        # duplicate segments and playback startup cost grows over time.
        cursor.execute("DELETE FROM track_segments")
        conn.commit()

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
                     avg_velocity, avg_heading,
                     liftoff_lat, liftoff_lon, liftoff_heading, liftoff_time,
                     touchdown_lat, touchdown_lon, approach_heading, touchdown_time,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, segments)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    logger.info("Built %d track segments from %d aircraft", len(segments), len(aircraft_rows))
    return len(segments)


def build_tracks_for_aircraft(conn, icao24_list):
    """
    Build track segments for a specific set of aircraft using their full history.

    Args:
        conn: SQLite connection to the flight database.
        icao24_list: Iterable of ICAO24 strings to rebuild.

    Returns:
        Number of track segments created.
    """
    aircraft = sorted({icao.lower() for icao in icao24_list if icao})
    if not aircraft:
        return 0

    placeholders = ",".join(["?"] * len(aircraft))
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT * FROM state_vectors WHERE icao24 IN ({placeholders}) ORDER BY icao24, timestamp",
        tuple(aircraft),
    )

    aircraft_rows = {}
    for row in cursor.fetchall():
        row_dict = dict(row)
        icao24 = row_dict["icao24"]
        aircraft_rows.setdefault(icao24, []).append(row_dict)

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
                     avg_velocity, avg_heading,
                     liftoff_lat, liftoff_lon, liftoff_heading, liftoff_time,
                     touchdown_lat, touchdown_lon, approach_heading, touchdown_time,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, segments)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    logger.info("Built %d track segments for %d aircraft", len(segments), len(aircraft_rows))
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

    # Get last build timestamp.
    cursor.execute(
        "SELECT value FROM track_build_meta WHERE key = 'last_build_timestamp'"
    )
    row = cursor.fetchone()
    last_build = int(row[0]) if row else None

    # Rebuild with a small overlap so slightly late-arriving reports are not
    # permanently skipped.
    overlap_seconds = TRACK_GAP_THRESHOLD_SECONDS * 2
    since_timestamp = None
    if last_build is not None:
        since_timestamp = max(0, last_build - overlap_seconds)

    if since_timestamp is None:
        count = build_tracks(conn, since_timestamp=None)
    else:
        cursor.execute(
            "SELECT DISTINCT icao24 FROM state_vectors WHERE timestamp >= ?",
            (since_timestamp,),
        )
        impacted_aircraft = [row[0] for row in cursor.fetchall()]

        if impacted_aircraft:
            placeholders = ",".join(["?"] * len(impacted_aircraft))
            cursor.execute(
                f"DELETE FROM track_segments WHERE icao24 IN ({placeholders})",
                tuple(impacted_aircraft),
            )
            conn.commit()
            count = build_tracks_for_aircraft(conn, impacted_aircraft)
        else:
            count = 0

    # Update metadata to max processed event timestamp, not wall-clock time.
    cursor.execute("SELECT MAX(timestamp) FROM state_vectors")
    max_ts_row = cursor.fetchone()
    max_ts = int(max_ts_row[0]) if max_ts_row and max_ts_row[0] is not None else last_build

    if max_ts is None:
        max_ts = int(time.time())

    cursor.execute(
        "INSERT OR REPLACE INTO track_build_meta (key, value) VALUES (?, ?)",
        ("last_build_timestamp", str(max_ts)),
    )
    conn.commit()

    return count


def get_tracks_near(
    conn,
    lat,
    lon,
    radius_miles,
    start_time=None,
    end_time=None,
    min_alt=0,
    phases=None,
):
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
    lon_delta = _safe_lon_delta(lat, radius_miles)
    bbox_min_lat = lat - lat_delta
    bbox_max_lat = lat + lat_delta
    bbox_min_lon = lon - lon_delta
    bbox_max_lon = lon + lon_delta

    params = [
        start_time,
        end_time,
        bbox_min_lat,
        bbox_max_lat,
        bbox_min_lon,
        bbox_max_lon,
    ]
    query = """
        SELECT * FROM track_segments
        WHERE end_time >= ? AND start_time <= ?
          AND max_lat >= ? AND min_lat <= ?
          AND max_lon >= ? AND min_lon <= ?
    """

    if min_alt and min_alt > 0:
        query += " AND COALESCE(max_altitude, 0) >= ?"
        params.append(min_alt)

    if phases:
        placeholders = ",".join(["?"] * len(phases))
        query += f" AND phase IN ({placeholders})"
        params.extend(phases)

    cursor = conn.cursor()
    cursor.execute(query, tuple(params))

    results = []
    for row in cursor.fetchall():
        track = dict(row)
        track["polyline"] = json.loads(track["polyline"])
        results.append(track)

    return results


def get_track_density(
    conn,
    lat,
    lon,
    radius_miles,
    start_time=None,
    end_time=None,
    min_alt=0,
    phases=None,
):
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
    lon_delta = _safe_lon_delta(lat, radius_miles)
    bbox_min_lat = lat - lat_delta
    bbox_max_lat = lat + lat_delta
    bbox_min_lon = lon - lon_delta
    bbox_max_lon = lon + lon_delta

    params = [
        start_time,
        end_time,
        bbox_min_lat,
        bbox_max_lat,
        bbox_min_lon,
        bbox_max_lon,
    ]
    query = """
        SELECT COUNT(*) FROM track_segments
        WHERE end_time >= ? AND start_time <= ?
          AND max_lat >= ? AND min_lat <= ?
          AND max_lon >= ? AND min_lon <= ?
    """

    if min_alt and min_alt > 0:
        query += " AND COALESCE(max_altitude, 0) >= ?"
        params.append(min_alt)

    if phases:
        placeholders = ",".join(["?"] * len(phases))
        query += f" AND phase IN ({placeholders})"
        params.extend(phases)

    cursor = conn.cursor()
    cursor.execute(query, tuple(params))

    return cursor.fetchone()[0]


def get_unique_aircraft_count(
    conn,
    lat,
    lon,
    radius_miles,
    start_time=None,
    end_time=None,
    min_alt=0,
    phases=None,
):
    """Count unique ICAO24 values matching spatial/temporal filters."""
    if start_time is None:
        start_time = int(time.time()) - (RETENTION_HOURS * 3600)
    if end_time is None:
        end_time = int(time.time())

    lat_delta = radius_miles / 69.0
    lon_delta = _safe_lon_delta(lat, radius_miles)
    bbox_min_lat = lat - lat_delta
    bbox_max_lat = lat + lat_delta
    bbox_min_lon = lon - lon_delta
    bbox_max_lon = lon + lon_delta

    params = [
        start_time,
        end_time,
        bbox_min_lat,
        bbox_max_lat,
        bbox_min_lon,
        bbox_max_lon,
    ]
    query = """
        SELECT COUNT(DISTINCT icao24) FROM track_segments
        WHERE end_time >= ? AND start_time <= ?
          AND max_lat >= ? AND min_lat <= ?
          AND max_lon >= ? AND min_lon <= ?
    """

    if min_alt and min_alt > 0:
        query += " AND COALESCE(max_altitude, 0) >= ?"
        params.append(min_alt)

    if phases:
        placeholders = ",".join(["?"] * len(phases))
        query += f" AND phase IN ({placeholders})"
        params.extend(phases)

    cursor = conn.cursor()
    cursor.execute(query, tuple(params))
    return cursor.fetchone()[0]


def get_track_time_bounds(
    conn,
    lat,
    lon,
    radius_miles,
    start_time=None,
    end_time=None,
    min_alt=0,
    phases=None,
):
    """Return the earliest start and latest end across matching track segments."""
    if start_time is None:
        start_time = int(time.time()) - (RETENTION_HOURS * 3600)
    if end_time is None:
        end_time = int(time.time())

    lat_delta = radius_miles / 69.0
    lon_delta = _safe_lon_delta(lat, radius_miles)
    bbox_min_lat = lat - lat_delta
    bbox_max_lat = lat + lat_delta
    bbox_min_lon = lon - lon_delta
    bbox_max_lon = lon + lon_delta

    params = [
        start_time,
        end_time,
        bbox_min_lat,
        bbox_max_lat,
        bbox_min_lon,
        bbox_max_lon,
    ]
    query = """
        SELECT MIN(start_time), MAX(end_time) FROM track_segments
        WHERE end_time >= ? AND start_time <= ?
          AND max_lat >= ? AND min_lat <= ?
          AND max_lon >= ? AND min_lon <= ?
    """

    if min_alt and min_alt > 0:
        query += " AND COALESCE(max_altitude, 0) >= ?"
        params.append(min_alt)

    if phases:
        placeholders = ",".join(["?"] * len(phases))
        query += f" AND phase IN ({placeholders})"
        params.extend(phases)

    cursor = conn.cursor()
    cursor.execute(query, tuple(params))
    row = cursor.fetchone()
    if not row or row[0] is None or row[1] is None:
        return (None, None)
    return (int(row[0]), int(row[1]))
