"""
Flask routes for the OverFlight web application.

Handles:
- Main page with location input and flight results
- API endpoint for AJAX flight lookups
- Zip code resolution endpoint
"""

import logging
import math
import time

from flask import Blueprint, jsonify, make_response, render_template, request

from overflight.config import (
    CHUNK_SIZE_HIGH_DENSITY_MINUTES,
    CHUNK_SIZE_LOW_DENSITY_HOURS,
    CHUNK_SIZE_MEDIUM_DENSITY_HOURS,
    DEFAULT_RADIUS_MILES,
    MAX_RADIUS_MILES,
    PLAYBACK_SPEED_RATIO,
    RENDER_BUDGET_MAX,
    RETENTION_HOURS,
)
from overflight.database.queries import enrich_results, find_flights_near
from overflight.database.tracks import (
    get_track_density,
    get_tracks_near,
    get_unique_aircraft_count,
)
from overflight.ingestion.zipcode import resolve_zipcode
from overflight.webapp import get_enrichment_db, get_flight_db

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# Allowed radius options in miles
RADIUS_OPTIONS = [5, 10, 25, 50]


@bp.route("/")
def index():
    """Render the main page."""
    # Read last-used location from cookie
    saved_lat = request.cookies.get("overflight_lat")
    saved_lon = request.cookies.get("overflight_lon")
    saved_zip = request.cookies.get("overflight_zip", "")
    saved_radius = request.cookies.get("overflight_radius", str(int(DEFAULT_RADIUS_MILES)))

    return render_template(
        "index.html",
        radius_options=RADIUS_OPTIONS,
        default_radius=int(float(saved_radius)),
        saved_lat=saved_lat,
        saved_lon=saved_lon,
        saved_zip=saved_zip,
        max_radius=int(MAX_RADIUS_MILES),
        config={
            "PLAYBACK_SPEED_RATIO": PLAYBACK_SPEED_RATIO,
            "RENDER_BUDGET_MAX": RENDER_BUDGET_MAX,
        },
    )


@bp.route("/api/flights")
def api_flights():
    """
    API endpoint: find flights near a location.

    Query parameters:
        lat: Latitude in decimal degrees (required)
        lon: Longitude in decimal degrees (required)
        radius: Search radius in miles (optional, default from config)
        zip: Zip code for cookie storage (optional)

    Returns JSON with flight results.
    """
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = request.args.get("radius", default=DEFAULT_RADIUS_MILES, type=float)
    zipcode = request.args.get("zip", "")

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    radius = min(max(radius, 1), MAX_RADIUS_MILES)

    flight_conn = None
    try:
        flight_conn = get_flight_db()
        results = find_flights_near(flight_conn, lat, lon, radius_miles=radius)
    except Exception:
        logger.exception("Flight query failed")
        return jsonify({"error": "Database query failed"}), 500
    finally:
        if flight_conn is not None:
            flight_conn.close()

    enrichment_conn = None
    try:
        enrichment_conn = get_enrichment_db()
        enrich_results(results, enrichment_conn)
    except Exception:
        logger.warning("Enrichment failed, returning unenriched results", exc_info=True)
    finally:
        if enrichment_conn is not None:
            enrichment_conn.close()

    # Build response with location cookie
    response = make_response(jsonify({
        "count": len(results),
        "flights": results,
        "query": {
            "lat": lat,
            "lon": lon,
            "radius_miles": radius,
            "hours": RETENTION_HOURS,
        },
    }))

    # Save location in cookies (expires in 365 days)
    max_age = 365 * 24 * 3600
    response.set_cookie("overflight_lat", str(lat), max_age=max_age, samesite="Lax")
    response.set_cookie("overflight_lon", str(lon), max_age=max_age, samesite="Lax")
    response.set_cookie("overflight_radius", str(int(radius)), max_age=max_age, samesite="Lax")
    if zipcode:
        response.set_cookie("overflight_zip", zipcode, max_age=max_age, samesite="Lax")

    return response


@bp.route("/api/resolve-zip")
def api_resolve_zip():
    """
    API endpoint: resolve a US zip code to coordinates.

    Query parameters:
        zip: 5-digit US zip code (required)

    Returns JSON with lat, lon, city, state.
    """
    zipcode = request.args.get("zip", "").strip()

    if not zipcode or len(zipcode) != 5 or not zipcode.isdigit():
        return jsonify({"error": "Valid 5-digit US zip code required"}), 400

    result = resolve_zipcode(zipcode)

    if result is None:
        return jsonify({"error": f"Zip code {zipcode} not found"}), 404

    return jsonify({
        "zipcode": result["zipcode"],
        "latitude": result["latitude"],
        "longitude": result["longitude"],
        "city": result["city"],
        "state": result["state"],
    })


