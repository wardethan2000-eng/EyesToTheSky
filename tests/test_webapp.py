"""
Tests for the OverFlight Flask web application.
"""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from overflight.database.schema import init_enrichment_db, init_flight_db, init_zipcode_db
from overflight.ingestion.poller import insert_state_vectors


class WebAppTestCase(unittest.TestCase):
    """Base class for webapp tests with test databases."""

    def setUp(self):
        """Create temp databases and configure the app to use them."""
        self.flight_db_fd, self.flight_db_path = tempfile.mkstemp(suffix=".db")
        self.enrichment_db_fd, self.enrichment_db_path = tempfile.mkstemp(suffix=".db")
        self.zipcode_db_fd, self.zipcode_db_path = tempfile.mkstemp(suffix=".db")

        # Initialize databases
        flight_conn, _ = init_flight_db(self.flight_db_path, use_spatialite=False)

        # Insert sample flight data around NYC
        now = int(time.time())
        rows = [
            ("a1b2c3", "UAL100", 40.6413, -73.7781, 10000, 250, 90, 0, 0, now - 3600),
            ("d4e5f6", "DAL200", 40.7589, -73.9851, 8000, 200, 180, -5, 0, now - 1800),
            ("g7h8i9", "AAL300", 41.8781, -87.6298, 35000, 450, 270, 0, 0, now - 900),
        ]
        insert_state_vectors(flight_conn, rows)
        flight_conn.close()

        enrichment_conn = init_enrichment_db(self.enrichment_db_path)
        enrichment_conn.execute("""
            INSERT INTO aircraft VALUES
            ('a1b2c3', 'N12345', 'Boeing', '737-800', 'United Airlines',
             'United Airlines Inc', 2005, 'United States')
        """)
        enrichment_conn.commit()
        enrichment_conn.close()

        zipcode_conn = init_zipcode_db(self.zipcode_db_path)
        zipcode_conn.execute("""
            INSERT INTO zipcodes VALUES ('10001', 40.7484, -73.9967, 'New York', 'NY')
        """)
        zipcode_conn.commit()
        zipcode_conn.close()

        # Patch config paths to point to test databases
        self.patchers = [
            patch("overflight.webapp.DB_PATH", self.flight_db_path),
            patch("overflight.webapp.ENRICHMENT_DB_PATH", self.enrichment_db_path),
            patch("overflight.webapp.ZIPCODE_DB_PATH", self.zipcode_db_path),
            patch("overflight.webapp.routes.resolve_zipcode",
                  lambda z: self._resolve_zip(z)),
        ]
        for p in self.patchers:
            p.start()

        from overflight.webapp import create_app

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def _resolve_zip(self, zipcode):
        """Test zip code resolver."""
        conn = sqlite3.connect(self.zipcode_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM zipcodes WHERE zipcode = ?", (str(zipcode).zfill(5),))
        row = cursor.fetchone()
        conn.close()
        if row:
            return dict(row)
        return None

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        os.close(self.flight_db_fd)
        os.close(self.enrichment_db_fd)
        os.close(self.zipcode_db_fd)
        os.unlink(self.flight_db_path)
        os.unlink(self.enrichment_db_path)
        os.unlink(self.zipcode_db_path)


class TestIndexPage(WebAppTestCase):
    """Test the main page renders."""

    def test_index_returns_200(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_index_contains_key_elements(self):
        resp = self.client.get("/")
        html = resp.data.decode()
        self.assertIn("OverFlight", html)
        self.assertIn("zipcode-input", html)
        self.assertIn("radius-select", html)
        self.assertIn("Use My Location", html)


class TestFlightsAPI(WebAppTestCase):
    """Test the /api/flights endpoint."""

    def test_flights_missing_params(self):
        resp = self.client.get("/api/flights")
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    def test_flights_invalid_coords(self):
        resp = self.client.get("/api/flights?lat=999&lon=999")
        self.assertEqual(resp.status_code, 400)

    def test_flights_near_nyc(self):
        resp = self.client.get("/api/flights?lat=40.758&lon=-73.9855&radius=25")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("flights", data)
        self.assertIn("count", data)
        # Should find at least the NYC-area flights
        icao_set = {f["icao24"] for f in data["flights"]}
        self.assertIn("a1b2c3", icao_set)
        self.assertIn("d4e5f6", icao_set)
        # Chicago should not appear
        self.assertNotIn("g7h8i9", icao_set)

    def test_flights_enrichment(self):
        resp = self.client.get("/api/flights?lat=40.758&lon=-73.9855&radius=25")
        data = json.loads(resp.data)
        # Find the United Airlines flight
        ual = next((f for f in data["flights"] if f["icao24"] == "a1b2c3"), None)
        self.assertIsNotNone(ual)
        self.assertEqual(ual.get("manufacturer"), "Boeing")
        self.assertEqual(ual.get("model"), "737-800")

    def test_flights_sets_cookies(self):
        resp = self.client.get("/api/flights?lat=40.758&lon=-73.9855&radius=10&zip=10001")
        # Extract Set-Cookie headers from the response
        set_cookies = resp.headers.getlist("Set-Cookie")
        cookie_str = " ".join(set_cookies)
        self.assertIn("overflight_lat=40.758", cookie_str)
        self.assertIn("overflight_zip=10001", cookie_str)


class TestResolveZipAPI(WebAppTestCase):
    """Test the /api/resolve-zip endpoint."""

    def test_resolve_valid_zip(self):
        resp = self.client.get("/api/resolve-zip?zip=10001")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["zipcode"], "10001")
        self.assertEqual(data["city"], "New York")
        self.assertAlmostEqual(data["latitude"], 40.7484, places=3)

    def test_resolve_invalid_zip_format(self):
        resp = self.client.get("/api/resolve-zip?zip=abc")
        self.assertEqual(resp.status_code, 400)

    def test_resolve_unknown_zip(self):
        resp = self.client.get("/api/resolve-zip?zip=99999")
        self.assertEqual(resp.status_code, 404)


class TestStatusAPI(WebAppTestCase):
    """Test the /api/status endpoint."""

    def test_status(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "ok")
        self.assertIn("record_count", data)


if __name__ == "__main__":
    unittest.main()
