"""Reusable Google Maps directions links.

A single, dependency-free helper builds a Google Maps *Directions* URL for any
place across the app (destinations, nearby attractions, hidden gems, trip
itinerary stops, search results, state pages and chatbot cards). It never needs
the Places/Maps API key — the public ``/maps/dir/`` endpoint just opens the
Google Maps UI in the browser.

Rules honoured here:
- Prefer precise latitude/longitude when available.
- Fall back to the canonical place name plus its region/address.
- Never fabricate coordinates; if none are trusted we only pass text.
- URLs are always generated, never hardcoded per place.
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlencode

_MAPS_DIR_BASE = "https://www.google.com/maps/dir/?api=1"


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def maps_directions_url(
    *,
    name: str = "",
    lat: Any = None,
    lng: Any = None,
    address: str = "",
    region: str = "",
    place_id: str = "",
) -> str:
    """Build a Google Maps directions URL to a single destination.

    Coordinates are preferred for accuracy. When they are missing we compose a
    human-readable destination string from the trusted name, region and address
    and let Google Maps geocode it.
    """
    params: dict[str, str] = {}
    lat_f, lng_f = _num(lat), _num(lng)
    if lat_f is not None and lng_f is not None and -90 <= lat_f <= 90 and -180 <= lng_f <= 180:
        params["destination"] = f"{lat_f:.6f},{lng_f:.6f}"
    else:
        parts: list[str] = []
        for part in (name, region, address):
            text = " ".join(str(part or "").split())
            if text and text not in parts:
                parts.append(text)
        destination = ", ".join(parts) or "India"
        if "india" not in destination.lower():
            destination = f"{destination}, India"
        params["destination"] = destination

    # A Google Place ID sharpens geocoding when we only have a text fallback.
    clean_place_id = str(place_id or "").strip()
    if "destination" in params and lat_f is None and clean_place_id:
        params["destination_place_id"] = clean_place_id

    return f"{_MAPS_DIR_BASE}&{urlencode(params)}"


def directions_url_for(entity: Any) -> str:
    """Build a directions URL from any place-like dict.

    Accepts the several shapes used across the app (curated places, Google
    Places candidates, community submissions, trip stops) and reads whatever
    trusted fields are present.
    """
    if not isinstance(entity, dict):
        return maps_directions_url()
    return maps_directions_url(
        name=entity.get("name") or entity.get("displayName") or "",
        lat=entity.get("lat"),
        lng=entity.get("lng"),
        address=entity.get("address") or entity.get("formatted_address") or "",
        region=entity.get("state") or entity.get("region") or "",
        place_id=entity.get("google_place_id") or entity.get("place_id") or "",
    )