@bp.route("/api/tracks")
def api_tracks():
    """
    API endpoint: get track segments near a location.

    Query parameters:
        lat: Latitude (required)
        lon: Longitude (required)
        radius: Radius in miles (optional)
        start: Unix timestamp, window start (optional, default 24h ago)
        end: Unix timestamp, window end (optional, default now)
        min_alt: Minimum altitude in meters (optional, default 0)
        phase: Comma-separated phase filter (optional)

    Returns JSON with tracks and metadata.
    """
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = request.args.get("radius", default=DEFAULT_RADIUS_MILES, type=float)
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    min_alt = request.args.get("min_alt", default=0, type=float)
    phase_filter = request.args.get("phase", default="", type=str).strip()

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    radius = min(max(radius, 1), MAX_RADIUS_MILES)

    now = int(time.time())
    if start is None:
        start = now - (RETENTION_HOURS * 3600)
    if end is None:
        end = now

    # Parse phase filter
    allowed_phases = {"ground", "departure", "enroute", "arrival"}
    phases = None
    if phase_filter:
        phases = [p.strip() for p in phase_filter.split(",") if p.strip() in allowed_phases]
        if not phases:
            phases = None

    flight_conn = None
    try:
        flight_conn = get_flight_db()
        tracks = get_tracks_near(
            flight_conn,
            lat,
            lon,
            radius,
            start_time=start,
            end_time=end,
            min_alt=min_alt,
            phases=phases,
        )

        # Get total density for 24h window as unique aircraft count.
        full_start = now - (RETENTION_HOURS * 3600)
        total_in_area = get_unique_aircraft_count(
            flight_conn,
            lat,
            lon,
            radius,
            start_time=full_start,
            end_time=now,
        )
    except Exception:
        logger.exception("Track query failed")
        return jsonify({"error": "Database query failed"}), 500
    finally:
        if flight_conn is not None:
            flight_conn.close()

    # Determine density classification and suggestion
    density, suggested_min_alt = _classify_density(total_in_area)

    # Build response tracks (serialize for JSON)
    track_list = []
    for t in tracks:
        track_list.append({
            "id": t["id"],
            "icao24": t["icao24"],
            "callsign": t.get("callsign"),
            "phase": t["phase"],
            "polyline": t["polyline"],
            "start_time": t["start_time"],
            "end_time": t["end_time"],
            "min_altitude": t.get("min_altitude"),
            "max_altitude": t.get("max_altitude"),
            "avg_velocity": t.get("avg_velocity"),
            "avg_heading": t.get("avg_heading"),
            "liftoff_lat": t.get("liftoff_lat"),
            "liftoff_lon": t.get("liftoff_lon"),
            "liftoff_heading": t.get("liftoff_heading"),
            "liftoff_time": t.get("liftoff_time"),
            "touchdown_lat": t.get("touchdown_lat"),
            "touchdown_lon": t.get("touchdown_lon"),
            "approach_heading": t.get("approach_heading"),
            "touchdown_time": t.get("touchdown_time"),
        })

    return jsonify({
        "tracks": track_list,
        "meta": {
            "count": len(track_list),
            "total_in_area": total_in_area,
            "density": density,
            "suggested_min_alt": suggested_min_alt,
            "window": {"start": start, "end": end},
            "query": {"lat": lat, "lon": lon, "radius": radius},
        },
    })


