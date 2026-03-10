"""
OpenSky Network API polling script.

Continuously fetches aircraft state vectors from the OpenSky REST API
and writes them to the flight positions database.
"""

import logging
import sqlite3
import time
from typing import Optional

import requests

from overflight.config import (
    DB_PATH,
    INGEST_BBOX,
    OPENSKY_BASE_URL,
    OPENSKY_PASSWORD,
    OPENSKY_USERNAME,
    POLL_INTERVAL_SECONDS,
)
from overflight.database.schema import init_flight_db

logger = logging.getLogger(__name__)

STATES_ENDPOINT = f"{OPENSKY_BASE_URL}/states/all"

# OpenSky state vector field indices
# See: https://openskynetwork.github.io/opensky-api/rest.html
IDX_ICAO24 = 0
IDX_CALLSIGN = 1
IDX_ORIGIN_COUNTRY = 2
IDX_TIME_POSITION = 3
IDX_LAST_CONTACT = 4
IDX_LONGITUDE = 5
IDX_LATITUDE = 6
IDX_BARO_ALTITUDE = 7
IDX_ON_GROUND = 8
IDX_VELOCITY = 9
IDX_TRUE_TRACK = 10
IDX_VERTICAL_RATE = 11
IDX_SENSORS = 12
IDX_GEO_ALTITUDE = 13
IDX_SQUAWK = 14
IDX_SPI = 15
IDX_POSITION_SOURCE = 16


def _get_auth():
    """Return auth tuple if credentials are configured, else None."""
    if OPENSKY_USERNAME and OPENSKY_PASSWORD:
        return (OPENSKY_USERNAME, OPENSKY_PASSWORD)
    return None


