"""
OverFlight Flask web application.

Serves the user-facing interface for looking up aircraft that flew
over a given location in the past 24 hours.
"""

import os
import sqlite3

from flask import Flask

from overflight.config import (
    DB_PATH,
    ENRICHMENT_DB_PATH,
    FLASK_DEBUG,
    FLASK_SECRET_KEY,
    ZIPCODE_DB_PATH,
)


def get_flight_db():
    """Open a read-only connection to the flight positions database."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_enrichment_db():
    """Open a read-only connection to the aircraft enrichment database."""
    conn = sqlite3.connect(f"file:{ENRICHMENT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_zipcode_db():
    """Open a read-only connection to the zip code database."""
    conn = sqlite3.connect(f"file:{ZIPCODE_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def create_app():
    """Flask application factory."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )
    app.secret_key = FLASK_SECRET_KEY
    app.debug = FLASK_DEBUG

    from overflight.webapp.routes import bp as main_bp

    app.register_blueprint(main_bp)

    return app
