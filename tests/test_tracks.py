"""Tests for track segment building, querying, and cleanup."""

import json
import os
import sqlite3
import tempfile
import time
import unittest

from overflight.database.cleanup import purge_old_tracks
from overflight.database.schema import init_flight_db, init_tracks_db
from overflight.database.tracks import (
    build_tracks,
    build_tracks_incremental,
    get_track_density,
    get_tracks_near,
    get_track_windows_near,
)
from overflight.webapp.routes import _build_track_plan_chunks


def _create_test_db():
    """Create a temporary flight database with tracks table."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn, _ = init_flight_db(db_path, use_spatialite=False)
    tracks_conn, _ = init_tracks_db(db_path, use_spatialite=False)
    tracks_conn.close()
    return conn, db_path


def _insert_state_vectors(conn, rows):
    """Insert test state vector rows."""
    conn.executemany("""
        INSERT INTO state_vectors
            (icao24, callsign, latitude, longitude, altitude,
             velocity, heading, vertical_rate, on_ground, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()


class TestTrackBuilding(unittest.TestCase):
    """Test track segment creation from raw state vectors."""

    def setUp(self):
        self.conn, self.db_path = _create_test_db()
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_build_single_aircraft_enroute(self):
        """A single aircraft with continuous enroute points creates one segment."""
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0 + i * 0.01,
             10000, 250, 45, 0, 0, self.now - 600 + i * 10)
            for i in range(20)
        ]
        _insert_state_vectors(self.conn, rows)

        count = build_tracks(self.conn)
        self.assertEqual(count, 1)

        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM track_segments")
        track = dict(cursor.fetchone())

        self.assertEqual(track["icao24"], "abc123")
        self.assertEqual(track["callsign"], "UAL100")
        self.assertEqual(track["phase"], "enroute")
        self.assertGreaterEqual(track["point_count"], 2)

        polyline = json.loads(track["polyline"])
        self.assertGreaterEqual(len(polyline), 2)
        self.assertLessEqual(len(polyline), 20)

    def test_build_multiple_aircraft(self):
        """Multiple aircraft create separate track segments."""
        rows = []
        for icao in ["aaa111", "bbb222", "ccc333"]:
            for i in range(10):
                rows.append((
                    icao, f"CS{icao[:3]}", 40.0 + i * 0.01, -74.0,
                    10000, 200, 0, 0, 0, self.now - 600 + i * 10
                ))
        _insert_state_vectors(self.conn, rows)

        count = build_tracks(self.conn)
        self.assertEqual(count, 3)

    def test_time_gap_creates_new_segment(self):
        """A >5 minute gap in reports splits into separate segments."""
        rows = []
        # First segment: 5 points
        for i in range(5):
            rows.append((
                "abc123", "UAL100", 40.0 + i * 0.01, -74.0,
                10000, 250, 0, 0, 0, self.now - 3600 + i * 10
            ))
        # 10 minute gap, then second segment: 5 points
        for i in range(5):
            rows.append((
                "abc123", "UAL100", 41.0 + i * 0.01, -74.0,
                10000, 250, 0, 0, 0, self.now - 3000 + i * 10
            ))
        _insert_state_vectors(self.conn, rows)

        count = build_tracks(self.conn)
        self.assertEqual(count, 2)

    def test_ground_air_transition_creates_segments(self):
        """Ground-to-air transition creates a departure segment."""
        rows = []
        # Ground phase
        for i in range(5):
            rows.append((
                "abc123", "UAL100", 40.0, -74.0,
                0, 10, 90, 0, 1, self.now - 600 + i * 10
            ))
        # Airborne phase (immediately following)
        for i in range(10):
            rows.append((
                "abc123", "UAL100", 40.0 + i * 0.01, -74.0 + i * 0.01,
                1000 + i * 500, 200, 45, 5, 0, self.now - 550 + i * 10
            ))
        _insert_state_vectors(self.conn, rows)

        count = build_tracks(self.conn)
        self.assertEqual(count, 1)

        cursor = self.conn.cursor()
        cursor.execute("SELECT phase FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], "departure")

    def test_phase_classification_arrival(self):
        """Air-to-ground transition is classified as arrival."""
        rows = []
        for i in range(8):
            rows.append((
                "abc123", "UAL100", 40.5 - i * 0.01, -74.0,
                4000 - i * 400, 180, 190, -5, 0, self.now - 200 + i * 10
            ))
        for i in range(5):
            rows.append((
                "abc123", "UAL100", 40.42, -74.0,
                0, 8, 180, 0, 1, self.now - 120 + i * 10
            ))

        _insert_state_vectors(self.conn, rows)
        count = build_tracks(self.conn)
        self.assertEqual(count, 1)

        cursor = self.conn.cursor()
        cursor.execute("SELECT phase FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], "arrival")

    def test_departure_metadata_captures_liftoff(self):
        """Departure segments store liftoff position/time/heading metadata."""
        rows = []
        # Ground roll
        for i in range(4):
            rows.append((
                "dep123", "DEP100", 40.6413, -73.7781,
                0, 12, 90, 0, 1, self.now - 200 + i * 10
            ))
        # Airborne climb (rapid enough for departure detection)
        for i in range(6):
            rows.append((
                "dep123", "DEP100", 40.6413 + i * 0.01, -73.7781 + i * 0.01,
                400 + i * 250, 160, 90, 8, 0, self.now - 160 + i * 10
            ))

        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT phase, liftoff_lat, liftoff_lon, liftoff_heading, liftoff_time
            FROM track_segments
            WHERE icao24 = 'dep123'
            """
        )
        row = cursor.fetchone()
        self.assertEqual(row[0], "departure")
        self.assertIsNotNone(row[1])
        self.assertIsNotNone(row[2])
        self.assertIsNotNone(row[3])
        self.assertIsNotNone(row[4])

    def test_arrival_metadata_captures_touchdown(self):
        """Arrival segments store touchdown and approach metadata."""
        rows = []
        # Descending approach
        for i in range(6):
            rows.append((
                "arr123", "ARR200", 40.70 - i * 0.01, -73.80 + i * 0.002,
                1200 - i * 150, 140, 185, -4, 0, self.now - 180 + i * 10
            ))
        # On ground after touchdown
        for i in range(4):
            rows.append((
                "arr123", "ARR200", 40.64, -73.78,
                0, 7, 180, 0, 1, self.now - 120 + i * 10
            ))

        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT phase, touchdown_lat, touchdown_lon, approach_heading, touchdown_time
            FROM track_segments
            WHERE icao24 = 'arr123'
            """
        )
        row = cursor.fetchone()
        self.assertEqual(row[0], "arrival")
        self.assertIsNotNone(row[1])
        self.assertIsNotNone(row[2])
        self.assertIsNotNone(row[3])
        self.assertIsNotNone(row[4])

    def test_ground_only_segments_are_skipped(self):
        """All on-ground points are ignored because they never flew."""
        rows = [
            ("abc123", "UAL100", 40.0, -74.0, 0, 5, 90, 0, 1, self.now - 100 + i * 10)
            for i in range(5)
        ]
        _insert_state_vectors(self.conn, rows)

        count = build_tracks(self.conn)
        self.assertEqual(count, 0)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], 0)

    def test_phase_classification_enroute(self):
        """All airborne points classified as 'enroute'."""
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0, 10000, 250, 0, 0, 0,
             self.now - 100 + i * 10)
            for i in range(5)
        ]
        _insert_state_vectors(self.conn, rows)

        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT phase FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], "enroute")

    def test_low_confidence_not_on_ground_segment_is_skipped(self):
        """Sparse false airborne states should not create a visible track."""
        rows = [
            ("ghost01", "GND001", 40.6413, -73.7781, None, 0, 90, 0, 0, self.now - 120 + i * 10)
            for i in range(4)
        ]
        _insert_state_vectors(self.conn, rows)

        count = build_tracks(self.conn)
        self.assertEqual(count, 0)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], 0)

    def test_bounding_box_computed(self):
        """Track segment has correct bounding box."""
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.05, -74.0 + i * 0.03,
             10000, 250, 45, 0, 0, self.now - 100 + i * 10)
            for i in range(10)
        ]
        _insert_state_vectors(self.conn, rows)

        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT min_lat, max_lat, min_lon, max_lon FROM track_segments")
        row = cursor.fetchone()
        self.assertAlmostEqual(row[0], 40.0, places=2)
        self.assertAlmostEqual(row[1], 40.45, places=2)
        self.assertAlmostEqual(row[2], -74.0, places=2)
        self.assertAlmostEqual(row[3], -73.73, places=2)

    def test_altitude_range(self):
        """Track segment records min and max altitude."""
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0,
             5000 + i * 500, 250, 0, 5, 0, self.now - 100 + i * 10)
            for i in range(10)
        ]
        _insert_state_vectors(self.conn, rows)

        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT min_altitude, max_altitude FROM track_segments")
        row = cursor.fetchone()
        self.assertEqual(row[0], 5000)
        self.assertEqual(row[1], 9500)

    def test_single_point_skipped(self):
        """A segment with only 1 point is not created."""
        rows = [("abc123", "UAL100", 40.0, -74.0, 10000, 250, 0, 0, 0, self.now)]
        _insert_state_vectors(self.conn, rows)

        count = build_tracks(self.conn)
        self.assertEqual(count, 0)

    def test_since_timestamp_filter(self):
        """build_tracks with since_timestamp only processes newer data."""
        # Old data
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0, 10000, 250, 0, 0, 0,
             self.now - 7200 + i * 10)
            for i in range(5)
        ]
        # New data
        rows += [
            ("def456", "DAL200", 41.0 + i * 0.01, -73.0, 8000, 200, 90, 0, 0,
             self.now - 100 + i * 10)
            for i in range(5)
        ]
        _insert_state_vectors(self.conn, rows)

        count = build_tracks(self.conn, since_timestamp=self.now - 200)
        self.assertEqual(count, 1)

        cursor = self.conn.cursor()
        cursor.execute("SELECT icao24 FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], "def456")

    def test_chunk_planning_matches_density_queries(self):
        """Chunk overlap estimation matches the prior per-chunk density query behavior."""
        rows = []
        for i in range(12):
            rows.append((
                "chunk01", "CHK101", 40.0 + i * 0.01, -74.0,
                5000 + i * 100, 180, 45, 3, 0, self.now - 900 + i * 30
            ))
        for i in range(10):
            rows.append((
                "chunk02", "CHK202", 40.1 + i * 0.008, -74.1,
                7000, 220, 90, 0, 0, self.now - 600 + i * 30
            ))

        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

        full_start = self.now - 1000
        window_end = self.now
        chunk_seconds = 300
        track_windows = get_track_windows_near(
            self.conn,
            40.05,
            -74.05,
            25,
            start_time=full_start,
            end_time=window_end,
        )
        chunks = _build_track_plan_chunks(track_windows, full_start, window_end, chunk_seconds)

        expected = [
            get_track_density(
                self.conn,
                40.05,
                -74.05,
                25,
                start_time=chunk["start"],
                end_time=chunk["end"],
            )
            for chunk in chunks
        ]

        self.assertEqual([chunk["estimated_tracks"] for chunk in chunks], expected)

    def test_full_rebuild_replaces_existing_segments(self):
        """Repeated full rebuilds should not duplicate derived track rows."""
        rows = [
            (
                "abc123", "UAL100", 40.0 + i * 0.01, -74.0 + i * 0.01,
                10000, 250, 45, 0, 0, self.now - 600 + i * 10
            )
            for i in range(10)
        ]
        _insert_state_vectors(self.conn, rows)

        first_count = build_tracks(self.conn)
        self.assertEqual(first_count, 1)

        second_count = build_tracks(self.conn)
        self.assertEqual(second_count, 1)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], 1)


