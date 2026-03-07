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


def cleanup_worker(db_path, interval_minutes):
    """Background thread that periodically purges old records and rebuilds tracks."""
    from overflight.database.tracks import build_tracks_incremental

    while not shutdown_event.is_set():
        shutdown_event.wait(interval_minutes * 60)
        if shutdown_event.is_set():
            break
        try:
            conn, _ = init_flight_db(db_path, use_spatialite=False)
            init_tracks_db(db_path, use_spatialite=False)
            deleted = purge_old_records(conn)
            tracks_deleted = purge_old_tracks(conn)
            tracks_built = build_tracks_incremental(conn)
            count = get_record_count(conn)
            conn.close()
            logger.info(
                "Cleanup: purged %d records, %d track segments; built %d tracks; %d records remaining",
                deleted, tracks_deleted, tracks_built, count,
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
        "--rebuild-tracks",
        action="store_true",
        help="Rebuild all track segments from current state vectors and exit",
    )
    args = parser.parse_args()

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

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Import poller here to defer the requests dependency
    from overflight.ingestion.poller import poll_once

    conn, has_spatialite = init_flight_db(args.db_path)
    bbox = CONUS_BBOX if args.conus_only else INGEST_BBOX

    logger.info(
        "Starting OverFlight poller (interval=%ds, spatialite=%s, bbox=%s)",
        POLL_INTERVAL_SECONDS,
        has_spatialite,
        bbox,
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
    while not shutdown_event.is_set():
        try:
            count = poll_once(conn, has_spatialite, bbox=bbox)
            if count > 0:
                consecutive_errors = 0
            else:
                logger.debug("Poll returned no records")
        except Exception:
            consecutive_errors += 1
            logger.exception("Poll error (consecutive: %d)", consecutive_errors)
            if consecutive_errors >= 5:
                backoff = min(consecutive_errors * POLL_INTERVAL_SECONDS, 300)
                logger.warning("Backing off for %d seconds", backoff)
                shutdown_event.wait(backoff)
                if shutdown_event.is_set():
                    break

        shutdown_event.wait(POLL_INTERVAL_SECONDS)

    conn.close()
    logger.info("Poller shut down cleanly")


if __name__ == "__main__":
    main()
