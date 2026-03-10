"""
Flask routes for the OverFlight web application.

Handles:
- Main page with location input and flight results
- API endpoint for AJAX flight lookups
- Zip code resolution endpoint
"""

import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, make_response, render_template, request

from overflight.airports import find_airports_near
from overflight.config import (
    CHUNK_SIZE_HIGH_DENSITY_MINUTES,
    CHUNK_SIZE_LOW_DENSITY_HOURS,
    CHUNK_SIZE_MEDIUM_DENSITY_HOURS,
    DEFAULT_RADIUS_MILES,
    MAX_RADIUS_MILES,
    PLAYBACK_DEFAULT_WINDOW_HOURS,
    PLAYBACK_CHUNK_BACKOFF_MS,
    PLAYBACK_CHUNK_FAILURE_COOLDOWN_MS,
    PLAYBACK_CHUNK_RETRY_MAX,
    PLAYBACK_DEPARTURE_PREVIEW_SECONDS,
    PLAYBACK_SPEED_RATIO,
    PLAYBACK_TARGET_FPS,
    RENDER_BUDGET_MAX,
    RETENTION_HOURS,
    SEARCH_DEFAULT_WINDOW_HOURS,
)
from overflight.database.queries import enrich_results, find_flights_near
from overflight.database.schema import ensure_tracks_schema, init_flight_db
from overflight.database.tracks import (
    build_tracks_incremental,
    get_track_windows_near,
    get_tracks_near,
    get_unique_aircraft_count,
)
from overflight.ingestion.poller import (
    fetch_state_vectors,
    insert_state_vectors,
    parse_state_vector,
)
from overflight.ingestion.zipcode import resolve_zipcode
from overflight.webapp import DB_PATH, get_enrichment_db, get_flight_db

logger = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# Allowed radius options in miles
RADIUS_OPTIONS = [5, 10, 25, 50]

# On-demand backfill tuning for testing sparse areas.
AREA_BACKFILL_COOLDOWN_SECONDS = 120
_area_backfill_lock = threading.Lock()
_area_backfill_last_run = {}


def _asset_version(*path_parts):
    """Return a stable cache-busting version for a static asset."""
    asset_path = os.path.join(os.path.dirname(__file__), "static", *path_parts)
    try:
        return int(os.path.getmtime(asset_path))
    except OSError:
        return int(time.time())


def _bbox_for_area(lat, lon, radius_miles):
    """Build an approximate lat/lon bounding box for a search area."""
    lat_delta = radius_miles / 69.0
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-6:
        cos_lat = 1e-6 if cos_lat >= 0 else -1e-6
    lon_delta = radius_miles / (69.0 * cos_lat)
    return (lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta)


def _area_backfill_key(lat, lon, radius):
    """Create a coarse key so nearby requests share one cooldown bucket."""
    return (
        round(lat, 2),
        round(lon, 2),
        int(round(radius)),
    )


def _backfill_area_from_opensky(lat, lon, radius):
    """
    On-demand OpenSky fetch for a specific area, then rebuild track segments.

    Returns a dict with counters and whether request was skipped due to cooldown.
    """
    key = _area_backfill_key(lat, lon, radius)
    now = time.time()

    with _area_backfill_lock:
        last_run = _area_backfill_last_run.get(key)
        if last_run and (now - last_run) < AREA_BACKFILL_COOLDOWN_SECONDS:
            return {"skipped": True, "fetched": 0, "inserted": 0, "segments": 0}
        _area_backfill_last_run[key] = now

    bbox = _bbox_for_area(lat, lon, radius)
    states = fetch_state_vectors(bbox=bbox)
    if not states:
        return {"skipped": False, "fetched": 0, "inserted": 0, "segments": 0}

    rows = []
    for sv in states:
        parsed = parse_state_vector(sv)
        if parsed is not None:
            rows.append(parsed)

    if not rows:
        return {"skipped": False, "fetched": len(states), "inserted": 0, "segments": 0}

    conn = None
    inserted = 0
    segments = 0
    try:
        conn, has_spatialite = init_flight_db(DB_PATH, use_spatialite=False)
        ensure_tracks_schema(conn, has_spatialite=has_spatialite)
        inserted = insert_state_vectors(conn, rows, has_spatialite=has_spatialite)
        segments = build_tracks_incremental(conn)
    except Exception:
        logger.warning("Area backfill failed", exc_info=True)
    finally:
        if conn is not None:
            conn.close()

    logger.info(
        "Area backfill lat=%.3f lon=%.3f radius=%.1f fetched=%d inserted=%d segments=%d",
        lat,
        lon,
        radius,
        len(states),
        inserted,
        segments,
    )
    return {
        "skipped": False,
        "fetched": len(states),
        "inserted": inserted,
        "segments": segments,
    }


