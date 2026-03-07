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
    var mapWarning;
    var aircraftCounter, aircraftCounterText;
    var playbackControls, playPauseBtn, scrubber, timeLabel;
    var speedSelect, altitudeSelect, settingsToggle;
    var detailSidebar, detailClose, detailContent, detailDragHandle;
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
        mapWarning = document.getElementById("map-warning");
        aircraftCounter = document.getElementById("aircraft-counter");
        aircraftCounterText = document.getElementById("aircraft-counter-text");
        playbackControls = document.getElementById("playback-controls");
        playPauseBtn = document.getElementById("playback-play-pause");
        scrubber = document.getElementById("playback-scrubber");
        timeLabel = document.getElementById("playback-time-label");
        speedSelect = document.getElementById("playback-speed");
        altitudeSelect = document.getElementById("altitude-filter");
        settingsToggle = document.getElementById("playback-settings-toggle");
        detailSidebar = document.getElementById("detail-sidebar");
        detailClose = document.getElementById("detail-close");
        detailContent = document.getElementById("detail-content");
        detailDragHandle = document.getElementById("detail-drag-handle");
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
        if (mapWarning) mapWarning.style.display = "none";
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
            syncRenderBudgetToZoom();

            map.on("zoomend", syncRenderBudgetToZoom);
            map.on("moveend", function () {
                var center = map.getCenter();
                window.OverflightPlayback.setMapCenter(center.lat, center.lng);
            });

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

    function renderBudgetForZoom(zoom) {
        if (zoom > 12) return 220;
        if (zoom >= 9) return window.OVERFLIGHT.renderBudgetMax || 150;
        return 90;
    }

    function syncRenderBudgetToZoom() {
        if (!map || !window.OverflightPlayback) return;
        var zoom = map.getZoom();
        window.OverflightPlayback.setRenderBudget(renderBudgetForZoom(zoom));
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

        // GeoJSON source for active aircraft trails in the current playback window.
        map.addSource("active-trails", {
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

        map.addLayer({
            id: "active-trails-line",
            type: "line",
            source: "active-trails",
            paint: {
                "line-color": "#1f7ae0",
                "line-width": 1.2,
                "line-opacity": 0.22
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

        // Detailed aircraft - rendered as a single basic airplane icon for all types.
        map.addLayer({
            id: "aircraft-icons-layer",
            type: "symbol",
            source: "aircraft-detailed",
            minzoom: 9,
            layout: {
                "icon-image": [
                    "case",
                    ["==", ["get", "selected"], true],
                    "aircraft-basic-selected",
                    "aircraft-basic"
                ],
                "icon-size": [
                    "interpolate", ["linear"], ["get", "sizeMult"],
                    0.3, 0.28,
                    0.5, 0.34,
                    0.75, 0.42,
                    1.0, 0.52
                ],
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
            minzoom: 12,
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

        // Register the simple airplane icon used for all aircraft categories.
        createAircraftMarkerImages();

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

    function createAircraftMarkerImages() {
        addAircraftMarkerImage("aircraft-basic", "#1f7ae0", "#0e3a6d");
        addAircraftMarkerImage("aircraft-basic-selected", "#e8391a", "#ffffff");
    }

    function addAircraftMarkerImage(name, fillColor, strokeColor) {
        var size = 48;
        var canvas = document.createElement("canvas");
        canvas.width = size;
        canvas.height = size;
        var ctx = canvas.getContext("2d");

        var cx = size / 2;
        var cy = size / 2;
        ctx.translate(cx, cy);

        // Simple airplane silhouette pointing up.
        ctx.beginPath();
        ctx.moveTo(0, -18);      // nose
        ctx.lineTo(3, -7);       // upper fuselage right
        ctx.lineTo(11, -5);      // right wing tip
        ctx.lineTo(10, -1);      // right wing trailing edge
        ctx.lineTo(3, -1);       // fuselage right
        ctx.lineTo(3, 10);       // right tail root
        ctx.lineTo(7, 13);       // right tail tip
        ctx.lineTo(6, 16);       // right tail lower
        ctx.lineTo(0, 13);       // tail center
        ctx.lineTo(-6, 16);      // left tail lower
        ctx.lineTo(-7, 13);      // left tail tip
        ctx.lineTo(-3, 10);      // left tail root
        ctx.lineTo(-3, -1);      // fuselage left
        ctx.lineTo(-10, -1);     // left wing trailing edge
        ctx.lineTo(-11, -5);     // left wing tip
        ctx.lineTo(-3, -7);      // upper fuselage left
        ctx.closePath();

        ctx.fillStyle = fillColor;
        ctx.fill();
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = strokeColor;
        ctx.stroke();

        if (!map.hasImage(name)) {
            map.addImage(name, {
                width: size,
                height: size,
                data: ctx.getImageData(0, 0, size, size).data
            });
        }
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

    function updateAircraftOnMap(detailed, dots, totalCount, trails) {
        if (!map || !mapInitialized) return;

        var zoom = map.getZoom();
        if (zoom < 9) {
            dots = detailed.concat(dots);
            detailed = [];
        }

        // Build GeoJSON for detailed aircraft
        var detailedFeatures = detailed.map(function (ac) {
            var iconInfo = window.OverflightIcons.getAircraftIcon({
                model: null,
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

        var trailFeatures = (trails || []).map(function (tr) {
            return {
                type: "Feature",
                geometry: {
                    type: "LineString",
                    coordinates: tr.coordinates || []
                },
                properties: {
                    id: tr.id,
                    icao24: tr.icao24
                }
            };
        }).filter(function (f) {
            return f.geometry.coordinates.length >= 2;
        });

        map.getSource("active-trails").setData({
            type: "FeatureCollection",
            features: trailFeatures
        });

        // Update counter
        if (totalCount > 0) {
            aircraftCounter.style.display = "block";
            var msg = totalCount + " aircraft";
            if (dots.length > 0) {
                msg = "Showing " + detailed.length + " of " + totalCount + " aircraft. Zoom in or increase altitude filter to see details.";
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
            startPlayback();
        };

        if (settingsToggle) {
            settingsToggle.onclick = function () {
                playbackControls.classList.toggle("show-settings");
            };
        }

        [playbackControls, scrubber, speedSelect, altitudeSelect].forEach(function (el) {
            if (!el) return;
            el.addEventListener("wheel", function (evt) {
                evt.stopPropagation();
            }, { passive: true });
            el.addEventListener("touchstart", function (evt) {
                evt.stopPropagation();
            }, { passive: true });
            el.addEventListener("touchmove", function (evt) {
                evt.stopPropagation();
            }, { passive: true });
        });
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
        if (mapWarning) mapWarning.style.display = "none";

        window.OverflightPlayback.init(searchLat, searchLon, searchRadius, {
            onAircraftUpdate: function (detailed, dots, totalCount, trails) {
                updateAircraftOnMap(detailed, dots, totalCount, trails);
            },
            onPlaybackTimeChange: function (t, progress) {
                updateTimeDisplay(t, progress);
            },
            onLoadingProgress: function (status) {
                if (typeof status === "string") {
                    mapLoadingText.textContent = status;
                    return;
                }

                if (status && status.message) {
                    if (status.percent != null) {
                        mapLoadingText.textContent = status.message + " " + status.percent + "%";
                    } else {
                        mapLoadingText.textContent = status.message;
                    }
                }

                if (status && status.warning) {
                    showMapWarning(status.message || "Playback data is temporarily unavailable.");
                }
            },
            onReady: function (state) {
                if (state && (state.empty || state.error)) {
                    playbackControls.style.display = "none";
                    mapLoading.style.display = "flex";
                    if (state.empty && state.collecting) {
                        mapLoadingText.textContent = "Collecting flight data - check back in a few minutes.";
                    } else if (state.empty) {
                        mapLoadingText.textContent = "No aircraft data available for this area yet.";
                    } else {
                        mapLoadingText.textContent = "Unable to load playback data. Please try again.";
                    }
                    return;
                }

                applySuggestedAltitude(state);
                mapLoading.style.display = "none";
                playbackControls.style.display = "flex";

                // Start playback automatically so aircraft motion is visible
                // without requiring an extra click after switching to map view.
                window.OverflightPlayback.play();
                playPauseBtn.innerHTML = "&#9646;&#9646;";
            }
        });
    }

    function applySuggestedAltitude(state) {
        if (!state || !state.suggestedMinAlt || !altitudeSelect) return;

        var suggested = String(state.suggestedMinAlt);
        for (var i = 0; i < altitudeSelect.options.length; i++) {
            var opt = altitudeSelect.options[i];
            opt.text = opt.text.replace(" (suggested)", "");
            if (opt.value === suggested) {
                opt.text += " (suggested)";
            }
        }
    }

    function showMapWarning(message) {
        if (!mapWarning) return;
        mapWarning.textContent = message;
        mapWarning.style.display = "block";
        window.setTimeout(function () {
            if (mapWarning) mapWarning.style.display = "none";
        }, 6000);
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
        detailSidebar.classList.add("is-open");
        detailSidebar.classList.add("is-peek");
        initDetailSidebarDrag();

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
        var coords = window.OverflightPlayback.getTrackPolylineByIcao(icao24);

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
        if (detailSidebar) {
            detailSidebar.classList.remove("is-open");
            detailSidebar.classList.remove("is-peek");
            detailSidebar.classList.remove("is-full");
            detailSidebar.style.height = "";
        }
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

    function isMobileViewport() {
        return window.matchMedia("(max-width: 599px)").matches;
    }

    function initDetailSidebarDrag() {
        if (!detailSidebar || !detailDragHandle || !isMobileViewport()) return;
        if (detailSidebar._dragBound) return;
        detailSidebar._dragBound = true;

        var dragState = {
            dragging: false,
            startY: 0,
            startHeight: 0
        };

        function getHeightFromClass() {
            return detailSidebar.classList.contains("is-full")
                ? Math.round(window.innerHeight * 0.85)
                : Math.round(window.innerHeight * 0.35);
        }

        function clampHeight(h) {
            var minHeight = Math.round(window.innerHeight * 0.25);
            var maxHeight = Math.round(window.innerHeight * 0.85);
            return Math.max(minHeight, Math.min(maxHeight, h));
        }

        function pointerMove(evt) {
            if (!dragState.dragging) return;
            var delta = dragState.startY - evt.clientY;
            var nextHeight = clampHeight(dragState.startHeight + delta);
            detailSidebar.style.height = nextHeight + "px";
        }

        function pointerUp() {
            if (!dragState.dragging) return;
            dragState.dragging = false;

            var currentHeight = parseFloat(detailSidebar.style.height || "0") || getHeightFromClass();
            var threshold = Math.round(window.innerHeight * 0.6);
            if (currentHeight >= threshold) {
                detailSidebar.classList.remove("is-peek");
                detailSidebar.classList.add("is-full");
                detailSidebar.style.height = Math.round(window.innerHeight * 0.85) + "px";
            } else {
                detailSidebar.classList.remove("is-full");
                detailSidebar.classList.add("is-peek");
                detailSidebar.style.height = Math.round(window.innerHeight * 0.35) + "px";
            }
        }

        detailDragHandle.addEventListener("pointerdown", function (evt) {
            dragState.dragging = true;
            dragState.startY = evt.clientY;
            dragState.startHeight = parseFloat(detailSidebar.style.height || "0") || getHeightFromClass();
        });

        window.addEventListener("pointermove", pointerMove);
        window.addEventListener("pointerup", pointerUp);
    }

    // Expose globally for app.js integration
    window.OverflightMap = {
        onSearchComplete: onSearchComplete,
        showMapView: showMapView,
        showCardView: showCardView
    };
})();
