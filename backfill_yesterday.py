#!/usr/bin/env python3
"""
Attempt to backfill yesterday's ADS-B position data from the OpenSky REST API.

OpenSky offers historical data interfaces, but the standard REST
`/states/all?time=` endpoint is currently limited to roughly 1 hour in the
past for normal authenticated users. This script is kept as an experimental
tool for legacy or special-access environments and now fails fast with a clear
message when the REST API rejects the historical request.

If you want a reliable full-day dataset on a normal API allowance, prefer live
capture with `run_poller.py` over a small bounding box for 24 hours.

Requirements:
  - OpenSky account credentials set in environment or config:
        OPENSKY_USERNAME=your_username
        OPENSKY_PASSWORD=your_password
    - Anonymous access ignores the `time` parameter.
    - Standard authenticated REST access may reject requests older than ~1 hour.

API credit usage:
    - Each small bounding-box call generally costs 1 credit.
    - 288 samples at 5-min intervals = about 288 credits if the endpoint accepts them.
    - Authenticated OpenSky REST users currently get 4000 API credits/day.

Usage:
    python backfill_yesterday.py
    python backfill_yesterday.py --interval 10   # coarser: 10-min steps (144 calls)
    python backfill_yesterday.py --bbox           # restrict to CONUS bounding box
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from overflight.config import (
    CONUS_BBOX,
    DB_PATH,
    INGEST_BBOX,
    OPENSKY_PASSWORD,
    OPENSKY_USERNAME,
)
from overflight.database.schema import init_flight_db, init_tracks_db
from overflight.database.tracks import build_tracks_incremental
from overflight.ingestion.poller import (
    fetch_state_vectors,
    insert_state_vectors,
    parse_state_vector,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Polite pause between API calls (seconds).
REQUEST_PAUSE_SECONDS = 2.0


def yesterday_window_utc():
    """Return (start_ts, end_ts) for yesterday's UTC calendar day."""
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def run_backfill(interval_minutes: int, bbox):
    if not OPENSKY_USERNAME or not OPENSKY_PASSWORD:
        logger.error(
            "OpenSky credentials are required for historical data.\n"
            "Set OPENSKY_USERNAME and OPENSKY_PASSWORD environment variables\n"
            "(or edit overflight/config.py) and re-run."
        )
        sys.exit(1)

    logger.warning(
        "OpenSky's REST API currently limits normal authenticated states/all historical lookups to about 1 hour. "
        "If this script exits immediately with a historical-limit error, use live capture instead."
    )

    start_ts, end_ts = yesterday_window_utc()
    yesterday_date = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    interval_secs = interval_minutes * 60
    sample_times = list(range(start_ts, end_ts, interval_secs))
    total = len(sample_times)

    logger.info(
        "Backfilling %s — %d samples every %d min (bbox=%s)",
        yesterday_date, total, interval_minutes,
        "CONUS" if bbox == CONUS_BBOX else (str(bbox) if bbox else "global"),
    )
    logger.info("Estimated time: ~%d min", int(total * REQUEST_PAUSE_SECONDS / 60) + 1)

    conn, has_spatialite = init_flight_db(DB_PATH, use_spatialite=False)

    total_fetched = 0
    total_inserted = 0
    errors = 0

    for idx, ts in enumerate(sample_times, 1):
        hhmm = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")
        try:
            states = fetch_state_vectors(bbox=bbox, at_time=ts)
        except (PermissionError, ValueError) as exc:
            logger.error("\n%s\n", exc)
            conn.close()
            sys.exit(1)
        except Exception as exc:
            logger.warning("Sample %d/%d (%s UTC) failed: %s", idx, total, hhmm, exc)
            errors += 1
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        if not states:
            logger.debug("Sample %d/%d (%s UTC) — no states returned", idx, total, hhmm)
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        rows = [r for r in (parse_state_vector(sv) for sv in states) if r is not None]
        inserted = insert_state_vectors(conn, rows, has_spatialite=has_spatialite)
        total_fetched += len(states)
        total_inserted += inserted

        if idx % 12 == 0 or idx == total:
            logger.info(
                "Progress %d/%d (%s UTC) — fetched=%d inserted=%d (session totals)",
                idx, total, hhmm, total_fetched, total_inserted,
            )

        time.sleep(REQUEST_PAUSE_SECONDS)

    logger.info(
        "Fetch complete. Total fetched=%d inserted=%d errors=%d",
        total_fetched, total_inserted, errors,
    )

    if total_inserted == 0:
        logger.warning(
            "No records inserted. Possible causes:\n"
            "  - OpenSky credentials don't have historical-data access\n"
            "  - No traffic over the selected bounding box yesterday\n"
            "  - API returned HTTP 403 (check credentials)"
        )
        conn.close()
        return

    logger.info("Building track segments from backfilled data...")
    segments = build_tracks_incremental(conn)
    logger.info("Done — built %d track segments for %s.", segments, yesterday_date)
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill yesterday's ADS-B data from OpenSky")
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        metavar="MINUTES",
        help="Sampling interval in minutes (default: 5). Use 10 to halve API usage.",
    )
    parser.add_argument(
        "--bbox",
        action="store_true",
        help=(
            "Restrict to the CONUS bounding box (saves credits and keeps DB small). "
            "Ignored if OVERFLIGHT_INGEST_BBOX env var is already set."
        ),
    )
    parser.add_argument("--lat", type=float, default=None, help="Center latitude for area bbox.")
    parser.add_argument("--lon", type=float, default=None, help="Center longitude for area bbox.")
    parser.add_argument(
        "--radius",
        type=float,
        default=50.0,
        help="Radius in miles around --lat/--lon (default: 50). Used when --lat/--lon are set.",
    )
    args = parser.parse_args()

    # Custom lat/lon/radius takes highest priority.
    if args.lat is not None and args.lon is not None:
        import math as _math
        lat_delta = args.radius / 69.0
        cos_lat = _math.cos(_math.radians(args.lat))
        if abs(cos_lat) < 1e-6:
            cos_lat = 1e-6
        lon_delta = args.radius / (69.0 * cos_lat)
        bbox = (
            args.lat - lat_delta,
            args.lat + lat_delta,
            args.lon - lon_delta,
            args.lon + lon_delta,
        )
        logger.info(
            "Using area bbox: lat %.4f–%.4f  lon %.4f–%.4f (radius %.0f mi)",
            bbox[0], bbox[1], bbox[2], bbox[3], args.radius,
        )
    else:
        bbox = INGEST_BBOX
        if bbox is None and args.bbox:
            bbox = CONUS_BBOX

    if args.interval < 1 or args.interval > 60:
        parser.error("--interval must be between 1 and 60 minutes")

    run_backfill(interval_minutes=args.interval, bbox=bbox)


if __name__ == "__main__":
    main()