def _backfill_status_payload(result, attempted):
    """Normalize backfill details for frontend status messaging."""
    if not attempted or result is None:
        return {
            "attempted": False,
            "cooldown": False,
            "fetched": 0,
            "inserted": 0,
            "segments": 0,
        }

    return {
        "attempted": True,
        "cooldown": bool(result.get("skipped")),
        "fetched": int(result.get("fetched", 0) or 0),
        "inserted": int(result.get("inserted", 0) or 0),
        "segments": int(result.get("segments", 0) or 0),
    }


def _parse_day_start_utc(day_str):
    """Parse YYYY-MM-DD into a UTC unix timestamp at 00:00:00."""
    if not day_str:
        return None
    try:
        day = datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _default_previous_day_iso():
    """Return yesterday's UTC date string in YYYY-MM-DD format."""
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def _today_iso():
    """Return today's UTC date string in YYYY-MM-DD format."""
    return datetime.now(timezone.utc).date().isoformat()


def _flight_window_from_request(window_key, day_str):
    """Resolve a card-results time filter into explicit UTC timestamps."""
    now_dt = datetime.now(timezone.utc)
    now_ts = int(now_dt.timestamp())
    today = now_dt.date()
    yesterday = today - timedelta(days=1)

    presets = {
        "last_hour": {
            "start": now_ts - 3600,
            "end": now_ts,
            "label": "Last hour",
            "key": "last_hour",
        },
        "last_24_hours": {
            "start": now_ts - (SEARCH_DEFAULT_WINDOW_HOURS * 3600),
            "end": now_ts,
            "label": f"Last {SEARCH_DEFAULT_WINDOW_HOURS} hours",
            "key": "last_24_hours",
        },
        "today": {
            "start": int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()),
            "end": now_ts,
            "label": "Today",
            "key": "today",
        },
        "yesterday": {
            "start": int(datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc).timestamp()),
            "end": int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()),
            "label": "Yesterday",
            "key": "yesterday",
        },
    }

    if day_str:
        day_start = _parse_day_start_utc(day_str)
        if day_start is not None:
            day_dt = datetime.fromtimestamp(day_start, tz=timezone.utc)
            return {
                "start": day_start,
                "end": day_start + 86400,
                "label": day_dt.strftime("%b %d, %Y").replace(" 0", " "),
                "key": "custom_day",
                "day": day_str,
            }

    return presets.get(window_key or "last_24_hours", presets["last_24_hours"])


def _summarize_track_windows(track_windows):
    """Return unique aircraft count and overall time bounds for track windows."""
    if not track_windows:
        return 0, None, None

    unique_aircraft = set()
    available_start = None
    available_end = None

    for window in track_windows:
        icao24 = window.get("icao24")
        if icao24:
            unique_aircraft.add(icao24)

        start_time = window.get("start_time")
        end_time = window.get("end_time")
        if start_time is not None and (available_start is None or start_time < available_start):
            available_start = start_time
        if end_time is not None and (available_end is None or end_time > available_end):
            available_end = end_time

    return len(unique_aircraft), available_start, available_end


def _build_track_plan_chunks(track_windows, full_start, window_end, chunk_seconds):
    """Estimate overlapping track counts for each playback chunk in one pass."""
    chunks = []
    chunk_start = full_start
    while chunk_start < window_end:
        chunk_end = min(chunk_start + chunk_seconds, window_end)
        chunks.append({
            "start": chunk_start,
            "end": chunk_end,
            "estimated_tracks": 0,
        })
        chunk_start = chunk_end

    if not chunks or not track_windows:
        return chunks

    deltas = [0] * (len(chunks) + 1)
    for window in track_windows:
        start_time = window.get("start_time")
        end_time = window.get("end_time")
        if start_time is None or end_time is None:
            continue
        if end_time < full_start or start_time > window_end:
            continue

        overlap_start = max(full_start, start_time)
        overlap_end = min(window_end, end_time)
        first_chunk_idx = max(0, math.ceil((overlap_start - full_start) / chunk_seconds) - 1)
        last_chunk_idx = min(
            len(chunks) - 1,
            math.floor((overlap_end - full_start) / chunk_seconds),
        )
        if first_chunk_idx > last_chunk_idx:
            continue

        deltas[first_chunk_idx] += 1
        deltas[last_chunk_idx + 1] -= 1

    running_total = 0
    for idx, chunk in enumerate(chunks):
        running_total += deltas[idx]
        chunk["estimated_tracks"] = running_total

    return chunks


