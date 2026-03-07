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
    var TARGET_FPS = 30;

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

    // Callbacks set by map.js
    var onAircraftUpdate = null;  // fn(visibleAircraft: Array)
    var onPlaybackTimeChange = null; // fn(playbackTime, progress0to1)
    var onLoadingProgress = null; // fn(message)
    var onReady = null;           // fn()

    // Map center for distance sorting (set by map.js)
    var mapCenterLat = 0;
    var mapCenterLon = 0;
    var searchRadiusMiles = 0;

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

        onLoadingProgress("Fetching playback plan\u2026");

        var url = "/api/tracks/plan?lat=" + lat + "&lon=" + lon + "&radius=" + radius;
        if (altitudeFilter > 0) url += "&min_alt=" + altitudeFilter;

        fetch(url)
            .then(function (resp) {
                if (!resp.ok) throw new Error("Failed to fetch plan");
                return resp.json();
            })
            .then(function (data) {
                plan = data;
                if (!plan.chunks || plan.chunks.length === 0) {
                    onLoadingProgress("No aircraft data available for this area.");
                    onAircraftUpdate([], [], 0);
                    onPlaybackTimeChange(0, 0);
                    onReady({ empty: true });
                    return;
                }
                playbackStart = plan.chunks[0].start;
                playbackEnd = plan.chunks[plan.chunks.length - 1].end;
                playbackTime = playbackStart;

                // Load first chunk, then signal ready
                loadChunk(0, function () {
                    onLoadingProgress("Ready \u2014 " + plan.total_unique_aircraft + " aircraft");
                    onReady();
                    // Prefetch next chunk
                    if (plan.chunks.length > 1) {
                        loadChunk(1);
                    }
                });
            })
            .catch(function () {
                onLoadingProgress("Failed to load playback data.");
                onAircraftUpdate([], [], 0);
                onPlaybackTimeChange(0, 0);
                onReady({ error: true });
            });
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

        pendingChunkLoads[index] = callback ? [callback] : [];

        var chunk = plan.chunks[index];
        var url = "/api/tracks?lat=" + mapCenterLat + "&lon=" + mapCenterLon +
            "&radius=" + searchRadiusMiles +
            "&start=" + chunk.start + "&end=" + chunk.end;
        if (altitudeFilter > 0) url += "&min_alt=" + altitudeFilter;

        onLoadingProgress("Loading chunk " + (index + 1) + " of " + plan.chunk_count + "\u2026");

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
                rebuildTrackCache();
                flushPendingCallbacks(index);
            })
            .catch(function () {
                flushPendingCallbacks(index);
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
        var t = playbackTime;

        for (var i = 0; i < allTracks.length; i++) {
            var track = allTracks[i];
            if (t < track.start_time || t > track.end_time) continue;
            if (altitudeFilter > 0 && (track.max_altitude || 0) < altitudeFilter) continue;

            var pos = interpolatePosition(track, t);
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
            }
        }

        // Sort by distance from map center (nearest first)
        visible.sort(function (a, b) {
            var dA = quickDist(a.lat, a.lon, mapCenterLat, mapCenterLon);
            var dB = quickDist(b.lat, b.lon, mapCenterLat, mapCenterLon);
            return dA - dB;
        });

        // Apply render budget
        var detailed = visible.slice(0, currentRenderBudget);
        var dots = visible.slice(currentRenderBudget);

        onAircraftUpdate(detailed, dots, visible.length);
    }

    function interpolatePosition(track, t) {
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
                dt = poly[i + 1][0] - poly[i][0];
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
