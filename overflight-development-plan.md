# OverFlight Development Plan

## Project Overview

OverFlight is a Python/Flask web application that shows users what aircraft flew over their location in the past 24 hours. It polls the OpenSky Network API for ADS-B state vectors, stores them in SQLite, enriches results with aircraft metadata, and serves a mobile-first search UI.

This plan extends the existing codebase with an animated map playback feature: a visual representation of aircraft flying over the user's area, with time-lapse controls, clickable aircraft, and intelligent buffering for high-density areas.

### Design Philosophy

OverFlight prioritizes showing the **right aircraft in roughly the right place** over fine-grained positional accuracy. The animation does not need to be a real-time flight simulator. Aircraft identity, general trajectory, and approximate timing are what matter. This means we can freely simplify, compress, and approximate track data without degrading the user experience.

### Playback Model

All playback is accelerated. 24 hours of data compresses into approximately 1–2 minutes of animation. The playback speed should be slow enough that a user can observe an aircraft, read its label, and click it — but no slower. There is no real-time mode planned.

---

## Current Architecture (Reference)

```
overflight/
├── config.py                  # Environment-based configuration
├── database/
│   ├── schema.py              # DB init: state_vectors, aircraft, zipcodes tables
│   ├── queries.py             # Spatial queries: bounding box + haversine
│   ├── enrichment.py          # Aircraft metadata lookup by ICAO24
│   └── cleanup.py             # Auto-purge records older than 24h
├── ingestion/
│   ├── poller.py              # OpenSky API polling + state vector insertion
│   └── zipcode.py             # Zip code → lat/lon resolution
└── webapp/
    ├── __init__.py             # Flask app factory
    ├── routes.py               # API endpoints + main page
    ├── templates/index.html    # Jinja2 template
    └── static/
        ├── css/style.css       # Mobile-first stylesheet
        └── js/app.js           # Vanilla JS: search, results, geolocation
```

Entry points: `run_poller.py` (ingestion), `run_webapp.py` (web server), `init_db.py` (database setup).

### Key Data Flow (Current)

1. `poller.py` fetches state vectors from OpenSky every 10 seconds
2. Parsed rows inserted into `state_vectors` table
3. User searches by zip or geolocation via the web UI
4. `queries.py` runs bounding-box query + haversine filter, deduplicates by ICAO24 (closest approach only)
5. Results enriched with aircraft metadata from `enrichment.db`
6. JSON returned to frontend, rendered as expandable flight cards

---

## Phase 1: Track Segments Infrastructure

**Goal:** Build the server-side data pipeline that groups raw state vectors into per-aircraft track segments with simplified polylines. This is the foundation everything else depends on.

### Task 1.1: Add `track_segments` Table to Schema

**File:** `overflight/database/schema.py`

Add a new function `init_tracks_db` (or extend `init_flight_db`) that creates the `track_segments` table:

```sql
CREATE TABLE IF NOT EXISTS track_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    icao24 TEXT NOT NULL,
    callsign TEXT,
    phase TEXT NOT NULL DEFAULT 'enroute',
    -- phase values: 'ground', 'departure', 'enroute', 'arrival'

    -- Simplified polyline as JSON array of [timestamp, lat, lon, altitude] points
    polyline TEXT NOT NULL,
    point_count INTEGER NOT NULL,

    -- Time range
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,

    -- Altitude range (meters)
    min_altitude REAL,
    max_altitude REAL,

    -- Bounding box for spatial queries
    min_lat REAL NOT NULL,
    max_lat REAL NOT NULL,
    min_lon REAL NOT NULL,
    max_lon REAL NOT NULL,

    -- Track metadata
    avg_velocity REAL,
    avg_heading REAL,

    -- When this segment was built
    created_at INTEGER NOT NULL
);
```

Indexes to create:

```sql
CREATE INDEX idx_tracks_time ON track_segments (start_time, end_time);
CREATE INDEX idx_tracks_icao24 ON track_segments (icao24);
CREATE INDEX idx_tracks_bbox ON track_segments (min_lat, max_lat, min_lon, max_lon);
CREATE INDEX idx_tracks_phase ON track_segments (phase);
```

Also add SpatiaLite geometry column and spatial index if SpatiaLite is available, following the same pattern used for `state_vectors`.

**Update `init_db.py`** to initialize the tracks table alongside the existing tables.

### Task 1.2: Track Builder Module

**New file:** `overflight/database/tracks.py`

This module groups raw `state_vectors` rows into track segments for individual aircraft. It should be runnable both as a background worker and as a one-shot command.

