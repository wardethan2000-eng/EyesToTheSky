(function () {
    "use strict";

    var statusEl = document.getElementById("status");
    var logEl = document.getElementById("log");
    var originalFetch = window.fetch.bind(window);

    function setStatus(text) {
        statusEl.textContent = "Status: " + text;
    }

    function appendLog(text, level) {
        var line = document.createElement("div");
        line.className = level || "";
        line.textContent = "[" + new Date().toLocaleTimeString() + "] " + text;
        logEl.appendChild(line);
        logEl.scrollTop = logEl.scrollHeight;
    }

    function resetPlayback() {
        window.OverflightPlayback.stop();
    }

    function runScenario(name, fetchImpl) {
        setStatus(name + " running");
        appendLog("Scenario started: " + name, "warn");

        window.fetch = fetchImpl;

        resetPlayback();
        window.OverflightPlayback.init(40.758, -73.985, 25, {
            onAircraftUpdate: function (detailed, dots, total) {
                appendLog("onAircraftUpdate: detailed=" + detailed.length + " dots=" + dots.length + " total=" + total);
            },
            onPlaybackTimeChange: function (t, p) {
                appendLog("onPlaybackTimeChange: t=" + Math.round(t) + " p=" + p.toFixed(3));
            },
            onLoadingProgress: function (state) {
                if (typeof state === "string") {
                    appendLog("onLoadingProgress: " + state);
                } else {
                    appendLog("onLoadingProgress: " + JSON.stringify(state));
                }
            },
            onReady: function (state) {
                appendLog("onReady: " + JSON.stringify(state || {}), "ok");
                setStatus(name + " complete");
                window.fetch = originalFetch;
            }
        });
    }

    function jsonResponse(data, ok) {
        return Promise.resolve({
            ok: ok !== false,
            json: function () {
                return Promise.resolve(data);
            }
        });
    }

    function scenarioEmpty() {
        runScenario("empty", function (url) {
            if (url.indexOf("/api/tracks/plan") === 0) {
                return jsonResponse({
                    chunks: [],
                    chunk_count: 0,
                    total_unique_aircraft: 0,
                    density: "low",
                    suggested_min_alt: null
                });
            }
            return jsonResponse({ tracks: [] });
        });
    }

    function scenarioPlanError() {
        runScenario("plan-error", function () {
            return Promise.resolve({ ok: false, json: function () { return Promise.resolve({}); } });
        });
    }

    function scenarioRetrySuccess() {
        var chunkAttempts = 0;
        runScenario("retry-success", function (url) {
            if (url.indexOf("/api/tracks/plan") === 0) {
                return jsonResponse({
                    chunks: [{ start: 1000, end: 2000 }],
                    chunk_count: 1,
                    total_unique_aircraft: 1,
                    density: "low",
                    suggested_min_alt: null
                });
            }

            chunkAttempts += 1;
            if (chunkAttempts < 3) {
                return Promise.resolve({ ok: false, json: function () { return Promise.resolve({}); } });
            }

            return jsonResponse({
                tracks: [{
                    id: 1,
                    icao24: "abc123",
                    callsign: "TEST01",
                    phase: "enroute",
                    polyline: [[1000, 40.75, -73.98, 1500], [2000, 40.8, -73.95, 1700]],
                    start_time: 1000,
                    end_time: 2000,
                    max_altitude: 1700
                }]
            });
        });
    }

    function scenarioRetryFail() {
        runScenario("retry-fail", function (url) {
            if (url.indexOf("/api/tracks/plan") === 0) {
                return jsonResponse({
                    chunks: [{ start: 1000, end: 2000 }],
                    chunk_count: 1,
                    total_unique_aircraft: 1,
                    density: "low",
                    suggested_min_alt: null
                });
            }
            return Promise.resolve({ ok: false, json: function () { return Promise.resolve({}); } });
        });
    }

    document.getElementById("btn-empty").addEventListener("click", scenarioEmpty);
    document.getElementById("btn-plan-error").addEventListener("click", scenarioPlanError);
    document.getElementById("btn-retry-success").addEventListener("click", scenarioRetrySuccess);
    document.getElementById("btn-retry-fail").addEventListener("click", scenarioRetryFail);
    document.getElementById("btn-clear").addEventListener("click", function () {
        logEl.textContent = "";
        setStatus("idle");
    });
})();
