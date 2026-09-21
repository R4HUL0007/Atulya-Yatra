"""Geographic helpers.

Every destination and hidden gem carries latitude/longitude, so distances are
always computed rather than hardcoded. This module is deliberately dependency
free (pure standard library) so it is trivial to test and reuse.
"""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Optional

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points in kilometres."""
    lat1_r, lng1_r, lat2_r, lng2_r = map(radians, (lat1, lng1, lat2, lng2))
    dlat = lat2_r - lat1_r
    dlng = lng2_r - lng1_r
    a = sin(dlat / 2) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def has_coords(obj) -> bool:
    """True when an object/dict exposes usable coordinates."""
    lat = _get(obj, "lat")
    lng = _get(obj, "lng")
    return isinstance(lat, (int, float)) and isinstance(lng, (int, float))


def distance_between(origin, target) -> Optional[float]:
    """Distance in km between two coordinate-bearing objects, or None."""
    if not (has_coords(origin) and has_coords(target)):
        return None
    return haversine_km(
        _get(origin, "lat"), _get(origin, "lng"),
        _get(target, "lat"), _get(target, "lng"),
    )


def _get(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