**Core function: `build_tracks(conn, since_timestamp=None)`**

Logic:

1. Query `state_vectors` for rows newer than `since_timestamp` (or all rows if None), ordered by `icao24, timestamp`.
2. Group consecutive rows by `icao24`. A new segment starts when:
   - There is a gap of more than 5 minutes between consecutive position reports for the same aircraft (the aircraft left coverage and returned).
   - The `on_ground` status changes (transition between ground and air).
3. For each segment:
   - Classify the phase:
     - `ground`: All points have `on_ground=1`.
     - `departure`: Starts with `on_ground=1`, ends with `on_ground=0` and increasing altitude.
     - `arrival`: Starts with `on_ground=0`, ends with `on_ground=1` and decreasing altitude.
     - `enroute`: All points have `on_ground=0`.
   - Simplify the polyline using the Ramer-Douglas-Peucker algorithm (see Task 1.3).
   - Compute bounding box, altitude range, average velocity, average heading.
   - Serialize polyline as JSON: `[[timestamp, lat, lon, altitude], ...]`.
4. Insert into `track_segments` table.
5. Return count of segments created.

**Additional functions:**

- `build_tracks_incremental(conn, last_build_time)`: Only process state vectors added since the last build. Track the last-processed timestamp in a simple metadata table or file.
- `get_tracks_near(conn, lat, lon, radius_miles, start_time, end_time)`: Query track segments that overlap the given spatial and temporal window. Use bounding-box index for the spatial filter, time range for temporal. Return full segment data including polylines.
- `get_track_density(conn, lat, lon, radius_miles, start_time, end_time)`: Return a count of track segments matching the criteria. Used by the API to determine chunk sizing and suggest altitude filters.

### Task 1.3: Polyline Simplification

**New file:** `overflight/utils/simplify.py`

Implement the Ramer-Douglas-Peucker algorithm for geographic polyline simplification.

**Function: `simplify_track(points, epsilon=0.001)`**

- Input: List of `(timestamp, lat, lon, altitude)` tuples, ordered by time.
- Output: Simplified list retaining only significant points.
- The epsilon value represents degrees (roughly 0.001° ≈ 111 meters). This should be configurable.
- The distance metric should use perpendicular distance from a point to the line segment between two retained points, computed in lat/lon space. Full haversine is not necessary here given the approximation philosophy.
- Always retain the first and last points.
- A straight-line flight across a 25-mile search radius might compress from ~40 points to 3–4. A curved approach should retain more detail.

**Write unit tests** in `tests/test_simplify.py`:

- A straight line reduces to 2 points.
- A 90-degree turn retains the corner point.
- An empty or single-point input returns as-is.
- Verify point count reduction on a realistic sample (40+ points of a slightly curving trajectory).

### Task 1.4: Track Cleanup

**File:** `overflight/database/cleanup.py`

Add a `purge_old_tracks(conn, retention_hours=None)` function that deletes track segments older than the retention period, mirroring `purge_old_records`.

Update `run_poller.py`'s cleanup worker to also purge old track segments and rebuild tracks incrementally on each cleanup cycle.

### Task 1.5: Track Builder Integration

**File:** `run_poller.py`

Add track building to the polling loop. After each poll cycle stores new state vectors, trigger an incremental track build. This can run on every poll or on a less frequent schedule (e.g., every 60 seconds) to batch work.

Also add a CLI option:

```
python run_poller.py --rebuild-tracks    # One-shot: rebuild all track segments from current state vectors
```

**Update `init_db.py`** to accept a `--build-tracks` flag that runs initial track building after database initialization.

---

## Phase 2: Tracks API

**Goal:** Expose track segment data through new API endpoints that support the time-windowed, buffered loading model the frontend will need.

### Task 2.1: Tracks API Endpoint

**File:** `overflight/webapp/routes.py`

Add new endpoint: `GET /api/tracks`

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `lat` | float | yes | — | Center latitude |
| `lon` | float | yes | — | Center longitude |
| `radius` | float | no | config default | Radius in miles |
| `start` | int | no | 24h ago | Unix timestamp, window start |
| `end` | int | no | now | Unix timestamp, window end |
| `min_alt` | float | no | 0 | Minimum altitude in meters (for filtering) |
| `phase` | string | no | all | Comma-separated phase filter: `enroute,departure,arrival,ground` |

**Response JSON:**

