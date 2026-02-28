"""
Tests for OverFlight database schema, queries, enrichment, and cleanup.
"""

import os
import sqlite3
import tempfile
import time
import unittest

from overflight.database.cleanup import get_oldest_record_age, get_record_count, purge_old_records
from overflight.database.enrichment import lookup_aircraft, lookup_aircraft_batch
from overflight.database.queries import (
    _bounding_box,
    enrich_results,
    find_flights_near,
    haversine_distance,
)
from overflight.database.schema import init_enrichment_db, init_flight_db, init_zipcode_db
from overflight.ingestion.poller import insert_state_vectors, parse_state_vector
from overflight.ingestion.zipcode import resolve_zipcode


class TestSchema(unittest.TestCase):
    """Test database schema creation."""

    def test_init_flight_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn, has_spatialite = init_flight_db(db_path, use_spatialite=False)
            # Verify table exists
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='state_vectors'"
            )
            self.assertIsNotNone(cursor.fetchone())
            conn.close()
        finally:
            os.unlink(db_path)

    def test_init_enrichment_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = init_enrichment_db(db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='aircraft'"
            )
            self.assertIsNotNone(cursor.fetchone())
            conn.close()
        finally:
            os.unlink(db_path)

    def test_init_zipcode_db(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = init_zipcode_db(db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='zipcodes'"
            )
            self.assertIsNotNone(cursor.fetchone())
            conn.close()
        finally:
            os.unlink(db_path)


class TestHaversine(unittest.TestCase):
    """Test haversine distance calculations."""

    def test_same_point(self):
        dist = haversine_distance(40.0, -74.0, 40.0, -74.0)
        self.assertAlmostEqual(dist, 0.0, places=5)

    def test_known_distance(self):
        # New York to Los Angeles: approximately 2,451 miles
        dist = haversine_distance(40.7128, -74.0060, 34.0522, -118.2437)
        self.assertAlmostEqual(dist, 2451, delta=10)

    def test_short_distance(self):
        # About 1 mile apart
        dist = haversine_distance(40.0, -74.0, 40.0145, -74.0)
        self.assertAlmostEqual(dist, 1.0, delta=0.1)


class TestBoundingBox(unittest.TestCase):
    """Test bounding box calculation."""

    def test_bounding_box_contains_center(self):
        min_lat, max_lat, min_lon, max_lon = _bounding_box(40.0, -74.0, 10)
        self.assertLess(min_lat, 40.0)
        self.assertGreater(max_lat, 40.0)
        self.assertLess(min_lon, -74.0)
        self.assertGreater(max_lon, -74.0)

    def test_bounding_box_size_scales_with_radius(self):
        small = _bounding_box(40.0, -74.0, 5)
        large = _bounding_box(40.0, -74.0, 25)
        # Larger radius should produce a wider box
        self.assertGreater(large[1] - large[0], small[1] - small[0])


class TestParseStateVector(unittest.TestCase):
    """Test OpenSky state vector parsing."""

    def _make_sv(self, **overrides):
        """Create a sample state vector array."""
        sv = [
            "abc123",    # icao24
            "UAL1234 ",  # callsign
            "United States",  # origin_country
            int(time.time()),  # time_position
            int(time.time()),  # last_contact
            -74.0060,    # longitude
            40.7128,     # latitude
            10000.0,     # baro_altitude
            False,       # on_ground
            250.0,       # velocity
            90.0,        # true_track
            0.0,         # vertical_rate
            None,        # sensors
            10050.0,     # geo_altitude
            None,        # squawk
            False,       # spi
            0,           # position_source
        ]
        for key, val in overrides.items():
            if key == "icao24":
                sv[0] = val
            elif key == "latitude":
                sv[6] = val
            elif key == "longitude":
                sv[5] = val
        return sv

    def test_parse_valid(self):
        sv = self._make_sv()
        result = parse_state_vector(sv)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "abc123")
        self.assertEqual(result[1], "UAL1234")  # stripped
        self.assertAlmostEqual(result[2], 40.7128)
        self.assertAlmostEqual(result[3], -74.0060)

    def test_parse_missing_position(self):
        sv = self._make_sv(latitude=None)
        result = parse_state_vector(sv)
        self.assertIsNone(result)

    def test_parse_missing_icao(self):
        sv = self._make_sv(icao24="")
        result = parse_state_vector(sv)
        self.assertIsNone(result)


