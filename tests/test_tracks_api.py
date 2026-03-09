"""Tests for the OverFlight tracks API endpoints."""

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from overflight.database.schema import init_enrichment_db, init_flight_db, init_tracks_db, init_zipcode_db
from overflight.database.tracks import build_tracks
from overflight.ingestion.poller import insert_state_vectors
from overflight.webapp.routes import _chunk_seconds_for_density, _classify_density


class TracksAPITestCase(unittest.TestCase):
    """Base class for tracks API tests with test databases."""

    def setUp(self):
        """Create temp databases, insert data, build tracks, configure app."""
        self.flight_db_fd, self.flight_db_path = tempfile.mkstemp(suffix=".db")
        self.enrichment_db_fd, self.enrichment_db_path = tempfile.mkstemp(suffix=".db")
        self.zipcode_db_fd, self.zipcode_db_path = tempfile.mkstemp(suffix=".db")

        # Initialize databases
        flight_conn, _ = init_flight_db(self.flight_db_path, use_spatialite=False)
        init_tracks_db(self.flight_db_path, use_spatialite=False)

        # Insert sample flight data around NYC
        self.now = int(time.time())
        rows = []
        # Enroute aircraft near NYC (40.758, -73.985−)
        for i in range(20):
            rows.append((
                "a1b2c3", "UAL100", 40.70 + i * 0.005, -73.95 + i * 0.002,
                10000, 250, 45, 0, 0, self.now - 3600 + i * 10,
            ))
        # Second aircraft near NYC
        for i in range(15):
            rows.append((
                "d4e5f6", "DAL200", 40.80 - i * 0.003, -74.00 + i * 0.004,
                8000, 200, 180, -5, 0, self.now - 1800 + i * 10,
            ))
        # Ground aircraft at JFK
        for i in range(10):
            rows.append((
                "g7h8i9", "AAL300", 40.6413, -73.7781,
                0, 5, 90, 0, 1, self.now - 900 + i * 10,
            ))
        # Departure aircraft with ground-to-air transition near JFK
        for i in range(4):
            rows.append((
                "dep001", "JBU111", 40.6413, -73.7781,
                0, 10, 88, 0, 1, self.now - 1400 + i * 10,
            ))
        for i in range(8):
            rows.append((
                "dep001", "JBU111", 40.6413 + i * 0.008, -73.7781 + i * 0.007,
                400 + i * 300, 180, 92, 7, 0, self.now - 1360 + i * 10,
            ))
        # Far away aircraft near Chicago — should NOT appear in NYC queries
        for i in range(10):
            rows.append((
                "chi001", "SWA400", 41.88 + i * 0.005, -87.63,
                35000, 450, 270, 0, 0, self.now - 600 + i * 10,
            ))

        insert_state_vectors(flight_conn, rows)

        # Build track segments
        tracks_conn, _ = init_tracks_db(self.flight_db_path, use_spatialite=False)
        build_tracks(flight_conn)
        flight_conn.close()
        tracks_conn.close()

        # Enrichment database
        enrichment_conn = init_enrichment_db(self.enrichment_db_path)
        enrichment_conn.execute("""
            INSERT INTO aircraft VALUES
            ('a1b2c3', 'N12345', 'Boeing', '737-800', 'United Airlines',
             'United Airlines Inc', 2005, 'United States')
        """)
        enrichment_conn.execute("""
            INSERT INTO aircraft VALUES
            ('d4e5f6', 'N67890', 'Airbus', 'A320', 'Delta Air Lines',
             'Delta Air Lines Inc', 2010, 'United States')
        """)
        enrichment_conn.commit()
        enrichment_conn.close()

        # Zipcode database
        zipcode_conn = init_zipcode_db(self.zipcode_db_path)
        zipcode_conn.commit()
        zipcode_conn.close()

        # Patch config paths
        self.patchers = [
            patch("overflight.webapp.DB_PATH", self.flight_db_path),
            patch("overflight.webapp.ENRICHMENT_DB_PATH", self.enrichment_db_path),
            patch("overflight.webapp.ZIPCODE_DB_PATH", self.zipcode_db_path),
        ]
        for p in self.patchers:
            p.start()

        from overflight.webapp import create_app

        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        os.close(self.flight_db_fd)
        os.close(self.enrichment_db_fd)
        os.close(self.zipcode_db_fd)
        # On Windows, SQLite WAL files may hold locks briefly after close
        for path in [self.flight_db_path, self.enrichment_db_path, self.zipcode_db_path]:
            for suffix in ["", "-wal", "-shm"]:
                try:
                    os.unlink(path + suffix)
                except (PermissionError, FileNotFoundError):
                    pass


