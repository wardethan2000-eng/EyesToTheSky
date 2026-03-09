/**
 * OverFlight — Main Application JavaScript
 *
 * Handles:
 * - Browser geolocation with zip code fallback
 * - Zip code resolution via API
 * - Flight search via API
 * - Rendering flight result cards
 * - Cookie-based location persistence
 */

(function () {
    "use strict";

    // --- DOM Elements ---
    const zipcodeInput = document.getElementById("zipcode-input");
    const searchBtn = document.getElementById("search-btn");
    const geoBtn = document.getElementById("geo-btn");
    const radiusSelect = document.getElementById("radius-select");
    const locationInfo = document.getElementById("location-info");
    const locationText = document.getElementById("location-text");
    const errorMsg = document.getElementById("error-msg");
    const timeFilterGrid = document.getElementById("time-filter-grid");
    const timeFilterButtons = Array.prototype.slice.call(document.querySelectorAll("[data-window]"));
    const loadingSection = document.getElementById("loading");
    const resultsSection = document.getElementById("results-section");
    const resultsTitle = document.getElementById("results-title");
    const resultsSubtitle = document.getElementById("results-subtitle");
    const flightList = document.getElementById("flight-list");
    const noResults = document.getElementById("no-results");
    const noResultsHeading = document.querySelector("#no-results h2");
    const noResultsBody = document.querySelector("#no-results p");

    // --- State ---
    let currentLat = parseCoord(window.OVERFLIGHT.savedLat);
    let currentLon = parseCoord(window.OVERFLIGHT.savedLon);
    let currentTimeWindow = "last_24_hours";
    let currentWindowLabel = defaultWindowLabel(currentTimeWindow);
    let activeQuery = null;

    // --- Init ---
    function init() {
        searchBtn.addEventListener("click", onSearchClick);
        zipcodeInput.addEventListener("keydown", function (e) {
            if (e.key === "Enter") onSearchClick();
        });
        geoBtn.addEventListener("click", onGeoClick);
        radiusSelect.addEventListener("change", function () {
            if (currentLat && currentLon) {
                fetchFlights(currentLat, currentLon);
            }
        });
        if (timeFilterGrid) {
            timeFilterGrid.addEventListener("click", onTimeFilterClick);
        }

        if (window.OverflightMap && typeof window.OverflightMap.initOverview === "function") {
            window.OverflightMap.initOverview({
                lat: currentLat,
                lon: currentLon,
                radius: parseFloat(radiusSelect.value)
            });
        }

        // Auto-search if we have saved coordinates
        if (currentLat && currentLon) {
            showLocationInfo();
            fetchFlights(currentLat, currentLon);
        }

        // Auto-geolocate only when browser has already granted location permission.
        if (currentLat == null && currentLon == null) {
            attemptAutoGeoWhenGranted();
        }
    }

    // --- Geolocation ---
    function attemptAutoGeo() {
        navigator.geolocation.getCurrentPosition(
            function (pos) {
                currentLat = pos.coords.latitude;
                currentLon = pos.coords.longitude;
                showLocationInfo();
                fetchFlights(currentLat, currentLon);
            },
            function () {
                // Silently fail — user can enter zip code
            },
            { timeout: 8000, maximumAge: 300000 }
        );
    }

    function attemptAutoGeoWhenGranted() {
        if (!navigator.geolocation) return;

        // Browser support for Permissions API varies; fall back to silent attempt when unavailable.
        if (!navigator.permissions || !navigator.permissions.query) {
            attemptAutoGeo();
            return;
        }

        navigator.permissions.query({ name: "geolocation" })
            .then(function (result) {
                if (result.state === "granted") {
                    attemptAutoGeo();
                }
            })
            .catch(function () {
                // If permissions API fails unexpectedly, keep startup resilient.
            });
    }

    function hasSecureGeolocationContext() {
        if (window.isSecureContext) return true;

        // Localhost origins are treated as secure for geolocation in modern browsers.
        var host = window.location && window.location.hostname ? window.location.hostname : "";
        return host === "localhost" || host === "127.0.0.1";
    }

    function requestUserLocation(onSuccess, onError) {
        if (!navigator.geolocation) {
            onError({
                message: "Geolocation is not available in this browser.",
                isPermission: false
            });
            return;
        }

        if (!hasSecureGeolocationContext()) {
            onError({
                message: "Location needs a secure context (HTTPS or localhost). Open the app on localhost or enable HTTPS.",
                isPermission: false
            });
            return;
        }

        navigator.geolocation.getCurrentPosition(
            function (pos) {
                onSuccess(pos);
            },
            function (err) {
                onError({
                    message: err && err.message ? err.message : "Could not determine your location.",
                    isPermission: !!(err && err.code === err.PERMISSION_DENIED)
                });
            },
            { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
        );
    }

    function onGeoClick() {
        geoBtn.disabled = true;
        geoBtn.textContent = "Locating...";
        hideError();

        requestUserLocation(
            function (pos) {
                geoBtn.disabled = false;
                geoBtn.textContent = "Use My Location";
                currentLat = pos.coords.latitude;
                currentLon = pos.coords.longitude;
                zipcodeInput.value = "";
                showLocationInfo();
                fetchFlights(currentLat, currentLon);
            },
            function (err) {
                geoBtn.disabled = false;
                geoBtn.textContent = "Use My Location";
                if (err.isPermission) {
                    showError("Location access was blocked. Allow location in browser site settings, then try again.");
                } else {
                    showError(err.message || "Could not determine your location. Please enter a zip code.");
                }
            }
        );
    }

    // --- Zip Code Search ---
    function onSearchClick() {
        const zip = zipcodeInput.value.trim();
        if (!zip || zip.length !== 5 || !/^\d{5}$/.test(zip)) {
            showError("Please enter a valid 5-digit US zip code.");
            return;
        }

        hideError();
        searchBtn.disabled = true;
        searchBtn.textContent = "Resolving…";

        fetch("/api/resolve-zip?zip=" + encodeURIComponent(zip))
            .then(function (resp) {
                if (!resp.ok) return resp.json().then(function (d) { throw new Error(d.error); });
                return resp.json();
            })
            .then(function (data) {
                searchBtn.disabled = false;
                searchBtn.textContent = "Search";
                currentLat = data.latitude;
                currentLon = data.longitude;
                var label = data.city && data.state
                    ? data.city + ", " + data.state + " (" + zip + ")"
                    : "Zip code " + zip;
                showLocationInfo(label);
                fetchFlights(currentLat, currentLon, zip);
            })
            .catch(function (err) {
                searchBtn.disabled = false;
                searchBtn.textContent = "Search";
                showError(err.message || "Could not resolve zip code.");
            });
    }

    function onTimeFilterClick(e) {
        var btn = e.target.closest("[data-window]");
        if (!btn) return;

        var nextWindow = btn.getAttribute("data-window") || "last_24_hours";
        if (nextWindow === currentTimeWindow) return;

        currentTimeWindow = nextWindow;
        currentWindowLabel = defaultWindowLabel(currentTimeWindow);
        syncTimeFilterButtons();

        if (currentLat != null && currentLon != null) {
            fetchFlights(currentLat, currentLon);
        }
    }

    function setTimeWindow(windowKey, options) {
        var nextWindow = windowKey || "last_24_hours";
        currentTimeWindow = nextWindow;
        currentWindowLabel = defaultWindowLabel(currentTimeWindow);
        syncTimeFilterButtons();

        if (options && options.fetch === false) {
            return;
        }

        if (currentLat != null && currentLon != null) {
            fetchFlights(currentLat, currentLon);
        }
    }

    // --- Flight Search ---
    function fetchFlights(lat, lon, zip) {
        showLoading();
        hideResults();
        hideNoResults();
        hideError();

        var radius = radiusSelect.value;
        var url = "/api/flights?lat=" + lat + "&lon=" + lon + "&radius=" + radius + "&window=" + encodeURIComponent(currentTimeWindow);
        if (zip) url += "&zip=" + encodeURIComponent(zip);

        fetch(url)
            .then(function (resp) {
                if (!resp.ok) return resp.json().then(function (d) { throw new Error(d.error); });
                return resp.json();
            })
            .then(function (data) {
                hideLoading();
                activeQuery = data.query || null;
                applyQueryState(activeQuery);

                if (window.OverflightMap) {
                    window.OverflightMap.onSearchComplete(currentLat, currentLon, parseFloat(radius), data.query);
                }

                if (data.flights && data.flights.length > 0) {
                    renderResults(data);
                } else {
                    showNoResults();
                }
            })
            .catch(function (err) {
                hideLoading();
                showError("Search failed: " + (err.message || "Unknown error"));
            });
    }

    // --- Render Results ---
    function renderResults(data) {
        var flights = data.flights;
        var count = data.count;
        var radius = data.query.radius_miles;
        var windowLabel = data.query.window_label || defaultWindowLabel(currentTimeWindow);

        resultsTitle.textContent = count + " aircraft in " + windowLabel;
        resultsSubtitle.textContent = "Within " + radius + " miles of your location";

        flightList.innerHTML = "";

        flights.forEach(function (f) {
            var card = createFlightCard(f);
            flightList.appendChild(card);
        });

        resultsSection.style.display = "block";
    }

    function createFlightCard(flight) {
        var card = document.createElement("div");
        card.className = "flight-card";
        card.dataset.icao24 = flight.icao24 || "";
        card.dataset.timestamp = String(flight.timestamp || "");

        // Determine display name
        var title = flight.manufacturer && flight.model
            ? flight.manufacturer + " " + flight.model
            : flight.icao24.toUpperCase();

        var callsign = flight.callsign || "—";
        var operator = flight.operator || "";
        var timeStr = formatTime(flight.timestamp);

        // Summary tags
        var tags = [];
        if (flight.altitude_feet != null) tags.push("✈ " + formatNumber(flight.altitude_feet) + " ft");
        if (flight.speed_knots != null) tags.push("⟶ " + flight.speed_knots + " kts");
        if (flight.distance_miles != null) tags.push("📏 " + flight.distance_miles + " mi");
        if (flight.aircraft_age != null) tags.push("📅 " + flight.aircraft_age + " yrs old");

        card.innerHTML =
            '<div class="flight-card-header">' +
                '<div>' +
                    '<div class="flight-card-callsign">' + escapeHtml(callsign) + '</div>' +
                    '<div class="flight-card-title">' + escapeHtml(title) + '</div>' +
                    (operator ? '<div style="font-size:0.85rem;color:var(--color-text-secondary)">' + escapeHtml(operator) + '</div>' : '') +
                '</div>' +
                '<div class="flight-card-actions">' +
                    '<button class="show-on-map-btn" type="button">Show on map</button>' +
                    '<div class="flight-card-time">' + escapeHtml(timeStr) + '</div>' +
                '</div>' +
            '</div>' +
            '<div class="flight-card-summary">' +
                tags.map(function (t) { return '<span class="tag">' + t + '</span>'; }).join("") +
            '</div>' +
            '<div class="flight-card-details">' +
                '<div class="flight-card-info">' +
                    infoItem("ICAO24", flight.icao24.toUpperCase()) +
                    infoItem("Registration", flight.registration || "—") +
                    infoItem("Callsign", callsign) +
                    infoItem("Operator", operator || "—") +
                    infoItem("Aircraft", title) +
                    infoItem("Owner", flight.owner || "—") +
                    infoItem("Altitude", flight.altitude_feet != null ? formatNumber(flight.altitude_feet) + " ft" : "—") +
                    infoItem("Ground Speed", flight.speed_knots != null ? flight.speed_knots + " kts (" + flight.speed_mph + " mph)" : "—") +
                    infoItem("Heading", flight.heading != null ? Math.round(flight.heading) + "°" : "—") +
                    infoItem("Vertical Rate", flight.vertical_rate != null ? (flight.vertical_rate > 0 ? "+" : "") + Math.round(flight.vertical_rate * 196.85) + " ft/min" : "—") +
                    infoItem("Distance", flight.distance_miles != null ? flight.distance_miles + " miles" : "—") +
                    infoItem("On Ground", flight.on_ground ? "Yes" : "No") +
                    infoItem("Built Year", flight.built_year || "—") +
                    infoItem("Aircraft Age", flight.aircraft_age != null ? flight.aircraft_age + " years" : "—") +
                    infoItem("Country", flight.registered_country || "—") +
                    infoItem("Spotted", timeStr) +
                '</div>' +
            '</div>';

        card.addEventListener("click", function () {
            if (window.OverflightMap && typeof window.OverflightMap.pausePlayback === "function") {
                window.OverflightMap.pausePlayback();
            }
            card.classList.toggle("expanded");
        });

        var showOnMapBtn = card.querySelector(".show-on-map-btn");
        if (showOnMapBtn) {
            showOnMapBtn.addEventListener("click", function (evt) {
                evt.preventDefault();
                evt.stopPropagation();
                if (window.OverflightMap && typeof window.OverflightMap.focusFlight === "function") {
                    window.OverflightMap.focusFlight({
                        icao24: flight.icao24,
                        timestamp: flight.timestamp,
                        lat: flight.latitude,
                        lon: flight.longitude
                    });
                }
            });
        }

        return card;
    }

    function infoItem(label, value) {
        return '<div class="flight-info-item">' +
            '<span class="flight-info-label">' + escapeHtml(label) + '</span>' +
            '<span class="flight-info-value">' + escapeHtml(String(value)) + '</span>' +
            '</div>';
    }

    // --- UI Helpers ---
    function showLocationInfo(label) {
        if (label) {
            locationText.textContent = "Location: " + label;
        } else if (currentLat && currentLon) {
            locationText.textContent = "Location: " + currentLat.toFixed(4) + ", " + currentLon.toFixed(4);
        }
        locationInfo.style.display = "block";
    }

    function showError(msg) {
        errorMsg.textContent = msg;
        errorMsg.style.display = "block";
    }

    function hideError() {
        errorMsg.style.display = "none";
    }

    function showLoading() {
        loadingSection.style.display = "block";
    }

    function hideLoading() {
        loadingSection.style.display = "none";
    }

    function hideResults() {
        resultsSection.style.display = "none";
    }

    function showNoResults() {
        if (noResultsHeading) noResultsHeading.textContent = "No aircraft found";
        if (noResultsBody) {
            noResultsBody.textContent = "No aircraft were detected within your selected radius during " + currentWindowLabel.toLowerCase() + ".";
        }
        noResults.style.display = "block";
    }

    function hideNoResults() {
        noResults.style.display = "none";
    }

    function formatTime(unixTimestamp) {
        var d = new Date(unixTimestamp * 1000);
        var now = new Date();
        var diffMs = now - d;
        var diffMin = Math.floor(diffMs / 60000);

        if (diffMin < 1) return "Just now";
        if (diffMin < 60) return diffMin + " min ago";
        var diffHrs = Math.floor(diffMin / 60);
        if (diffHrs < 24) return diffHrs + "h " + (diffMin % 60) + "m ago";

        return d.toLocaleString();
    }

    function formatNumber(n) {
        return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }

    function parseCoord(value) {
        if (value == null || value === "") return null;
        var n = Number(value);
        return Number.isFinite(n) ? n : null;
    }

    function syncTimeFilterButtons() {
        timeFilterButtons.forEach(function (btn) {
            btn.classList.toggle("time-filter-btn-active", btn.getAttribute("data-window") === currentTimeWindow);
        });
    }

    function applyQueryState(query) {
        if (!query) return;
        currentWindowLabel = query.window_label || currentWindowLabel;
        currentTimeWindow = query.window || currentTimeWindow;
        syncTimeFilterButtons();
    }

    function focusFlightByIcao(icao24, options) {
        if (!icao24 || !flightList) return false;
        var selector = '.flight-card[data-icao24="' + cssEscape(icao24) + '"]';
        var card = flightList.querySelector(selector);
        if (!card) return false;

        if (!card.classList.contains("expanded") || (options && options.expand)) {
            card.classList.add("expanded");
        }

        card.classList.add("flight-card-focused");
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        window.setTimeout(function () {
            card.classList.remove("flight-card-focused");
        }, 1800);
        return true;
    }

    function cssEscape(value) {
        if (window.CSS && typeof window.CSS.escape === "function") {
            return window.CSS.escape(value);
        }
        return String(value).replace(/(["\\])/g, "\\$1");
    }

    function defaultWindowLabel(windowKey) {
        switch (windowKey) {
            case "last_hour":
                return "Last hour";
            case "today":
                return "Today";
            case "yesterday":
                return "Yesterday";
            default:
                return "Last " + (window.OVERFLIGHT.searchDefaultWindowHours || 24) + " hours";
        }
    }

    function escapeHtml(str) {
        var div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    window.OverflightApp = {
        setTimeWindow: setTimeWindow,
        focusFlightByIcao: focusFlightByIcao,
        getSearchState: function () {
            return {
                lat: currentLat,
                lon: currentLon,
                radius: parseFloat(radiusSelect.value),
                window: currentTimeWindow,
                windowLabel: currentWindowLabel,
                query: activeQuery
            };
        }
    };

    // --- Start ---
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