class TestInsertAndQuery(unittest.TestCase):
    """Test inserting state vectors and querying them spatially."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.conn, _ = init_flight_db(self.db_path, use_spatialite=False)

    def tearDown(self):
        self.conn.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_sample_data(self):
        """Insert some sample state vectors around New York City."""
        now = int(time.time())
        rows = [
            # Aircraft near JFK (40.6413, -73.7781)
            ("a1b2c3", "UAL100", 40.6413, -73.7781, 10000, 250, 90, 0, 0, now - 3600),
            # Aircraft over Manhattan (40.7589, -73.9851)
            ("d4e5f6", "DAL200", 40.7589, -73.9851, 8000, 200, 180, -5, 0, now - 1800),
            # Aircraft far away in Chicago (41.8781, -87.6298)
            ("g7h8i9", "AAL300", 41.8781, -87.6298, 35000, 450, 270, 0, 0, now - 900),
            # Same aircraft near JFK at a different time
            ("a1b2c3", "UAL100", 40.6500, -73.7700, 9500, 240, 85, -2, 0, now - 600),
            # Old record that should be purged
            ("j0k1l2", "SWA400", 40.7000, -74.0000, 5000, 150, 45, 0, 0, now - 90000),
        ]
        insert_state_vectors(self.conn, rows)
        return now

    def test_insert_and_count(self):
        self._insert_sample_data()
        count = get_record_count(self.conn)
        self.assertEqual(count, 5)

    def test_find_flights_near_nyc(self):
        self._insert_sample_data()
        # Query near NYC (Times Square: 40.7580, -73.9855)
        results = find_flights_near(self.conn, 40.7580, -73.9855, radius_miles=25, hours=24)

        # Should find UAL100 (JFK area) and DAL200 (Manhattan) but not AAL300 (Chicago)
        icao_set = {r["icao24"] for r in results}
        self.assertIn("a1b2c3", icao_set)
        self.assertIn("d4e5f6", icao_set)
        self.assertNotIn("g7h8i9", icao_set)

    def test_deduplication(self):
        self._insert_sample_data()
        results = find_flights_near(self.conn, 40.7580, -73.9855, radius_miles=25, hours=24)

        # a1b2c3 appears twice but should be deduplicated to closest approach
        a1_results = [r for r in results if r["icao24"] == "a1b2c3"]
        self.assertEqual(len(a1_results), 1)

    def test_results_sorted_by_time(self):
        self._insert_sample_data()
        results = find_flights_near(self.conn, 40.7580, -73.9855, radius_miles=25, hours=24)

        if len(results) > 1:
            for i in range(len(results) - 1):
                self.assertGreaterEqual(
                    results[i]["timestamp"], results[i + 1]["timestamp"]
                )

    def test_radius_filter(self):
        self._insert_sample_data()
        # Very small radius should only catch the Manhattan aircraft
        results = find_flights_near(self.conn, 40.7580, -73.9855, radius_miles=1, hours=24)
        icao_set = {r["icao24"] for r in results}
        self.assertIn("d4e5f6", icao_set)  # DAL200 in Manhattan
        self.assertNotIn("g7h8i9", icao_set)  # AAL300 in Chicago

    def test_distance_included(self):
        self._insert_sample_data()
        results = find_flights_near(self.conn, 40.7580, -73.9855, radius_miles=25, hours=24)
        for r in results:
            self.assertIn("distance_miles", r)
            self.assertGreaterEqual(r["distance_miles"], 0)


class TestCleanup(unittest.TestCase):
    """Test auto-purge cleanup."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.conn, _ = init_flight_db(self.db_path, use_spatialite=False)

    def tearDown(self):
        self.conn.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_purge_old_records(self):
        now = int(time.time())
        rows = [
            ("abc123", None, 40.0, -74.0, 10000, 250, 90, 0, 0, now - 100),
            ("def456", None, 41.0, -75.0, 8000, 200, 180, 0, 0, now - 90000),  # >24h old
        ]
        insert_state_vectors(self.conn, rows)
        self.assertEqual(get_record_count(self.conn), 2)

        deleted = purge_old_records(self.conn, retention_hours=24)
        self.assertEqual(deleted, 1)
        self.assertEqual(get_record_count(self.conn), 1)

    def test_purge_empty_db(self):
        deleted = purge_old_records(self.conn)
        self.assertEqual(deleted, 0)


