/**
 * OverFlight — Map View Controller
 *
 * Handles:
 * - MapLibre GL JS map initialization
 * - View toggle between card view and map view
 * - Aircraft marker rendering using GeoJSON source + symbol layers
 * - Search radius circle overlay
 * - Detail sidebar for clicked aircraft
 * - Playback control wiring
 */

(function () {
    "use strict";

    var map = null;
    var mapInitialized = false;
    var selectedAircraft = null;
    var iconImagesLoaded = {};

    // DOM references (resolved on init)
    var mapSection, mapEl, mapLoading, mapLoadingText;
    var aircraftCounter, aircraftCounterText;
    var playbackControls, playPauseBtn, scrubber, timeLabel;
    var speedSelect, altitudeSelect;
    var detailSidebar, detailClose, detailContent;
    var viewToggle, btnCardView, btnMapView;
    var resultsSection, flightList;

    // Current search params
    var searchLat = null;
    var searchLon = null;
    var searchRadius = null;

    // Icon images loaded as Image objects keyed by category
    var ICON_SIZE = 40; // px base size for widebody (1.0 multiplier)

    function initDOM() {
        mapSection = document.getElementById("map-section");
        mapEl = document.getElementById("map");
        mapLoading = document.getElementById("map-loading");
        mapLoadingText = document.getElementById("map-loading-text");
        aircraftCounter = document.getElementById("aircraft-counter");
        aircraftCounterText = document.getElementById("aircraft-counter-text");
        playbackControls = document.getElementById("playback-controls");
        playPauseBtn = document.getElementById("playback-play-pause");
        scrubber = document.getElementById("playback-scrubber");
        timeLabel = document.getElementById("playback-time-label");
        speedSelect = document.getElementById("playback-speed");
        altitudeSelect = document.getElementById("altitude-filter");
        detailSidebar = document.getElementById("detail-sidebar");
        detailClose = document.getElementById("detail-close");
        detailContent = document.getElementById("detail-content");
        viewToggle = document.getElementById("view-toggle");
        btnCardView = document.getElementById("btn-card-view");
        btnMapView = document.getElementById("btn-map-view");
        resultsSection = document.getElementById("results-section");
        flightList = document.getElementById("flight-list");
    }

    /**
     * Called by app.js after a successful flight search.
     * Shows the view toggle and stores search params for map use.
     */
    function onSearchComplete(lat, lon, radius) {
        initDOM();
        searchLat = lat;
        searchLon = lon;
        searchRadius = radius;

        if (viewToggle) viewToggle.style.display = "flex";
        bindViewToggle();
    }

    function bindViewToggle() {
        if (!btnCardView || !btnMapView) return;

        btnCardView.onclick = function () {
            showCardView();
        };
        btnMapView.onclick = function () {
            showMapView();
        };
    }

    function showCardView() {
        if (btnCardView) btnCardView.classList.add("active");
        if (btnMapView) btnMapView.classList.remove("active");
        if (flightList) flightList.style.display = "";
        if (mapSection) mapSection.style.display = "none";
        hideDetailSidebar();
    }

    function showMapView() {
        if (btnMapView) btnMapView.classList.add("active");
        if (btnCardView) btnCardView.classList.remove("active");
        if (flightList) flightList.style.display = "none";
        if (mapSection) mapSection.style.display = "block";

        if (!mapInitialized) {
            initMap();
        } else {
            map.resize();
        }
    }

    function initMap() {
        if (mapInitialized) return;

        map = new maplibregl.Map({
            container: "map",
            style: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
            center: [searchLon, searchLat],
            zoom: getZoomForRadius(searchRadius),
            attributionControl: true
        });

        map.addControl(new maplibregl.NavigationControl(), "top-right");

        map.on("load", function () {
            mapInitialized = true;
            setupMapLayers();
            drawSearchRadius();
            bindPlaybackControls();
            startPlayback();
        });
    }

    function getZoomForRadius(radiusMiles) {
        // Approximate zoom level for a given radius
        if (radiusMiles <= 5) return 11;
        if (radiusMiles <= 10) return 10;
        if (radiusMiles <= 25) return 9;
        if (radiusMiles <= 50) return 8;
        return 7;
    }

    // --- Map Layers ---

    function setupMapLayers() {
        // GeoJSON source for detailed aircraft (full icons)
        map.addSource("aircraft-detailed", {
            type: "geojson",
            data: emptyFeatureCollection()
        });

        // GeoJSON source for overflow dots
        map.addSource("aircraft-dots", {
            type: "geojson",
            data: emptyFeatureCollection()
        });

        // GeoJSON source for selected aircraft track polyline
        map.addSource("selected-track", {
            type: "geojson",
            data: emptyFeatureCollection()
        });

        // GeoJSON source for search radius circle
        map.addSource("search-radius", {
            type: "geojson",
            data: emptyFeatureCollection()
        });

        // Search radius circle
        map.addLayer({
            id: "search-radius-fill",
            type: "fill",
            source: "search-radius",
            paint: {
                "fill-color": "#1a73e8",
                "fill-opacity": 0.05
            }
        });
        map.addLayer({
            id: "search-radius-stroke",
            type: "line",
            source: "search-radius",
            paint: {
                "line-color": "#1a73e8",
                "line-width": 1.5,
                "line-dasharray": [4, 3],
                "line-opacity": 0.4
            }
        });

        // Selected track polyline
        map.addLayer({
            id: "selected-track-line",
            type: "line",
            source: "selected-track",
            paint: {
                "line-color": "#e8391a",
                "line-width": 2.5,
                "line-dasharray": [3, 2],
                "line-opacity": 0.8
            }
        });

        // Overflow dots layer
        map.addLayer({
            id: "aircraft-dots-layer",
            type: "circle",
            source: "aircraft-dots",
            paint: {
                "circle-radius": 3,
                "circle-color": [
                    "case",
                    ["<", ["get", "altitude"], 3000], "#4CAF50",
                    ["<", ["get", "altitude"], 10000], "#FF9800",
                    "#2196F3"
                ],
                "circle-opacity": 0.7,
                "circle-stroke-width": 0.5,
                "circle-stroke-color": "#fff"
            }
        });

        // Detailed aircraft - rendered as circles with callsign labels
        // (We use circle + symbol layers since custom SVG images require async loading)
        map.addLayer({
            id: "aircraft-icons-layer",
            type: "circle",
            source: "aircraft-detailed",
            paint: {
                "circle-radius": [
                    "interpolate", ["linear"], ["get", "sizeMult"],
                    0.3, 5,
                    0.5, 7,
                    0.75, 9,
                    1.0, 11
                ],
                "circle-color": [
                    "case",
                    ["==", ["get", "selected"], true], "#e8391a",
                    ["==", ["get", "phase"], "ground"], "#9E9E9E",
                    ["<", ["get", "altitude"], 3000], "#4CAF50",
                    ["<", ["get", "altitude"], 10000], "#FF9800",
                    "#2196F3"
                ],
                "circle-opacity": 0.9,
                "circle-stroke-width": [
                    "case",
                    ["==", ["get", "selected"], true], 3,
                    1.5
                ],
                "circle-stroke-color": [
                    "case",
                    ["==", ["get", "selected"], true], "#fff",
                    "rgba(255,255,255,0.8)"
                ]
            }
        });

        // Heading indicator - a small triangle showing direction
        map.addLayer({
            id: "aircraft-heading-layer",
            type: "symbol",
            source: "aircraft-detailed",
            layout: {
                "icon-image": "heading-arrow",
                "icon-size": 0.5,
                "icon-rotate": ["get", "heading"],
                "icon-rotation-alignment": "map",
                "icon-allow-overlap": true,
                "icon-ignore-placement": true
            }
        });

        // Callsign labels
        map.addLayer({
            id: "aircraft-labels-layer",
            type: "symbol",
            source: "aircraft-detailed",
            minzoom: 9,
            layout: {
                "text-field": ["get", "callsign"],
                "text-font": ["Open Sans Regular"],
                "text-size": 10,
                "text-offset": [0, 1.5],
                "text-anchor": "top",
                "text-allow-overlap": false,
                "text-optional": true
            },
            paint: {
                "text-color": "#333",
                "text-halo-color": "#fff",
                "text-halo-width": 1
            }
        });

        // Create heading arrow image
        createHeadingArrowImage();

        // Click handler for detailed aircraft
        map.on("click", "aircraft-icons-layer", function (e) {
            if (e.features && e.features.length > 0) {
                var feat = e.features[0];
                onAircraftClick(feat.properties);
            }
        });

        map.on("mouseenter", "aircraft-icons-layer", function () {
            map.getCanvas().style.cursor = "pointer";
        });
        map.on("mouseleave", "aircraft-icons-layer", function () {
            map.getCanvas().style.cursor = "";
        });
    }

    function createHeadingArrowImage() {
        // Create a small triangle arrow image for heading indication
        var size = 32;
        var canvas = document.createElement("canvas");
        canvas.width = size;
        canvas.height = size;
        var ctx = canvas.getContext("2d");

        // Draw a small upward-pointing triangle
        ctx.beginPath();
        ctx.moveTo(size / 2, 2);
        ctx.lineTo(size / 2 + 6, size / 2 + 4);
        ctx.lineTo(size / 2 - 6, size / 2 + 4);
        ctx.closePath();
        ctx.fillStyle = "rgba(0,0,0,0.6)";
        ctx.fill();

        map.addImage("heading-arrow", {
            width: size,
            height: size,
            data: ctx.getImageData(0, 0, size, size).data
        });
    }

    function emptyFeatureCollection() {
        return { type: "FeatureCollection", features: [] };
    }

    // --- Search Radius Circle ---

    function drawSearchRadius() {
        if (!searchLat || !searchLon || !searchRadius) return;

        var center = [searchLon, searchLat];
        var radiusKm = searchRadius * 1.60934;
        var points = 64;
        var coords = [];

        for (var i = 0; i <= points; i++) {
            var angle = (i / points) * 2 * Math.PI;
            var dx = radiusKm * Math.cos(angle);
            var dy = radiusKm * Math.sin(angle);
            var lat = searchLat + (dy / 111.32);
            var lon = searchLon + (dx / (111.32 * Math.cos(searchLat * Math.PI / 180)));
            coords.push([lon, lat]);
        }

        map.getSource("search-radius").setData({
            type: "Feature",
            geometry: {
                type: "Polygon",
                coordinates: [coords]
            }
        });
    }

    // --- Aircraft Rendering ---

    function updateAircraftOnMap(detailed, dots, totalCount) {
        if (!map || !mapInitialized) return;

        // Build GeoJSON for detailed aircraft
        var detailedFeatures = detailed.map(function (ac) {
            var iconInfo = window.OverflightIcons.getAircraftIcon({
                model: ac.callsign,  // Best we have without fetching enrichment
                operator: null
            });

            return {
                type: "Feature",
                geometry: {
                    type: "Point",
                    coordinates: [ac.lon, ac.lat]
                },
                properties: {
                    id: ac.id,
                    icao24: ac.icao24,
                    callsign: ac.callsign || "",
                    phase: ac.phase,
                    altitude: ac.altitude || 0,
                    heading: ac.heading || 0,
                    sizeMult: iconInfo.size,
                    category: iconInfo.category,
                    selected: selectedAircraft && selectedAircraft.icao24 === ac.icao24
                }
            };
        });

        map.getSource("aircraft-detailed").setData({
            type: "FeatureCollection",
            features: detailedFeatures
        });

        // Build GeoJSON for dots
        var dotFeatures = dots.map(function (ac) {
            return {
                type: "Feature",
                geometry: {
                    type: "Point",
                    coordinates: [ac.lon, ac.lat]
                },
                properties: {
                    altitude: ac.altitude || 0
                }
            };
        });

        map.getSource("aircraft-dots").setData({
            type: "FeatureCollection",
            features: dotFeatures
        });

        // Update counter
        if (totalCount > 0) {
            aircraftCounter.style.display = "block";
            var msg = totalCount + " aircraft";
            if (dots.length > 0) {
                msg = "Showing " + detailed.length + " of " + totalCount + " aircraft";
            }
            aircraftCounterText.textContent = msg;
        } else {
            aircraftCounter.style.display = "none";
        }
    }

    // --- Playback Controls ---

    function bindPlaybackControls() {
        playPauseBtn.onclick = function () {
            var playing = window.OverflightPlayback.togglePlayPause();
            playPauseBtn.innerHTML = playing ? "&#9646;&#9646;" : "&#9654;";
        };

        scrubber.oninput = function () {
            var progress = parseInt(scrubber.value, 10) / 1000;
            window.OverflightPlayback.seek(progress);
        };

        speedSelect.onchange = function () {
            window.OverflightPlayback.setSpeed(parseFloat(speedSelect.value));
        };

        altitudeSelect.onchange = function () {
            window.OverflightPlayback.setAltitudeFilter(parseFloat(altitudeSelect.value));
        };
    }

    function updateTimeDisplay(playbackTime, progress) {
        // Update scrubber position
        scrubber.value = Math.round(progress * 1000);

        // Format time
        var d = new Date(playbackTime * 1000);
        var hours = d.getHours();
        var minutes = d.getMinutes();
        var ampm = hours >= 12 ? "PM" : "AM";
        hours = hours % 12;
        if (hours === 0) hours = 12;
        var minStr = minutes < 10 ? "0" + minutes : "" + minutes;
        timeLabel.textContent = hours + ":" + minStr + " " + ampm;
    }

    function startPlayback() {
        mapLoading.style.display = "flex";

        window.OverflightPlayback.init(searchLat, searchLon, searchRadius, {
            onAircraftUpdate: function (detailed, dots, totalCount) {
                updateAircraftOnMap(detailed, dots, totalCount);
            },
            onPlaybackTimeChange: function (t, progress) {
                updateTimeDisplay(t, progress);
            },
            onLoadingProgress: function (message) {
                mapLoadingText.textContent = message;
            },
            onReady: function () {
                mapLoading.style.display = "none";
                playbackControls.style.display = "flex";
            }
        });
    }

    // --- Aircraft Click / Detail Sidebar ---

    function onAircraftClick(properties) {
        var icao24 = properties.icao24;
        if (!icao24) return;

        selectedAircraft = { icao24: icao24 };

        // Show track line for this aircraft (find in allTracks)
        drawSelectedTrack(icao24);

        // Open sidebar with loading state
        detailContent.innerHTML =
            '<div class="detail-loading"><div class="spinner"></div><p>Loading details\u2026</p></div>';
        detailSidebar.style.display = "block";

        // Fetch detail
        fetch("/api/track-detail/" + encodeURIComponent(icao24))
            .then(function (resp) {
                if (!resp.ok) throw new Error("Detail fetch failed");
                return resp.json();
            })
            .then(function (data) {
                renderDetailSidebar(data, properties);
            })
            .catch(function () {
                detailContent.innerHTML = '<p class="detail-error">Could not load aircraft details.</p>';
            });

        detailClose.onclick = function () {
            hideDetailSidebar();
        };
    }

    function drawSelectedTrack(icao24) {
        // Find polyline data from playback state
        var state = window.OverflightPlayback.getState();
        // We need to search allTracks — access via global
        var tracks = window._overflightAllTracks || [];
        var coords = [];

        for (var i = 0; i < tracks.length; i++) {
            if (tracks[i].icao24 === icao24 && tracks[i].polyline) {
                var poly = tracks[i].polyline;
                for (var j = 0; j < poly.length; j++) {
                    coords.push([poly[j][2], poly[j][1]]);
                }
            }
        }

        if (coords.length > 0 && map.getSource("selected-track")) {
            map.getSource("selected-track").setData({
                type: "Feature",
                geometry: {
                    type: "LineString",
                    coordinates: coords
                }
            });
        }
    }

    function renderDetailSidebar(data, properties) {
        var iconInfo = window.OverflightIcons.getAircraftIcon(data);
        var alt = properties.altitude;
        var altFt = alt ? Math.round(alt * 3.28084) : null;

        var html =
            '<div class="detail-header">' +
                '<div class="detail-icon-badge" style="background:' + altitudeColor(alt) + '">' +
                    '<span class="detail-icon-label">' + escapeHtml(iconInfo.label) + '</span>' +
                '</div>' +
                '<h3 class="detail-title">' + escapeHtml(data.manufacturer && data.model ? data.manufacturer + " " + data.model : data.icao24 || properties.icao24) + '</h3>' +
            '</div>' +
            '<div class="detail-info">' +
                detailRow("Callsign", data.callsign || properties.callsign || "\u2014") +
                detailRow("Registration", data.registration || "\u2014") +
                detailRow("ICAO24", (data.icao24 || properties.icao24 || "").toUpperCase()) +
                detailRow("Operator", data.operator || "\u2014") +
                detailRow("Owner", data.owner || "\u2014") +
                detailRow("Manufacturer", data.manufacturer || "\u2014") +
                detailRow("Model", data.model || "\u2014") +
                detailRow("Built Year", data.built_year || "\u2014") +
                detailRow("Aircraft Age", data.aircraft_age != null ? data.aircraft_age + " years" : "\u2014") +
                detailRow("Country", data.registered_country || "\u2014") +
                detailRow("Altitude", altFt != null ? formatNumber(altFt) + " ft" : "\u2014") +
                detailRow("Heading", properties.heading != null ? Math.round(properties.heading) + "\u00b0" : "\u2014") +
            '</div>';

        detailContent.innerHTML = html;
    }

    function detailRow(label, value) {
        return '<div class="detail-row">' +
            '<span class="detail-label">' + escapeHtml(label) + '</span>' +
            '<span class="detail-value">' + escapeHtml(String(value)) + '</span>' +
            '</div>';
    }

    function hideDetailSidebar() {
        if (detailSidebar) detailSidebar.style.display = "none";
        selectedAircraft = null;

        // Clear track line
        if (map && map.getSource("selected-track")) {
            map.getSource("selected-track").setData(emptyFeatureCollection());
        }
    }

    function altitudeColor(alt) {
        if (!alt || alt < 3000) return "#4CAF50";
        if (alt < 10000) return "#FF9800";
        return "#2196F3";
    }

    function escapeHtml(str) {
        var div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    function formatNumber(n) {
        return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }

    // Expose globally for app.js integration
    window.OverflightMap = {
        onSearchComplete: onSearchComplete,
        showMapView: showMapView,
        showCardView: showCardView
    };
})();
