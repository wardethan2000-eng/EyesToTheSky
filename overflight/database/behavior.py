"""Behavior heuristics for determining whether an aircraft actually flew."""

from overflight.config import (
    MIN_FLIGHT_ALTITUDE_METERS,
    MIN_FLIGHT_SPEED_MPS,
    MIN_FLIGHT_VERTICAL_RATE_MPS,
)


def state_vector_indicates_flight(row):
    """Return True when a state vector shows credible airborne activity."""
    on_ground = bool(row.get("on_ground"))
    altitude = row.get("altitude")
    velocity = row.get("velocity")
    vertical_rate = row.get("vertical_rate")

    if not on_ground:
        if altitude is not None and altitude >= MIN_FLIGHT_ALTITUDE_METERS:
            return True
        if velocity is not None and velocity >= MIN_FLIGHT_SPEED_MPS:
            return True
        if vertical_rate is not None and abs(vertical_rate) >= MIN_FLIGHT_VERTICAL_RATE_MPS:
            return True

    if altitude is not None and altitude >= MIN_FLIGHT_ALTITUDE_METERS:
        return True
    if vertical_rate is not None and abs(vertical_rate) >= MIN_FLIGHT_VERTICAL_RATE_MPS:
        return True

    return False


def segment_indicates_flight(rows):
    """Return True when any point in a segment shows flight activity."""
    return any(state_vector_indicates_flight(row) for row in rows)