@bp.route("/api/track-detail/<icao24>")
def api_track_detail(icao24):
    """
    API endpoint: get enrichment data for a single aircraft.

    Returns full aircraft metadata for display when the user
    clicks an aircraft on the map.
    """
    if not icao24 or len(icao24) > 6:
        return jsonify({"error": "Invalid ICAO24 address"}), 400

    icao24 = icao24.lower().strip()

    enrichment_conn = None
    try:
        enrichment_conn = get_enrichment_db()
        from overflight.database.enrichment import lookup_aircraft

        aircraft = lookup_aircraft(enrichment_conn, icao24)
    except Exception:
        logger.exception("Enrichment lookup failed")
        return jsonify({"error": "Enrichment lookup failed"}), 500
    finally:
        if enrichment_conn is not None:
            enrichment_conn.close()

    if aircraft is None:
        return jsonify({
            "icao24": icao24,
            "callsign": None,
            "registration": None,
            "manufacturer": None,
            "model": None,
            "operator": None,
            "owner": None,
            "built_year": None,
            "aircraft_age": None,
            "registered_country": None,
        })

    import datetime

    aircraft_age = None
    if aircraft.get("built_year"):
        aircraft_age = datetime.datetime.now().year - aircraft["built_year"]

    return jsonify({
        "icao24": icao24,
        "callsign": None,
        "registration": aircraft.get("registration"),
        "manufacturer": aircraft.get("manufacturer"),
        "model": aircraft.get("model"),
        "operator": aircraft.get("operator"),
        "owner": aircraft.get("owner"),
        "built_year": aircraft.get("built_year"),
        "aircraft_age": aircraft_age,
        "registered_country": aircraft.get("registered_country"),
    })


@bp.route("/api/tracks/plan")
def api_tracks_plan():
    """
    API endpoint: generate a playback chunk plan.

    Returns a list of time-window chunks with estimated track counts,
    sized adaptively based on track density in the area.

    Query parameters:
        lat: Latitude (required)
        lon: Longitude (required)
        radius: Radius in miles (optional)
        min_alt: Minimum altitude in meters (optional)
    """
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = request.args.get("radius", default=DEFAULT_RADIUS_MILES, type=float)
    min_alt = request.args.get("min_alt", default=0, type=float)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    radius = min(max(radius, 1), MAX_RADIUS_MILES)

    now = int(time.time())
    full_start = now - (RETENTION_HOURS * 3600)
    total_duration = RETENTION_HOURS * 3600

    flight_conn = None
    try:
        flight_conn = get_flight_db()
        total_unique_aircraft = get_unique_aircraft_count(
            flight_conn,
            lat,
            lon,
            radius,
            start_time=full_start,
            end_time=now,
            min_alt=min_alt,
        )

        # Determine chunk size based on density
        density, suggested_min_alt = _classify_density(total_unique_aircraft)
        chunk_seconds = _chunk_seconds_for_density(density)

        # Build chunk plan
        chunks = []
        chunk_start = full_start
        while chunk_start < now:
            chunk_end = min(chunk_start + chunk_seconds, now)
            estimated = get_track_density(
                flight_conn,
                lat,
                lon,
                radius,
                start_time=chunk_start,
                end_time=chunk_end,
                min_alt=min_alt,
            )
            chunks.append({
                "start": chunk_start,
                "end": chunk_end,
                "estimated_tracks": estimated,
            })
            chunk_start = chunk_end
    except Exception:
        logger.exception("Track plan query failed")
        return jsonify({"error": "Database query failed"}), 500
    finally:
        if flight_conn is not None:
            flight_conn.close()

    return jsonify({
        "total_duration_seconds": total_duration,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "density": density,
        "suggested_min_alt": suggested_min_alt,
        "total_unique_aircraft": total_unique_aircraft,
        "query": {"lat": lat, "lon": lon, "radius": radius, "min_alt": min_alt},
    })


def _classify_density(total_count):
    """
    Classify area density and suggest altitude filter.

    Args:
        total_count: Total track segments in the area over 24h.

    Returns:
        (density_label, suggested_min_alt) tuple.
    """
    if total_count > 500:
        return "high", 5000
    elif total_count > 200:
        return "medium", 1000
    else:
        return "low", None


def _chunk_seconds_for_density(density):
    """
    Return the chunk duration in seconds based on density classification.
    """
    if density == "high":
        return CHUNK_SIZE_HIGH_DENSITY_MINUTES * 60
    elif density == "medium":
        return CHUNK_SIZE_MEDIUM_DENSITY_HOURS * 3600
    else:
        return CHUNK_SIZE_LOW_DENSITY_HOURS * 3600


@bp.route("/api/status")
def api_status():
    """Health check / status endpoint."""
    from overflight.database.cleanup import get_oldest_record_age, get_record_count

    status = {"status": "ok", "timestamp": int(time.time())}

    flight_conn = None
    try:
        flight_conn = get_flight_db()
        status["record_count"] = get_record_count(flight_conn)
        oldest = get_oldest_record_age(flight_conn)
        status["oldest_record_hours"] = round(oldest, 2) if oldest else None
    except Exception:
        status["database"] = "unavailable"
    finally:
        if flight_conn is not None:
            flight_conn.close()

    return jsonify(status)
