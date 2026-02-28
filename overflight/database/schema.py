"""
Database schema creation and management for OverFlight.

Supports two modes:
- SpatiaLite: Uses spatial indexes and geometry functions (preferred for production)
- Pure SQLite: Uses compound indexes on lat/lon with bounding-box queries (fallback)
"""

import os
import sqlite3


def _spatialite_available(conn):
    """Check if SpatiaLite extension can be loaded."""
    try:
        conn.enable_load_extension(True)
        conn.load_extension("mod_spatialite")
        return True
    except (sqlite3.OperationalError, AttributeError):
        return False


def get_connection(db_path, use_spatialite=True):
    """
    Open a SQLite connection, optionally loading SpatiaLite.

    Returns (connection, has_spatialite) tuple.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    has_spatialite = False
    if use_spatialite:
        has_spatialite = _spatialite_available(conn)
        if has_spatialite:
            conn.execute("SELECT InitSpatialMetaData(1)")

    return conn, has_spatialite


def init_flight_db(db_path, use_spatialite=True):
    """
    Initialize the flight positions database.

    Creates the state_vectors table for storing aircraft position reports
    from the OpenSky API, with appropriate indexes for spatial and temporal
    queries.
    """
    conn, has_spatialite = get_connection(db_path, use_spatialite)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS state_vectors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            icao24 TEXT NOT NULL,
            callsign TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            altitude REAL,
            velocity REAL,
            heading REAL,
            vertical_rate REAL,
            on_ground INTEGER NOT NULL DEFAULT 0,
            timestamp INTEGER NOT NULL
        )
    """)

    # Index for temporal queries and auto-purge
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_vectors_timestamp
        ON state_vectors (timestamp)
    """)

    # Index for ICAO24 lookups (deduplication and enrichment joins)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_vectors_icao24
        ON state_vectors (icao24)
    """)

    # Compound index for spatial bounding-box queries
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_vectors_lat_lon
        ON state_vectors (latitude, longitude)
    """)

    # Compound index for time-bounded spatial queries (the primary query pattern)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_state_vectors_time_lat_lon
        ON state_vectors (timestamp, latitude, longitude)
    """)

    if has_spatialite:
        # Add a geometry column and spatial index for SpatiaLite
        try:
            cursor.execute("""
                SELECT AddGeometryColumn('state_vectors', 'geom', 4326, 'POINT', 'XY')
            """)
        except sqlite3.OperationalError:
            pass  # Column already exists

        try:
            cursor.execute("""
                SELECT CreateSpatialIndex('state_vectors', 'geom')
            """)
        except sqlite3.OperationalError:
            pass  # Index already exists

    conn.commit()
    return conn, has_spatialite


def init_enrichment_db(db_path):
    """
    Initialize the aircraft enrichment database.

    This is a static lookup table mapping ICAO24 hex addresses to
    human-readable aircraft details (type, operator, registration, etc.).
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS aircraft (
            icao24 TEXT PRIMARY KEY,
            registration TEXT,
            manufacturer TEXT,
            model TEXT,
            operator TEXT,
            owner TEXT,
            built_year INTEGER,
            registered_country TEXT
        )
    """)

    # Index for fast lookups during enrichment joins
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_aircraft_registration
        ON aircraft (registration)
    """)

    conn.commit()
    return conn


def init_zipcode_db(db_path):
    """
    Initialize the zip code lookup database.

    Maps US zip codes to centroid lat/lon coordinates for location resolution.
    """
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS zipcodes (
            zipcode TEXT PRIMARY KEY,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            city TEXT,
            state TEXT
        )
    """)

    conn.commit()
    return conn
