"""Thin client for the Google Places API (New).

This is intentionally kept separate from the running Flask app: the web app
never calls Google at request time (that would be slow, costly per-visitor,
and would break offline/local development). Instead, this client is used by
``scripts/fetch_places.py`` to dynamically discover real destinations and
hidden gems and write them into the same JSON files the app already reads via
``DataStore``'s ``destinations*.json`` / ``hidden_gems*.json`` glob loader.

API reference used:
  https://developers.google.com/maps/documentation/places/web-service/nearby-search
  https://developers.google.com/maps/documentation/places/web-service/text-search
  https://developers.google.com/maps/documentation/places/web-service/place-details
  https://developers.google.com/maps/documentation/places/web-service/place-photos

Costs money beyond Google's free monthly credit — every call below is a
regular Places API (New) SKU request. Nothing here is called automatically;
it only runs when you explicitly invoke a script.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

PLACES_BASE = "https://places.googleapis.com/v1/places"

# Table A place types (see place-types docs) used to find genuine tourism POIs.
# Kept broad but curated so we don't pull in petrol stations / ATMs / shops.
# NOTE: "natural_feature" is a Table B (response-only) type and is NOT valid as
# an includedTypes filter value — using it returns an INVALID_ARGUMENT error.
TOURISM_TYPES = [
    "tourist_attraction",
    "historical_landmark",
    "museum",
    "hindu_temple",
    "national_park",
    "monument",
    "art_gallery",
    "church",
    "mosque",
    "state_park",
    "hiking_area",
    "wildlife_park",
    "wildlife_refuge",
    "scenic_spot",
    "cultural_landmark",
    "historical_place",
]

# Deliberately excludes premium/"Enterprise+Atmosphere" SKU fields (e.g.
# editorialSummary, generativeSummary, reviews) to keep each call on the
# cheaper base SKU. Narrative content (culture/food/festivals/safety) is
# authored separately, not fabricated from these fields.
DEFAULT_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.primaryType",
        "places.rating",
        "places.userRatingCount",
        "places.photos",
        "places.googleMapsUri",
    ]
)


class PlacesApiError(RuntimeError):
    """Raised when the Places API returns an error response."""


@dataclass
class PlaceResult:
    """A normalised subset of a Google Places (New) ``Place`` object."""

    place_id: str
    name: str
    lat: float
    lng: float
    address: str = ""
    types: List[str] = field(default_factory=list)
    primary_type: Optional[str] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    summary: str = ""
    photo_names: List[str] = field(default_factory=list)
    maps_url: str = ""

    @classmethod
    def from_api(cls, place: Dict[str, Any]) -> "PlaceResult":
        loc = place.get("location") or {}
        display_name = (place.get("displayName") or {}).get("text", "")
        photos = place.get("photos") or []
        return cls(
            place_id=place.get("id", ""),
            name=display_name,
            lat=loc.get("latitude", 0.0),
            lng=loc.get("longitude", 0.0),
            address=place.get("formattedAddress", ""),
            types=place.get("types", []) or [],
            primary_type=place.get("primaryType"),
            rating=place.get("rating"),
            review_count=place.get("userRatingCount"),
            summary="",
            photo_names=[p.get("name", "") for p in photos if p.get("name")],
            maps_url=place.get("googleMapsUri", ""),
        )


class PlacesClient:
    """Minimal, explicit wrapper around the Places API (New) HTTP endpoints."""

    def __init__(self, api_key: str, *, session: Optional[requests.Session] = None, rate_limit_sec: float = 0.15):
        if not api_key:
            raise ValueError(
                "A Google Places API key is required. Set GOOGLE_MAPS_API_KEY "
                "(or Google_maps) in your .env file."
            )
        self.api_key = api_key
        self.session = session or requests.Session()
        self.rate_limit_sec = rate_limit_sec

    def _post(self, path: str, body: dict, field_mask: str) -> dict:
        url = f"{PLACES_BASE}:{path}"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": field_mask,
        }
        resp = self.session.post(url, json=body, headers=headers, timeout=20)
        time.sleep(self.rate_limit_sec)  # gentle self-throttling
        if resp.status_code != 200:
            raise PlacesApiError(f"Places API {path} failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def search_nearby(
        self,
        lat: float,
        lng: float,
        radius_m: float,
        *,
        included_types: Optional[List[str]] = None,
        max_results: int = 20,
        rank_by_distance: bool = False,
        field_mask: str = DEFAULT_FIELD_MASK,
    ) -> List[PlaceResult]:
        """Places API (New) Nearby Search — real POIs within a circle."""
        body: Dict[str, Any] = {
            "maxResultCount": max(1, min(max_results, 20)),
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": max(1.0, min(radius_m, 50000.0)),
                }
            },
        }
        if included_types:
            body["includedTypes"] = included_types
        if rank_by_distance:
            body["rankPreference"] = "DISTANCE"
        data = self._post("searchNearby", body, field_mask)
        return [PlaceResult.from_api(p) for p in data.get("places", [])]

    def search_text(
        self,
        query: str,
        *,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        radius_m: Optional[float] = None,
        max_results: int = 10,
        field_mask: str = DEFAULT_FIELD_MASK,
    ) -> List[PlaceResult]:
        """Places API (New) Text Search — find a specific named place."""
        body: Dict[str, Any] = {"textQuery": query, "pageSize": max(1, min(max_results, 20))}
        if lat is not None and lng is not None and radius_m is not None:
            body["locationBias"] = {
                "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_m}
            }
        data = self._post("searchText", body, field_mask)
        return [PlaceResult.from_api(p) for p in data.get("places", [])]

    @staticmethod
    def photo_resource(photo_name: str) -> str:
        """Return a safe Places photo resource name for deferred handling.

        Resource names contain no credential. Persisting a direct media URL
        with ``?key=...`` would leak the server key into JSON and browser HTML,
        so ingestion stores these names separately and leaves display images to
        the application's local placeholder/media policy.
        """
        return photo_name if str(photo_name).startswith("places/") else ""
