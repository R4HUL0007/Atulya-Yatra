"""Recommendation engine.

Two responsibilities, both driven by transparent scoring rather than fixed
manual rankings:

1. ``rank_hidden_gems`` — ranks hidden gems near a destination using distance,
   ratings, reviews, popularity, hidden-gem relevance, seasonal fit, category
   relevance and data completeness. New gems added to the dataset participate
   automatically; no manual reordering is ever required. Distance matters but is
   only one factor, and it is used as the tie-breaker when scores are close.

2. ``recommend_for_preferences`` — powers the AI assistant. It uses a matching
   score so a place that satisfies several of a user's preferences ranks higher,
   without requiring an exact tag match.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable, List, Optional, Sequence

# Weights for the hidden-gem recommendation score. Centralised so the ranking
# philosophy is easy to read and tune.
GEM_WEIGHTS = {
    "distance": 0.28,
    "rating": 0.20,
    "reviews": 0.12,
    "hidden_relevance": 0.12,
    "season": 0.10,
    "category": 0.10,
    "popularity": 0.05,
    "data_quality": 0.03,
}


def _current_month_name(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%B")


def _norm(value: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


def _distance_factor(distance_km: Optional[float], radius_km: float) -> float:
    if distance_km is None:
        return 0.4  # unknown distance: neutral-ish, don't over-reward
    # Linear decay: right next door ~1.0, at the edge of the radius ~0.
    return max(0.0, 1.0 - (distance_km / radius_km))


def _reviews_factor(reviews: int) -> float:
    # Log scale so a handful of reviews still counts but doesn't dominate.
    return _norm(math.log10(max(reviews, 0) + 1), 0.0, math.log10(50000 + 1))


def _hidden_relevance(place: dict) -> float:
    # Reward genuine offbeat places: hidden-gem flag plus a lower popularity.
    base = 0.6 if place.get("is_hidden_gem") else 0.2
    popularity = place.get("popularity", 50)
    offbeat_bonus = _norm(100 - popularity, 0, 100) * 0.4
    return min(1.0, base + offbeat_bonus)


def _season_factor(place: dict, month: str) -> float:
    seasons = [str(m).lower() for m in place.get("best_season", []) or []]
    if not seasons:
        return 0.5  # no data: neutral
    return 1.0 if month.lower() in seasons else 0.25


def _category_relevance(place: dict, interests: Sequence[str]) -> float:
    if not interests:
        return 0.5
    interests_l = {i.lower() for i in interests}
    haystack = {str(c).lower() for c in place.get("categories", []) or []}
    haystack |= {str(t).lower() for t in place.get("tags", []) or []}
    if not haystack:
        return 0.4
    overlap = interests_l & haystack
    return min(1.0, len(overlap) / max(1, len(interests_l)))


def score_hidden_gem(
    place: dict,
    *,
    radius_km: float,
    month: str,
    interests: Sequence[str] = (),
) -> float:
    """Return a 0–100 recommendation score for a single hidden gem."""
    w = GEM_WEIGHTS
    factors = {
        "distance": _distance_factor(place.get("distance_km"), radius_km),
        "rating": _norm(place.get("rating", 0) or 0, 0, 5),
        "reviews": _reviews_factor(place.get("reviews", 0) or 0),
        "hidden_relevance": _hidden_relevance(place),
        "season": _season_factor(place, month),
        "category": _category_relevance(place, interests),
        "popularity": _norm(place.get("popularity", 0) or 0, 0, 100),
        "data_quality": float(place.get("data_quality", 0.5) or 0.5),
    }
    total = sum(w[k] * factors[k] for k in w)
    return round(total * 100, 2)


def rank_hidden_gems(
    gems: Iterable[dict],
    *,
    radius_km: float,
    interests: Sequence[str] = (),
    now: Optional[datetime] = None,
) -> List[dict]:
    """Rank hidden gems by recommendation score, distance as tie-breaker."""
    month = _current_month_name(now)
    ranked = []
    for gem in gems:
        item = dict(gem)
        item["recommendation_score"] = score_hidden_gem(
            item, radius_km=radius_km, month=month, interests=interests
        )
        ranked.append(item)
    # Higher score first; when scores are within a small epsilon, prefer nearer.
    ranked.sort(
        key=lambda g: (-g["recommendation_score"], g.get("distance_km", 9e9))
    )
    return ranked


def recommend_for_preferences(
    places: Iterable[dict],
    interests: Sequence[str],
    *,
    prefer: str = "both",
    limit: int = 8,
    now: Optional[datetime] = None,
) -> List[dict]:
    """Recommend destinations/gems for a set of user interests.

    ``prefer`` is one of ``popular``, ``hidden`` or ``both`` and softly biases
    the results rather than filtering hard, so users still get strong matches.
    A place matching several interests scores higher than one matching a single
    interest, but an exact match is never required.
    """
    month = _current_month_name(now)
    interests_l = [i.lower() for i in interests if i]
    scored: List[dict] = []

    for place in places:
        haystack = {str(c).lower() for c in place.get("categories", []) or []}
        haystack |= {str(t).lower() for t in place.get("tags", []) or []}
        haystack |= {str(a).lower() for a in place.get("activities", []) or []}

        matches = [i for i in interests_l if i in haystack]
        match_count = len(matches)
        # Base relevance grows with the number of satisfied preferences.
        relevance = match_count / max(1, len(interests_l)) if interests_l else 0.0

        rating = _norm(place.get("rating", 0) or 0, 0, 5)
        popularity = _norm(place.get("popularity", 0) or 0, 0, 100)
        season = _season_factor(place, month)

        score = (
            relevance * 55.0
            + match_count * 4.0
            + rating * 20.0
            + season * 10.0
        )

        if prefer == "hidden":
            score += 12.0 if place.get("is_hidden_gem") else 0.0
            score += (1 - popularity) * 8.0
        elif prefer == "popular":
            score += popularity * 14.0
        else:  # both
            score += popularity * 5.0
            score += 4.0 if place.get("is_hidden_gem") else 0.0

        if match_count == 0 and interests_l:
            # Keep only places with at least a thematic connection when the user
            # expressed interests, but still allow rating/season to rank them.
            score *= 0.4

        item = dict(place)
        item["match_count"] = match_count
        item["matched_interests"] = matches
        item["recommendation_score"] = round(score, 2)
        scored.append(item)

    scored.sort(
        key=lambda p: (-p["recommendation_score"], -(p.get("rating") or 0))
    )
    return scored[:limit]