```json
{
  "tracks": [
    {
      "id": 1,
      "icao24": "a1b2c3",
      "callsign": "UAL100",
      "phase": "enroute",
      "polyline": [[1700000000, 40.65, -73.78, 10000], ...],
      "start_time": 1700000000,
      "end_time": 1700000300,
      "min_altitude": 9500,
      "max_altitude": 10500,
      "avg_velocity": 250,
      "avg_heading": 90
    }
  ],
  "meta": {
    "count": 42,
    "total_in_area": 200,
    "density": "high",
    "suggested_min_alt": 1000,
    "window": { "start": 1700000000, "end": 1700003600 },
    "query": { "lat": 40.758, "lon": -73.985, "radius": 25 }
  }
}
```

**Density logic in the response:**

- Query `get_track_density` for the full 24-hour window.
- If total unique aircraft > 500: `density: "high"`, suggest `min_alt: 5000`.
- If total unique aircraft > 200: `density: "medium"`, suggest `min_alt: 1000`.
- Otherwise: `density: "low"`, no altitude suggestion.

Include `total_in_area` so the frontend knows how many aircraft exist before filtering.

### Task 2.2: Enriched Tracks Endpoint

**File:** `overflight/webapp/routes.py`

Add endpoint: `GET /api/track-detail/<icao24>`

Returns full enrichment data for a single aircraft, used when the user clicks an aircraft on the map. This avoids sending enrichment data for every track in the main payload.

**Response JSON:**

```json
{
  "icao24": "a1b2c3",
  "callsign": "UAL100",
  "registration": "N12345",
  "manufacturer": "Boeing",
  "model": "737-800",
  "operator": "United Airlines",
  "owner": "United Airlines Inc",
  "built_year": 2005,
  "aircraft_age": 21,
  "registered_country": "United States"
}
```

### Task 2.3: Adaptive Chunk Sizing

**File:** `overflight/webapp/routes.py` (or a new helper module)

Implement server-side logic that determines optimal time-window chunk size based on track density in the area:

- Low density (< 50 tracks/hour): Serve 4-hour chunks.
- Medium density (50–200 tracks/hour): Serve 1-hour chunks.
- High density (> 200 tracks/hour): Serve 30-minute chunks.

Add a `GET /api/tracks/plan` endpoint that the frontend calls once when initiating playback:

**Query parameters:** `lat`, `lon`, `radius`, `min_alt` (optional).

**Response:**

```json
{
  "total_duration_seconds": 86400,
  "chunk_count": 12,
  "chunks": [
    { "start": 1700000000, "end": 1700007200, "estimated_tracks": 45 },
    { "start": 1700007200, "end": 1700014400, "estimated_tracks": 120 }
  ],
  "density": "medium",
  "suggested_min_alt": 1000,
  "total_unique_aircraft": 340
}
```

The frontend uses this plan to schedule prefetch requests.

---

## Phase 3: Map View Frontend

**Goal:** Build the animated map interface with playback controls. This is the largest frontend phase.

### Task 3.1: Add Mapbox GL JS or Leaflet

**Decision:** Use Mapbox GL JS for canvas-based rendering (better performance with many moving markers) or Leaflet with the Canvas renderer for a free/open-source option. If cost is a concern, use Leaflet. If rendering performance is the priority, use Mapbox.

**File:** `overflight/webapp/templates/index.html`

- Add the chosen map library's CSS and JS from CDN.
- Add a new section in the HTML for the map view, hidden by default.

**File:** `overflight/webapp/static/css/style.css`

- Add styles for the map container (full-width, fixed height or viewport-based).
- Add styles for the view toggle (card view vs. map view).
- Add styles for the detail sidebar (slide-out panel on the right).

### Task 3.2: Map Initialization and View Toggle

**New file:** `overflight/webapp/static/js/map.js`

Implement:

- Map initialization centered on the user's searched location.
- View toggle: a button or tab that switches between the existing card-based results and the map view. The card view remains the default. The map view is an alternative.
- When the user switches to map view, trigger the playback loading pipeline (Task 3.4).
- The map should zoom to fit the search radius circle.
- Draw a subtle circle on the map showing the search radius boundary.

### Task 3.3: Aircraft Icon System

**New file:** `overflight/webapp/static/js/aircraft-icons.js`

**New directory:** `overflight/webapp/static/img/aircraft/`

Create an aircraft icon classification system:

