#!/usr/bin/env python3
"""
Entry point for the OverFlight data ingestion pipeline.

Runs the OpenSky API poller continuously, with periodic cleanup of
old records. Designed to be managed by systemd or supervisord.

Usage:
    python run_poller.py
    python run_poller.py --conus-only     # Filter to Continental US only
    python run_poller.py --cleanup-only   # Run a single cleanup pass and exit
"""

import argparse
import logging
import math
import signal
import sys
import threading
import time

from overflight.config import (
    CLEANUP_INTERVAL_MINUTES,
    CONUS_BBOX,
    DB_PATH,
    INGEST_BBOX,
    POLL_INTERVAL_SECONDS,
    TRACK_BUILD_INTERVAL_SECONDS,
)
from overflight.database.cleanup import (
    get_record_count,
    purge_old_records,
    purge_old_tracks,
    vacuum_database,
)
from overflight.database.schema import init_flight_db, init_tracks_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("overflight.poller")

shutdown_event = threading.Event()

AREA_PRESETS = {
    "wichita-ks": (37.6889, -97.3361),
}


def _parse_bbox(value):
    """Parse CLI bbox argument as: min_lat,max_lat,min_lon,max_lon."""
    if not value:
        return None

    try:
        parts = [float(x.strip()) for x in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("bbox values must be numeric") from exc

    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "bbox must have 4 comma-separated values: min_lat,max_lat,min_lon,max_lon"
        )

    min_lat, max_lat, min_lon, max_lon = parts
    if not (-90 <= min_lat <= 90 and -90 <= max_lat <= 90):
        raise argparse.ArgumentTypeError("bbox latitude values must be between -90 and 90")
    if not (-180 <= min_lon <= 180 and -180 <= max_lon <= 180):
        raise argparse.ArgumentTypeError("bbox longitude values must be between -180 and 180")
    if min_lat >= max_lat or min_lon >= max_lon:
        raise argparse.ArgumentTypeError("bbox min values must be less than max values")

    return (min_lat, max_lat, min_lon, max_lon)


def _bbox_from_center(lat, lon, radius_miles):
    """Build an approximate bounding box around a center point."""
    lat_delta = radius_miles / 69.0
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-6:
        cos_lat = 1e-6 if cos_lat >= 0 else -1e-6
    lon_delta = radius_miles / (69.0 * cos_lat)
    return (lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta)


def cleanup_worker(db_path, interval_minutes):
    """Background thread that periodically purges old records and rebuilds tracks."""
    while not shutdown_event.is_set():
        shutdown_event.wait(interval_minutes * 60)
        if shutdown_event.is_set():
            break
        try:
            conn, _ = init_flight_db(db_path, use_spatialite=False)
            init_tracks_db(db_path, use_spatialite=False)
            deleted = purge_old_records(conn)
            tracks_deleted = purge_old_tracks(conn)
            count = get_record_count(conn)
            conn.close()
            logger.info(
                "Cleanup: purged %d records, %d track segments; %d records remaining",
                deleted, tracks_deleted, count,
            )
        except Exception:
            logger.exception("Cleanup error")


def track_build_worker(db_path, interval_seconds):
    """Background thread that incrementally builds track segments."""
    from overflight.database.tracks import build_tracks_incremental

    while not shutdown_event.is_set():
        shutdown_event.wait(interval_seconds)
        if shutdown_event.is_set():
            break
        try:
            conn, _ = init_flight_db(db_path, use_spatialite=False)
            init_tracks_db(db_path, use_spatialite=False)
            count = build_tracks_incremental(conn)
            conn.close()
            if count > 0:
                logger.debug("Track build: created %d segments", count)
        except Exception:
            logger.exception("Track build error")


def signal_handler(signum, frame):
    logger.info("Received signal %d, shutting down...", signum)
    shutdown_event.set()


