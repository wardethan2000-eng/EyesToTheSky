"""
OverFlight configuration.

All settings can be overridden via environment variables.
"""

import os

# --- Paths ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("OVERFLIGHT_DB", os.path.join(DATA_DIR, "overflight.db"))
ENRICHMENT_DB_PATH = os.environ.get(
    "OVERFLIGHT_ENRICHMENT_DB", os.path.join(DATA_DIR, "enrichment.db")
)
ZIPCODE_DB_PATH = os.environ.get(
    "OVERFLIGHT_ZIPCODE_DB", os.path.join(DATA_DIR, "zipcodes.db")
)

# --- OpenSky API ---
OPENSKY_BASE_URL = "https://opensky-network.org/api"
OPENSKY_USERNAME = os.environ.get("OPENSKY_USERNAME", "")
OPENSKY_PASSWORD = os.environ.get("OPENSKY_PASSWORD", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("OVERFLIGHT_POLL_INTERVAL", "10"))

# --- Data Retention ---
RETENTION_HOURS = int(os.environ.get("OVERFLIGHT_RETENTION_HOURS", "24"))
CLEANUP_INTERVAL_MINUTES = int(os.environ.get("OVERFLIGHT_CLEANUP_INTERVAL", "60"))

# --- Query Defaults ---
DEFAULT_RADIUS_MILES = float(os.environ.get("OVERFLIGHT_DEFAULT_RADIUS", "10"))
MAX_RADIUS_MILES = float(os.environ.get("OVERFLIGHT_MAX_RADIUS", "50"))

# --- Ingestion Bounding Box (optional CONUS filter) ---
# Set to None to ingest global data; set coordinates to filter
INGEST_BBOX = None  # (min_lat, max_lat, min_lon, max_lon)
_bbox_env = os.environ.get("OVERFLIGHT_INGEST_BBOX", "")
if _bbox_env:
    parts = [float(x.strip()) for x in _bbox_env.split(",")]
    if len(parts) == 4:
        INGEST_BBOX = tuple(parts)

# Preset: Continental US bounding box
CONUS_BBOX = (24.396308, 49.384358, -125.0, -66.93457)

# --- Web App ---
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-in-production")
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"
