"""
Tests for OverFlight database schema, queries, enrichment, and cleanup.
"""

import os
import sqlite3
import tempfile
import time
import unittest

from overflight.database.cleanup import get_oldest_record_age, get_record_count, purge_old_records
from overflight.database.enrichment import load_opensky_csv, lookup_aircraft, lookup_aircraft_batch
from overflight.database.queries import (
    _bounding_box,
    enrich_results,
    find_flights_near,
    haversine_distance,
)
from overflight.database.schema import init_enrichment_db, init_flight_db, init_zipcode_db
from overflight.ingestion.poller import insert_state_vectors, parse_state_vector
from overflight.ingestion.zipcode import resolve_zipcode


def insert_aircraft(conn, icao24, registration, manufacturer, model, operator, owner,
                    built_year, registered_country, **extra_fields):
    values = {
        "icao24": icao24,
        "registration": registration,
        "manufacturer": manufacturer,
        "model": model,
        "operator": operator,
        "owner": owner,
        "built_year": built_year,
        "registered_country": registered_country,
        "typecode": None,
        "icao_aircraft_type": None,
        "engines": None,
        "first_flight_date": None,
        "seat_configuration": None,
        "category_description": None,
        "operator_icao": None,
        "operator_iata": None,
        "serial_number": None,
        "status": None,
    }
    values.update(extra_fields)

    conn.execute(
        """
        INSERT INTO aircraft (
            icao24, registration, manufacturer, model, operator, owner,
            built_year, registered_country, typecode, icao_aircraft_type,
            engines, first_flight_date, seat_configuration, category_description,
            operator_icao, operator_iata, serial_number, status
        ) VALUES (
            :icao24, :registration, :manufacturer, :model, :operator, :owner,
            :built_year, :registered_country, :typecode, :icao_aircraft_type,
            :engines, :first_flight_date, :seat_configuration, :category_description,
            :operator_icao, :operator_iata, :serial_number, :status
        )
        """,
        values,
    )


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
            cursor.execute("PRAGMA table_info(state_vectors)")
            columns = {row[1] for row in cursor.fetchall()}
            self.assertIn("origin_country", columns)
            self.assertIn("squawk", columns)
            self.assertIn("geo_altitude", columns)
            self.assertIn("spi", columns)
            self.assertIn("position_source", columns)
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
            cursor.execute("PRAGMA table_info(aircraft)")
            columns = {row[1] for row in cursor.fetchall()}
            self.assertIn("typecode", columns)
            self.assertIn("engines", columns)
            self.assertIn("category_description", columns)
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
        self.assertEqual(result[10], "United States")
        self.assertIsNone(result[11])
        self.assertAlmostEqual(result[12], 10050.0)
        self.assertEqual(result[13], 0)
        self.assertEqual(result[14], 0)

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
            # Parked aircraft near Manhattan that never flies
            ("parked1", "JBU000", 40.7595, -73.9845, 0, 4, 180, 0, 1, now - 1200),
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
        self.assertEqual(count, 6)

    def test_find_flights_near_nyc(self):
        self._insert_sample_data()
        # Query near NYC (Times Square: 40.7580, -73.9855)
        results = find_flights_near(self.conn, 40.7580, -73.9855, radius_miles=25, hours=24)

        # Should find UAL100 (JFK area) and DAL200 (Manhattan) but not AAL300 (Chicago)
        icao_set = {r["icao24"] for r in results}
        self.assertIn("a1b2c3", icao_set)
        self.assertIn("d4e5f6", icao_set)
        self.assertNotIn("parked1", icao_set)
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

    def test_low_confidence_not_on_ground_state_is_excluded(self):
        now = int(time.time())
        rows = [
            ("ghost01", "GND001", 40.7589, -73.9851, None, 0, 90, 0, 0, now - 300),
            ("flight1", "UAL100", 40.7600, -73.9860, 500, 90, 90, 3, 0, now - 200),
        ]
        insert_state_vectors(self.conn, rows)

        results = find_flights_near(self.conn, 40.7580, -73.9855, radius_miles=5, hours=24)
        icao_set = {r["icao24"] for r in results}
        self.assertIn("flight1", icao_set)
        self.assertNotIn("ghost01", icao_set)


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
        insert_aircraft(
            self.conn,
            "abc123",
            "N12345",
            "Boeing",
            "737-800",
            "United Airlines",
            "United Airlines Inc",
            2005,
            "United States",
            typecode="B738",
            icao_aircraft_type="L2J",
            engines="2 x CFM56",
            first_flight_date="2005-03-01",
            seat_configuration="16F 144Y",
            category_description="Large",
            operator_icao="UAL",
            operator_iata="UA",
            serial_number="32456",
            status="active",
        )
        insert_aircraft(
            self.conn,
            "def456",
            "N67890",
            "Airbus",
            "A320-200",
            "Delta Air Lines",
            "Delta Air Lines Inc",
            2010,
            "United States",
            typecode="A320",
            engines="2 x CFM56",
            category_description="Large",
        )
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
        self.assertEqual(result["typecode"], "B738")
        self.assertEqual(result["category_description"], "Large")

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
                "geo_altitude": 10100,
                "squawk": "7700",
                "position_source": 2,
            }
        ]
        enriched = enrich_results(flight_results, self.conn)
        self.assertEqual(enriched[0]["manufacturer"], "Boeing")
        self.assertEqual(enriched[0]["model"], "737-800")
        self.assertIsNotNone(enriched[0]["aircraft_age"])
        self.assertIsNotNone(enriched[0]["altitude_feet"])
        self.assertEqual(enriched[0]["typecode"], "B738")
        self.assertEqual(enriched[0]["position_source_label"], "MLAT")
        self.assertEqual(enriched[0]["squawk_meaning"], "Emergency")
        self.assertIsNotNone(enriched[0]["geo_altitude_feet"])

    def test_squawk_meaning_non_special_code(self):
        flight_results = [{
            "icao24": "abc123",
            "altitude": 10000,
            "velocity": 250,
            "position_source": 0,
            "squawk": "1200",
        }]
        enriched = enrich_results(flight_results, self.conn)
        self.assertEqual(enriched[0]["position_source_label"], "ADS-B")
        self.assertIsNone(enriched[0]["squawk_meaning"])

    def test_load_opensky_csv_new_fields(self):
        csv_fd, csv_path = tempfile.mkstemp(suffix=".csv")
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(csv_fd)
        os.close(db_fd)
        try:
            header = ",".join(str(i) for i in range(27))
            row = [""] * 27
            row[0] = "abc999"
            row[1] = "N54321"
            row[3] = "Cessna"
            row[4] = "172S"
            row[5] = "C172"
            row[6] = "SN172"
            row[8] = "L1P"
            row[9] = "Example Air Club"
            row[11] = "EXA"
            row[12] = "E1"
            row[13] = "Example Owner"
            row[15] = "United States"
            row[17] = "active"
            row[18] = "2002"
            row[19] = "2002-05-04"
            row[20] = "4"
            row[21] = "1 x Lycoming"
            row[26] = "Light"
            with open(csv_path, "w", encoding="utf-8") as handle:
                handle.write(header + "\n")
                handle.write(",".join(row) + "\n")

            load_opensky_csv(csv_path, db_path, rebuild=True)
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            aircraft = lookup_aircraft(conn, "abc999")
            conn.close()

            self.assertEqual(aircraft["typecode"], "C172")
            self.assertEqual(aircraft["engines"], "1 x Lycoming")
            self.assertEqual(aircraft["category_description"], "Light")
            self.assertEqual(aircraft["operator_icao"], "EXA")
        finally:
            os.unlink(csv_path)
            for suffix in ["", "-wal", "-shm"]:
                try:
                    os.unlink(db_path + suffix)
                except FileNotFoundError:
                    pass


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
