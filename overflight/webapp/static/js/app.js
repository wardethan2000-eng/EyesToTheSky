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
    const loadingSection = document.getElementById("loading");
    const resultsSection = document.getElementById("results-section");
    const resultsTitle = document.getElementById("results-title");
    const resultsSubtitle = document.getElementById("results-subtitle");
    const flightList = document.getElementById("flight-list");
    const noResults = document.getElementById("no-results");

    // --- State ---
    let currentLat = window.OVERFLIGHT.savedLat;
    let currentLon = window.OVERFLIGHT.savedLon;

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

        // Auto-search if we have saved coordinates
        if (currentLat && currentLon) {
            showLocationInfo();
            fetchFlights(currentLat, currentLon);
        }

        // Try geolocation automatically on first visit (no saved location)
        if (!currentLat && !currentLon && navigator.geolocation) {
            attemptAutoGeo();
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

    function onGeoClick() {
        if (!navigator.geolocation) {
            showError("Geolocation is not supported by your browser. Please enter a zip code.");
            return;
        }

        geoBtn.disabled = true;
        geoBtn.textContent = "Locating…";
        hideError();

        navigator.geolocation.getCurrentPosition(
            function (pos) {
                geoBtn.disabled = false;
                geoBtn.textContent = "📍 Use My Location";
                currentLat = pos.coords.latitude;
                currentLon = pos.coords.longitude;
                zipcodeInput.value = "";
                showLocationInfo();
                fetchFlights(currentLat, currentLon);
            },
            function (err) {
                geoBtn.disabled = false;
                geoBtn.textContent = "📍 Use My Location";
                if (err.code === err.PERMISSION_DENIED) {
                    showError("Location access denied. Please enter a zip code instead.");
                } else {
                    showError("Could not determine your location. Please enter a zip code.");
                }
            },
            { timeout: 10000, maximumAge: 60000 }
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

    // --- Flight Search ---
    function fetchFlights(lat, lon, zip) {
        showLoading();
        hideResults();
        hideNoResults();
        hideError();

        var radius = radiusSelect.value;
        var url = "/api/flights?lat=" + lat + "&lon=" + lon + "&radius=" + radius;
        if (zip) url += "&zip=" + encodeURIComponent(zip);

        fetch(url)
            .then(function (resp) {
                if (!resp.ok) return resp.json().then(function (d) { throw new Error(d.error); });
                return resp.json();
            })
            .then(function (data) {
                hideLoading();
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

        resultsTitle.textContent = count + " aircraft" + (count !== 1 ? "" : "") + " in the past 24 hours";
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
                '<div class="flight-card-time">' + escapeHtml(timeStr) + '</div>' +
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
            card.classList.toggle("expanded");
        });

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
            locationText.textContent = "📍 " + label;
        } else if (currentLat && currentLon) {
            locationText.textContent = "📍 " + currentLat.toFixed(4) + ", " + currentLon.toFixed(4);
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

    function escapeHtml(str) {
        var div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // --- Start ---
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
