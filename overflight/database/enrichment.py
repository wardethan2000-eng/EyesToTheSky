"""
Aircraft enrichment database loader.

Loads aircraft metadata from publicly available CSV datasets into the
enrichment database. Supports the OpenSky aircraft database format and
common community CSV formats.
"""

import csv
import logging
import os
import sqlite3

from overflight.database.schema import init_enrichment_db

logger = logging.getLogger(__name__)

# Expected CSV column mappings for the OpenSky aircraft database
# Source: https://opensky-network.org/datasets/metadata/aircraftDatabase.csv
OPENSKY_COLUMNS = {
    "icao24": 0,
    "registration": 1,
    "manufacturericao": 2,
    "manufacturername": 3,
    "model": 4,
    "typecode": 5,
    "serialnumber": 6,
    "linenumber": 7,
    "icaoaircrafttype": 8,
    "operator": 9,
    "operatorcallsign": 10,
    "operatoricao": 11,
    "operatoriata": 12,
    "owner": 13,
    "testreg": 14,
    "registered": 15,
    "reguntil": 16,
    "status": 17,
    "built": 18,
    "firstflightdate": 19,
    "seatconfiguration": 20,
    "engines": 21,
    "modes": 22,
    "adsb": 23,
    "acars": 24,
    "notes": 25,
    "categoryDescription": 26,
}


def load_opensky_csv(csv_path, db_path):
    """
    Load the OpenSky aircraft database CSV into the enrichment database.

    The CSV is available at:
    https://opensky-network.org/datasets/metadata/aircraftDatabase.csv

    Args:
        csv_path: Path to the downloaded aircraftDatabase.csv file.
        db_path: Path to the enrichment SQLite database.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Aircraft database CSV not found: {csv_path}")

    conn = init_enrichment_db(db_path)
    cursor = conn.cursor()

    loaded = 0
    skipped = 0

    with open(csv_path, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # skip header row

        batch = []
        for row in reader:
            if len(row) < 19:
                skipped += 1
                continue

            icao24 = row[OPENSKY_COLUMNS["icao24"]].strip().lower()
            if not icao24:
                skipped += 1
                continue

            registration = row[OPENSKY_COLUMNS["registration"]].strip() or None
            manufacturer = row[OPENSKY_COLUMNS["manufacturername"]].strip() or None
            model = row[OPENSKY_COLUMNS["model"]].strip() or None
            operator = row[OPENSKY_COLUMNS["operator"]].strip() or None
            owner = row[OPENSKY_COLUMNS["owner"]].strip() or None
            registered_country = row[OPENSKY_COLUMNS["registered"]].strip() or None

            # Parse built year from the 'built' field (may be YYYY or YYYY-MM-DD)
            built_raw = row[OPENSKY_COLUMNS["built"]].strip()
            built_year = None
            if built_raw:
                try:
                    built_year = int(built_raw[:4])
                except (ValueError, IndexError):
                    pass

            batch.append((
                icao24, registration, manufacturer, model,
                operator, owner, built_year, registered_country,
            ))

            if len(batch) >= 5000:
                _insert_batch(cursor, batch)
                loaded += len(batch)
                batch = []

        if batch:
            _insert_batch(cursor, batch)
            loaded += len(batch)

    conn.commit()
    conn.close()
    logger.info("Loaded %d aircraft records (%d skipped)", loaded, skipped)
    return loaded


def _insert_batch(cursor, batch):
    """Insert a batch of aircraft records using INSERT OR REPLACE."""
    cursor.executemany("""
        INSERT OR REPLACE INTO aircraft
            (icao24, registration, manufacturer, model, operator, owner,
             built_year, registered_country)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, batch)


def lookup_aircraft(conn, icao24):
    """
    Look up enrichment data for a single aircraft by ICAO24 hex address.

    Args:
        conn: SQLite connection to the enrichment database.
        icao24: ICAO 24-bit hex address (lowercase).

    Returns:
        A dict with aircraft details, or None if not found.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM aircraft WHERE icao24 = ?", (icao24.lower(),))
    row = cursor.fetchone()
    if row:
        return dict(row)
    return None


def lookup_aircraft_batch(conn, icao24_list):
    """
    Look up enrichment data for multiple aircraft.

    Args:
        conn: SQLite connection to the enrichment database.
        icao24_list: List of ICAO24 hex addresses.

    Returns:
        A dict mapping icao24 -> aircraft detail dict.
    """
    if not icao24_list:
        return {}

    placeholders = ",".join("?" for _ in icao24_list)
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT * FROM aircraft WHERE icao24 IN ({placeholders})",
        [x.lower() for x in icao24_list],
    )

    results = {}
    for row in cursor.fetchall():
        results[row["icao24"]] = dict(row)
    return results
