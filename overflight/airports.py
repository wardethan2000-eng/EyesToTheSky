"""Airport dataset loading and proximity queries for map overlays."""

import json
import math
import os
from functools import lru_cache

from overflight.config import DATA_DIR
from overflight.database.queries import haversine_distance

AIRPORTS_DATA_PATH = os.environ.get(
    "OVERFLIGHT_AIRPORTS_DATA",
    os.path.join(DATA_DIR, "airports_us_major.json"),
)

_TYPE_PRIORITY = {
    "large_airport": 3,
    "medium_airport": 2,
    "small_airport": 1,
}


def _bearing_seed(airport):
    """Return a deterministic pseudo-bearing for schematic runway generation."""
    seed_str = airport.get("icao") or airport.get("iata") or airport.get("name") or "APT"
    seed = sum(ord(ch) for ch in seed_str)
    return float(seed % 180)


def _move_point(lat, lon, bearing_deg, distance_ft):
    """Move a lat/lon point by a short distance along a bearing."""
    distance_miles = distance_ft / 5280.0
    lat_delta = (distance_miles / 69.0) * math.cos(math.radians(bearing_deg))
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-6:
        cos_lat = 1e-6 if cos_lat >= 0 else -1e-6
    lon_delta = (distance_miles / (69.0 * cos_lat)) * math.sin(math.radians(bearing_deg))
    return [round(lon + lon_delta, 6), round(lat + lat_delta, 6)]


def _runway_designator(bearing_deg):
    """Convert a bearing into a runway designator like 09 or 27."""
    designator = int(round((bearing_deg % 360) / 10.0))
    if designator == 0:
        designator = 36
    return f"{designator:02d}"


def _runway_ident_for_bearing(bearing_deg):
    reciprocal = (bearing_deg + 180.0) % 360.0
    return f"{_runway_designator(bearing_deg)}/{_runway_designator(reciprocal)}"


def _schematic_runway(lat, lon, length_ft, bearing_deg, surface, suffix=""):
    """Build a schematic runway centerline around an airport centroid."""
    half_length = max(2000.0, float(length_ft or 6000) / 2.0)
    start = _move_point(lat, lon, bearing_deg + 180.0, half_length)
    end = _move_point(lat, lon, bearing_deg, half_length)
    ident = _runway_ident_for_bearing(bearing_deg)
    if suffix:
        left, right = ident.split("/")
        ident = f"{left}{suffix}/{right}{suffix}"
    return {
        "ident": ident,
        "surface": surface,
        "length_ft": int(length_ft or 0),
        "centerline": [start, end],
        "schematic": True,
    }


def _generate_schematic_runways(airport):
    """Create approximate runway geometry for airports lacking explicit data."""
    lat = float(airport["latitude"])
    lon = float(airport["longitude"])
    longest = int(airport.get("longest_runway_ft") or 6000)
    airport_type = airport.get("type") or "medium_airport"
    base_bearing = _bearing_seed(airport)
    base_surface = "concrete" if airport_type == "large_airport" else "asphalt"

    if airport_type == "large_airport":
        return [
            _schematic_runway(lat, lon, longest, base_bearing, base_surface, "L"),
            _schematic_runway(lat, lon, max(5000, longest - 1500), (base_bearing + 12.0) % 360.0, base_surface, "R"),
            _schematic_runway(lat, lon, max(4500, int(longest * 0.72)), (base_bearing + 90.0) % 360.0, "asphalt"),
        ]

    if airport_type == "medium_airport":
        return [
            _schematic_runway(lat, lon, longest, base_bearing, base_surface),
            _schematic_runway(lat, lon, max(3500, int(longest * 0.68)), (base_bearing + 60.0) % 360.0, "asphalt"),
        ]

    return [
        _schematic_runway(lat, lon, longest, base_bearing, base_surface),
    ]


@lru_cache(maxsize=4)
def load_airports(data_path=AIRPORTS_DATA_PATH):
    """Load the bundled airport catalog from disk."""
    with open(data_path, "r", encoding="utf-8") as fh:
        airports = json.load(fh)

    normalized = []
    for airport in airports:
        explicit_runways = airport.get("runways") or []
        normalized.append({
            "icao": (airport.get("icao") or "").upper(),
            "iata": (airport.get("iata") or "").upper(),
            "name": airport.get("name") or "Unknown airport",
            "type": airport.get("type") or "medium_airport",
            "latitude": float(airport["latitude"]),
            "longitude": float(airport["longitude"]),
            "municipality": airport.get("municipality") or "",
            "state": airport.get("state") or "",
            "elevation_ft": airport.get("elevation_ft"),
            "longest_runway_ft": int(airport.get("longest_runway_ft") or 0),
            "runways": explicit_runways,
        })

    for airport in normalized:
        if airport["runways"]:
            continue
        airport["runways"] = _generate_schematic_runways(airport)

    return normalized


def _importance_for_airport(airport):
    """Return a coarse display importance bucket for styling and ranking."""
    type_priority = _TYPE_PRIORITY.get(airport.get("type"), 1)
    runway_bonus = min(int((airport.get("longest_runway_ft") or 0) / 2000), 6)
    return max(1, min(5, type_priority + runway_bonus - 1))


def _display_code(airport):
    return airport.get("iata") or airport.get("icao") or airport.get("name")


def find_airports_near(lat, lon, radius_miles, limit=40, data_path=AIRPORTS_DATA_PATH):
    """Return airports within radius of a point, sorted by proximity and importance."""
    matches = []
    for airport in load_airports(data_path):
        distance_miles = haversine_distance(lat, lon, airport["latitude"], airport["longitude"])
        if distance_miles > radius_miles:
            continue

        record = dict(airport)
        record["distance_miles"] = round(distance_miles, 1)
        record["display_code"] = _display_code(airport)
        record["importance"] = _importance_for_airport(airport)
        matches.append(record)

    matches.sort(key=lambda airport: (
        airport["distance_miles"],
        -airport["importance"],
        airport["name"],
    ))
    return matches[:limit]