def main():
    parser = argparse.ArgumentParser(description="OverFlight data ingestion poller")
    parser.add_argument(
        "--conus-only",
        action="store_true",
        help="Only ingest data within the Continental US bounding box",
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Run a single cleanup pass and exit",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Run VACUUM after cleanup (use with --cleanup-only)",
    )
    parser.add_argument(
        "--db-path",
        default=DB_PATH,
        help=f"Path to flight database (default: {DB_PATH})",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=(
            "Polling interval in seconds "
            f"(default from config: {POLL_INTERVAL_SECONDS})"
        ),
    )
    parser.add_argument(
        "--max-idle-backoff",
        type=int,
        default=300,
        help=(
            "Max seconds to wait between polls when repeated polls return no records "
            "(default: 300)"
        ),
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=0,
        help=(
            "Stop after N poll attempts (default: 0 = run forever). "
            "Useful for low-credit testing."
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one poll attempt, then exit.",
    )
    parser.add_argument(
        "--bbox",
        type=_parse_bbox,
        help=(
            "Override ingest bounding box with "
            "min_lat,max_lat,min_lon,max_lon (e.g. 40.4,41.0,-74.3,-73.6)"
        ),
    )
    parser.add_argument(
        "--lat",
        type=float,
        help="Center latitude for a focused area capture.",
    )
    parser.add_argument(
        "--lon",
        type=float,
        help="Center longitude for a focused area capture.",
    )
    parser.add_argument(
        "--radius",
        type=float,
        default=25.0,
        help="Radius in miles for --lat/--lon or --preset-area capture (default: 25).",
    )
    parser.add_argument(
        "--preset-area",
        choices=sorted(AREA_PRESETS.keys()),
        help="Named test area preset. Current option: wichita-ks.",
    )
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=0.0,
        help=(
            "Stop after approximately this many hours of polling. "
            "If set and --max-polls is not provided, max polls is derived from the poll interval."
        ),
    )
    parser.add_argument(
        "--test-mode",
        action="store_true",
        help=(
            "Enable conservative defaults for API-credit-friendly testing: "
            "forces CONUS bbox (unless --bbox provided), raises poll interval to >=60s, "
            "and limits to 3 polls (unless overridden)."
        ),
    )
    parser.add_argument(
        "--rebuild-tracks",
        action="store_true",
        help="Rebuild all track segments from current state vectors and exit",
    )
    args = parser.parse_args()

    if args.poll_interval < 1:
        parser.error("--poll-interval must be >= 1")
    if args.max_idle_backoff < args.poll_interval:
        parser.error("--max-idle-backoff must be >= --poll-interval")
    if args.max_polls < 0:
        parser.error("--max-polls must be >= 0")
    if args.radius <= 0:
        parser.error("--radius must be > 0")
    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon must be provided together")
    if args.duration_hours < 0:
        parser.error("--duration-hours must be >= 0")
    if args.preset_area and (args.lat is not None or args.lon is not None):
        parser.error("Use either --preset-area or --lat/--lon, not both")

    preset_lat = None
    preset_lon = None
    if args.preset_area:
        preset_lat, preset_lon = AREA_PRESETS[args.preset_area]

    area_bbox = None
    if preset_lat is not None and preset_lon is not None:
        area_bbox = _bbox_from_center(preset_lat, preset_lon, args.radius)
    elif args.lat is not None and args.lon is not None:
        area_bbox = _bbox_from_center(args.lat, args.lon, args.radius)

    if args.test_mode:
        # Apply safer defaults for local/manual testing without overriding explicit flags.
        if "--poll-interval" not in sys.argv and args.poll_interval < 60:
            args.poll_interval = 60
        if "--max-polls" not in sys.argv and args.max_polls == 0:
            args.max_polls = 3
        if args.bbox is None and not args.conus_only:
            args.conus_only = True

        logger.info(
            "Test mode enabled: poll_interval=%ds, max_polls=%d, conus_only=%s, bbox=%s",
            args.poll_interval,
            args.max_polls,
            args.conus_only,
            args.bbox,
        )

    if args.duration_hours > 0 and "--max-polls" not in sys.argv:
        args.max_polls = max(1, math.ceil((args.duration_hours * 3600) / args.poll_interval))

    if args.rebuild_tracks:
        from overflight.database.tracks import build_tracks

        conn, _ = init_flight_db(args.db_path, use_spatialite=False)
        init_tracks_db(args.db_path, use_spatialite=False)
        conn.execute("DELETE FROM track_segments")
        conn.commit()
        count = build_tracks(conn)
        logger.info("Rebuilt %d track segments", count)
        conn.close()
        return

    if args.cleanup_only:
        conn, _ = init_flight_db(args.db_path, use_spatialite=False)
        deleted = purge_old_records(conn)
        count = get_record_count(conn)
        logger.info("Cleanup: purged %d records, %d remaining", deleted, count)
        if args.vacuum:
            vacuum_database(conn)
        conn.close()
        return

    if args.once:
        args.max_polls = 1

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Import poller here to defer the requests dependency
    from overflight.ingestion.poller import poll_once

    conn, has_spatialite = init_flight_db(args.db_path)
    bbox = args.bbox if args.bbox is not None else area_bbox
    if bbox is None:
        bbox = CONUS_BBOX if args.conus_only else INGEST_BBOX

    logger.info(
        (
            "Starting OverFlight poller "
            "(interval=%ds, max_idle_backoff=%ds, max_polls=%s, spatialite=%s, bbox=%s)"
        ),
        args.poll_interval,
        args.max_idle_backoff,
        args.max_polls if args.max_polls > 0 else "unlimited",
        has_spatialite,
        bbox,
    )

    if area_bbox is not None:
        center_lat = preset_lat if preset_lat is not None else args.lat
        center_lon = preset_lon if preset_lon is not None else args.lon
        logger.info(
            "Area capture mode: center=(%.4f, %.4f), radius=%.1fmi, approx daily credits at current interval=%d",
            center_lat,
            center_lon,
            args.radius,
            math.ceil(86400 / args.poll_interval),
        )

    # Initialize tracks table
    init_tracks_db(args.db_path, use_spatialite=False)

    # Start cleanup background thread
    cleanup_thread = threading.Thread(
        target=cleanup_worker,
        args=(args.db_path, CLEANUP_INTERVAL_MINUTES),
        daemon=True,
    )
    cleanup_thread.start()

    # Start track building background thread
    track_thread = threading.Thread(
        target=track_build_worker,
        args=(args.db_path, TRACK_BUILD_INTERVAL_SECONDS),
        daemon=True,
    )
    track_thread.start()

    consecutive_errors = 0
    idle_polls = 0
    poll_attempts = 0

    while not shutdown_event.is_set():
        if args.max_polls and poll_attempts >= args.max_polls:
            logger.info("Reached max poll attempts (%d), exiting", args.max_polls)
            break

        wait_seconds = args.poll_interval
        try:
            count = poll_once(conn, has_spatialite, bbox=bbox)
            poll_attempts += 1
            if count > 0:
                consecutive_errors = 0
                idle_polls = 0
            else:
                idle_polls += 1
                logger.debug("Poll returned no records")

                # Exponential backoff on empty polls to reduce API credit usage.
                wait_seconds = min(
                    args.poll_interval * (2 ** min(idle_polls, 4)),
                    args.max_idle_backoff,
                )
                logger.info(
                    "No records returned (idle streak=%d); next poll in %ds",
                    idle_polls,
                    wait_seconds,
                )
        except Exception:
            poll_attempts += 1
            consecutive_errors += 1
            logger.exception("Poll error (consecutive: %d)", consecutive_errors)
            if consecutive_errors >= 5:
                backoff = min(consecutive_errors * args.poll_interval, args.max_idle_backoff)
                logger.warning("Backing off for %d seconds", backoff)
                wait_seconds = backoff

        shutdown_event.wait(wait_seconds)

    conn.close()
    logger.info("Poller shut down cleanly")


if __name__ == "__main__":
    main()