def fetch_state_vectors(bbox=None, at_time=None):
    """
    Fetch aircraft state vectors from OpenSky API.

    Args:
        bbox: Optional (min_lat, max_lat, min_lon, max_lon) to filter results.
        at_time: Optional Unix timestamp to fetch historical positions at that
                 moment in time.  Requires authenticated credentials.
                 OpenSky retains historical state data for ~30 days for
                 registered users.

    Returns:
        List of state vector arrays, or empty list on error.
    """
    params = {}
    if bbox:
        min_lat, max_lat, min_lon, max_lon = bbox
        params = {
            "lamin": min_lat,
            "lamax": max_lat,
            "lomin": min_lon,
            "lomax": max_lon,
        }
    if at_time is not None:
        params["time"] = int(at_time)

    try:
        response = requests.get(
            STATES_ENDPOINT,
            params=params,
            auth=_get_auth(),
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        states = data.get("states", [])
        if states is None:
            return []
        return states

    except requests.exceptions.Timeout:
        logger.warning("OpenSky API request timed out")
        return []
    except requests.exceptions.ConnectionError:
        logger.warning("Failed to connect to OpenSky API")
        return []
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 400 and at_time is not None:
            raise ValueError(
                "OpenSky rejected the historical states/all request. Standard REST access only supports "
                "queries up to 1 hour in the past. For a full prior day, capture data live over time or "
                "use OpenSky's historical interfaces such as Trino."
            ) from e
        if status == 401:
            raise PermissionError(
                "OpenSky returned 401 Unauthorized. Check that:\n"
                "  1. OPENSKY_USERNAME is your account USERNAME (not your email address).\n"
                "     Log in to opensky-network.org and check your profile for the username field.\n"
                "  2. OPENSKY_PASSWORD is correct.\n"
                "  3. Historical requests now require valid authenticated API access; OpenSky is migrating to OAuth2 client credentials."
            ) from e
        if status == 403:
            raise PermissionError(
                "OpenSky returned 403 Forbidden. Check that your OpenSky API access is enabled and that "
                "you are using currently supported credentials. OpenSky is migrating authenticated REST "
                "access to OAuth2 client credentials."
            ) from e
        logger.warning("OpenSky API HTTP error: %s", e)
        return []
    except (ValueError, KeyError) as e:
        logger.warning("Failed to parse OpenSky response: %s", e)
        return []


def parse_state_vector(sv):
    """
    Parse a single OpenSky state vector array into a database row tuple.

    Returns None if the state vector is missing required fields (icao24, position).
    """
    try:
        icao24 = sv[IDX_ICAO24]
        if not icao24:
            return None

        latitude = sv[IDX_LATITUDE]
        longitude = sv[IDX_LONGITUDE]
        if latitude is None or longitude is None:
            return None

        callsign = sv[IDX_CALLSIGN]
        if callsign:
            callsign = callsign.strip()

        origin_country = sv[IDX_ORIGIN_COUNTRY]
        if origin_country:
            origin_country = origin_country.strip()

        altitude = sv[IDX_BARO_ALTITUDE]  # may be None for on-ground
        velocity = sv[IDX_VELOCITY]
        heading = sv[IDX_TRUE_TRACK]
        vertical_rate = sv[IDX_VERTICAL_RATE]
        geo_altitude = sv[IDX_GEO_ALTITUDE]
        squawk = sv[IDX_SQUAWK]
        if squawk:
            squawk = squawk.strip()
        spi = 1 if sv[IDX_SPI] else 0
        position_source = sv[IDX_POSITION_SOURCE]
        on_ground = 1 if sv[IDX_ON_GROUND] else 0
        timestamp = sv[IDX_TIME_POSITION] or sv[IDX_LAST_CONTACT] or int(time.time())

        return (
            icao24.lower().strip(),
            callsign or None,
            latitude,
            longitude,
            altitude,
            velocity,
            heading,
            vertical_rate,
            on_ground,
            int(timestamp),
            origin_country or None,
            squawk or None,
            geo_altitude,
            spi,
            position_source,
        )

    except (IndexError, TypeError) as e:
        logger.debug("Failed to parse state vector: %s", e)
        return None


def insert_state_vectors(conn, rows, has_spatialite=False):
    """
    Batch-insert parsed state vector rows into the database.

    Args:
        conn: SQLite connection to the flight database.
        rows: List of tuples from parse_state_vector.
        has_spatialite: Whether to also set the geometry column.

    Returns:
        Number of rows inserted.
    """
    if not rows:
        return 0

    normalized_rows = []
    for row in rows:
        if len(row) == 10:
            normalized_rows.append(row + (None, None, None, 0, None))
        elif len(row) == 15:
            normalized_rows.append(row)
        else:
            raise ValueError(f"State vector row must have 10 or 15 values, got {len(row)}")

    cursor = conn.cursor()

    if has_spatialite:
        cursor.executemany("""
            INSERT INTO state_vectors
                (icao24, callsign, latitude, longitude, altitude,
                 velocity, heading, vertical_rate, on_ground, timestamp,
                 origin_country, squawk, geo_altitude, spi, position_source, geom)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    MakePoint(?, ?, 4326))
        """, [row + (row[3], row[2]) for row in normalized_rows])  # lon, lat for MakePoint
    else:
        cursor.executemany("""
            INSERT INTO state_vectors
                (icao24, callsign, latitude, longitude, altitude,
                 velocity, heading, vertical_rate, on_ground, timestamp,
                 origin_country, squawk, geo_altitude, spi, position_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, normalized_rows)

    conn.commit()
    return len(normalized_rows)


def poll_once(conn, has_spatialite=False, bbox=None):
    """
    Execute a single poll cycle: fetch, parse, and store state vectors.

    Args:
        conn: SQLite connection to the flight database.
        has_spatialite: Whether SpatiaLite is available.
        bbox: Optional geographic bounding box filter.

    Returns:
        Number of records stored.
    """
    if bbox is None:
        bbox = INGEST_BBOX

    states = fetch_state_vectors(bbox=bbox)
    if not states:
        return 0

    rows = []
    for sv in states:
        parsed = parse_state_vector(sv)
        if parsed is not None:
            rows.append(parsed)

    count = insert_state_vectors(conn, rows, has_spatialite)
    logger.debug("Stored %d state vectors (%d total fetched)", count, len(states))
    return count


def run_poller(db_path=None):
    """
    Main polling loop. Runs continuously until interrupted.

    Args:
        db_path: Path to the flight database. Defaults to config value.
    """
    if db_path is None:
        db_path = DB_PATH

    conn, has_spatialite = init_flight_db(db_path)
    logger.info(
        "Starting poller (interval=%ds, spatialite=%s, bbox=%s)",
        POLL_INTERVAL_SECONDS,
        has_spatialite,
        INGEST_BBOX,
    )

    consecutive_errors = 0
    while True:
        try:
            count = poll_once(conn, has_spatialite)
            if count > 0:
                consecutive_errors = 0
                logger.info("Poll: stored %d records", count)
            else:
                logger.debug("Poll: no records returned")
        except Exception:
            consecutive_errors += 1
            logger.exception("Poll error (consecutive: %d)", consecutive_errors)
            # Back off on repeated errors
            if consecutive_errors >= 5:
                backoff = min(consecutive_errors * POLL_INTERVAL_SECONDS, 300)
                logger.warning("Backing off for %d seconds", backoff)
                time.sleep(backoff)

        time.sleep(POLL_INTERVAL_SECONDS)