@bp.route("/")
def index():
    """Render the main page."""
    # Read last-used location from cookie
    saved_lat = request.cookies.get("overflight_lat")
    saved_lon = request.cookies.get("overflight_lon")
    saved_zip = request.cookies.get("overflight_zip", "")
    saved_radius = request.cookies.get("overflight_radius", str(int(DEFAULT_RADIUS_MILES)))

    yesterday = _default_previous_day_iso()
    today = _today_iso()

    response = make_response(render_template(
        "index.html",
        radius_options=RADIUS_OPTIONS,
        default_radius=int(float(saved_radius)),
        saved_lat=saved_lat,
        saved_lon=saved_lon,
        saved_zip=saved_zip,
        playback_day=yesterday,
        today_iso=today,
        yesterday_iso=yesterday,
        max_radius=int(MAX_RADIUS_MILES),
        config={
            "SEARCH_DEFAULT_WINDOW_HOURS": SEARCH_DEFAULT_WINDOW_HOURS,
            "PLAYBACK_SPEED_RATIO": PLAYBACK_SPEED_RATIO,
            "RENDER_BUDGET_MAX": RENDER_BUDGET_MAX,
            "PLAYBACK_TARGET_FPS": PLAYBACK_TARGET_FPS,
            "PLAYBACK_DEPARTURE_PREVIEW_SECONDS": PLAYBACK_DEPARTURE_PREVIEW_SECONDS,
            "PLAYBACK_CHUNK_RETRY_MAX": PLAYBACK_CHUNK_RETRY_MAX,
            "PLAYBACK_CHUNK_BACKOFF_MS": PLAYBACK_CHUNK_BACKOFF_MS,
            "PLAYBACK_CHUNK_FAILURE_COOLDOWN_MS": PLAYBACK_CHUNK_FAILURE_COOLDOWN_MS,
        },
        asset_version={
            "css": _asset_version("css", "style.css"),
            "icons_js": _asset_version("js", "aircraft-icons.js"),
            "playback_js": _asset_version("js", "playback.js"),
            "map_js": _asset_version("js", "map.js"),
            "app_js": _asset_version("js", "app.js"),
        },
    ))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@bp.route("/dev/playback-harness")
def playback_harness():
    """Render a lightweight manual harness for playback state testing."""
    return render_template("playback_harness.html")


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
    window_key = request.args.get("window", default="last_24_hours", type=str).strip()
    day = request.args.get("day", default="", type=str).strip()

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    radius = min(max(radius, 1), MAX_RADIUS_MILES)
    flight_window = _flight_window_from_request(window_key, day)
    can_backfill_live = flight_window["end"] >= int(time.time()) - 900

    flight_conn = None
    try:
        flight_conn = get_flight_db()
        results = find_flights_near(
            flight_conn,
            lat,
            lon,
            radius_miles=radius,
            start_time=flight_window["start"],
            end_time=flight_window["end"],
        )

        # Testing-friendly behavior: when an area is empty, try one quick
        # on-demand area backfill from OpenSky and then re-run the query.
        if not results and can_backfill_live:
            _backfill_area_from_opensky(lat, lon, radius)
            try:
                flight_conn.close()
            except Exception:
                pass
            flight_conn = get_flight_db()
            results = find_flights_near(
                flight_conn,
                lat,
                lon,
                radius_miles=radius,
                start_time=flight_window["start"],
                end_time=flight_window["end"],
            )
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
            "hours": SEARCH_DEFAULT_WINDOW_HOURS,
            "window": flight_window["key"],
            "window_label": flight_window["label"],
            "start": flight_window["start"],
            "end": flight_window["end"],
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


