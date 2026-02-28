"""
Auto-purge cleanup job for the flight positions database.

Deletes state vector records older than the configured retention period
(default 24 hours) to keep the database lean.
"""

import logging
import sqlite3
import time

from overflight.config import RETENTION_HOURS

logger = logging.getLogger(__name__)


def purge_old_records(conn, retention_hours=None):
    """
    Delete state vector records older than the retention period.

    Args:
        conn: SQLite connection to the flight database.
        retention_hours: Hours of data to retain. Defaults to config value.

    Returns:
        Number of rows deleted.
    """
    if retention_hours is None:
        retention_hours = RETENTION_HOURS

    cutoff = int(time.time()) - (retention_hours * 3600)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM state_vectors WHERE timestamp < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()

    if deleted > 0:
        logger.info("Purged %d records older than %d hours", deleted, retention_hours)

    return deleted


def vacuum_database(conn):
    """
    Run VACUUM to reclaim disk space after large deletes.

    This should be run less frequently than purge (e.g., once per day)
    as it rewrites the entire database file.
    """
    conn.execute("VACUUM")
    logger.info("Database VACUUM complete")


def get_record_count(conn):
    """Return the total number of state vector records."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM state_vectors")
    return cursor.fetchone()[0]


def get_oldest_record_age(conn):
    """Return the age in hours of the oldest record, or None if empty."""
    cursor = conn.cursor()
    cursor.execute("SELECT MIN(timestamp) FROM state_vectors")
    row = cursor.fetchone()
    if row and row[0]:
        age_seconds = int(time.time()) - row[0]
        return age_seconds / 3600.0
    return None
