#!/usr/bin/env python3
"""
Initialize OverFlight databases.

Creates the flight positions, aircraft enrichment, and zip code databases.
Optionally loads enrichment and zip code data from CSV files.

Usage:
    python init_db.py                          # Create empty databases
    python init_db.py --aircraft-csv FILE      # Load aircraft enrichment data
    python init_db.py --zipcode-csv FILE       # Load zip code data
    python init_db.py --aircraft-csv FILE --zipcode-csv FILE  # Load both
"""

import argparse
import logging
import sys

from overflight.config import DB_PATH, ENRICHMENT_DB_PATH, ZIPCODE_DB_PATH
from overflight.database.schema import init_enrichment_db, init_flight_db, init_zipcode_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("overflight.init")


def main():
    parser = argparse.ArgumentParser(description="Initialize OverFlight databases")
    parser.add_argument(
        "--aircraft-csv",
        help="Path to OpenSky aircraftDatabase.csv to load into enrichment DB",
    )
    parser.add_argument(
        "--zipcode-csv",
        help="Path to US zip code CSV to load into zip code DB",
    )
    parser.add_argument(
        "--flight-db",
        default=DB_PATH,
        help=f"Path for flight database (default: {DB_PATH})",
    )
    parser.add_argument(
        "--enrichment-db",
        default=ENRICHMENT_DB_PATH,
        help=f"Path for enrichment database (default: {ENRICHMENT_DB_PATH})",
    )
    parser.add_argument(
        "--zipcode-db",
        default=ZIPCODE_DB_PATH,
        help=f"Path for zip code database (default: {ZIPCODE_DB_PATH})",
    )
    args = parser.parse_args()

    # Initialize flight positions database
    logger.info("Initializing flight database: %s", args.flight_db)
    conn, has_spatialite = init_flight_db(args.flight_db)
    logger.info("Flight database ready (SpatiaLite: %s)", has_spatialite)
    conn.close()

    # Initialize enrichment database
    logger.info("Initializing enrichment database: %s", args.enrichment_db)
    conn = init_enrichment_db(args.enrichment_db)
    conn.close()
    logger.info("Enrichment database ready")

    # Initialize zip code database
    logger.info("Initializing zip code database: %s", args.zipcode_db)
    conn = init_zipcode_db(args.zipcode_db)
    conn.close()
    logger.info("Zip code database ready")

    # Load aircraft enrichment data if CSV provided
    if args.aircraft_csv:
        from overflight.database.enrichment import load_opensky_csv

        logger.info("Loading aircraft data from: %s", args.aircraft_csv)
        count = load_opensky_csv(args.aircraft_csv, args.enrichment_db)
        logger.info("Loaded %d aircraft records", count)

    # Load zip code data if CSV provided
    if args.zipcode_csv:
        from overflight.ingestion.zipcode import load_zipcode_csv

        logger.info("Loading zip code data from: %s", args.zipcode_csv)
        count = load_zipcode_csv(args.zipcode_csv, args.zipcode_db)
        logger.info("Loaded %d zip codes", count)

    logger.info("Database initialization complete!")


if __name__ == "__main__":
    main()