class TestTracksEndpoint(TracksAPITestCase):
    """Test the /api/tracks endpoint."""

    def test_tracks_missing_params(self):
        resp = self.client.get("/api/tracks")
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    def test_tracks_invalid_coords(self):
        resp = self.client.get("/api/tracks?lat=999&lon=999")
        self.assertEqual(resp.status_code, 400)

    def test_tracks_near_nyc(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=25")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("tracks", data)
        self.assertIn("meta", data)
        icao_set = {t["icao24"] for t in data["tracks"]}
        # Should find NYC aircraft
        self.assertTrue(icao_set & {"a1b2c3", "d4e5f6"})
        self.assertNotIn("g7h8i9", icao_set)
        # Chicago should not appear
        self.assertNotIn("chi001", icao_set)

    def test_tracks_response_structure(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=25")
        data = json.loads(resp.data)

        # Check meta structure
        meta = data["meta"]
        self.assertIn("count", meta)
        self.assertIn("total_in_area", meta)
        self.assertIn("density", meta)
        self.assertIn("window", meta)
        self.assertIn("query", meta)
        self.assertEqual(meta["query"]["lat"], 40.758)
        self.assertEqual(meta["query"]["lon"], -73.985)

    def test_tracks_contain_polyline(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=25")
        data = json.loads(resp.data)
        if data["tracks"]:
            track = data["tracks"][0]
            self.assertIn("polyline", track)
            self.assertIsInstance(track["polyline"], list)
            self.assertGreater(len(track["polyline"]), 0)

    def test_tracks_track_fields(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=50")
        data = json.loads(resp.data)
        self.assertGreater(len(data["tracks"]), 0)
        track = data["tracks"][0]
        for field in ["id", "icao24", "phase", "polyline", "start_time", "end_time"]:
            self.assertIn(field, track)
        for field in [
            "liftoff_lat", "liftoff_lon", "liftoff_heading", "liftoff_time",
            "touchdown_lat", "touchdown_lon", "approach_heading", "touchdown_time",
        ]:
            self.assertIn(field, track)

    def test_tracks_departure_includes_liftoff_metadata(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=50&phase=departure")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertGreater(len(data["tracks"]), 0)
        dep = data["tracks"][0]
        self.assertEqual(dep["phase"], "departure")
        self.assertIsNotNone(dep["liftoff_lat"])
        self.assertIsNotNone(dep["liftoff_lon"])
        self.assertIsNotNone(dep["liftoff_time"])

    def test_tracks_altitude_filter(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=25&min_alt=5000")
        data = json.loads(resp.data)
        # Ground aircraft should be filtered out
        for track in data["tracks"]:
            self.assertNotEqual(track["phase"], "ground")

    def test_tracks_phase_filter_enroute(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=25&phase=enroute")
        data = json.loads(resp.data)
        for track in data["tracks"]:
            self.assertEqual(track["phase"], "enroute")

    def test_tracks_phase_filter_ground(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=25&phase=ground")
        data = json.loads(resp.data)
        self.assertEqual(data["tracks"], [])

    def test_tracks_phase_filter_multiple(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=25&phase=enroute,ground")
        data = json.loads(resp.data)
        for track in data["tracks"]:
            self.assertEqual(track["phase"], "enroute")

    def test_tracks_time_window(self):
        """Tracks outside the time window are excluded."""
        future = self.now + 7200
        resp = self.client.get(
            f"/api/tracks?lat=40.758&lon=-73.985&radius=25&start={future}&end={future + 3600}"
        )
        data = json.loads(resp.data)
        self.assertEqual(len(data["tracks"]), 0)

    def test_tracks_density_field(self):
        resp = self.client.get("/api/tracks?lat=40.758&lon=-73.985&radius=25")
        data = json.loads(resp.data)
        self.assertIn(data["meta"]["density"], ["low", "medium", "high"])

    def test_tracks_no_results_at_remote_location(self):
        resp = self.client.get("/api/tracks?lat=0.0&lon=0.0&radius=10")
        data = json.loads(resp.data)
        self.assertEqual(len(data["tracks"]), 0)
        self.assertEqual(data["meta"]["count"], 0)


class TestTrackDetailEndpoint(TracksAPITestCase):
    """Test the /api/track-detail/<icao24> endpoint."""

    def test_detail_known_aircraft(self):
        resp = self.client.get("/api/track-detail/a1b2c3")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["icao24"], "a1b2c3")
        self.assertEqual(data["manufacturer"], "Boeing")
        self.assertEqual(data["model"], "737-800")
        self.assertEqual(data["operator"], "United Airlines")
        self.assertEqual(data["registration"], "N12345")
        self.assertEqual(data["built_year"], 2005)
        self.assertIsNotNone(data["aircraft_age"])
        self.assertEqual(data["registered_country"], "United States")

    def test_detail_second_aircraft(self):
        resp = self.client.get("/api/track-detail/d4e5f6")
        data = json.loads(resp.data)
        self.assertEqual(data["manufacturer"], "Airbus")
        self.assertEqual(data["model"], "A320")

    def test_detail_unknown_aircraft(self):
        resp = self.client.get("/api/track-detail/zzzzzz")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["icao24"], "zzzzzz")
        self.assertIsNone(data["manufacturer"])
        self.assertIsNone(data["model"])

    def test_detail_invalid_icao24(self):
        resp = self.client.get("/api/track-detail/toolongicao24value")
        self.assertEqual(resp.status_code, 400)

    def test_detail_case_insensitive(self):
        resp = self.client.get("/api/track-detail/A1B2C3")
        data = json.loads(resp.data)
        self.assertEqual(data["icao24"], "a1b2c3")
        self.assertEqual(data["manufacturer"], "Boeing")


class TestTracksPlanEndpoint(TracksAPITestCase):
    """Test the /api/tracks/plan endpoint."""

    def test_plan_missing_params(self):
        resp = self.client.get("/api/tracks/plan")
        self.assertEqual(resp.status_code, 400)

    def test_plan_invalid_coords(self):
        resp = self.client.get("/api/tracks/plan?lat=999&lon=999")
        self.assertEqual(resp.status_code, 400)

    def test_plan_response_structure(self):
        resp = self.client.get("/api/tracks/plan?lat=40.758&lon=-73.985&radius=25")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("total_duration_seconds", data)
        self.assertIn("chunk_count", data)
        self.assertIn("chunks", data)
        self.assertIn("density", data)
        self.assertIn("total_unique_aircraft", data)
        self.assertEqual(data["total_duration_seconds"], 24 * 3600)

    def test_plan_chunks_cover_full_window(self):
        resp = self.client.get("/api/tracks/plan?lat=40.758&lon=-73.985&radius=25")
        data = json.loads(resp.data)
        chunks = data["chunks"]
        self.assertGreater(len(chunks), 0)

        # Verify chunks are contiguous
        for i in range(1, len(chunks)):
            self.assertEqual(chunks[i]["start"], chunks[i - 1]["end"])

    def test_plan_chunk_structure(self):
        resp = self.client.get("/api/tracks/plan?lat=40.758&lon=-73.985&radius=25")
        data = json.loads(resp.data)
        for chunk in data["chunks"]:
            self.assertIn("start", chunk)
            self.assertIn("end", chunk)
            self.assertIn("estimated_tracks", chunk)
            self.assertGreater(chunk["end"], chunk["start"])

    def test_plan_density_classification(self):
        resp = self.client.get("/api/tracks/plan?lat=40.758&lon=-73.985&radius=25")
        data = json.loads(resp.data)
        self.assertIn(data["density"], ["low", "medium", "high"])

    def test_plan_explicit_time_window(self):
        start = self.now - 3600
        end = self.now
        resp = self.client.get(
            f"/api/tracks/plan?lat=40.758&lon=-73.985&radius=25&start={start}&end={end}"
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["total_duration_seconds"], 3600)
        self.assertTrue(data["playback_window"]["explicit_window"])
        self.assertEqual(data["playback_window"]["start"], start)
        self.assertEqual(data["playback_window"]["end"], end)

    def test_plan_no_tracks_area(self):
        resp = self.client.get("/api/tracks/plan?lat=0.0&lon=0.0&radius=10")
        data = json.loads(resp.data)
        self.assertEqual(data["density"], "low")
        self.assertEqual(data["total_unique_aircraft"], 0)

    def test_plan_trim_to_available_coverage(self):
        start = self.now - 24 * 3600
        end = self.now
        resp = self.client.get(
            f"/api/tracks/plan?lat=40.758&lon=-73.985&radius=25&start={start}&end={end}&trim=1"
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        playback_window = data["playback_window"]
        self.assertTrue(playback_window["trimmed_to_available"])
        self.assertEqual(playback_window["start"], start)
        self.assertEqual(playback_window["end"], end)
        self.assertGreater(playback_window["effective_start"], start)
        self.assertLessEqual(playback_window["effective_end"], end)
        self.assertLess(data["total_duration_seconds"], 24 * 3600)
        self.assertTrue(playback_window["coverage_notice"])


class TestAdaptiveChunkSizing(TracksAPITestCase):
    """Test that chunk sizing adapts to density."""

    def test_low_density_large_chunks(self):
        """Low density areas get 4-hour chunks."""
        resp = self.client.get("/api/tracks/plan?lat=40.758&lon=-73.985&radius=25")
        data = json.loads(resp.data)
        # Our test data has very few tracks, so density should be low
        self.assertEqual(data["density"], "low")
        if len(data["chunks"]) >= 2:
            chunk = data["chunks"][0]
            chunk_duration = chunk["end"] - chunk["start"]
            self.assertEqual(chunk_duration, 4 * 3600)


class TestTracksPlanHelpers(unittest.TestCase):
    """Unit tests for density thresholds and chunk sizing helpers."""

    def test_density_thresholds(self):
        self.assertEqual(_classify_density(0), ("low", None))
        self.assertEqual(_classify_density(200), ("low", None))
        self.assertEqual(_classify_density(201), ("medium", 1000))
        self.assertEqual(_classify_density(500), ("medium", 1000))
        self.assertEqual(_classify_density(501), ("high", 5000))

    def test_chunk_seconds_for_density(self):
        self.assertEqual(_chunk_seconds_for_density("low"), 4 * 3600)
        self.assertEqual(_chunk_seconds_for_density("medium"), 1 * 3600)
        self.assertEqual(_chunk_seconds_for_density("high"), 30 * 60)


if __name__ == "__main__":
    unittest.main()
