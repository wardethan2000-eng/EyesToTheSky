/**
 * OverFlight — Map View Controller
 *
 * Handles:
 * - MapLibre GL JS map initialization
 * - Startup location overview and map recentering
 * - Aircraft marker rendering using GeoJSON source + symbol layers
 * - Search radius circle overlay
 * - Detail sidebar for clicked aircraft
 * - Playback control wiring
 */

(function () {
    "use strict";

    var map = null;
    var mapInitialized = false;
    var mapLoaded = false;
    var selectedAircraft = null;
    var iconImagesLoaded = {};
    var playbackBound = false;
    var aircraftFirstSeen = {};
    var FADE_IN_MS = 1200;

    // SVG icons for the play/pause button — render crisply at any DPI.
    var ICON_PLAY  = '<svg width="12" height="14" viewBox="0 0 12 14" fill="currentColor"><path d="M0 0L12 7L0 14Z"/></svg>';
    var ICON_PAUSE = '<svg width="12" height="14" viewBox="0 0 12 14" fill="currentColor"><rect x="0" y="0" width="4" height="14" rx="1.5"/><rect x="8" y="0" width="4" height="14" rx="1.5"/></svg>';

    // DOM references (resolved on init)
    var mapSection, mapEl, mapLoading, mapLoadingText;
    var mapWarning;
    var aircraftCounter, aircraftCounterText;
    var playbackControls, playPauseBtn, scrubber, timeLabel;
    var speedSelect, speedNote, altitudeSelect, settingsToggle;
    var detailSidebar, detailClose, detailContent, detailDragHandle;
    var flightList;
    var dayToggle, dayBtnYesterday, dayBtnToday;
    var pendingFocusFlight = null;
    var lastSpeedWindowSignature = "";
    var playbackStartupTimer = null;
    var lastPlaybackStatusText = "";

    // Current search params
    var searchLat = null;
    var searchLon = null;
    var searchRadius = null;
    var searchDateLabel = ""; // Displayed in the map chip
    var selectedWindowKey = "yesterday";
    var selectedWindowLabel = "Yesterday";
    var selectedWindowStart = null;
    var selectedWindowEnd = null;
    var selectedDay = (window.OVERFLIGHT.yesterdayIso || window.OVERFLIGHT.playbackDay || "").trim();

    var DEFAULT_US_VIEW = {
        lat: 39.8283,
        lon: -98.5795,
        zoom: 3.5
    };

    // Marker sprite IDs used by the symbol layer.
    var AIRCRAFT_ICON_ID = "aircraft-photo";
    var AIRCRAFT_ICON_SELECTED_ID = "aircraft-photo-selected";

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
        speedNote = document.getElementById("playback-speed-note");
        altitudeSelect = document.getElementById("altitude-filter");
        settingsToggle = document.getElementById("playback-settings-toggle");
        detailSidebar = document.getElementById("detail-sidebar");
        detailClose = document.getElementById("detail-close");
        detailContent = document.getElementById("detail-content");
        detailDragHandle = document.getElementById("detail-drag-handle");
        flightList = document.getElementById("flight-list");
        dayToggle = document.getElementById("day-toggle");
        dayBtnYesterday = document.getElementById("day-btn-yesterday");
        dayBtnToday = document.getElementById("day-btn-today");

        // Day toggle click handlers.
        if (dayBtnYesterday) {
            dayBtnYesterday.onclick = function () {
                if (window.OverflightApp && typeof window.OverflightApp.setTimeWindow === "function") {
                    window.OverflightApp.setTimeWindow("yesterday");
                }
            };
        }
        if (dayBtnToday) {
            dayBtnToday.onclick = function () {
                if (window.OverflightApp && typeof window.OverflightApp.setTimeWindow === "function") {
                    window.OverflightApp.setTimeWindow("today");
                }
            };
        }

        // Derive initial date label from selectedDay.
        syncTimeWindowPresentation();
    }

    function setDayToggleActive(which) {
        if (!dayBtnYesterday || !dayBtnToday) return;
        dayBtnYesterday.classList.toggle("day-btn-active", which === "yesterday");
        dayBtnToday.classList.toggle("day-btn-active", which === "today");
    }

    function syncTimeWindowPresentation() {
        if (selectedWindowKey === "today") {
            selectedDay = window.OVERFLIGHT.todayIso || "";
            setDayToggleActive("today");
        } else if (selectedWindowKey === "yesterday") {
            selectedDay = window.OVERFLIGHT.yesterdayIso || "";
            setDayToggleActive("yesterday");
        } else {
            selectedDay = "";
            if (dayBtnYesterday) dayBtnYesterday.classList.remove("day-btn-active");
            if (dayBtnToday) dayBtnToday.classList.remove("day-btn-active");
        }

        searchDateLabel = selectedWindowLabel || "Area overview";
    }

    function setTimeWindowFromQuery(query) {
        if (!query) return;
        var nextKey = query.window || selectedWindowKey;
        var nextStart = (query.start != null) ? query.start : selectedWindowStart;
        var nextEnd = (query.end != null) ? query.end : selectedWindowEnd;
        var nextSignature = [nextKey, nextStart, nextEnd].join(":");

        selectedWindowKey = query.window || selectedWindowKey;
        selectedWindowLabel = query.window_label || selectedWindowLabel;
        selectedWindowStart = (query.start != null) ? query.start : selectedWindowStart;
        selectedWindowEnd = (query.end != null) ? query.end : selectedWindowEnd;
        syncTimeWindowPresentation();

        if (nextSignature !== lastSpeedWindowSignature) {
            applyRecommendedSpeedForWindow();
            lastSpeedWindowSignature = nextSignature;
        }
    }

    function getRecommendedSpeedForWindow() {
        var duration = 0;
        if (selectedWindowStart != null && selectedWindowEnd != null && selectedWindowEnd > selectedWindowStart) {
            duration = selectedWindowEnd - selectedWindowStart;
        }

        if (selectedWindowKey === "last_hour") return 60;
        if (selectedWindowKey === "today") {
            if (duration <= 6 * 3600) return 60;
            if (duration <= 12 * 3600) return 120;
            return 240;
        }
        if (selectedWindowKey === "yesterday" || selectedWindowKey === "last_24_hours") {
            return 480;
        }
        if (duration <= 3600) return 60;
        if (duration <= 6 * 3600) return 120;
        if (duration <= 12 * 3600) return 240;
        if (duration <= 24 * 3600) return 480;
        return 960;
    }

    function applyRecommendedSpeedForWindow() {
        if (!speedSelect) return;
        var recommended = String(getRecommendedSpeedForWindow());
        var optionExists = false;
        for (var i = 0; i < speedSelect.options.length; i++) {
            if (speedSelect.options[i].value === recommended) {
                optionExists = true;
                break;
            }
        }
        if (!optionExists) return;

        speedSelect.value = recommended;
        if (window.OverflightPlayback && typeof window.OverflightPlayback.setSpeed === "function") {
            window.OverflightPlayback.setSpeed(parseFloat(recommended));
        }
        updateSpeedRecommendationNote();
    }

    function updateSpeedRecommendationNote() {
        if (!speedSelect || !speedNote) return;
        var recommended = String(getRecommendedSpeedForWindow());
        var selected = String(speedSelect.value || "");
        if (selected === recommended) {
            speedNote.textContent = "Recommended";
            speedNote.classList.remove("playback-speed-note-muted");
        } else {
            speedNote.textContent = "Suggested " + recommended + "x";
            speedNote.classList.add("playback-speed-note-muted");
        }
    }

    function initOverview(opts) {
        initDOM();

        var lat = opts && typeof opts.lat === "number" ? opts.lat : null;
        var lon = opts && typeof opts.lon === "number" ? opts.lon : null;
        var radius = opts && typeof opts.radius === "number" ? opts.radius : null;

        if (lat != null && lon != null) {
            searchLat = lat;
            searchLon = lon;
            searchRadius = radius || 25;
            ensureMap(lat, lon, getZoomForRadius(searchRadius));
            onMapReady(function () {
                drawSearchRadius();
            });
        } else {
            ensureMap(DEFAULT_US_VIEW.lat, DEFAULT_US_VIEW.lon, DEFAULT_US_VIEW.zoom);
            onMapReady(function () {
                clearSearchRadius();
            });
        }
    }

    /**
     * Called by app.js after a successful flight search.
     * Stores search params, recenters map, and starts playback.
     */
    function onSearchComplete(lat, lon, radius, query) {
        initDOM();
        searchLat = lat;
        searchLon = lon;
        searchRadius = radius;
        setTimeWindowFromQuery(query);

        ensureMap(lat, lon, getZoomForRadius(radius));
        onMapReady(function () {
            centerMap(lat, lon, getZoomForRadius(radius));
            drawSearchRadius();
            // Show the day toggle once the user has an active search area.
            if (dayToggle) dayToggle.style.display = "flex";
            startPlayback();
        });
    }

    function onMapReady(callback) {
        if (mapLoaded) {
            callback();
            return;
        }

        if (!map) return;
        map.once("load", callback);
    }

    function ensureMap(lat, lon, zoom) {
        if (!mapInitialized) {
            initMap(lat, lon, zoom);
            return;
        }

        map.resize();
    }

    function centerMap(lat, lon, zoom) {
        if (!map || !mapLoaded) return;

        map.easeTo({
            center: [lon, lat],
            zoom: zoom,
            duration: 750,
            essential: true
        });
    }

    function showCardView() {
        if (flightList) flightList.style.display = "";
    }

    function showMapView() {
        if (mapSection) mapSection.style.display = "block";
        if (map) map.resize();
    }

    function initMap(initialLat, initialLon, initialZoom) {
        if (mapInitialized) return;

        mapInitialized = true;

        var centerLon = typeof initialLon === "number" ? initialLon : DEFAULT_US_VIEW.lon;
        var centerLat = typeof initialLat === "number" ? initialLat : DEFAULT_US_VIEW.lat;
        var zoom = typeof initialZoom === "number" ? initialZoom : DEFAULT_US_VIEW.zoom;

        map = new maplibregl.Map({
            container: "map",
            style: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
            center: [centerLon, centerLat],
            zoom: zoom,
            attributionControl: true
        });

        map.addControl(new maplibregl.NavigationControl(), "top-right");

        map.on("load", function () {
            mapLoaded = true;
            setupMapLayers();
            bindPlaybackControlsOnce();
            syncRenderBudgetToZoom();

            map.on("zoomend", syncRenderBudgetToZoom);
            map.on("moveend", function () {
                var center = map.getCenter();
                window.OverflightPlayback.setMapCenter(center.lat, center.lng);
            });
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
                    AIRCRAFT_ICON_SELECTED_ID,
                    AIRCRAFT_ICON_ID
                ],
                "icon-size": [
                    "*",
                    [
                        "interpolate", ["linear"], ["get", "sizeMult"],
                        0.3, 0.62,
                        0.5, 0.74,
                        0.75, 0.88,
                        1.0, 1.0
                    ],
                    ["case", ["==", ["get", "selected"], true], 1.2, 1.0]
                ],
                "icon-rotate": ["get", "heading"],
                "icon-rotation-alignment": "map",
                "icon-allow-overlap": true,
                "icon-ignore-placement": true
            },
            paint: {
                "icon-opacity": ["get", "opacity"]
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
                "text-halo-width": 1,
                "text-opacity": ["get", "opacity"]
            }
        });

        // Register base marker images. First add generated fallbacks, then try
        // loading a higher-fidelity static image sprite from the app assets.
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
        addAircraftMarkerImage(AIRCRAFT_ICON_ID, "#2b8cef", "#0b4a8a", 64);
        addAircraftMarkerImage(AIRCRAFT_ICON_SELECTED_ID, "#ff5733", "#ffffff", 72);
    }

    function loadAircraftSpriteImage() {
        // Allow dropping in a custom transparent PNG without code changes.
        var preferred = "/static/img/aircraft/custom-plane.png";
        var fallback = "/static/img/aircraft/widebody.svg";

        tryLoadMapImage(preferred, function (imageData) {
            upsertMapImage(AIRCRAFT_ICON_ID, imageData);
            upsertMapImage(AIRCRAFT_ICON_SELECTED_ID, imageData);
        }, function () {
            tryLoadMapImage(fallback, function (imageData) {
                upsertMapImage(AIRCRAFT_ICON_ID, imageData);
                upsertMapImage(AIRCRAFT_ICON_SELECTED_ID, imageData);
            });
        });
    }

    function tryLoadMapImage(url, onSuccess, onError) {
        map.loadImage(url, function (err, image) {
            if (err || !image) {
                if (onError) onError();
                return;
            }
            if (onSuccess) onSuccess(image);
        });
    }

    function upsertMapImage(name, imageData) {
        if (map.hasImage(name)) {
            map.updateImage(name, imageData);
        } else {
            map.addImage(name, imageData);
        }
    }

    function addAircraftMarkerImage(name, fillColor, strokeColor, size) {
        size = size || 64;
        var canvas = document.createElement("canvas");
        canvas.width = size;
        canvas.height = size;
        var ctx = canvas.getContext("2d");

        var cx = size / 2;
        var cy = size / 2;
        ctx.translate(cx, cy);

        // Scale factor so the silhouette fills the canvas regardless of size.
        var s = size / 64;

        // Drop shadow for depth
        ctx.shadowColor = "rgba(0, 0, 0, 0.40)";
        ctx.shadowBlur = 4 * s;
        ctx.shadowOffsetX = 0;
        ctx.shadowOffsetY = 1.5 * s;

        // Top-down aircraft silhouette — nose points up (negative y).
        // Shape mirrors the proportions used by the existing SVG assets.
        ctx.beginPath();
        ctx.moveTo(0, -22 * s);           // Nose tip
        ctx.lineTo(2.5 * s, -8 * s);      // Right fuselage shoulder
        ctx.lineTo(18 * s, -2 * s);        // Right main wing leading edge
        ctx.lineTo(17 * s, 2 * s);         // Right wingtip
        ctx.lineTo(3 * s, -1 * s);         // Right wing root trailing
        ctx.lineTo(3 * s, 13 * s);         // Right fuselage, tail section
        ctx.lineTo(11 * s, 17 * s);        // Right horizontal stabiliser leading
        ctx.lineTo(10 * s, 21 * s);        // Right stabiliser tip
        ctx.lineTo(0, 19 * s);             // Tail centreline
        ctx.lineTo(-10 * s, 21 * s);       // Left stabiliser tip
        ctx.lineTo(-11 * s, 17 * s);       // Left horizontal stabiliser leading
        ctx.lineTo(-3 * s, 13 * s);        // Left fuselage, tail section
        ctx.lineTo(-3 * s, -1 * s);        // Left wing root trailing
        ctx.lineTo(-17 * s, 2 * s);        // Left wingtip
        ctx.lineTo(-18 * s, -2 * s);       // Left main wing leading edge
        ctx.lineTo(-2.5 * s, -8 * s);      // Left fuselage shoulder
        ctx.closePath();

        ctx.fillStyle = fillColor;
        ctx.fill();

        ctx.shadowColor = "transparent";
        ctx.lineWidth = 1.8 * s;
        ctx.strokeStyle = strokeColor;
        ctx.stroke();

        // Cockpit window highlight — small oval just behind the nose.
        ctx.beginPath();
        ctx.ellipse(0, -11 * s, 1.8 * s, 2.5 * s, 0, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(255, 255, 255, 0.70)";
        ctx.fill();

        upsertMapImage(name, {
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
        if (!map || !mapLoaded || !map.getSource("search-radius")) return;

        if (searchLat == null || searchLon == null || searchRadius == null) {
            clearSearchRadius();
            return;
        }

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

    function clearSearchRadius() {
        if (!map || !mapLoaded || !map.getSource("search-radius")) return;
        map.getSource("search-radius").setData(emptyFeatureCollection());
    }

    // --- Aircraft Rendering ---

    function updateAircraftOnMap(detailed, dots, totalCount, trails) {
        if (!map || !mapInitialized) return;

        var detailedSource = map.getSource("aircraft-detailed");
        var dotsSource = map.getSource("aircraft-dots");
        var trailsSource = map.getSource("active-trails");
        if (!detailedSource || !dotsSource || !trailsSource) {
            return;
        }

        var zoom = map.getZoom();
        if (zoom < 9) {
            dots = detailed.concat(dots);
            detailed = [];
        }

        // Build GeoJSON for detailed aircraft
        var now = performance.now();
        var detailedFeatures = detailed.map(function (ac) {
            if (!isFinite(ac.lat) || !isFinite(ac.lon)) {
                return null;
            }

            var iconInfo = window.OverflightIcons.getAircraftIcon({
                model: null,
                operator: null
            });

            if (!aircraftFirstSeen[ac.id]) {
                aircraftFirstSeen[ac.id] = now;
            }
            var elapsed = now - aircraftFirstSeen[ac.id];
            var opacity = Math.min(1.0, elapsed / FADE_IN_MS);

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
                    selected: !!(selectedAircraft && selectedAircraft.icao24 === ac.icao24),
                    opacity: opacity
                }
            };
        }).filter(Boolean);

        detailedSource.setData({
            type: "FeatureCollection",
            features: detailedFeatures
        });

        // Build GeoJSON for dots
        var dotFeatures = dots.map(function (ac) {
            if (!isFinite(ac.lat) || !isFinite(ac.lon)) {
                return null;
            }
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
        }).filter(Boolean);

        dotsSource.setData({
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
            if (f.geometry.coordinates.length < 2) {
                return false;
            }
            for (var i = 0; i < f.geometry.coordinates.length; i++) {
                var coord = f.geometry.coordinates[i];
                if (!coord || coord.length < 2 || !isFinite(coord[0]) || !isFinite(coord[1])) {
                    return false;
                }
            }
            return true;
        });

        trailsSource.setData({
            type: "FeatureCollection",
            features: trailFeatures
        });

        // Update counter
        if (totalCount > 0) {
            aircraftCounter.style.display = "block";
            aircraftCounterText.textContent = totalCount + " aircraft";
        } else {
            aircraftCounter.style.display = "none";
        }
    }

    // --- Playback Controls ---

    function bindPlaybackControlsOnce() {
        if (playbackBound) return;
        playbackBound = true;

        playPauseBtn.onclick = function () {
            var playing = window.OverflightPlayback.togglePlayPause();
            playPauseBtn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
        };

        scrubber.oninput = function () {
            var progress = parseInt(scrubber.value, 10) / 1000;
            window.OverflightPlayback.seek(progress);
        };

        speedSelect.onchange = function () {
            window.OverflightPlayback.setSpeed(parseFloat(speedSelect.value));
            updateSpeedRecommendationNote();
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

        updateSpeedRecommendationNote();

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
        if (searchLat == null || searchLon == null || searchRadius == null) return;

        var playbackDay = selectedWindowKey === "yesterday" ? selectedDay : "";
        var playbackStartTime = selectedWindowKey === "yesterday" ? null : selectedWindowStart;
        var playbackEndTime = selectedWindowKey === "yesterday" ? null : selectedWindowEnd;

        mapLoading.style.display = "flex";
        mapLoading.classList.remove("is-empty");
        if (mapWarning) mapWarning.style.display = "none";
        lastPlaybackStatusText = "Starting playback...";

        if (playbackStartupTimer) {
            window.clearTimeout(playbackStartupTimer);
        }
        playbackStartupTimer = window.setTimeout(function () {
            var state = window.OverflightPlayback && typeof window.OverflightPlayback.getState === "function"
                ? window.OverflightPlayback.getState()
                : null;
            mapLoading.classList.add("is-empty");
            mapLoading.style.display = "flex";
            playbackControls.style.display = "none";
            mapLoadingText.textContent = "Playback startup stalled after loading data.";
            showMapWarning(
                "Startup stalled at " +
                ((state && state.startupStage) || "unknown stage") +
                ". Last status: " +
                (lastPlaybackStatusText || "none")
            );
        }, 12000);

        window.OverflightPlayback.init(searchLat, searchLon, searchRadius, {
            day: playbackDay,
            startTime: playbackStartTime,
            endTime: playbackEndTime,
            onAircraftUpdate: function (detailed, dots, totalCount, trails) {
                updateAircraftOnMap(detailed, dots, totalCount, trails);
            },
            onPlaybackTimeChange: function (t, progress) {
                updateTimeDisplay(t, progress);
            },
            onLoadingProgress: function (status) {
                if (typeof status === "string") {
                    lastPlaybackStatusText = status;
                    mapLoadingText.textContent = status;
                    return;
                }

                if (status && status.message) {
                    lastPlaybackStatusText = status.message;
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
            onPlaybackStateChange: function (playing) {
                if (playPauseBtn) {
                    playPauseBtn.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
                }
            },
            onReady: function (state) {
                try {
                    if (playbackStartupTimer) {
                        window.clearTimeout(playbackStartupTimer);
                        playbackStartupTimer = null;
                    }

                    // Reset fade-in tracking for the new playback session
                    aircraftFirstSeen = {};

                    if (state && (state.empty || state.error)) {
                        if (playbackControls) playbackControls.style.display = "none";
                        if (mapLoading) {
                            mapLoading.style.display = "flex";
                            mapLoading.classList.remove("is-empty");
                        }
                        if (state.empty && state.collecting) {
                            if (mapLoadingText) mapLoadingText.textContent = "Collecting flight data - check back in a few minutes.";
                        } else if (state.empty) {
                            if (mapLoading) mapLoading.classList.add("is-empty");
                            if (mapLoadingText) mapLoadingText.textContent = "No aircraft data available for this area yet.";
                        } else {
                            if (mapLoading) mapLoading.classList.add("is-empty");
                            if (mapLoadingText) mapLoadingText.textContent = "Unable to load playback data. Please try again.";
                            if (state.startupStage || state.startupError) {
                                showMapWarning(
                                    "Startup failed at " +
                                    (state.startupStage || "unknown stage") +
                                    ": " +
                                    (state.startupError || "unknown error")
                                );
                            }
                        }
                        return;
                    }

                    if (state && state.staticOnly) {
                        if (mapLoading) {
                            mapLoading.classList.remove("is-empty");
                            mapLoading.style.display = "none";
                        }
                        if (playbackControls) playbackControls.style.display = "none";
                        showMapWarning("Showing latest position snapshots. Animated playback will appear as track history fills in.");
                        maybeApplyPendingFocus();
                        return;
                    }

                    applySuggestedAltitude(state);
                    if (mapLoading) {
                        mapLoading.classList.remove("is-empty");
                        mapLoading.style.display = "none";
                    }
                    if (playbackControls) playbackControls.style.display = "flex";

                    if (state && state.coverageNotice) {
                        showMapWarning(state.coverageNotice);
                    }

                    // Update the map chip to show the date being replayed.
                    var chip = document.getElementById("map-chip");
                    if (chip) {
                        chip.textContent = searchDateLabel
                            ? (searchDateLabel + " replay")
                            : "Area replay";
                    }

                    // Autoplay is best-effort only; if it fails, keep the initial frame visible.
                    try {
                        window.OverflightPlayback.play();
                        if (playPauseBtn) {
                            playPauseBtn.innerHTML = ICON_PAUSE;
                        }
                    } catch (err) {
                        console.error("Playback autoplay failed", err);
                        showMapWarning("Playback loaded, but autoplay failed. Use the play button to start replay.");
                    }

                    maybeApplyPendingFocus();
                } catch (err) {
                    console.error("Map onReady failed", err);
                    if (mapLoading) {
                        mapLoading.classList.add("is-empty");
                        mapLoading.style.display = "flex";
                    }
                    if (mapLoadingText) {
                        mapLoadingText.textContent = "Playback loaded, but the map UI could not finish initializing.";
                    }
                    if (playbackControls) playbackControls.style.display = "none";
                    showMapWarning(err && err.message ? err.message : "Map UI initialization failed.");
                }
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

        if (window.OverflightPlayback && typeof window.OverflightPlayback.pause === "function") {
            window.OverflightPlayback.pause();
        }

        selectedAircraft = { icao24: icao24 };

        // Show track line for this aircraft (find in allTracks)
        drawSelectedTrack(icao24);
        if (window.OverflightPlayback && typeof window.OverflightPlayback.seekToTime === "function") {
            var playbackState = window.OverflightPlayback.getState();
            window.OverflightPlayback.seekToTime(playbackState.playbackTime);
        }

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

        if (window.OverflightApp && typeof window.OverflightApp.focusFlightByIcao === "function") {
            window.OverflightApp.focusFlightByIcao(icao24, { expand: true, source: "map" });
        }
    }

    function focusFlight(flight) {
        if (!flight || !flight.icao24) return;

        if (window.OverflightPlayback && typeof window.OverflightPlayback.pause === "function") {
            window.OverflightPlayback.pause();
        }

        pendingFocusFlight = flight;
        showMapView();

        if (typeof flight.lat === "number" && typeof flight.lon === "number") {
            centerMap(flight.lat, flight.lon, Math.max(getZoomForRadius(searchRadius || 25), 10));
        }

        maybeApplyPendingFocus();
    }

    function maybeApplyPendingFocus() {
        if (!pendingFocusFlight || !window.OverflightPlayback) return;
        var state = window.OverflightPlayback.getState();
        if (!state || !state.plan) return;

        var focus = pendingFocusFlight;
        var targetTime = focus.timestamp != null ? focus.timestamp : state.playbackTime;

        window.OverflightPlayback.seekToTime(targetTime, function (ok) {
            if (!ok) return;

            selectedAircraft = { icao24: focus.icao24 };
            drawSelectedTrack(focus.icao24);
            window.OverflightPlayback.seekToTime(targetTime);

            if (typeof focus.lat === "number" && typeof focus.lon === "number") {
                centerMap(focus.lat, focus.lon, Math.max(getZoomForRadius(searchRadius || 25), 10));
            }

            pendingFocusFlight = null;
        });
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
        initOverview: initOverview,
        onSearchComplete: onSearchComplete,
        showMapView: showMapView,
        showCardView: showCardView,
        focusFlight: focusFlight,
        pausePlayback: function () {
            if (window.OverflightPlayback && typeof window.OverflightPlayback.pause === "function") {
                window.OverflightPlayback.pause();
            }
        }
    };
})();