- Define icon categories based on enrichment data. Map the `model` field or a new `typecode` field to categories:
  - `widebody`: Boeing 747, 767, 777, 787; Airbus A330, A340, A350, A380
  - `narrowbody`: Boeing 737, 757; Airbus A320 family
  - `regional`: CRJ, ERJ, ATR, Dash 8
  - `turboprop`: King Air, C208, ATR, Dash 8 (if not already caught by regional)
  - `ga_single`: C172, C182, PA-28, SR22
  - `ga_twin`: C310, PA-34, Baron
  - `helicopter`: Any rotorcraft
  - `military`: Based on operator or registration patterns
  - `unknown`: Default fallback
- Each category gets an SVG icon file in the `aircraft/` directory. Icons should be simple, recognizable silhouettes viewed from above. Each category should differ in shape and relative size.
- Export a function `getAircraftIcon(enrichmentData)` that returns the appropriate icon path and a relative size multiplier (e.g., widebody = 1.0, narrowbody = 0.75, GA = 0.4).
- Icons must rotate to match the aircraft heading.

### Task 3.4: Playback Data Pipeline

**New file:** `overflight/webapp/static/js/playback.js`

This is the core animation controller. Implement:

**Initialization:**

1. Call `GET /api/tracks/plan` with the user's location and radius.
2. Receive the chunk plan.
3. Immediately fetch the first chunk via `GET /api/tracks`.
4. Begin prefetching the next chunk.

**Buffer management:**

- Maintain a buffer of loaded track data, keyed by time window.
- Always keep at least 1 chunk ahead of the current playback position.
- Discard chunks more than 1 chunk behind the playback position to manage memory.
- If the user scrubs the timeline, flush the buffer and reload from the new position.

**Playback loop:**

- Maintain a `playbackTime` variable that maps wall-clock time to data time.
- The playback speed maps 24 hours to approximately 90 seconds (configurable).
- On each animation frame (`requestAnimationFrame`):
  - Advance `playbackTime` by `(deltaWallTime * speedMultiplier)`.
  - For each active track in the current buffer:
    - Determine if the track is visible at the current `playbackTime` (between its `start_time` and `end_time`).
    - If visible, interpolate position between the two nearest polyline points.
    - Update the marker position and heading on the map.
  - Add markers for tracks that just became active.
  - Remove markers for tracks that ended.
- The interpolation between polyline points should be linear (lat/lon/altitude). Given the playback speed and data density, this will look smooth.

**Rendering budget:**

- Track the number of simultaneously visible aircraft.
- If visible count exceeds 150:
  - Sort by distance from map center.
  - The nearest 150 get full animated icons (category-appropriate, sized, rotated, clickable).
  - Additional aircraft render as small dots (2–3px circles, colored by altitude band).
  - Dots are not clickable.
- Display a count indicator: "Showing 150 of 312 aircraft. Zoom in or increase altitude filter to see details."

### Task 3.5: Playback Controls UI

**File:** `overflight/webapp/static/js/playback.js` and new CSS

Build a playback control bar at the bottom of the map view:

- **Play/Pause button**: Toggles animation.
- **Timeline scrubber**: A horizontal slider representing 0–24 hours. Shows the current playback position. Draggable for seeking. Displays a clock time label (e.g., "2:34 PM").
- **Speed selector**: Options for 0.5x, 1x (default), 2x speed relative to the base 24h → 90s mapping.
- **Active aircraft counter**: "142 aircraft visible".
- **Altitude filter dropdown**: "All altitudes", "Above 1,000 ft", "Above 5,000 ft", "Above 20,000 ft". Show the server's suggestion if density is high.

The control bar should be compact and mobile-friendly. On mobile, it should be a thin strip at the bottom with just play/pause, the timeline, and a settings gear that expands to show speed and altitude options.

### Task 3.6: Aircraft Detail Sidebar

**File:** `overflight/webapp/static/js/map.js` and new CSS

When the user clicks a fully-rendered aircraft marker:

1. Highlight the selected aircraft (e.g., brighter color or glow effect).
2. Draw the aircraft's full track polyline on the map as a dashed or colored line.
3. Slide in a detail panel from the right side (desktop) or bottom sheet (mobile).
4. Fetch `GET /api/track-detail/<icao24>` and display:
   - Aircraft type and icon (large)
   - Callsign, registration, ICAO24
   - Operator and owner
   - Manufacturer, model, built year, age
   - Current (at playback time) altitude, speed, heading
   - Distance from user's location at closest approach
   - Country of registration
5. A "Close" button dismisses the panel, deselects the aircraft, and removes the track line.

Playback continues while the detail panel is open. The selected aircraft's position continues to update in real time.

---

## Phase 4: Ground Traffic and Departure Animations

