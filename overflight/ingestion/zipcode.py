"""
US zip code to coordinates resolver.

Uses a bundled static dataset to resolve zip codes to centroid lat/lon
coordinates without requiring any external API calls.
"""

import csv
import logging
import os
import sqlite3

import requests

from overflight.config import ZIPCODE_DB_PATH
from overflight.database.schema import init_zipcode_db

logger = logging.getLogger(__name__)

ZIP_REMOTE_FALLBACK_ENV = "OVERFLIGHT_ZIP_REMOTE_FALLBACK"
ZIP_REMOTE_TIMEOUT_SECONDS = 6


def load_zipcode_csv(csv_path, db_path=None):
    """
    Load a zip code CSV file into the zip code lookup database.

    Expects a CSV with at minimum columns: zip/zipcode, lat/latitude, lng/longitude.
    Optionally also: city, state.

    Common free sources:
    - https://github.com/scpike/us-state-county-zip (zip_code_database.csv)
    - https://simplemaps.com/data/us-zips (uszips.csv)

    Args:
        csv_path: Path to the zip code CSV file.
        db_path: Path to the zip code database. Defaults to config value.
    """
    if db_path is None:
        db_path = ZIPCODE_DB_PATH

    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Zip code CSV not found: {csv_path}")

    conn = init_zipcode_db(db_path)
    cursor = conn.cursor()

    loaded = 0
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        # Normalize column names (different datasets use different names)
        fieldnames = [name.lower().strip() for name in reader.fieldnames]
        reader.fieldnames = fieldnames

        batch = []
        for row in reader:
            # Try common column name variants
            zipcode = (
                row.get("zip")
                or row.get("zipcode")
                or row.get("zip_code")
                or row.get("postal_code")
            )
            lat = row.get("lat") or row.get("latitude")
            lon = row.get("lng") or row.get("longitude") or row.get("lon")
            city = row.get("city") or row.get("primary_city")
            state = row.get("state") or row.get("state_id") or row.get("state_abbr")

            if not zipcode or not lat or not lon:
                continue

            try:
                batch.append((
                    zipcode.strip().zfill(5),
                    float(lat),
                    float(lon),
                    city.strip() if city else None,
                    state.strip() if state else None,
                ))
            except (ValueError, AttributeError):
                continue

            if len(batch) >= 5000:
                _insert_batch(cursor, batch)
                loaded += len(batch)
                batch = []

        if batch:
            _insert_batch(cursor, batch)
            loaded += len(batch)

    conn.commit()
    conn.close()
    logger.info("Loaded %d zip codes", loaded)
    return loaded


def _insert_batch(cursor, batch):
    """Insert a batch of zip code records."""
    cursor.executemany("""
        INSERT OR REPLACE INTO zipcodes (zipcode, latitude, longitude, city, state)
        VALUES (?, ?, ?, ?, ?)
    """, batch)


def resolve_zipcode(zipcode, db_path=None):
    """
    Resolve a US zip code to lat/lon coordinates.

    Args:
        zipcode: A 5-digit US zip code string.
        db_path: Path to the zip code database. Defaults to config value.

    Returns:
        A dict with keys: zipcode, latitude, longitude, city, state.
        Returns None if the zip code is not found.
    """
    using_default_db = db_path is None
    if db_path is None:
        db_path = ZIPCODE_DB_PATH

    zipcode = str(zipcode).strip().zfill(5)

    # Remote fallback is enabled by default for app usage. It is intentionally
    # disabled when callers pass an explicit db_path (tests and deterministic scripts).
    allow_remote_fallback = (
        using_default_db and os.environ.get(ZIP_REMOTE_FALLBACK_ENV, "1") != "0"
    )

    if not os.path.exists(db_path):
        logger.warning("Zip code database not found: %s", db_path)
        if allow_remote_fallback:
            remote = _resolve_zipcode_remote(zipcode)
            if remote:
                _cache_zipcode_record(remote, db_path)
            return remote
        return None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM zipcodes WHERE zipcode = ?",
        (zipcode,),
    )
    row = cursor.fetchone()
    conn.close()

    if row:
        return dict(row)

    if allow_remote_fallback:
        remote = _resolve_zipcode_remote(zipcode)
        if remote:
            _cache_zipcode_record(remote, db_path)
        return remote

    return None


def _resolve_zipcode_remote(zipcode):
    """Resolve zipcode from a public API as a fallback when local data is absent."""
    url = f"https://api.zippopotam.us/us/{zipcode}"
    try:
        response = requests.get(url, timeout=ZIP_REMOTE_TIMEOUT_SECONDS)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()

        places = payload.get("places") or []
        if not places:
            return None

        place = places[0]
        lat = place.get("latitude")
        lon = place.get("longitude")
        if lat is None or lon is None:
            return None

        return {
            "zipcode": str(payload.get("post code", zipcode)).zfill(5),
            "latitude": float(lat),
            "longitude": float(lon),
            "city": place.get("place name"),
            "state": place.get("state abbreviation") or place.get("state"),
        }
    except Exception:
        logger.warning("Remote zip lookup failed for %s", zipcode, exc_info=True)
        return None


def _cache_zipcode_record(record, db_path):
    """Store a remotely resolved zipcode locally to reduce repeated network lookups."""
    try:
        conn = init_zipcode_db(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO zipcodes (zipcode, latitude, longitude, city, state)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                record["zipcode"],
                record["latitude"],
                record["longitude"],
                record.get("city"),
                record.get("state"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.warning("Failed caching zipcode %s", record.get("zipcode"), exc_info=True)
