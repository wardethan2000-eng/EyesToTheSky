# OverFlight — Project Review

## Project Overview

OverFlight is a Flask + SQLite application that ingests aircraft ADS-B position
data from the OpenSky Network API, stores it locally, builds track segments, and
visualizes 24-hour flight activity on a MapLibre GL map with animated playback.

## What's Done Well

- **Clean separation of concerns** — config, database, ingestion, webapp, and
  utility modules are well-separated with clear responsibilities.
- **Dual query strategy** — SpatiaLite spatial index when available,
  bounding-box + haversine fallback otherwise.
- **Incremental track building** with overlap windows to avoid data loss from
  late-arriving reports.
- **Adaptive chunk-based playback** that scales chunk sizes with density to
  balance network requests and rendering cost.
- **Good test coverage** — 5 test files covering schema, queries, tracks,
  simplification, and API endpoints.
- **Graceful degradation** — fallback to snapshot view when track history is
  sparse, on-demand backfill from OpenSky.
- **Proper error handling** in the poller with exponential backoff and
  configurable retry limits.
- **Responsive CSS** with mobile-aware sidebar drag gestures.

## Areas for Improvement

### 1. Security Issues

**`FLASK_SECRET_KEY` defaults to a hardcoded string** (`config.py:74`).
This is fine for dev but dangerous if deployed without setting the env var.
Consider logging a warning at startup or refusing to start in non-debug mode
without a proper key.

**No rate limiting on API endpoints.** The `/api/flights`, `/api/tracks`, and
`/api/tracks/plan` endpoints have no rate limiting. The on-demand backfill has a
cooldown, but database queries can still be hammered. Consider adding
`flask-limiter` or similar.

**Unpinned CDN dependencies** (`index.html`). `maplibre-gl@4.7.1` is loaded
from `unpkg.com`. Consider self-hosting or using subresource integrity (SRI)
hashes.

### 2. Code Duplication

**Bounding box calculation is duplicated 3 times:**
- `queries.py:39` (`_bounding_box`)
- `tracks.py:26` (`_safe_lon_delta` + inline calculation)
- `routes.py:61` (`_bbox_for_area`)

All three compute the same lat/lon bounding box with the same 69.0 miles/degree
constant. Extract to a single shared utility function.

**Query builder pattern duplicated in `tracks.py`:** `get_tracks_near`,
`get_track_density`, and `get_unique_aircraft_count` share ~90% identical
SQL/parameter logic. Extract a shared query builder helper.

**`escapeHtml` and `formatNumber` duplicated** between `app.js` and `map.js`.
Extract to a shared `utils.js`.

### 3. Database Connection Management

**Connections are opened/closed manually everywhere** with try/finally blocks.
Consider using context managers or Flask's `g` object with
`teardown_appcontext` for request-scoped connections.

**Inconsistent connection patterns:** `get_flight_db()` opens read-only
connections but `_backfill_area_from_opensky` opens read-write connections by
calling `init_flight_db()` directly — potential WAL-mode contention.

### 4. Performance Concerns

**`build_tracks` loads all state vectors into memory** (`tracks.py:352`).
`cursor.fetchall()` with `SELECT *` pulls the entire dataset into Python dicts.
For large databases this will be memory-intensive. Consider streaming with
`fetchmany()` or processing per-aircraft with `GROUP BY` queries.

**`find_flights_near` also does `fetchall()`** — all candidate rows loaded into
memory, then filtered with haversine in Python.

**Recursive simplification** (`simplify.py`). The RDP algorithm uses Python
recursion, which hits the stack limit on very long tracks. An iterative
implementation would be safer.

### 5. Aircraft Icon System is a No-Op

`aircraft-icons.js` defines detailed `MODEL_PATTERNS` and `CATEGORIES` but
the `getAircraftIcon` function ignores all of them and always returns
`CATEGORIES.basic`. The classification system is dead code — either implement it
or remove the unused patterns.

### 6. Missing Infrastructure

- **No `pyproject.toml` or `setup.py`** — no installable package configuration.
- **No `.env.example`** — 15+ env vars with no documentation outside the code.
- **No Dockerfile or docker-compose** — containerization would simplify
  deployment of the multi-component system (webapp, poller, db init).
- **No database migration framework** — schema changes use manual ALTER TABLE
  statements that will become brittle.

### 7. Testing Gaps

- **No tests for the poller's retry/backoff logic** in `run_poller.py`.
- **No integration test** for the full pipeline (ingest → build tracks → query
  tracks → API response).
- **No tests for track segmentation edge cases** (duplicate timestamps,
  out-of-order data — plausible with real ADS-B data).
- **No JavaScript tests** for the playback engine, interpolation, or map logic.

### 8. Minor Code Quality Issues

- `import datetime` inside function bodies instead of at module top level
  (`queries.py:213`, `routes.py:491`).
- `enrich_results` mutates its input list in-place and also returns it — pick
  one pattern.
- `_backfill_area_from_opensky` opens two connections to the same DB file —
  one should suffice.
- Unused variable `header` in `load_opensky_csv` (`enrichment.py:73`).
- `on_ground` stored as `INTEGER` but compared as boolean in various places —
  works in SQLite but worth documenting.

### 9. Frontend Improvements

- **No JavaScript build tooling** — 4 JS files loaded synchronously,
  communicating via `window.*` globals. Load order is fragile.
- **No error boundary on the map** — if MapLibre GL fails to load (CDN down,
  ad blocker), the map breaks silently.
- **US-only UI assumptions** — zip code input is US-only. The backend works
  globally but the UI doesn't expose coordinate entry.

### 10. Operations Gaps

- **Health check is minimal** — `/api/status` doesn't check poller freshness,
  enrichment DB population, or track build recency.
- **VACUUM is never scheduled** — only runs via manual `--vacuum` CLI flag.
  A daily vacuum job should be automated.

## Prioritized Recommendations

| Priority   | Area         | Action                                                    |
|------------|--------------|-----------------------------------------------------------|
| **High**   | Security     | Add rate limiting, warn on default secret key             |
| **High**   | Performance  | Stream large query results instead of `fetchall()`        |
| **High**   | Code quality | Extract shared bounding-box utility, deduplicate queries  |
| **Medium** | Frontend     | Implement aircraft icon classification or remove dead code|
| **Medium** | Infra        | Add `pyproject.toml`, `.env.example`, Dockerfile          |
| **Medium** | DB           | Use connection context managers, add migration versioning |
| **Medium** | Testing      | Add poller, pipeline integration, and edge-case tests     |
| **Low**    | Frontend     | Add JS build step, CDN fallback, SRI hashes               |
| **Low**    | UX           | Support international locations in the UI                 |
| **Low**    | Ops          | Automate VACUUM, add richer health checks                 |
