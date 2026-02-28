"""
Flask routes for the OverFlight web application.

Handles:
- Main page with location input and flight results
- API endpoint for AJAX flight lookups
- Zip code resolution endpoint
"""

import logging
import time

from flask import Blueprint, jsonify, make_response, render_template, request

from overflight.config import DEFAULT_RADIUS_MILES, MAX_RADIUS_MILES, RETENTION_HOURS
from overflight.database.queries import enrich_results, find_flights_near
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

    try:
        flight_conn = get_flight_db()
        results = find_flights_near(flight_conn, lat, lon, radius_miles=radius)
        flight_conn.close()
    except Exception:
        logger.exception("Flight query failed")
        return jsonify({"error": "Database query failed"}), 500

    try:
        enrichment_conn = get_enrichment_db()
        enrich_results(results, enrichment_conn)
        enrichment_conn.close()
    except Exception:
        logger.warning("Enrichment failed, returning unenriched results", exc_info=True)

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


@bp.route("/api/status")
def api_status():
    """Health check / status endpoint."""
    from overflight.database.cleanup import get_oldest_record_age, get_record_count

    status = {"status": "ok", "timestamp": int(time.time())}

    try:
        flight_conn = get_flight_db()
        status["record_count"] = get_record_count(flight_conn)
        oldest = get_oldest_record_age(flight_conn)
        status["oldest_record_hours"] = round(oldest, 2) if oldest else None
        flight_conn.close()
    except Exception:
        status["database"] = "unavailable"

    return jsonify(status)
