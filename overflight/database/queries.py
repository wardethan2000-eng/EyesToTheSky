"""
Spatial query logic for OverFlight.

Provides functions to find aircraft that flew within a given radius of a
location over the past 24 hours. Works with both SpatiaLite (spatial index)
and pure SQLite (bounding-box + haversine filter).
"""

import math
import sqlite3
import time
from typing import Optional

from overflight.config import DEFAULT_RADIUS_MILES, MAX_RADIUS_MILES, RETENTION_HOURS

# Earth's radius in miles
EARTH_RADIUS_MILES = 3958.8

# Conversion factors
METERS_TO_FEET = 3.28084
MPS_TO_MPH = 2.23694
MPS_TO_KNOTS = 1.94384


def haversine_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great-circle distance between two points in miles.

    Uses the Haversine formula.
    """
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))
    return EARTH_RADIUS_MILES * c


def _bounding_box(lat, lon, radius_miles):
    """
    Calculate a lat/lon bounding box for a given radius.

    Returns (min_lat, max_lat, min_lon, max_lon).
    The bounding box is slightly oversized to ensure all points within
    the radius are captured; exact distances are refined with haversine.
    """
    lat_delta = radius_miles / 69.0  # ~69 miles per degree of latitude
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-6:
        cos_lat = 1e-6 if cos_lat >= 0 else -1e-6
    lon_delta = radius_miles / (69.0 * cos_lat)

    return (
        lat - lat_delta,
        lat + lat_delta,
        lon - lon_delta,
        lon + lon_delta,
    )


def find_flights_near(conn, lat, lon, radius_miles=None, hours=None):
    """
    Find all distinct aircraft that passed within a radius of a location.

    For each aircraft, returns the closest-approach position report
    (the point where it was nearest to the user's location).

    Args:
        conn: SQLite connection to the flight database.
        lat: User latitude in decimal degrees.
        lon: User longitude in decimal degrees.
        radius_miles: Search radius in miles. Defaults to config value.
        hours: Hours of history to search. Defaults to config retention.

    Returns:
        List of dicts, each representing one aircraft's closest approach,
        sorted by time (most recent first). Keys include:
        icao24, callsign, latitude, longitude, altitude, velocity,
        heading, vertical_rate, on_ground, timestamp, distance_miles
    """
    if radius_miles is None:
        radius_miles = DEFAULT_RADIUS_MILES
    radius_miles = min(radius_miles, MAX_RADIUS_MILES)

    if hours is None:
        hours = RETENTION_HOURS

    cutoff = int(time.time()) - (hours * 3600)
    min_lat, max_lat, min_lon, max_lon = _bounding_box(lat, lon, radius_miles)

    # Bounding-box query to get candidate rows
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            icao24, callsign, latitude, longitude, altitude,
            velocity, heading, vertical_rate, on_ground, timestamp
        FROM state_vectors
        WHERE timestamp >= ?
          AND latitude BETWEEN ? AND ?
          AND longitude BETWEEN ? AND ?
    """, (cutoff, min_lat, max_lat, min_lon, max_lon))

    # Group by icao24 and find closest approach for each
    aircraft = {}  # icao24 -> (min_distance, row_dict)

    for row in cursor.fetchall():
        row_dict = dict(row)
        distance = haversine_distance(lat, lon, row_dict["latitude"], row_dict["longitude"])

        # Filter by exact radius (bounding box is an approximation)
        if distance > radius_miles:
            continue

        icao24 = row_dict["icao24"]
        if icao24 not in aircraft or distance < aircraft[icao24][0]:
            row_dict["distance_miles"] = round(distance, 2)
            aircraft[icao24] = (distance, row_dict)

    # Extract results and sort by timestamp (most recent first)
    results = [entry[1] for entry in aircraft.values()]
    results.sort(key=lambda x: x["timestamp"], reverse=True)

    return results


def find_flights_near_spatialite(conn, lat, lon, radius_miles=None, hours=None):
    """
    SpatiaLite-optimized version of find_flights_near.

    Uses the spatial index for efficient radius queries. Only available
    when SpatiaLite extension is loaded.

    Args:
        Same as find_flights_near.

    Returns:
        Same as find_flights_near.
    """
    if radius_miles is None:
        radius_miles = DEFAULT_RADIUS_MILES
    radius_miles = min(radius_miles, MAX_RADIUS_MILES)

    if hours is None:
        hours = RETENTION_HOURS

    cutoff = int(time.time()) - (hours * 3600)
    radius_meters = radius_miles * 1609.34

    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            icao24, callsign, latitude, longitude, altitude,
            velocity, heading, vertical_rate, on_ground, timestamp,
            ST_Distance(geom, MakePoint(?, ?, 4326), 1) AS distance_m
        FROM state_vectors
        WHERE timestamp >= ?
          AND ROWID IN (
              SELECT ROWID FROM SpatialIndex
              WHERE f_table_name = 'state_vectors'
                AND f_geometry_column = 'geom'
                AND search_frame = BuildCircleMbr(?, ?, ?, 4326)
          )
          AND ST_Distance(geom, MakePoint(?, ?, 4326), 1) <= ?
        ORDER BY icao24, distance_m
    """, (lon, lat, cutoff, lon, lat, radius_meters, lon, lat, radius_meters))

    # Deduplicate by icao24, keeping closest approach
    aircraft = {}
    for row in cursor.fetchall():
        row_dict = dict(row)
        icao24 = row_dict["icao24"]
        if icao24 not in aircraft:
            row_dict["distance_miles"] = round(row_dict.pop("distance_m") / 1609.34, 2)
            aircraft[icao24] = row_dict

    results = list(aircraft.values())
    results.sort(key=lambda x: x["timestamp"], reverse=True)

    return results


def enrich_results(flight_results, enrichment_conn):
    """
    Join flight results with aircraft enrichment data.

    Args:
        flight_results: List of flight dicts from find_flights_near.
        enrichment_conn: SQLite connection to the enrichment database.

    Returns:
        The same list with enrichment fields added to each dict.
    """
    if not flight_results:
        return flight_results

    from overflight.database.enrichment import lookup_aircraft_batch

    icao24_list = [f["icao24"] for f in flight_results]
    enrichment = lookup_aircraft_batch(enrichment_conn, icao24_list)

    for flight in flight_results:
        aircraft_info = enrichment.get(flight["icao24"], {})
        flight["registration"] = aircraft_info.get("registration")
        flight["manufacturer"] = aircraft_info.get("manufacturer")
        flight["model"] = aircraft_info.get("model")
        flight["operator"] = aircraft_info.get("operator")
        flight["owner"] = aircraft_info.get("owner")
        flight["built_year"] = aircraft_info.get("built_year")
        flight["registered_country"] = aircraft_info.get("registered_country")

        # Calculate aircraft age if built year is available
        if flight["built_year"]:
            import datetime
            current_year = datetime.datetime.now().year
            flight["aircraft_age"] = current_year - flight["built_year"]
        else:
            flight["aircraft_age"] = None

        # Convert units for display
        if flight.get("altitude") is not None:
            flight["altitude_feet"] = round(flight["altitude"] * METERS_TO_FEET)
        else:
            flight["altitude_feet"] = None

        if flight.get("velocity") is not None:
            flight["speed_mph"] = round(flight["velocity"] * MPS_TO_MPH)
            flight["speed_knots"] = round(flight["velocity"] * MPS_TO_KNOTS)
        else:
            flight["speed_mph"] = None
            flight["speed_knots"] = None

    return flight_results