class TestEnrichment(unittest.TestCase):
    """Test aircraft enrichment lookup."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        self.conn = init_enrichment_db(self.db_path)
        # Insert sample aircraft
        self.conn.execute("""
            INSERT INTO aircraft VALUES
            ('abc123', 'N12345', 'Boeing', '737-800', 'United Airlines',
             'United Airlines Inc', 2005, 'United States')
        """)
        self.conn.execute("""
            INSERT INTO aircraft VALUES
            ('def456', 'N67890', 'Airbus', 'A320-200', 'Delta Air Lines',
             'Delta Air Lines Inc', 2010, 'United States')
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_lookup_single(self):
        result = lookup_aircraft(self.conn, "abc123")
        self.assertIsNotNone(result)
        self.assertEqual(result["manufacturer"], "Boeing")
        self.assertEqual(result["model"], "737-800")
        self.assertEqual(result["operator"], "United Airlines")

    def test_lookup_not_found(self):
        result = lookup_aircraft(self.conn, "zzz999")
        self.assertIsNone(result)

    def test_lookup_batch(self):
        results = lookup_aircraft_batch(self.conn, ["abc123", "def456", "zzz999"])
        self.assertEqual(len(results), 2)
        self.assertIn("abc123", results)
        self.assertIn("def456", results)
        self.assertNotIn("zzz999", results)

    def test_enrich_results(self):
        flight_results = [
            {
                "icao24": "abc123",
                "callsign": "UAL100",
                "latitude": 40.7,
                "longitude": -74.0,
                "altitude": 10000,
                "velocity": 250,
                "heading": 90,
                "vertical_rate": 0,
                "on_ground": 0,
                "timestamp": int(time.time()),
                "distance_miles": 5.0,
            }
        ]
        enriched = enrich_results(flight_results, self.conn)
        self.assertEqual(enriched[0]["manufacturer"], "Boeing")
        self.assertEqual(enriched[0]["model"], "737-800")
        self.assertIsNotNone(enriched[0]["aircraft_age"])
        self.assertIsNotNone(enriched[0]["altitude_feet"])


class TestZipcode(unittest.TestCase):
    """Test zip code resolution."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = init_zipcode_db(self.db_path)
        conn.execute("""
            INSERT INTO zipcodes VALUES ('10001', 40.7484, -73.9967, 'New York', 'NY')
        """)
        conn.execute("""
            INSERT INTO zipcodes VALUES ('90210', 34.0901, -118.4065, 'Beverly Hills', 'CA')
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_resolve_valid_zipcode(self):
        result = resolve_zipcode("10001", db_path=self.db_path)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["latitude"], 40.7484, places=3)
        self.assertAlmostEqual(result["longitude"], -73.9967, places=3)
        self.assertEqual(result["city"], "New York")
        self.assertEqual(result["state"], "NY")

    def test_resolve_invalid_zipcode(self):
        result = resolve_zipcode("99999", db_path=self.db_path)
        self.assertIsNone(result)

    def test_resolve_zero_padded(self):
        result = resolve_zipcode("10001", db_path=self.db_path)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
