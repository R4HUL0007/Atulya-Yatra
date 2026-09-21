"""Live discovery of places across a whole state.

The curated catalogue carries only a handful of records per state, so a state as
large as Rajasthan renders with two destinations. That is a data-volume problem,
not a layout problem: no amount of styling makes two cards look like a state.

This module asks Google Places for the rest. It runs several tourism-themed
queries per state so the results span forts, lakes, hill stations, temples and
parks rather than whatever one generic query happened to return, deduplicates
them against each other and against the curated records, and hands back plain
render-ready cards.

Discovered places are never presented as curated: they carry
``source="google_places_api"`` and link to the live ``/place`` page, exactly like
a search result from the homepage.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Optional

from flask import current_app, url_for

from .images import external_photo_url, named_image_url

# Broad coverage of what people actually travel to a state to see. Each query is
# a separate cached Places call, so this is a fixed, predictable cost per state.
DESTINATION_QUERIES = (
    "top tourist attractions in {state}, India",
    "famous forts palaces and monuments in {state}, India",
    "best hill stations and viewpoints in {state}, India",
    "famous temples and heritage sites in {state}, India",
    "lakes waterfalls and national parks in {state}, India",
    "most visited cities in {state}, India",
)

# Deliberately different vocabulary: these surface the quieter places rather than
# repeating the headline attractions.
OFFBEAT_QUERIES = (
    "offbeat places to visit in {state}, India",
    "hidden gems and lesser known places in {state}, India",
    "scenic villages and countryside in {state}, India",
    "peaceful unexplored places in {state}, India",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _normalise_name(value: object) -> str:
    return _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-")


def _popularity(place: dict) -> float:
    """Rank discovered places the way travellers perceive prominence."""
    rating = float(place.get("rating") or 0)
    reviews = max(0, int(place.get("reviews") or 0))
    return rating * 20 + math.log1p(reviews) * 10


def discover_state_places(
    state: dict,
    *,
    queries: Iterable[str] = DESTINATION_QUERIES,
    limit: int = 60,
    exclude_names: Iterable[str] = (),
) -> list[dict]:
    """Return many real places across a state, deduplicated and ranked.

    Returns an empty list when Places is not configured, so every caller
    degrades to curated-only content instead of failing.
    """
    resolver = getattr(current_app, "places_resolver", None)
    if not resolver or not resolver.enabled:
        return []

    state_name = str(state.get("name") or "").strip()
    if not state_name:
        return []

    excluded = {_normalise_name(name) for name in exclude_names if name}
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    found: list[dict] = []

    for template in queries:
        for candidate in resolver.search_places(
            template.format(state=state_name), limit=20
        ):
            place_id = candidate.get("google_place_id")
            name_key = _normalise_name(candidate.get("name"))
            if not name_key or name_key in excluded or name_key in seen_names:
                continue
            if place_id and place_id in seen_ids:
                continue
            # Places sometimes returns the state or country itself for a broad
            # query; that is not somewhere you visit.
            if name_key == _normalise_name(state_name) or name_key == "india":
                continue
            if place_id:
                seen_ids.add(place_id)
            seen_names.add(name_key)
            candidate["state"] = state_name
            candidate["state_slug"] = state.get("slug")
            found.append(candidate)

    found.sort(key=_popularity, reverse=True)
    return found[: max(1, int(limit))]


def discovered_card(candidate: dict, *, label: Optional[str] = None) -> dict:
    """Render-ready card for a discovered place.

    Links to the live ``/place`` page rather than a curated guide, because no
    curated guide exists for these yet.
    """
    names = candidate.get("google_photo_names") or []
    image = external_photo_url(names[0], width=1400) if names else None
    if not image:
        image = named_image_url(
            candidate.get("name") or "",
            context=candidate.get("state") or candidate.get("address") or "",
        )
    name = candidate.get("name") or ""
    return {
        "name": name,
        "label": label,
        "type": (candidate.get("type") or "").replace("_", " ").title(),
        "state": candidate.get("state"),
        "address": candidate.get("address"),
        "rating": candidate.get("rating"),
        "reviews": candidate.get("reviews"),
        "distance_km": candidate.get("distance_km"),
        "image": image,
        "explore_url": url_for("pages.place", q=name) if name else None,
        "raw": candidate,
    }