class TestIncrementalBuild(unittest.TestCase):
    """Test incremental track building."""

    def setUp(self):
        self.conn, self.db_path = _create_test_db()
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_incremental_build_first_run(self):
        """First incremental build processes all data."""
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0, 10000, 250, 0, 0, 0,
             self.now - 100 + i * 10)
            for i in range(10)
        ]
        _insert_state_vectors(self.conn, rows)

        count = build_tracks_incremental(self.conn)
        self.assertEqual(count, 1)

    def test_incremental_build_updates_metadata(self):
        """Incremental build stores the last build timestamp."""
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0, 10000, 250, 0, 0, 0,
             self.now - 100 + i * 10)
            for i in range(5)
        ]
        _insert_state_vectors(self.conn, rows)

        build_tracks_incremental(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM track_build_meta WHERE key = 'last_build_timestamp'")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertGreater(int(row[0]), 0)

    def test_incremental_build_preserves_existing_track_start(self):
        """Ongoing flights keep their original start time across incremental rebuilds."""
        base_time = self.now - 1200
        initial_rows = [
            (
                "abc123", "UAL100", 40.0 + i * 0.01, -74.0 + i * 0.01,
                10000, 250, 45, 0, 0, base_time + i * 60
            )
            for i in range(11)
        ]
        _insert_state_vectors(self.conn, initial_rows)

        first_count = build_tracks_incremental(self.conn)
        self.assertEqual(first_count, 1)

        cursor = self.conn.cursor()
        cursor.execute("SELECT start_time, end_time FROM track_segments WHERE icao24 = 'abc123'")
        first_start, first_end = cursor.fetchone()
        self.assertEqual(first_start, base_time)

        later_rows = [
            (
                "abc123", "UAL100", 40.11 + i * 0.01, -73.89 + i * 0.01,
                10000, 250, 45, 0, 0, base_time + 660 + i * 60
            )
            for i in range(5)
        ]
        _insert_state_vectors(self.conn, later_rows)

        second_count = build_tracks_incremental(self.conn)
        self.assertEqual(second_count, 1)

        cursor.execute("SELECT start_time, end_time FROM track_segments WHERE icao24 = 'abc123'")
        second_start, second_end = cursor.fetchone()
        self.assertEqual(second_start, base_time)
        self.assertGreater(second_end, first_end)


class TestTrackQueries(unittest.TestCase):
    """Test spatial and temporal track queries."""

    def setUp(self):
        self.conn, self.db_path = _create_test_db()
        self.now = int(time.time())
        self._insert_test_tracks()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def _insert_test_tracks(self):
        """Insert state vectors and build tracks for query testing."""
        rows = []
        # Aircraft near NYC (40.758, -73.985)
        for i in range(10):
            rows.append((
                "nyc001", "UAL100", 40.75 + i * 0.005, -73.98,
                10000, 250, 0, 0, 0, self.now - 600 + i * 10
            ))
        # Aircraft near LA (34.05, -118.25) — far away
        for i in range(10):
            rows.append((
                "lax001", "DAL200", 34.05 + i * 0.005, -118.25,
                8000, 200, 90, 0, 0, self.now - 600 + i * 10
            ))
        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

    def test_find_tracks_near_location(self):
        """Tracks near a location are returned."""
        results = get_tracks_near(self.conn, 40.758, -73.985, 25)
        icao24s = [t["icao24"] for t in results]
        self.assertIn("nyc001", icao24s)
        self.assertNotIn("lax001", icao24s)

    def test_tracks_include_parsed_polyline(self):
        """Returned tracks include polyline as a parsed list."""
        results = get_tracks_near(self.conn, 40.758, -73.985, 25)
        self.assertGreater(len(results), 0)
        track = results[0]
        self.assertIsInstance(track["polyline"], list)
        self.assertGreater(len(track["polyline"]), 0)

    def test_temporal_filter(self):
        """Tracks outside the time window are excluded."""
        # Search for future time window — should find nothing
        future = self.now + 7200
        results = get_tracks_near(self.conn, 40.758, -73.985, 25,
                                  start_time=future, end_time=future + 3600)
        self.assertEqual(len(results), 0)

    def test_track_density(self):
        """Track density returns correct count."""
        count = get_track_density(self.conn, 40.758, -73.985, 25)
        self.assertEqual(count, 1)  # Only nyc001

    def test_density_far_location_zero(self):
        """Density at a location with no nearby tracks returns 0."""
        count = get_track_density(self.conn, 0.0, 0.0, 10)
        self.assertEqual(count, 0)


class TestTrackCleanup(unittest.TestCase):
    """Test track segment cleanup/purge."""

    def setUp(self):
        self.conn, self.db_path = _create_test_db()
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_purge_old_tracks(self):
        """Old track segments are deleted."""
        # Insert old state vectors (48 hours ago)
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0, 10000, 250, 0, 0, 0,
             self.now - 48 * 3600 + i * 10)
            for i in range(5)
        ]
        # Insert recent state vectors
        rows += [
            ("def456", "DAL200", 41.0 + i * 0.01, -73.0, 8000, 200, 90, 0, 0,
             self.now - 100 + i * 10)
            for i in range(5)
        ]
        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], 2)

        deleted = purge_old_tracks(self.conn, retention_hours=24)
        self.assertEqual(deleted, 1)

        cursor.execute("SELECT COUNT(*) FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], 1)

        cursor.execute("SELECT icao24 FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], "def456")

    def test_purge_nothing_when_all_recent(self):
        """No tracks deleted when all are within retention."""
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0, 10000, 250, 0, 0, 0,
             self.now - 100 + i * 10)
            for i in range(5)
        ]
        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

        deleted = purge_old_tracks(self.conn, retention_hours=24)
        self.assertEqual(deleted, 0)


