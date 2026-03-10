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

from overflight.airports import load_airports
from overflight.database.schema import init_enrichment_db, init_flight_db, init_zipcode_db
from overflight.ingestion.poller import insert_state_vectors


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
            ("a1b2c3", "UAL100", 40.6413, -73.7781, 10000, 250, 90, 0, 0, now - 3600,
             "United States", "7700", 10100, 0, 2),
            ("d4e5f6", "DAL200", 40.7589, -73.9851, 8000, 200, 180, -5, 0, now - 1800),
            ("parked1", "JBU000", 40.7592, -73.9850, 0, 5, 90, 0, 1, now - 1500),
            ("h1i2j3", "FFT400", 40.7520, -73.9900, 12000, 280, 70, 0, 0, now - 7200),
            ("g7h8i9", "AAL300", 41.8781, -87.6298, 35000, 450, 270, 0, 0, now - 900),
        ]
        insert_state_vectors(flight_conn, rows)
        flight_conn.close()

        enrichment_conn = init_enrichment_db(self.enrichment_db_path)
        insert_aircraft(
            enrichment_conn,
            "a1b2c3",
            "N12345",
            "Boeing",
            "737-800",
            "United Airlines",
            "United Airlines Inc",
            2005,
            "United States",
            typecode="B738",
            engines="2 x CFM56",
            category_description="Large",
            operator_icao="UAL",
            operator_iata="UA",
            serial_number="32456",
            status="active",
        )
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

    def test_index_busts_static_asset_cache(self):
        resp = self.client.get("/")
        html = resp.data.decode()
        self.assertIn("css/style.css?v=", html)
        self.assertIn("js/playback.js?v=", html)
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store, no-cache, must-revalidate, max-age=0")

    def test_index_contains_playback_tuning_config(self):
        resp = self.client.get("/")
        html = resp.data.decode()
        self.assertIn("playbackTargetFps", html)
        self.assertIn("departurePreviewSeconds", html)
        self.assertIn("chunkFetchRetryMax", html)
        self.assertIn("chunkFetchBackoffMs", html)
        self.assertIn("chunkFailureCooldownMs", html)

    def test_index_contains_expanded_playback_speeds(self):
        resp = self.client.get("/")
        html = resp.data.decode()
        self.assertIn('value="15"', html)
        self.assertIn('value="60"', html)
        self.assertIn('value="240"', html)
        self.assertIn('value="960"', html)
        self.assertIn('id="playback-speed-note"', html)

    def test_index_contains_card_time_filters(self):
        resp = self.client.get("/")
        html = resp.data.decode()
        self.assertIn("Cards Time Range", html)
        self.assertIn('data-window="last_hour"', html)
        self.assertIn('data-window="today"', html)
        self.assertIn('data-window="yesterday"', html)
        self.assertIn('data-window="last_24_hours"', html)


class TestDevHarness(WebAppTestCase):
    """Test developer playback harness route."""

    def test_playback_harness_route(self):
        resp = self.client.get("/dev/playback-harness")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("Playback State Harness", html)
        self.assertIn("btn-retry-success", html)


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
        self.assertNotIn("parked1", icao_set)
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
        self.assertEqual(ual.get("typecode"), "B738")
        self.assertEqual(ual.get("category_description"), "Large")

    def test_flights_include_new_live_fields(self):
        resp = self.client.get("/api/flights?lat=40.758&lon=-73.9855&radius=25")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        ual = next((f for f in data["flights"] if f["icao24"] == "a1b2c3"), None)
        self.assertIsNotNone(ual)
        self.assertEqual(ual.get("origin_country"), "United States")
        self.assertEqual(ual.get("squawk"), "7700")
        self.assertEqual(ual.get("squawk_meaning"), "Emergency")
        self.assertEqual(ual.get("position_source_label"), "MLAT")
        self.assertIsNotNone(ual.get("geo_altitude_feet"))

    def test_flights_sets_cookies(self):
        resp = self.client.get("/api/flights?lat=40.758&lon=-73.9855&radius=10&zip=10001")
        # Extract Set-Cookie headers from the response
        set_cookies = resp.headers.getlist("Set-Cookie")
        cookie_str = " ".join(set_cookies)
        self.assertIn("overflight_lat=40.758", cookie_str)
        self.assertIn("overflight_zip=10001", cookie_str)

    def test_flights_last_hour_filter(self):
        resp = self.client.get("/api/flights?lat=40.758&lon=-73.9855&radius=25&window=last_hour")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        icao_set = {f["icao24"] for f in data["flights"]}
        self.assertIn("d4e5f6", icao_set)
        self.assertNotIn("h1i2j3", icao_set)
        self.assertEqual(data["query"]["window"], "last_hour")
        self.assertEqual(data["query"]["window_label"], "Last hour")


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


class TestAirportsAPI(WebAppTestCase):
    """Test the /api/airports endpoint."""

    def test_airports_missing_params(self):
        resp = self.client.get("/api/airports")
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    def test_airports_invalid_coords(self):
        resp = self.client.get("/api/airports?lat=999&lon=999")
        self.assertEqual(resp.status_code, 400)

    def test_airports_near_nyc(self):
        resp = self.client.get("/api/airports?lat=40.758&lon=-73.9855&radius=25")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("airports", data)
        self.assertIn("meta", data)
        airport_codes = {airport["icao"] for airport in data["airports"]}
        self.assertIn("KJFK", airport_codes)
        self.assertIn("KLGA", airport_codes)
        self.assertIn("KEWR", airport_codes)
        self.assertNotIn("KORD", airport_codes)

    def test_airports_include_display_metadata(self):
        resp = self.client.get("/api/airports?lat=37.687&lon=-97.33&radius=20")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertGreater(len(data["airports"]), 0)
        airport = data["airports"][0]
        self.assertIn("display_code", airport)
        self.assertIn("importance", airport)
        self.assertIn("distance_miles", airport)

    def test_wichita_airports_include_runways(self):
        resp = self.client.get("/api/airports?lat=37.6872&lon=-97.3301&radius=35")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        ict = next((airport for airport in data["airports"] if airport["icao"] == "KICT"), None)
        self.assertIsNotNone(ict)
        self.assertIn("runways", ict)
        self.assertGreaterEqual(len(ict["runways"]), 1)
        self.assertIn("centerline", ict["runways"][0])

    def test_non_wichita_airports_get_schematic_runways(self):
        resp = self.client.get("/api/airports?lat=40.758&lon=-73.9855&radius=25")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        jfk = next((airport for airport in data["airports"] if airport["icao"] == "KJFK"), None)
        self.assertIsNotNone(jfk)
        self.assertIn("runways", jfk)
        self.assertGreaterEqual(len(jfk["runways"]), 1)
        self.assertTrue(jfk["runways"][0].get("schematic"))
        self.assertIn("centerline", jfk["runways"][0])

    def test_all_bundled_airports_have_runway_geometry(self):
        airports = load_airports()
        self.assertGreater(len(airports), 0)
        for airport in airports:
            self.assertGreaterEqual(len(airport.get("runways") or []), 1)
            first = airport["runways"][0]
            self.assertIn("centerline", first)
            self.assertGreaterEqual(len(first["centerline"]), 2)


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
