/**
 * OverFlight — Playback Data Pipeline & Animation Controller
 *
 * Handles:
 * - Fetching chunk plan from API
 * - Buffered chunk loading with prefetch
 * - Animation loop with interpolated aircraft positions
 * - Rendering budget management
 */

(function () {
    "use strict";

    var SPEED_RATIO = window.OVERFLIGHT.playbackSpeedRatio || 960;
    var RENDER_BUDGET = window.OVERFLIGHT.renderBudgetMax || 150;
    var TARGET_FPS = window.OVERFLIGHT.playbackTargetFps || 30;
    var DEPARTURE_ANIMATION_SECONDS = 30;
    var DEPARTURE_BLEND_SECONDS = 8;
    var CHUNK_FETCH_RETRY_MAX = window.OVERFLIGHT.chunkFetchRetryMax || 3;
    var CHUNK_FETCH_BACKOFF_MS = window.OVERFLIGHT.chunkFetchBackoffMs || 700;
    var CHUNK_FAILURE_COOLDOWN_MS = window.OVERFLIGHT.chunkFailureCooldownMs || 15000;
    var VISIBILITY_LEAD_SECONDS = 120;
    var VISIBILITY_LINGER_SECONDS = 900;

    // --- State ---
    var plan = null;           // Chunk plan from server
    var chunkBuffer = {};      // loaded chunks keyed by chunk index
    var pendingChunkLoads = {}; // in-flight fetches keyed by chunk index
    var allTracks = [];        // flat list of all loaded track objects
    var playbackTime = 0;      // current data-time (unix timestamp)
    var playbackStart = 0;     // earliest timestamp in plan
    var playbackEnd = 0;       // latest timestamp in plan
    var isPlaying = false;
    var lastFrameTime = null;
    var animFrameId = null;
    var speedMultiplier = 1.0;
    var currentRenderBudget = RENDER_BUDGET;
    var altitudeFilter = 0;
    var currentChunkIndex = -1;
    var lastRenderTime = 0;
    var failedChunkCooldown = {};
    var initialChunkIndex = 0;

    // Callbacks set by map.js
    var onAircraftUpdate = null;  // fn(visibleAircraft: Array)
    var onPlaybackTimeChange = null; // fn(playbackTime, progress0to1)
    var onLoadingProgress = null; // fn(message)
    var onReady = null;           // fn()

    // Map center for distance sorting (set by map.js)
    var mapCenterLat = 0;
    var mapCenterLon = 0;
    var searchRadiusMiles = 0;
    var playbackDay = (window.OVERFLIGHT.playbackDay || "").trim();

    function buildBackfillStatusText(backfill) {
        if (!backfill || !backfill.attempted) {
            return "";
        }

        if (backfill.cooldown) {
            return " | Backfill: cooldown active, please retry shortly.";
        }

        return " | Backfill: fetched " + backfill.fetched +
            ", inserted " + backfill.inserted +
            ", tracks built " + backfill.segments + ".";
    }

    function emitSnapshotFallback(snapshotFlights) {
        var visible = (snapshotFlights || []).map(function (f, idx) {
            return {
                id: "snapshot-" + (f.icao24 || idx),
                icao24: f.icao24 || "",
                callsign: f.callsign || "",
                phase: f.on_ground ? "ground" : "enroute",
                lat: f.latitude,
                lon: f.longitude,
                altitude: f.altitude || 0,
                heading: f.heading || 0,
                avg_velocity: null,
                avg_heading: f.heading || 0
            };
        }).filter(function (ac) {
            return ac.lat != null && ac.lon != null;
        });

        visible.sort(function (a, b) {
            var dA = quickDist(a.lat, a.lon, mapCenterLat, mapCenterLon);
            var dB = quickDist(b.lat, b.lon, mapCenterLat, mapCenterLon);
            return dA - dB;
        });

        var detailed = visible.slice(0, currentRenderBudget);
        var dots = visible.slice(currentRenderBudget);
        onAircraftUpdate(detailed, dots, visible.length, []);
    }

    /**
     * Initialize playback for a location.
     *
     * @param {number} lat
     * @param {number} lon
     * @param {number} radius
     * @param {Object} callbacks - { onAircraftUpdate, onPlaybackTimeChange, onLoadingProgress, onReady }
     */
    function init(lat, lon, radius, callbacks) {
        onAircraftUpdate = callbacks.onAircraftUpdate || function () {};
        onPlaybackTimeChange = callbacks.onPlaybackTimeChange || function () {};
        onLoadingProgress = callbacks.onLoadingProgress || function () {};
        onReady = callbacks.onReady || function () {};
        mapCenterLat = lat;
        mapCenterLon = lon;
        searchRadiusMiles = radius;

        // Reset state
        stop();
        plan = null;
        chunkBuffer = {};
        pendingChunkLoads = {};
        allTracks = [];
        currentChunkIndex = -1;
        lastRenderTime = 0;
        initialChunkIndex = 0;

        emitLoadingProgress("Fetching playback plan...", 0);

        function buildPlanUrl(dayParam) {
            var url = "/api/tracks/plan?lat=" + lat + "&lon=" + lon + "&radius=" + radius;
            if (altitudeFilter > 0) url += "&min_alt=" + altitudeFilter;
            if (dayParam) url += "&day=" + encodeURIComponent(dayParam);
            return url;
        }

        function requestPlan(dayParam, attemptedFallback) {
            return fetch(buildPlanUrl(dayParam))
                .then(function (resp) {
                    if (!resp.ok) throw new Error("Failed to fetch plan");
                    return resp.json();
                })
                .then(function (data) {
                    var hasNoPlaybackTracks = !data.chunks || data.chunks.length === 0 || (data.total_unique_aircraft || 0) <= 0;

                    if (hasNoPlaybackTracks && dayParam && !attemptedFallback) {
                        onLoadingProgress({
                            message: "No playback history for " + dayParam + " in this area. Loading latest available timeline...",
                            percent: 45,
                            warning: true
                        });
                        return requestPlan("", true);
                    }

                    plan = data;

                    if (attemptedFallback) {
                        onLoadingProgress({
                            message: "Using latest available playback window.",
                            warning: true
                        });
                    }

                    if (!plan.chunks || plan.chunks.length === 0 || (plan.total_unique_aircraft || 0) <= 0) {
                        var isCollecting = !!plan.is_collecting;
                        var snapshots = plan.snapshot_flights || [];
                        var backfillSuffix = buildBackfillStatusText(plan.backfill_status);

                        if (!isCollecting && snapshots.length > 0) {
                            emitSnapshotFallback(snapshots);
                            onPlaybackTimeChange(0, 0);
                            onLoadingProgress({
                                message: "Showing latest aircraft snapshots for this area." + backfillSuffix,
                                percent: 100,
                                collecting: false
                            });
                            onReady({
                                staticOnly: true,
                                snapshotCount: snapshots.length,
                                density: plan.density,
                                suggestedMinAlt: plan.suggested_min_alt,
                                totalUniqueAircraft: plan.total_unique_aircraft || 0
                            });
                            return;
                        }

                        var message = isCollecting
                            ? "Collecting flight data - check back in a few minutes." + backfillSuffix
                            : "No playback traffic found for this area in the last 24 hours." + backfillSuffix;

                        onLoadingProgress({
                            message: message,
                            percent: 100,
                            collecting: isCollecting
                        });
                        onAircraftUpdate([], [], 0);
                        onPlaybackTimeChange(0, 0);
                        onReady({
                            empty: true,
                            collecting: isCollecting,
                            density: plan.density,
                            suggestedMinAlt: plan.suggested_min_alt,
                            totalUniqueAircraft: plan.total_unique_aircraft || 0
                        });
                        return;
                    }

                    playbackStart = plan.chunks[0].start;
                    playbackEnd = plan.chunks[plan.chunks.length - 1].end;
                    initialChunkIndex = findInitialChunkIndex(plan);
                    playbackTime = plan.chunks[initialChunkIndex].start;

                    // Load initial chunk (first with data when available), then signal ready.
                    loadChunk(initialChunkIndex, function () {
                        // Render the initial playback frame immediately so aircraft are visible
                        // even before the user presses play or touches the scrubber.
                        updateVisibleAircraft();
                        var initialProgress = (playbackTime - playbackStart) / (playbackEnd - playbackStart);
                        onPlaybackTimeChange(playbackTime, initialProgress);

                        emitLoadingProgress("Ready - " + plan.total_unique_aircraft + " aircraft", 100);
                        onReady({
                            density: plan.density,
                            suggestedMinAlt: plan.suggested_min_alt,
                            totalUniqueAircraft: plan.total_unique_aircraft || 0
                        });
                        // Prefetch neighboring chunks around the initial playback window.
                        if (initialChunkIndex + 1 < plan.chunks.length) {
                            loadChunk(initialChunkIndex + 1);
                        }
                        if (initialChunkIndex - 1 >= 0) {
                            loadChunk(initialChunkIndex - 1);
                        }
                    });
                });
        }

        requestPlan(playbackDay, false)
            .catch(function () {
                onLoadingProgress({
                    message: "Failed to load playback data.",
                    warning: true
                });
                onAircraftUpdate([], [], 0);
                onPlaybackTimeChange(0, 0);
                onReady({ error: true });
            });
    }

    function findInitialChunkIndex(playbackPlan) {
        if (!playbackPlan || !playbackPlan.chunks || playbackPlan.chunks.length === 0) {
            return 0;
        }

        // Start playback from the first chunk likely to contain tracks.
        for (var i = 0; i < playbackPlan.chunks.length; i++) {
            if ((playbackPlan.chunks[i].estimated_tracks || 0) > 0) {
                return i;
            }
        }
        return 0;
    }

    function loadChunk(index, callback) {
        if (index < 0 || !plan || index >= plan.chunks.length) {
            if (callback) callback();
            return;
        }
        if (chunkBuffer[index]) {
            if (callback) callback();
            return;
        }
        if (pendingChunkLoads[index]) {
            if (callback) pendingChunkLoads[index].push(callback);
            return;
        }

        if (failedChunkCooldown[index] && Date.now() < failedChunkCooldown[index]) {
            if (callback) callback();
            return;
        }

        pendingChunkLoads[index] = callback ? [callback] : [];

        var chunk = plan.chunks[index];
        var url = "/api/tracks?lat=" + mapCenterLat + "&lon=" + mapCenterLon +
            "&radius=" + searchRadiusMiles +
            "&start=" + chunk.start + "&end=" + chunk.end;
        if (altitudeFilter > 0) url += "&min_alt=" + altitudeFilter;

        emitLoadingProgress(
            "Loading aircraft data...",
            estimateProgressPercent(index, false)
        );

        fetchChunkWithRetry(index, url, 1);
    }

    function fetchChunkWithRetry(index, url, attempt) {
        fetch(url)
            .then(function (resp) {
                if (!resp.ok) throw new Error("Chunk fetch failed");
                return resp.json();
            })
            .then(function (data) {
                chunkBuffer[index] = data.tracks;
                data.tracks.forEach(function (t) {
                    // Parse polyline if it's a string
                    if (typeof t.polyline === "string") {
                        t.polyline = JSON.parse(t.polyline);
                    }
                    t._interpIndex = 0;
                });
                delete failedChunkCooldown[index];
                rebuildTrackCache();
                emitLoadingProgress(
                    "Loading aircraft data...",
                    estimateProgressPercent(index, true)
                );
                flushPendingCallbacks(index);
            })
            .catch(function () {
                if (attempt < CHUNK_FETCH_RETRY_MAX) {
                    var delay = CHUNK_FETCH_BACKOFF_MS * Math.pow(2, attempt - 1);
                    setTimeout(function () {
                        fetchChunkWithRetry(index, url, attempt + 1);
                    }, delay);
                    return;
                }

                failedChunkCooldown[index] = Date.now() + CHUNK_FAILURE_COOLDOWN_MS;
                onLoadingProgress({
                    message: "Some playback data failed to load. Retrying in a few seconds.",
                    warning: true
                });
                flushPendingCallbacks(index);
            });
    }

    function estimateProgressPercent(chunkIndex, includeCurrent) {
        if (!plan || !plan.chunk_count) return 0;

        var loaded = Object.keys(chunkBuffer).length;
        if (includeCurrent && !chunkBuffer[chunkIndex]) {
            loaded += 1;
        }

        var percent = Math.round((loaded / plan.chunk_count) * 100);
        if (percent < 0) percent = 0;
        if (percent > 100) percent = 100;
        return percent;
    }

    function emitLoadingProgress(message, percent) {
        onLoadingProgress({
            message: message,
            percent: percent
        });
    }

    function flushPendingCallbacks(index) {
        var callbacks = pendingChunkLoads[index] || [];
        delete pendingChunkLoads[index];
        for (var i = 0; i < callbacks.length; i++) {
            callbacks[i]();
        }
    }

    function rebuildTrackCache() {
        var byId = {};
        var merged = [];
        for (var key in chunkBuffer) {
            if (!Object.prototype.hasOwnProperty.call(chunkBuffer, key)) continue;
            var tracks = chunkBuffer[key] || [];
            for (var i = 0; i < tracks.length; i++) {
                var track = tracks[i];
                if (!byId[track.id]) {
                    byId[track.id] = true;
                    merged.push(track);
                }
            }
        }
        allTracks = merged;
    }

    function play() {
        if (isPlaying) return;
        isPlaying = true;
        lastFrameTime = performance.now();
        animFrameId = requestAnimationFrame(tick);
    }

    function pause() {
        isPlaying = false;
        if (animFrameId) {
            cancelAnimationFrame(animFrameId);
            animFrameId = null;
        }
    }

    function stop() {
        pause();
        playbackTime = playbackStart;
        lastFrameTime = null;
    }

    function togglePlayPause() {
        if (isPlaying) {
            pause();
        } else {
            play();
        }
        return isPlaying;
    }

    function seek(progress) {
        // progress is 0..1
        var t = playbackStart + progress * (playbackEnd - playbackStart);
        playbackTime = Math.max(playbackStart, Math.min(playbackEnd, t));
        var targetChunk = findChunkIndexForTime(playbackTime);

        // Large timeline jumps flush older buffer to keep memory bounded.
        if (targetChunk >= 0 && currentChunkIndex >= 0 && Math.abs(targetChunk - currentChunkIndex) > 1) {
            resetBufferForChunk(targetChunk);
        }
        resetInterpolationCaches();

        // Check if we need to load chunks for this position
        ensureChunksLoaded();

        // Update display immediately
        updateVisibleAircraft();
        var p = (playbackTime - playbackStart) / (playbackEnd - playbackStart);
        onPlaybackTimeChange(playbackTime, p);
    }

    function setSpeed(multiplier) {
        speedMultiplier = multiplier;
    }

    function setAltitudeFilter(minAlt) {
        altitudeFilter = minAlt;
    }

    function setRenderBudget(budget) {
        var parsed = parseInt(budget, 10);
        if (!isNaN(parsed) && parsed >= 25) {
            currentRenderBudget = parsed;
        }
    }

    function setMapCenter(lat, lon) {
        mapCenterLat = lat;
        mapCenterLon = lon;
    }

    function getTrackPolylineByIcao(icao24) {
        if (!icao24) return [];
        var coords = [];
        for (var i = 0; i < allTracks.length; i++) {
            var track = allTracks[i];
            if (track.icao24 !== icao24 || !track.polyline) continue;
            for (var j = 0; j < track.polyline.length; j++) {
                coords.push([track.polyline[j][2], track.polyline[j][1]]);
            }
        }
        return coords;
    }

    function tick(now) {
        if (!isPlaying) return;

        var dt = (now - lastFrameTime) / 1000; // seconds of wall time
        lastFrameTime = now;

        // Advance playback time
        playbackTime += dt * SPEED_RATIO * speedMultiplier;

        if (playbackTime >= playbackEnd) {
            playbackTime = playbackEnd;
            pause();
        }

        // Ensure we have the right chunks loaded
        ensureChunksLoaded();

        // Cap heavy render work to a stable FPS to reduce CPU load.
        if ((now - lastRenderTime) >= (1000 / TARGET_FPS)) {
            updateVisibleAircraft();
            lastRenderTime = now;
        }

        // Notify time update
        var progress = (playbackTime - playbackStart) / (playbackEnd - playbackStart);
        onPlaybackTimeChange(playbackTime, progress);

        if (isPlaying) {
            animFrameId = requestAnimationFrame(tick);
        }
    }

    function ensureChunksLoaded() {
        if (!plan) return;
        var i = findChunkIndexForTime(playbackTime);
        if (i < 0) return;

        if (i !== currentChunkIndex) {
            currentChunkIndex = i;
            resetInterpolationCaches();

            // Keep a small sliding window in memory (one behind, one ahead).
            var kept = {};
            kept[i] = true;
            if (i - 1 >= 0) kept[i - 1] = true;
            if (i + 1 < plan.chunks.length) kept[i + 1] = true;

            for (var key in chunkBuffer) {
                if (!Object.prototype.hasOwnProperty.call(chunkBuffer, key)) continue;
                var k = parseInt(key, 10);
                if (!kept[k]) {
                    delete chunkBuffer[k];
                }
            }
            rebuildTrackCache();

            loadChunk(i);
            if (i + 1 < plan.chunks.length) {
                loadChunk(i + 1);
            }
            if (i - 1 >= 0) {
                loadChunk(i - 1);
            }
        } else {
            // Ensure current and next chunk are present even without index change.
            loadChunk(i);
            if (i + 1 < plan.chunks.length) {
                loadChunk(i + 1);
            }
        }
    }

    function resetBufferForChunk(chunkIndex) {
        var keep = {};
        keep[chunkIndex] = true;
        if (chunkIndex + 1 < (plan ? plan.chunks.length : 0)) keep[chunkIndex + 1] = true;
        if (chunkIndex - 1 >= 0) keep[chunkIndex - 1] = true;

        for (var key in chunkBuffer) {
            if (!Object.prototype.hasOwnProperty.call(chunkBuffer, key)) continue;
            var k = parseInt(key, 10);
            if (!keep[k]) {
                delete chunkBuffer[k];
            }
        }
        rebuildTrackCache();
        currentChunkIndex = chunkIndex;
    }

    function resetInterpolationCaches() {
        for (var i = 0; i < allTracks.length; i++) {
            allTracks[i]._interpIndex = 0;
        }
    }

    function findChunkIndexForTime(t) {
        if (!plan || !plan.chunks || plan.chunks.length === 0) return -1;
        for (var i = 0; i < plan.chunks.length; i++) {
            if (t >= plan.chunks[i].start && t < plan.chunks[i].end) {
                return i;
            }
        }
        return plan.chunks.length - 1;
    }

    function updateVisibleAircraft() {
        var visible = [];
        var trails = [];
        var t = playbackTime;

        for (var i = 0; i < allTracks.length; i++) {
            var track = allTracks[i];
            if (t < (track.start_time - VISIBILITY_LEAD_SECONDS)) continue;
            if (t > (track.end_time + VISIBILITY_LINGER_SECONDS)) continue;
            if (altitudeFilter > 0 && (track.max_altitude || 0) < altitudeFilter) continue;

            var sampleTime = t;
            if (sampleTime < track.start_time) sampleTime = track.start_time;
            if (sampleTime > track.end_time) sampleTime = track.end_time;

            var pos = interpolatePosition(track, sampleTime);
            if (pos) {
                visible.push({
                    id: track.id,
                    icao24: track.icao24,
                    callsign: track.callsign,
                    phase: track.phase,
                    lat: pos.lat,
                    lon: pos.lon,
                    altitude: pos.altitude,
                    heading: pos.heading,
                    avg_velocity: track.avg_velocity,
                    avg_heading: track.avg_heading
                });

                trails.push({
                    id: track.id,
                    icao24: track.icao24,
                    selected: false,
                    coordinates: polylineToCoords(track.polyline)
                });
            }
        }

        // Sort by distance from map center (nearest first)
        visible.sort(function (a, b) {
            // Ground traffic is lowest rendering priority.
            var aGround = a.phase === "ground" ? 1 : 0;
            var bGround = b.phase === "ground" ? 1 : 0;
            if (aGround !== bGround) {
                return aGround - bGround;
            }

            var dA = quickDist(a.lat, a.lon, mapCenterLat, mapCenterLon);
            var dB = quickDist(b.lat, b.lon, mapCenterLat, mapCenterLon);
            return dA - dB;
        });

        // Apply render budget
        var detailed = visible.slice(0, currentRenderBudget);
        var dots = visible.slice(currentRenderBudget);

        onAircraftUpdate(detailed, dots, visible.length, trails);
    }

    function polylineToCoords(polyline) {
        if (!polyline || !polyline.length) return [];
        var coords = [];
        for (var i = 0; i < polyline.length; i++) {
            coords.push([polyline[i][2], polyline[i][1]]);
        }
        return coords;
    }

    function interpolatePosition(track, t) {
        if (track.phase === "ground") {
            return getGroundPosition(track);
        }

        if (track.phase === "departure") {
            var scriptedDeparture = getDepartureAnimatedPosition(track, t);
            if (scriptedDeparture) {
                return scriptedDeparture;
            }
        }

        return getPolylinePositionAtTime(track, t);
    }

    function getGroundPosition(track) {
        var poly = track.polyline;
        if (!poly || poly.length === 0) return null;

        if (!track._groundAnchor) {
            var anchor = poly[0];
            track._groundAnchor = {
                lat: anchor[1],
                lon: anchor[2],
                altitude: anchor[3] || 0,
                heading: track.avg_heading || 0
            };
        }
        return track._groundAnchor;
    }

    function getDepartureAnimatedPosition(track, t) {
        var liftoffTime = track.liftoff_time;
        var liftoffLat = track.liftoff_lat;
        var liftoffLon = track.liftoff_lon;
        if (liftoffTime == null || liftoffLat == null || liftoffLon == null) {
            return null;
        }

        var heading = track.liftoff_heading;
        if (heading == null) {
            heading = track.avg_heading || 0;
        }

        var liftoffDataPos = getPolylinePositionAtTime(track, liftoffTime);
        var baseAltitude = liftoffDataPos ? (liftoffDataPos.altitude || 0) : 0;

        if (t < liftoffTime) {
            // Hold just behind the liftoff point to imply runway position.
            var pre = offsetByHeading(liftoffLat, liftoffLon, (heading + 180) % 360, 0.00012);
            return {
                lat: pre.lat,
                lon: pre.lon,
                altitude: 0,
                heading: heading
            };
        }

        var animEnd = liftoffTime + DEPARTURE_ANIMATION_SECONDS;
        if (t <= animEnd) {
            var progress = (t - liftoffTime) / DEPARTURE_ANIMATION_SECONDS;
            if (progress < 0) progress = 0;
            if (progress > 1) progress = 1;

            // Ease-in acceleration and climb during liftoff.
            var eased = progress * progress;
            var scriptedDist = 0.00008 + (0.00135 * eased);
            var scriptedPoint = offsetByHeading(liftoffLat, liftoffLon, heading, scriptedDist);
            var scripted = {
                lat: scriptedPoint.lat,
                lon: scriptedPoint.lon,
                altitude: baseAltitude + (350 * eased),
                heading: heading
            };

            // Blend into data-driven position near the end to avoid popping.
            if (t >= animEnd - DEPARTURE_BLEND_SECONDS) {
                var blendProgress = (t - (animEnd - DEPARTURE_BLEND_SECONDS)) / DEPARTURE_BLEND_SECONDS;
                if (blendProgress < 0) blendProgress = 0;
                if (blendProgress > 1) blendProgress = 1;
                var dataPos = getPolylinePositionAtTime(track, t);
                if (dataPos) {
                    scripted.lat = scripted.lat + (dataPos.lat - scripted.lat) * blendProgress;
                    scripted.lon = scripted.lon + (dataPos.lon - scripted.lon) * blendProgress;
                    scripted.altitude = scripted.altitude + ((dataPos.altitude || 0) - scripted.altitude) * blendProgress;
                    scripted.heading = scripted.heading + ((dataPos.heading || scripted.heading) - scripted.heading) * blendProgress;
                }
            }

            return scripted;
        }

        return null;
    }

    function offsetByHeading(lat, lon, headingDeg, distDeg) {
        var rad = headingDeg * Math.PI / 180;
        var dLat = Math.cos(rad) * distDeg;
        var cosLat = Math.cos(lat * Math.PI / 180);
        if (Math.abs(cosLat) < 1e-6) cosLat = 1e-6;
        var dLon = (Math.sin(rad) * distDeg) / cosLat;
        return { lat: lat + dLat, lon: lon + dLon };
    }

    function getPolylinePositionAtTime(track, t) {
        var poly = track.polyline;
        if (!poly || poly.length === 0) return null;

        // Single point
        if (poly.length === 1) {
            return { lat: poly[0][1], lon: poly[0][2], altitude: poly[0][3], heading: track.avg_heading || 0 };
        }

        // Before first point
        if (t <= poly[0][0]) {
            return { lat: poly[0][1], lon: poly[0][2], altitude: poly[0][3], heading: computeHeading(poly, 0) };
        }

        // After last point
        if (t >= poly[poly.length - 1][0]) {
            var last = poly.length - 1;
            return { lat: poly[last][1], lon: poly[last][2], altitude: poly[last][3], heading: computeHeading(poly, last - 1) };
        }

        // Use a rolling index cache to avoid scanning from the start each frame.
        var i = track._interpIndex || 0;
        if (i < 0) i = 0;
        if (i > poly.length - 2) i = poly.length - 2;

        while (i < poly.length - 2 && t > poly[i + 1][0]) {
            i++;
        }
        while (i > 0 && t < poly[i][0]) {
            i--;
        }
        track._interpIndex = i;

        if (t >= poly[i][0] && t <= poly[i + 1][0]) {
            var dt = poly[i + 1][0] - poly[i][0];
            var frac = dt > 0 ? (t - poly[i][0]) / dt : 0;

            return {
                lat: poly[i][1] + frac * (poly[i + 1][1] - poly[i][1]),
                lon: poly[i][2] + frac * (poly[i + 1][2] - poly[i][2]),
                altitude: poly[i][3] + frac * ((poly[i + 1][3] || 0) - (poly[i][3] || 0)),
                heading: computeHeading(poly, i)
            };
        }

        // Fallback for edge cases when there are duplicate timestamps.
        for (i = 0; i < poly.length - 1; i++) {
            if (t >= poly[i][0] && t <= poly[i + 1][0]) {
                var dt = poly[i + 1][0] - poly[i][0];
                var frac = dt > 0 ? (t - poly[i][0]) / dt : 0;

                return {
                    lat: poly[i][1] + frac * (poly[i + 1][1] - poly[i][1]),
                    lon: poly[i][2] + frac * (poly[i + 1][2] - poly[i][2]),
                    altitude: poly[i][3] + frac * ((poly[i + 1][3] || 0) - (poly[i][3] || 0)),
                    heading: computeHeading(poly, i)
                };
            }
        }
        return null;
    }

    function computeHeading(poly, index) {
        var i = Math.min(index, poly.length - 2);
        if (i < 0) i = 0;
        var dLon = poly[i + 1][2] - poly[i][2];
        var dLat = poly[i + 1][1] - poly[i][1];
        var heading = Math.atan2(dLon, dLat) * 180 / Math.PI;
        return (heading + 360) % 360;
    }

    function quickDist(lat1, lon1, lat2, lon2) {
        var dLat = lat1 - lat2;
        var dLon = (lon1 - lon2) * Math.cos(lat1 * Math.PI / 180);
        return dLat * dLat + dLon * dLon;
    }

    function getState() {
        return {
            isPlaying: isPlaying,
            playbackTime: playbackTime,
            playbackStart: playbackStart,
            playbackEnd: playbackEnd,
            speedMultiplier: speedMultiplier,
            altitudeFilter: altitudeFilter,
            plan: plan,
            trackCount: allTracks.length
        };
    }

    // Expose globally
    window.OverflightPlayback = {
        init: init,
        play: play,
        pause: pause,
        stop: stop,
        togglePlayPause: togglePlayPause,
        seek: seek,
        setSpeed: setSpeed,
        setAltitudeFilter: setAltitudeFilter,
        setRenderBudget: setRenderBudget,
        setMapCenter: setMapCenter,
        getTrackPolylineByIcao: getTrackPolylineByIcao,
        getState: getState
    };
})();