**Goal:** Improve the visual handling of ground operations near airports. This is the advanced feature phase.

> **Note:** This phase is optional and should only be started after Phases 1–3 are complete and tested. The rest of the system works without it — ground-phase tracks simply won't render, or will render as stationary dots.

### Task 4.1: Departure Detection

**File:** `overflight/database/tracks.py`

Enhance the phase classification logic from Task 1.2:

- **Departure detection**: Identify track segments where:
  - The first N points have `on_ground=1`.
  - A transition to `on_ground=0` occurs.
  - Altitude increases rapidly after the transition (> 500 ft/min climb rate).
  - Capture the **liftoff point** (first airborne position) and **initial heading**.
- Store additional metadata on departure segments:
  - `liftoff_lat`, `liftoff_lon`: Position at wheels-up.
  - `liftoff_heading`: Heading at departure.
  - `liftoff_time`: Timestamp of transition.

- **Arrival detection**: Similar logic in reverse. Identify:
  - Decreasing altitude on approach.
  - Transition to `on_ground=1`.
  - Capture the **touchdown point** and **approach heading**.

### Task 4.2: Ground Traffic Rendering

**File:** `overflight/webapp/static/js/playback.js`

For aircraft with `phase: "ground"`:

- Do not animate ground movement paths. Instead, show the aircraft as a **stationary icon at the airport location** (or the first/last position if airport is unknown).
- Optionally show a subtle "taxiing" label or pulsing dot.
- Ground aircraft are lowest priority in the rendering budget (rendered last, cut first).

### Task 4.3: Departure Animation

**File:** `overflight/webapp/static/js/playback.js`

For aircraft with `phase: "departure"`:

- Before `liftoff_time`: Show the aircraft stationary at the liftoff point (or slightly behind it, as if on the runway).
- At `liftoff_time`: Play a short, generic takeoff animation — the aircraft icon smoothly accelerates along the `liftoff_heading` and begins climbing. This is a **scripted animation**, not data-driven. It lasts a few seconds of playback time.
- After the animation completes: Transition to the normal data-driven track following for the `enroute` portion.

### Task 4.4: Arrival Animation (Stretch Goal)

Reverse of departure:

- Normal data-driven tracking on approach.
- As the aircraft transitions to `phase: "arrival"`, play a landing animation along the approach heading.
- After touchdown, show stationary at the airport.

---

## Phase 5: Polish and Optimization

**Goal:** Performance tuning, UX refinement, and edge case handling.

### Task 5.1: Rendering Performance Optimization

**File:** `overflight/webapp/static/js/playback.js`

- Profile animation frame rate on mobile devices. Target 30fps minimum.
- Implement level-of-detail rendering:
  - Zoom level > 12: Full icons with labels.
  - Zoom level 9–12: Icons without labels.
  - Zoom level < 9: Dots only.
- Batch marker updates — update all positions in a single frame rather than individually.
- If using Leaflet: ensure the Canvas renderer is active (not SVG/DOM).
- If using Mapbox GL: use a single GeoJSON source with symbol layer for all aircraft, updated per frame. This is dramatically faster than individual markers.

### Task 5.2: Mobile UX

- Test and optimize the map view for small screens (320px–414px width).
- The detail sidebar should become a bottom sheet on mobile, dragable between peek (summary) and full height.
- Playback controls should be thumb-reachable at the bottom of the screen.
- Ensure the map is not accidentally scrollable when the user is trying to interact with playback controls.
- Test touch interactions: tap to select aircraft, pinch to zoom, drag to pan.

### Task 5.3: Loading States and Error Handling

- Show a progress indicator during initial track loading ("Loading aircraft data... 45%").
- Handle chunk fetch failures gracefully — retry with backoff, show a non-blocking warning if a chunk fails repeatedly.
- If the user's area has zero tracks, show a friendly empty state on the map (not just a blank map).
- Handle the case where the track database hasn't been built yet (new installation, poller just started). Show a message like "Collecting flight data — check back in a few minutes."

### Task 5.4: Existing Features — Ensure No Regressions

The current card-based search UI must continue to work as-is. Specifically:

- `GET /api/flights` endpoint: No changes. The existing deduplication and closest-approach logic stays.
- `GET /api/resolve-zip` endpoint: No changes.
- The card-based results view remains the default view after a search.
- The map view is an **opt-in alternative** accessed via a view toggle.
- All existing tests in `tests/test_database.py` and `tests/test_webapp.py` must continue to pass.

