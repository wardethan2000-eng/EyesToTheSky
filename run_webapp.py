#!/usr/bin/env python3
"""
Entry point for the OverFlight web application.

Usage:
    python run_webapp.py                    # Development server (debug mode)
    python run_webapp.py --port 8080        # Custom port
    gunicorn "overflight.webapp:create_app()"  # Production (gunicorn)
"""

import argparse
import logging

from overflight.webapp import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(description="OverFlight web server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    args = parser.parse_args()

    app = create_app()
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