class TestTrackMetadata(unittest.TestCase):
    """Test track segment metadata correctness."""

    def setUp(self):
        self.conn, self.db_path = _create_test_db()
        self.now = int(time.time())

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def test_avg_velocity(self):
        """Average velocity is computed correctly."""
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0,
             10000, 200 + i * 10, 0, 0, 0, self.now - 100 + i * 10)
            for i in range(5)
        ]
        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT avg_velocity FROM track_segments")
        avg_vel = cursor.fetchone()[0]
        # Expected: mean of [200, 210, 220, 230, 240] = 220
        self.assertAlmostEqual(avg_vel, 220.0, places=1)

    def test_callsign_most_common(self):
        """Most common non-null callsign is used."""
        rows = [
            ("abc123", "UAL100", 40.0, -74.0, 10000, 250, 0, 0, 0, self.now - 50),
            ("abc123", "UAL100", 40.01, -74.0, 10000, 250, 0, 0, 0, self.now - 40),
            ("abc123", None, 40.02, -74.0, 10000, 250, 0, 0, 0, self.now - 30),
            ("abc123", "UAL100", 40.03, -74.0, 10000, 250, 0, 0, 0, self.now - 20),
            ("abc123", "UAL101", 40.04, -74.0, 10000, 250, 0, 0, 0, self.now - 10),
        ]
        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT callsign FROM track_segments")
        self.assertEqual(cursor.fetchone()[0], "UAL100")

    def test_time_range(self):
        """Start and end times match the data."""
        start = self.now - 200
        end = self.now - 100
        rows = [
            ("abc123", "UAL100", 40.0 + i * 0.01, -74.0, 10000, 250, 0, 0, 0,
             start + i * 10)
            for i in range(11)  # 0 to 100 seconds
        ]
        _insert_state_vectors(self.conn, rows)
        build_tracks(self.conn)

        cursor = self.conn.cursor()
        cursor.execute("SELECT start_time, end_time FROM track_segments")
        row = cursor.fetchone()
        self.assertEqual(row[0], start)
        self.assertEqual(row[1], end)


if __name__ == "__main__":
    unittest.main()