### Task 5.5: New Test Coverage

**New file:** `tests/test_tracks.py`

- Test track segment building from sample state vectors.
- Test phase classification (ground, departure, enroute, arrival).
- Test polyline simplification integration.
- Test bounding box and time-window queries on track segments.
- Test incremental track building (only processes new data).
- Test track cleanup/purge.

**New file:** `tests/test_tracks_api.py`

- Test `/api/tracks` endpoint with various parameters.
- Test `/api/tracks/plan` endpoint density calculation.
- Test `/api/track-detail/<icao24>` endpoint.
- Test altitude filtering.
- Test adaptive chunk sizing.

---

## Architecture Changes Summary

### Files Modified

| File | Changes |
|------|---------|
| `overflight/database/schema.py` | Add `track_segments` table and indexes |
| `overflight/database/cleanup.py` | Add `purge_old_tracks()` function |
| `overflight/webapp/routes.py` | Add `/api/tracks`, `/api/tracks/plan`, `/api/track-detail/<icao24>` endpoints |
| `overflight/webapp/__init__.py` | No changes needed (existing DB helpers suffice) |
| `overflight/webapp/templates/index.html` | Add map library, map container, view toggle, control bar |
| `overflight/webapp/static/css/style.css` | Add map, sidebar, controls, mobile styles |
| `overflight/webapp/static/js/app.js` | Add view toggle logic, integrate with map module |
| `overflight/config.py` | Add track-related config (chunk sizes, simplification epsilon, render budget) |
| `run_poller.py` | Add track building to poll loop, add `--rebuild-tracks` flag |
| `init_db.py` | Add `--build-tracks` flag |
| `requirements.txt` | No new server dependencies needed (simplification is pure Python) |

### Files Created

| File | Purpose |
|------|---------|
| `overflight/database/tracks.py` | Track segment builder, queries, phase classification |
| `overflight/utils/__init__.py` | Utils package init |
| `overflight/utils/simplify.py` | Ramer-Douglas-Peucker polyline simplification |
| `overflight/webapp/static/js/map.js` | Map initialization, view management, detail sidebar |
| `overflight/webapp/static/js/playback.js` | Animation controller, buffer management, rendering |
| `overflight/webapp/static/js/aircraft-icons.js` | Icon classification and mapping |
| `overflight/webapp/static/img/aircraft/*.svg` | Aircraft category icon files |
| `tests/test_simplify.py` | Simplification algorithm tests |
| `tests/test_tracks.py` | Track building and query tests |
| `tests/test_tracks_api.py` | Tracks API endpoint tests |

### Config Additions

Add to `overflight/config.py`:

```python
# --- Track Building ---
TRACK_GAP_THRESHOLD_SECONDS = int(os.environ.get("OVERFLIGHT_TRACK_GAP", "300"))
SIMPLIFICATION_EPSILON = float(os.environ.get("OVERFLIGHT_SIMPLIFY_EPSILON", "0.001"))
TRACK_BUILD_INTERVAL_SECONDS = int(os.environ.get("OVERFLIGHT_TRACK_BUILD_INTERVAL", "60"))

# --- Playback ---
PLAYBACK_SPEED_RATIO = float(os.environ.get("OVERFLIGHT_PLAYBACK_SPEED", "960"))
# 960 = 24 hours / 90 seconds
RENDER_BUDGET_MAX = int(os.environ.get("OVERFLIGHT_RENDER_BUDGET", "150"))
CHUNK_SIZE_LOW_DENSITY_HOURS = 4
CHUNK_SIZE_MEDIUM_DENSITY_HOURS = 1
CHUNK_SIZE_HIGH_DENSITY_MINUTES = 30

# --- Altitude Filter Presets (meters) ---
ALTITUDE_PRESETS = {
    "all": 0,
    "above_1000ft": 304.8,
    "above_5000ft": 1524.0,
    "above_20000ft": 6096.0,
}
```

---

## Phase Execution Order

| Phase | Depends On | Estimated Complexity |
|-------|-----------|---------------------|
| Phase 1: Track Segments Infrastructure | Nothing (extends current DB) | Medium |
| Phase 2: Tracks API | Phase 1 | Low–Medium |
| Phase 3: Map View Frontend | Phase 2 | High |
| Phase 4: Ground Traffic & Departures | Phases 1–3 | Medium |
| Phase 5: Polish & Optimization | Phases 1–3 | Medium |

Phases 4 and 5 can be worked in parallel once Phase 3 is functional. Phase 4 is entirely optional for the initial release.