@bp.route("/api/airports")
def api_airports():
    """Return bundled airport points near a location for map overlays."""
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    radius = request.args.get("radius", default=DEFAULT_RADIUS_MILES, type=float)
    limit = request.args.get("limit", default=40, type=int)

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    radius = min(max(radius, 1), MAX_RADIUS_MILES)
    limit = min(max(limit, 1), 100)

    try:
        airports = find_airports_near(lat, lon, radius, limit=limit)
    except FileNotFoundError:
        logger.warning("Bundled airports dataset is missing")
        airports = []
    except Exception:
        logger.exception("Airport query failed")
        return jsonify({"error": "Airport query failed"}), 500

    return jsonify({
        "airports": airports,
        "meta": {
            "count": len(airports),
            "query": {
                "lat": lat,
                "lon": lon,
                "radius": radius,
                "limit": limit,
            },
        },
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
    day = request.args.get("day", default="", type=str).strip()
    min_alt = request.args.get("min_alt", default=0, type=float)
    phase_filter = request.args.get("phase", default="", type=str).strip()

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    radius = min(max(radius, 1), MAX_RADIUS_MILES)

    now = int(time.time())
    if start is None or end is None:
        day_start = _parse_day_start_utc(day)
        if day_start is not None:
            if start is None:
                start = day_start
            if end is None:
                end = day_start + 86400
        else:
            if start is None:
                start = now - (PLAYBACK_DEFAULT_WINDOW_HOURS * 3600)
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
        full_start = now - (PLAYBACK_DEFAULT_WINDOW_HOURS * 3600)
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

    track_enrichment = {}
    enrichment_conn = None
    try:
        enrichment_conn = get_enrichment_db()
        from overflight.database.enrichment import lookup_aircraft_batch

        track_enrichment = lookup_aircraft_batch(
            enrichment_conn,
            [t["icao24"] for t in tracks if t.get("icao24")],
        )
    except Exception:
        logger.warning("Track enrichment failed, returning unenriched tracks", exc_info=True)
    finally:
        if enrichment_conn is not None:
            enrichment_conn.close()

    # Determine density classification and suggestion
    density, suggested_min_alt = _classify_density(total_in_area)

    # Build response tracks (serialize for JSON)
    track_list = []
    for t in tracks:
        aircraft = track_enrichment.get(t["icao24"], {})
        track_list.append({
            "id": t["id"],
            "icao24": t["icao24"],
            "callsign": t.get("callsign"),
            "phase": t["phase"],
            "registration": aircraft.get("registration"),
            "manufacturer": aircraft.get("manufacturer"),
            "model": aircraft.get("model"),
            "operator": aircraft.get("operator"),
            "owner": aircraft.get("owner"),
            "built_year": aircraft.get("built_year"),
            "registered_country": aircraft.get("registered_country"),
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
            "typecode": None,
            "icao_aircraft_type": None,
            "engines": None,
            "first_flight_date": None,
            "seat_configuration": None,
            "category_description": None,
            "operator_icao": None,
            "operator_iata": None,
            "serial_number": None,
            "status": None,
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
        "typecode": aircraft.get("typecode"),
        "icao_aircraft_type": aircraft.get("icao_aircraft_type"),
        "engines": aircraft.get("engines"),
        "first_flight_date": aircraft.get("first_flight_date"),
        "seat_configuration": aircraft.get("seat_configuration"),
        "category_description": aircraft.get("category_description"),
        "operator_icao": aircraft.get("operator_icao"),
        "operator_iata": aircraft.get("operator_iata"),
        "serial_number": aircraft.get("serial_number"),
        "status": aircraft.get("status"),
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
    day = request.args.get("day", default="", type=str).strip()
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    min_alt = request.args.get("min_alt", default=0, type=float)
    trim_to_available = bool(request.args.get("trim", default=0, type=int))

    if lat is None or lon is None:
        return jsonify({"error": "lat and lon are required"}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Invalid coordinates"}), 400

    radius = min(max(radius, 1), MAX_RADIUS_MILES)

    now_ts = int(time.time())
    retention_start = now_ts - (RETENTION_HOURS * 3600)
    default_window_start = now_ts - (PLAYBACK_DEFAULT_WINDOW_HOURS * 3600)

    requested_day_start = _parse_day_start_utc(day)
    requested_day = requested_day_start is not None
    explicit_window = start is not None and end is not None and end > start
    if explicit_window:
        full_start = start
        window_end = end
    elif requested_day:
        full_start = requested_day_start
        window_end = requested_day_start + 86400
    else:
        window_end = now_ts
        full_start = default_window_start

    total_duration = max(0, window_end - full_start)
    requested_start = full_start
    requested_end = window_end
    within_retention = full_start >= retention_start
    can_backfill_live = (not requested_day) and (not explicit_window)
    backfill_attempted = False
    backfill_result = None
    snapshot_flights = []
    trimmed_to_available = False
    coverage_notice = None

    flight_conn = None
    try:
        flight_conn = get_flight_db()
        cursor = flight_conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM state_vectors WHERE timestamp >= ? AND timestamp <= ?",
            (full_start, window_end),
        )
        recent_state_vector_count = int(cursor.fetchone()[0] or 0)

        track_windows = get_track_windows_near(
            flight_conn,
            lat,
            lon,
            radius,
            start_time=full_start,
            end_time=window_end,
            min_alt=min_alt,
        )
        total_unique_aircraft, available_start, available_end = _summarize_track_windows(track_windows)

        if (
            trim_to_available
            and total_unique_aircraft > 0
            and available_start is not None
            and available_end is not None
        ):
            effective_start = max(full_start, available_start)
            effective_end = min(window_end, available_end)
            if effective_end > effective_start and (
                effective_start > full_start or effective_end < window_end
            ):
                full_start = effective_start
                window_end = effective_end
                total_duration = max(0, window_end - full_start)
                trimmed_to_available = True
                coverage_notice = (
                    "Limited replay history available. Showing the most recent captured playback window for this area."
                )

        # Determine chunk size based on density
        density, suggested_min_alt = _classify_density(total_unique_aircraft)
        chunk_seconds = _chunk_seconds_for_density(density)
        chunks = _build_track_plan_chunks(track_windows, full_start, window_end, chunk_seconds)

        # If the selected area has no playback tracks, attempt a lightweight
        # on-demand backfill for testing and recompute the plan.
        if total_unique_aircraft <= 0 and can_backfill_live:
            backfill_attempted = True
            backfill_result = _backfill_area_from_opensky(lat, lon, radius)

            try:
                flight_conn.close()
            except Exception:
                pass
            flight_conn = get_flight_db()

            cursor = flight_conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM state_vectors WHERE timestamp >= ? AND timestamp <= ?",
                (full_start, window_end),
            )
            recent_state_vector_count = int(cursor.fetchone()[0] or 0)

            track_windows = get_track_windows_near(
                flight_conn,
                lat,
                lon,
                radius,
                start_time=full_start,
                end_time=window_end,
                min_alt=min_alt,
            )
            total_unique_aircraft, available_start, available_end = _summarize_track_windows(track_windows)
            density, suggested_min_alt = _classify_density(total_unique_aircraft)
            chunk_seconds = _chunk_seconds_for_density(density)
            chunks = _build_track_plan_chunks(track_windows, full_start, window_end, chunk_seconds)

        # Fallback snapshot when we have state vectors but not enough segment
        # history yet for animated playback.
        if total_unique_aircraft <= 0 and recent_state_vector_count > 0 and can_backfill_live:
            snapshot_candidates = find_flights_near(
                flight_conn,
                lat,
                lon,
                radius_miles=radius,
            )
            snapshot_flights = [
                {
                    "icao24": f.get("icao24"),
                    "callsign": f.get("callsign"),
                    "latitude": f.get("latitude"),
                    "longitude": f.get("longitude"),
                    "altitude": f.get("altitude"),
                    "heading": f.get("heading"),
                    "on_ground": bool(f.get("on_ground")),
                    "timestamp": f.get("timestamp"),
                }
                for f in snapshot_candidates[:200]
                if f.get("latitude") is not None and f.get("longitude") is not None
            ]
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
        "recent_state_vector_count": recent_state_vector_count,
        "is_collecting": (recent_state_vector_count <= 0) and can_backfill_live,
        "backfill_status": _backfill_status_payload(backfill_result, backfill_attempted),
        "snapshot_flights": snapshot_flights,
        "playback_window": {
            "start": requested_start,
            "end": requested_end,
            "effective_start": full_start,
            "effective_end": window_end,
            "day": day if requested_day else None,
            "requested_day": requested_day,
            "explicit_window": explicit_window,
            "within_retention": within_retention,
            "trimmed_to_available": trimmed_to_available,
            "coverage_notice": coverage_notice,
        },
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
