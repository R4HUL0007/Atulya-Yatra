"""Dynamic Nearby Hidden Gems engine.

This is a discovery system distinct from the 30 km "Nearby Attractions" list.
For any origin destination it builds a *candidate universe* within a 100 km
radius from three sources and then decides, using a transparent weighted score,
which of those places actually deserve to be surfaced as a hidden gem:

    candidate universe
      = curated Atulya Yatra hidden gems        (trusted editorial data)
      + approved community recommendations       (moderated user data)
      + Google Places candidates                 (geographic discovery only)

Google/Maps only supplies the geographic universe of candidates and their
factual attributes (rating, review counts, coordinates). Atulya Yatra decides
whether a place is a hidden gem. Nothing here invents ratings, coordinates,
popularity, reviews, travel times or descriptions — every value is derived from
data that already exists, and derived estimates (travel time) are clearly
labelled as estimates.

Score (normalised, weighted):

    rating / experience quality      20%
    low / medium popularity          20%
    reasonable distance              15%
    tourism relevance                15%
    local / community recommendation 15%
    seasonal relevance               10%
    mainstream attraction penalty   -15%

The numeric score is never shown to users; friendly labels are used instead.
New curated or community-approved places automatically join the ranking with no
manual priority assignment.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Iterable, Optional, Sequence

WEIGHTS = {
    "rating": 0.20,
    "low_popularity": 0.20,
    "distance": 0.15,
    "tourism": 0.15,
    "community": 0.15,
    "season": 0.10,
}
MAINSTREAM_PENALTY = 0.15

# Average effective road speed (km/h) used only to derive a labelled travel-time
# *estimate* from the real great-circle distance. Regional Indian highways with
# terrain and town crossings sit well below straight-line speed.
_ROAD_SPEED_KMH = 42.0

_TOURISM_WORDS = {
    "temple", "fort", "palace", "lake", "waterfall", "falls", "beach", "hill",
    "valley", "wildlife", "sanctuary", "national park", "park", "monument",
    "heritage", "historical", "museum", "monastery", "church", "mosque",
    "trek", "hiking", "nature", "scenic", "viewpoint", "cave", "dam", "island",
    "garden", "landmark", "pilgrimage", "spiritual", "cultural", "adventure",
    "mountain", "river", "forest", "tourist", "attraction", "gurudwara",
}

_MAINSTREAM_WORDS = {
    "taj mahal", "gateway of india", "red fort", "qutub minar", "india gate",
    "golden temple", "hawa mahal", "amber fort", "mysore palace",
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _norm(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return _clamp01((value - lo) / (hi - lo))


def _current_month(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%B").lower()


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


# --------------------------------------------------------------------- factors
def _popularity_estimate(candidate: dict) -> float:
    """A 0–100 popularity read.

    Curated places carry an explicit popularity. For everything else we infer it
    only from the real Google review count (log scale), never inventing a value.
    Community submissions default to a modest, grassroots level.
    """
    popularity = candidate.get("popularity")
    if isinstance(popularity, (int, float)):
        return float(max(0, min(100, popularity)))
    reviews = candidate.get("reviews") or 0
    if reviews:
        return 100.0 * _norm(math.log10(reviews + 1), 0.0, math.log10(20000 + 1))
    if candidate.get("source") == "community":
        return 25.0
    return 40.0  # unknown, neutral-low


def _rating_factor(candidate: dict) -> float:
    rating = candidate.get("rating")
    if isinstance(rating, (int, float)) and rating > 0:
        return _norm(rating, 3.0, 5.0)
    # No rating available: neutral so a place is neither rewarded nor punished.
    return 0.5


def _low_popularity_factor(popularity: float) -> float:
    """Reward low/medium popularity, but do not simply reward zero reviews.

    A gentle plateau keeps genuinely offbeat-but-real places high while stopping
    an utterly unknown pin from automatically beating a loved local favourite.
    """
    if popularity <= 55:
        return 0.75 + 0.25 * (1 - popularity / 55)  # 1.0 .. 0.75
    return _clamp01(0.75 * (100 - popularity) / 45)  # 0.75 .. 0.0


def _distance_factor(distance_km: Optional[float], radius_km: float) -> float:
    if distance_km is None:
        return 0.4
    # Reasonable distance: near is better, but this is only 15% of the score so
    # long distance is discouraged without dominating the ranking.
    return _clamp01(1.0 - (distance_km / radius_km))


def _tourism_factor(candidate: dict) -> float:
    haystack = " ".join(
        str(part).lower()
        for part in (
            candidate.get("category") or "",
            candidate.get("type") or "",
            " ".join(candidate.get("categories") or []),
            " ".join(candidate.get("tags") or []),
        )
    )
    if not haystack.strip():
        return 0.45
    hits = sum(1 for word in _TOURISM_WORDS if word in haystack)
    if hits == 0:
        return 0.3
    return _clamp01(0.55 + 0.15 * hits)


def _community_factor(candidate: dict) -> float:
    source = candidate.get("source")
    if source == "community":
        return 1.0
    if source == "curated" or candidate.get("is_hidden_gem"):
        return 0.7
    return 0.1  # pure Google candidate, no editorial/community signal


def _season_factor(candidate: dict, month: str) -> float:
    seasons = [str(s).lower() for s in (candidate.get("best_season") or [])]
    if not seasons:
        return 0.5
    return 1.0 if month in seasons else 0.3


def _is_mainstream(candidate: dict, popularity: float) -> bool:
    name = str(candidate.get("name") or "").lower()
    if any(word in name for word in _MAINSTREAM_WORDS):
        return True
    reviews = candidate.get("reviews") or 0
    if reviews >= 8000:
        return True
    # Very high popularity curated destinations are mainstream by definition.
    return popularity >= 80


def score_candidate(candidate: dict, *, radius_km: float, month: str) -> dict:
    """Attach a normalised score, label and human 'why' to one candidate."""
    popularity = _popularity_estimate(candidate)
    factors = {
        "rating": _rating_factor(candidate),
        "low_popularity": _low_popularity_factor(popularity),
        "distance": _distance_factor(candidate.get("distance_km"), radius_km),
        "tourism": _tourism_factor(candidate),
        "community": _community_factor(candidate),
        "season": _season_factor(candidate, month),
    }
    total = sum(WEIGHTS[key] * factors[key] for key in WEIGHTS)
    mainstream = _is_mainstream(candidate, popularity)
    if mainstream:
        total -= MAINSTREAM_PENALTY
    total = _clamp01(total)

    enriched = dict(candidate)
    enriched["_score"] = round(total, 4)
    enriched["_popularity"] = round(popularity, 1)
    enriched["_mainstream"] = mainstream
    enriched["label"] = _label_for(total, candidate, mainstream)
    enriched["why"] = _why_for(candidate, factors, popularity, mainstream)
    enriched["travel_time"] = travel_time_estimate(candidate.get("distance_km"))
    return enriched


def _label_for(score: float, candidate: dict, mainstream: bool) -> str:
    """Friendly, non-numeric label. Mainstream places are never 'Hidden Gem'."""
    if mainstream:
        return "Worth Discovering"
    if score >= 0.62:
        return "Hidden Gem"
    if score >= 0.44:
        return "Local Favorite"
    return "Worth Discovering"


def _why_for(candidate: dict, factors: dict, popularity: float, mainstream: bool) -> str:
    clauses: list[str] = []
    rating = candidate.get("rating")
    if isinstance(rating, (int, float)) and rating >= 4.4:
        clauses.append("highly rated by visitors")
    elif isinstance(rating, (int, float)) and rating >= 4.0:
        clauses.append("well rated by visitors")

    if candidate.get("source") == "community":
        clauses.append("recommended by our community")
    elif candidate.get("source") == "curated" or candidate.get("is_hidden_gem"):
        clauses.append("an Atulya Yatra editorial pick")

    if not mainstream and popularity <= 55:
        clauses.append("still relatively uncrowded")

    distance = candidate.get("distance_km")
    origin_name = candidate.get("_origin_name")
    if distance is not None and origin_name:
        clauses.append(f"about {distance:g} km from {origin_name}")

    if not clauses:
        clauses.append("a worthwhile stop in the region")
    sentence = ", ".join(clauses)
    return sentence[0].upper() + sentence[1:] + "."


def travel_time_estimate(distance_km: Optional[float]) -> Optional[str]:
    """Return a clearly-labelled by-road time estimate from real distance."""
    if distance_km is None or distance_km <= 0:
        return None
    minutes = int(round((distance_km / _ROAD_SPEED_KMH) * 60))
    if minutes < 60:
        return f"~{max(minutes, 5)} min by road (est.)"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"~{hours} h by road (est.)"
    return f"~{hours} h {mins} min by road (est.)"


# --------------------------------------------------------- candidate builders
def candidate_from_curated(place: dict) -> dict:
    return {
        "source": "curated",
        "slug": place.get("slug"),
        "place_slug": place.get("slug"),
        "name": place.get("name"),
        "region": place.get("state"),
        "state": place.get("state"),
        "state_slug": place.get("state_slug"),
        "category": (place.get("categories") or [None])[0],
        "categories": place.get("categories") or [],
        "tags": place.get("tags") or [],
        "type": place.get("type"),
        "description": place.get("short_intro") or place.get("overview"),
        "lat": place.get("lat"),
        "lng": place.get("lng"),
        "rating": place.get("rating"),
        "reviews": place.get("reviews"),
        "popularity": place.get("popularity"),
        "best_season": place.get("best_season") or [],
        "is_hidden_gem": place.get("is_hidden_gem", True),
        "distance_km": place.get("distance_km"),
        "has_page": True,
    }


def candidate_from_community(row) -> dict:
    return {
        "source": "community",
        "slug": f"community-{getattr(row, 'id', '')}",
        "place_slug": None,
        "name": getattr(row, "name", None),
        "region": getattr(row, "state", None) or getattr(row, "location", None),
        "state": getattr(row, "state", None),
        "category": getattr(row, "category", None),
        "categories": [getattr(row, "category", None)] if getattr(row, "category", None) else [],
        "tags": [],
        "type": getattr(row, "category", None),
        "description": getattr(row, "why_special", None) or getattr(row, "description", None),
        "lat": getattr(row, "lat", None),
        "lng": getattr(row, "lng", None),
        "photo_url": getattr(row, "photo_url", None),
        "rating": None,
        "reviews": None,
        "popularity": None,
        "best_season": [],
        "is_hidden_gem": True,
        "has_page": False,
    }


def candidate_from_maps(place: dict) -> dict:
    return {
        "source": "maps",
        "slug": place.get("slug"),
        "place_slug": None,
        "name": place.get("name"),
        "region": place.get("state") or place.get("address"),
        "state": place.get("state"),
        "category": place.get("type"),
        "categories": [],
        "tags": [],
        "type": place.get("type"),
        "description": place.get("short_intro"),
        "lat": place.get("lat"),
        "lng": place.get("lng"),
        "address": place.get("address"),
        "google_place_id": place.get("google_place_id"),
        "google_photo_names": place.get("google_photo_names") or [],
        "google_maps_url": place.get("google_maps_url"),
        "rating": place.get("rating"),
        "reviews": place.get("reviews"),
        "popularity": None,
        "best_season": [],
        "is_hidden_gem": False,
        "distance_km": place.get("distance_km"),
        "has_page": False,
    }


# ----------------------------------------------------------------- assembly
def _dedupe_key(candidate: dict) -> tuple:
    lat, lng = candidate.get("lat"), candidate.get("lng")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return ("geo", round(lat, 3), round(lng, 3))
    return ("name", _slugify(candidate.get("name") or ""))


_SOURCE_PRIORITY = {"curated": 0, "community": 1, "maps": 2}


def rank_hidden_gems(
    candidates: Iterable[dict],
    *,
    origin_name: str = "",
    radius_km: float = 100.0,
    now: Optional[datetime] = None,
) -> list[dict]:
    """Filter to radius, de-duplicate, score dynamically and sort.

    Sort key: highest score first, then nearest distance as a tie-breaker.
    """
    month = _current_month(now)
    best: dict[tuple, dict] = {}
    for candidate in candidates:
        name = str(candidate.get("name") or "").strip()
        if not name:
            continue
        distance = candidate.get("distance_km")
        if distance is not None and distance > radius_km:
            continue
        candidate = dict(candidate)
        candidate["_origin_name"] = origin_name
        key = _dedupe_key(candidate)
        current = best.get(key)
        if current is None:
            best[key] = candidate
            continue
        # Prefer the more trustworthy source; keep richer data on ties.
        if _SOURCE_PRIORITY.get(candidate["source"], 9) < _SOURCE_PRIORITY.get(current["source"], 9):
            best[key] = candidate

    scored = [
        score_candidate(candidate, radius_km=radius_km, month=month)
        for candidate in best.values()
    ]
    scored.sort(key=lambda c: (-c["_score"], c.get("distance_km") if c.get("distance_km") is not None else 9e9))
    return scored


def collect_scored_gems(
    origin: dict,
    *,
    radius_km: float = 100.0,
    exclude_slug: Optional[str] = None,
) -> list[dict]:
    """Build the 100 km candidate universe for an origin and score it.

    Sources are combined then ranked. Google Places only widens the candidate
    universe; if it is unavailable we fall back gracefully to curated and
    community data. Never fabricates data.
    """
    from flask import current_app

    from .extensions import db
    from .geo import distance_between, has_coords
    from .models import HiddenGemSubmission

    store = current_app.data_store
    candidates: list[dict] = []

    # 1) Curated Atulya Yatra hidden gems within the radius.
    for place in store.within_radius(
        origin, radius_km, only_hidden_gems=True, exclude_slug=exclude_slug
    ):
        candidates.append(candidate_from_curated(place))

    # 2) Approved community recommendations that carry coordinates.
    try:
        approved = (
            HiddenGemSubmission.query.filter_by(
                status=HiddenGemSubmission.STATUS_APPROVED
            )
            .filter(HiddenGemSubmission.lat.isnot(None))
            .filter(HiddenGemSubmission.lng.isnot(None))
            .all()
        )
    except Exception:  # pragma: no cover - defensive: never break the page
        approved = []
    for row in approved:
        target = {"lat": row.lat, "lng": row.lng}
        if not has_coords(target):
            continue
        distance = distance_between(origin, target)
        if distance is None or distance > radius_km:
            continue
        candidate = candidate_from_community(row)
        candidate["distance_km"] = round(distance, 1)
        candidates.append(candidate)

    # 3) Google Places candidates (geographic universe only). The Nearby Search
    # API caps at 50 km, so this widens — never replaces — trusted data.
    resolver = getattr(current_app, "places_resolver", None)
    if resolver and resolver.enabled and has_coords(origin):
        maps_radius = min(float(radius_km), 50.0)
        for raw in resolver.search_nearby(origin, radius_km=maps_radius, max_results=20):
            candidates.append(candidate_from_maps(raw))

    return rank_hidden_gems(
        candidates, origin_name=origin.get("name") or "", radius_km=radius_km
    )


def _shorten(text: object, limit: int = 160) -> str:
    words = str(text or "").split()
    if not words:
        return ""
    out = " ".join(words)
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


def gem_view(candidate: dict) -> dict:
    """Turn a scored candidate into a render-ready card (image + links).

    Imports the Flask-bound media/directions helpers lazily so the scoring
    engine itself stays pure and free of request-context coupling.
    """
    from flask import current_app, url_for

    from .directions import directions_url_for
    from .images import external_photo_url, named_image_url, resolve_entity_image

    source = candidate.get("source")
    name = candidate.get("name") or "Place"
    region = candidate.get("region") or candidate.get("state") or ""
    category = candidate.get("category") or ""
    if isinstance(category, str):
        category = category.replace("_", " ").strip().title()

    image = None
    explore_url = None
    place_slug = candidate.get("place_slug")

    if source == "curated" and place_slug:
        place = current_app.data_store.get_place(place_slug)
        if place:
            image = resolve_entity_image(place, "place", width=900)
            explore_url = url_for("pages.destination", slug=place_slug)
    elif source == "community":
        photo = candidate.get("photo_url") or ""
        if isinstance(photo, str) and photo.startswith("https://"):
            image = photo
    elif source == "maps":
        names = candidate.get("google_photo_names") or []
        if names:
            image = external_photo_url(names[0])

    # Every gem is explorable: curated ones open their full guide, everything
    # else opens a live detail page built the same way as any destination.
    if not explore_url:
        explore_url = url_for("pages.place", q=name)

    if not image:
        image = named_image_url(name, context=region, category=candidate.get("category") or "")

    return {
        "name": name,
        "region": region,
        "category": category,
        "description": _shorten(candidate.get("description"), 160),
        "distance_km": candidate.get("distance_km"),
        "travel_time": candidate.get("travel_time"),
        "rating": candidate.get("rating"),
        "label": candidate.get("label") or "Hidden Gem",
        "why": candidate.get("why"),
        "image": image,
        "explore_url": explore_url,
        "directions_url": directions_url_for(candidate),
        "source": source,
    }
