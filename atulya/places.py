"""Google Places resolution, nearby discovery, and secure photo lookup.

API keys stay in server-side headers. Browser responses receive only validated
photo resource names, which are converted to signed internal media URLs by the
API layer.
"""
from __future__ import annotations

import math
import re
from typing import Optional
from urllib.parse import urlparse

import requests

_PHOTO_NAME_RE = re.compile(r"^places/[^/]+/photos/[^/]+$")

# Valid Places API (New) Table A types chosen for travel discovery.
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


class GooglePlacesResolver:
    SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
    NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
    FIELD_MASK = ",".join(
        (
            "places.id",
            "places.displayName",
            "places.formattedAddress",
            "places.addressComponents",
            "places.location",
            "places.primaryType",
            "places.types",
            "places.rating",
            "places.userRatingCount",
            "places.googleMapsUri",
            "places.photos",
            "places.photos.widthPx",
            "places.photos.heightPx",
        )
    )

    def __init__(self, api_key: str, *, session: Optional[requests.Session] = None):
        self.api_key = (api_key or "").strip()
        self.session = session or requests.Session()
        self._cache: dict[str, Optional[dict]] = {}
        self._nearby_cache: dict[tuple, list[dict]] = {}
        self._photo_cache: dict[str, Optional[str]] = {}
        self._text_cache: dict[tuple, list[dict]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": self.FIELD_MASK,
        }

    def resolve(self, query: str) -> Optional[dict]:
        key = " ".join(str(query or "").lower().split())
        if not self.api_key or not key or len(key) > 160:
            return None
        if key in self._cache:
            return self._cache[key]
        try:
            response = self.session.post(
                self.SEARCH_URL,
                json={"textQuery": f"{query}, India", "pageSize": 1, "regionCode": "IN"},
                headers=self._headers(),
                timeout=10,
            )
            response.raise_for_status()
            places = response.json().get("places") or []
            result = self._normalise(places[0]) if places else None
            if result and not self._is_india(places[0]):
                result = None
        except (requests.RequestException, ValueError, TypeError, KeyError):
            result = None
        self._cache[key] = result
        return result

    def search_places(self, query: str, *, limit: int = 20) -> list[dict]:
        """Text search returning many real Indian places, not just the best one.

        `resolve()` deliberately asks for a single match because it answers
        "which place is this?". Discovery pages ask a different question: "what
        is worth seeing here?". The curated catalogue only carries a handful of
        records per state, so state pages need Places to supply the breadth.

        Results are normalised exactly like `resolve()` output and cached per
        query, so paging and repeat visits cost no extra API calls.
        """
        key = " ".join(str(query or "").lower().split())
        if not self.api_key or not key or len(key) > 200:
            return []
        # Places API (New) text search caps a page at 20 results.
        limit = max(1, min(int(limit), 20))
        cache_key = (key, limit)
        if cache_key in self._text_cache:
            return [dict(item) for item in self._text_cache[cache_key]]

        results: list[dict] = []
        try:
            response = self.session.post(
                self.SEARCH_URL,
                json={"textQuery": query, "pageSize": limit, "regionCode": "IN"},
                headers=self._headers(),
                timeout=12,
            )
            response.raise_for_status()
            for raw in response.json().get("places") or []:
                if not self._is_india(raw):
                    continue
                item = self._normalise(raw)
                if item:
                    item["kind"] = "external_location"
                    results.append(item)
        except (requests.RequestException, ValueError, TypeError, KeyError):
            results = []

        self._text_cache[cache_key] = results
        return [dict(item) for item in results]

    def search_nearby(
        self,
        origin: dict,
        *,
        radius_km: float = 40.0,
        max_results: int = 20,
    ) -> list[dict]:
        """Return photo-rich tourism POIs around a trusted resolved location."""
        if not self.api_key:
            return []
        try:
            lat = float(origin.get("lat"))
            lng = float(origin.get("lng"))
        except (TypeError, ValueError):
            return []
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return []

        radius = max(1.0, min(float(radius_km), 50.0))
        limit = max(1, min(int(max_results), 20))
        cache_key = (round(lat, 4), round(lng, 4), round(radius, 1), limit)
        if cache_key in self._nearby_cache:
            return [dict(item) for item in self._nearby_cache[cache_key]]

        body = {
            "includedTypes": TOURISM_TYPES,
            "maxResultCount": limit,
            "rankPreference": "POPULARITY",
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius * 1000,
                }
            },
        }
        try:
            response = self.session.post(
                self.NEARBY_URL,
                json=body,
                headers=self._headers(),
                timeout=12,
            )
            response.raise_for_status()
            raw_places = response.json().get("places") or []
            results = []
            for raw in raw_places:
                item = self._normalise(raw, origin=origin)
                if item:
                    item["kind"] = "external_attraction"
                    item["candidate_type"] = "Nearby attraction"
                    item["short_intro"] = self._nearby_intro(item, origin)
                    # Google results are candidates, never curated hidden gems.
                    item["is_hidden_gem"] = False
                    results.append(item)
        except (requests.RequestException, ValueError, TypeError, KeyError):
            results = []

        self._nearby_cache[cache_key] = results
        return [dict(item) for item in results]

    @staticmethod
    def grouped_nearby(places: list[dict]) -> dict[str, list[dict]]:
        """Split one Nearby Search into non-overlapping transparent groups."""
        remaining = [dict(place) for place in places]

        def popularity(place: dict) -> float:
            rating = float(place.get("rating") or 0)
            reviews = max(0, int(place.get("reviews") or 0))
            return rating * 20 + math.log1p(reviews) * 10

        top = sorted(remaining, key=popularity, reverse=True)[:6]
        used = {item.get("google_place_id") for item in top}
        remaining = [item for item in remaining if item.get("google_place_id") not in used]
        for item in top:
            item["candidate_type"] = "Top attraction"

        offbeat_pool = [
            item for item in remaining
            if float(item.get("rating") or 0) >= 4.0
            and 5 <= int(item.get("reviews") or 0) <= 800
        ]
        offbeat = sorted(
            offbeat_pool,
            key=lambda item: (
                float(item.get("rating") or 0),
                -int(item.get("reviews") or 0),
                -float(item.get("distance_km") or 0),
            ),
            reverse=True,
        )[:4]
        used.update(item.get("google_place_id") for item in offbeat)
        for item in offbeat:
            item["candidate_type"] = "Offbeat candidate"

        nearby = sorted(
            [item for item in remaining if item.get("google_place_id") not in used],
            key=lambda item: float(item.get("distance_km") or 9999),
        )[:6]
        for item in nearby:
            item["candidate_type"] = "Nearby attraction"

        return {"top": top, "nearby": nearby, "offbeat": offbeat}

    def photo_name_for(self, place: dict, *, index: int = 0) -> Optional[str]:
        """Get a validated photo resource for a trusted entity record."""
        direct = [
            name for name in (place.get("google_photo_names") or [])
            if self._valid_photo_name(name)
        ]
        if direct:
            return direct[max(0, int(index)) % len(direct)]

        query = ", ".join(
            part for part in (place.get("name"), place.get("state")) if part
        )
        resolved = self.resolve(query)
        names = [
            name for name in ((resolved or {}).get("google_photo_names") or [])
            if self._valid_photo_name(name)
        ]
        return names[max(0, int(index)) % len(names)] if names else None

    def photo_count_for(self, place: dict, *, max_count: int = 8) -> int:
        """How many distinct, real Places photos are available for this entity."""
        direct = [
            name for name in (place.get("google_photo_names") or [])
            if self._valid_photo_name(name)
        ]
        if direct:
            return min(len(direct), max_count)
        query = ", ".join(
            part for part in (place.get("name"), place.get("state")) if part
        )
        resolved = self.resolve(query)
        names = [
            name for name in ((resolved or {}).get("google_photo_names") or [])
            if self._valid_photo_name(name)
        ]
        return min(len(names), max_count)

    def photo_uri_for(
        self, place: dict, *, max_width: int = 1440, index: int = 0
    ) -> Optional[str]:
        """Resolve a Places photo to a credential-free HTTPS media URI."""
        photo_name = self.photo_name_for(place, index=index)
        if not photo_name:
            return None
        width = max(320, min(int(max_width), 2560))
        cache_key = f"{photo_name}@{width}"
        if cache_key in self._photo_cache:
            return self._photo_cache[cache_key]
        try:
            response = self.session.get(
                f"https://places.googleapis.com/v1/{photo_name}/media",
                params={"maxWidthPx": width, "skipHttpRedirect": "true"},
                headers={"X-Goog-Api-Key": self.api_key},
                timeout=10,
            )
            response.raise_for_status()
            uri = response.json().get("photoUri")
            parsed = urlparse(uri or "")
            if parsed.scheme != "https" or not parsed.netloc:
                uri = None
        except (requests.RequestException, ValueError, TypeError):
            uri = None
        self._photo_cache[cache_key] = uri
        return uri

    @staticmethod
    def _is_india(place: dict) -> bool:
        components = place.get("addressComponents") or []
        for component in components:
            if "country" in (component.get("types") or []):
                return str(component.get("shortText") or "").upper() == "IN"
        address = str(place.get("formattedAddress") or "").lower()
        return "india" in address or address.endswith(" in")

    @staticmethod
    def _nearby_intro(place: dict, origin: dict) -> str:
        type_name = str(place.get("type") or "attraction").strip().lower()
        distance = place.get("distance_km")
        distance_text = f" about {distance:g} km from {origin.get('name')}" if distance is not None else f" near {origin.get('name')}"
        return f"A {type_name}{distance_text}, listed by Google Places."

    @staticmethod
    def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        radius = 6371.0088
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lng2 - lng1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def _valid_photo_name(value: object) -> bool:
        return bool(_PHOTO_NAME_RE.fullmatch(str(value or "")))

    @staticmethod
    def _photo_rank(photo: dict) -> tuple:
        """Rank a Places photo: crisp, well-framed, hero-worthy images first.

        Uses only the real pixel dimensions Places reports. Places photos are
        user submissions, so the first one returned is frequently a low-resolution
        portrait phone snap of a signboard or car park. Sorting by framing and
        then by total pixel count pushes the images that actually look like
        tourism photography to the front.
        """
        try:
            width = int(photo.get("widthPx") or 0)
            height = int(photo.get("heightPx") or 0)
        except (TypeError, ValueError):
            return (-1, 0, 0)
        # Anything this small looks soft even in a card, and terrible full-bleed.
        if width < 900 or height < 600:
            return (-1, 0, 0)

        ratio = width / height
        if 1.45 <= ratio <= 2.0:
            framing = 3      # 3:2 / 16:9 — how landscape photography is shot
        elif 1.25 <= ratio < 1.45:
            framing = 2      # 4:3 landscape
        elif ratio > 2.0:
            framing = 1      # panorama: usable, but crops awkwardly in cards
        else:
            framing = 0      # portrait / square

        megapixels = (width * height) / 1_000_000
        if megapixels >= 8:
            resolution = 3
        elif megapixels >= 4:
            resolution = 2
        elif megapixels >= 2:
            resolution = 1
        else:
            resolution = 0
        return (framing, resolution, width)

    @staticmethod
    def _normalise(place: dict, *, origin: Optional[dict] = None) -> Optional[dict]:
        location = place.get("location") or {}
        lat, lng = location.get("latitude"), location.get("longitude")
        name = (place.get("displayName") or {}).get("text")
        if not name or not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            return None
        primary_type = str(place.get("primaryType") or "place").replace("_", " ")
        photos = place.get("photos") or []
        # Order photos best-first instead of taking whatever Maps returned
        # first: prefer genuinely high-resolution, wide/landscape images, which
        # are the professional tourism-style shots rather than small portrait
        # phone snaps. Uses only real widthPx/heightPx reported by Places.
        photo_names = [
            photo.get("name")
            for photo in sorted(photos, key=GooglePlacesResolver._photo_rank, reverse=True)
            if GooglePlacesResolver._valid_photo_name(photo.get("name"))
        ]
        attributions = []
        for photo in photos:
            for author in photo.get("authorAttributions") or []:
                display_name = author.get("displayName")
                if display_name and display_name not in attributions:
                    attributions.append(display_name)
        result = {
            "slug": f"google-{place.get('id') or name}",
            "name": name,
            "kind": "external_location",
            "is_hidden_gem": False,
            "source": "google_places_api",
            "google_place_id": place.get("id"),
            "google_photo_names": photo_names,
            "photo_attributions": attributions,
            "lat": float(lat),
            "lng": float(lng),
            "type": primary_type,
            "categories": [],
            "tags": [],
            "address": place.get("formattedAddress") or "",
            "google_maps_url": place.get("googleMapsUri") or "",
            "rating": place.get("rating"),
            "reviews": place.get("userRatingCount"),
            "short_intro": f"A location in India resolved through Google Places as {name}.",
            "images": {"hero": None, "gallery": []},
        }
        if origin:
            try:
                result["distance_km"] = round(
                    GooglePlacesResolver._distance_km(
                        float(origin.get("lat")), float(origin.get("lng")),
                        float(lat), float(lng),
                    ),
                    1,
                )
            except (TypeError, ValueError):
                pass
            result["state"] = origin.get("state")
            result["state_slug"] = origin.get("state_slug")
        return result
