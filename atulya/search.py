"""Destination discovery search.

Powers both the homepage autocomplete and the "search takes you somewhere
useful" behaviour: a query can match a destination, a hidden gem or a state, by
exact name, prefix, substring, token overlap, tag/category or fuzzy similarity.
Users never need to know the exact database name.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional

from .data_store import DataStore

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalise(text: str) -> str:
    return (text or "").strip().lower()


def _tokens(text: str) -> List[str]:
    return _WORD_RE.findall(_normalise(text))


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


class SearchService:
    def __init__(self, store: DataStore):
        self.store = store

    def _candidates(self) -> List[dict]:
        """Flatten places and states into a common candidate shape."""
        candidates: List[dict] = []
        for place in self.store.all_places():
            kind = "hidden_gem" if place.get("is_hidden_gem") else "destination"
            candidates.append(
                {
                    "type": kind,
                    "slug": place["slug"],
                    "name": place.get("name", ""),
                    "subtitle": place.get("state", ""),
                    "state_slug": place.get("state_slug"),
                    "keywords": self._place_keywords(place),
                    "aliases": self._alias_names(place),
                    "popularity": place.get("popularity", 0),
                }
            )
        for state in self.store.all_states():
            label = "Union Territory" if state.get("type") == "union_territory" else "State"
            candidates.append(
                {
                    "type": "state",
                    "slug": state["slug"],
                    "name": state.get("name", ""),
                    "subtitle": label,
                    "state_slug": state.get("slug"),
                    "keywords": _tokens(state.get("name", "")),
                    "popularity": 50,
                }
            )
        return candidates

    @staticmethod
    def _place_keywords(place: dict) -> List[str]:
        words: List[str] = []
        words += _tokens(place.get("name", ""))
        words += _tokens(place.get("state", ""))
        words += _tokens(place.get("region", ""))
        words += _tokens(place.get("type", ""))
        for tag in place.get("tags", []) or []:
            words += _tokens(tag)
        for cat in place.get("categories", []) or []:
            words += _tokens(cat)
        # Attractions often carry the name people actually search for
        for attraction in place.get("attractions", []) or []:
            words += _tokens(attraction.get("name", ""))
        # Optional alternative names (e.g. Cherrapunji <-> Sohra) so a place is
        # discoverable under any commonly used name without duplicating records.
        for alias in place.get("aliases", []) or []:
            words += _tokens(alias)
        return words

    @staticmethod
    def _alias_names(place: dict) -> List[str]:
        return [str(a) for a in (place.get("aliases") or [])]

    def _score(self, query: str, candidate: dict) -> float:
        q = _normalise(query)
        name = _normalise(candidate["name"])
        if not q:
            return 0.0

        alias_names = [_normalise(a) for a in candidate.get("aliases", [])]

        score = 0.0
        if name == q or q in alias_names:
            score = 100.0
        elif name.startswith(q) or any(a.startswith(q) for a in alias_names):
            score = 85.0
        elif q in name or any(q in a for a in alias_names):
            score = 70.0
        else:
            # token overlap (handles multi-word and reordered queries)
            q_tokens = set(_tokens(query))
            kw = set(candidate["keywords"])
            overlap = q_tokens & kw
            if overlap:
                score = 45.0 + 8.0 * len(overlap)
                if any(k.startswith(q) for k in candidate["keywords"]):
                    score += 8.0
            else:
                # fuzzy fallback for typos / partial names
                ratio = _similarity(q, name)
                if ratio >= 0.6:
                    score = 30.0 + 25.0 * ratio

        if score <= 0:
            return 0.0

        # Gentle tie-breakers: prefer destinations and popular places.
        score += min(candidate.get("popularity", 0), 100) * 0.05
        if candidate["type"] == "destination":
            score += 3.0
        elif candidate["type"] == "state":
            score += 1.5
        return score

    def suggest(self, query: str, limit: int = 8) -> List[dict]:
        """Ranked autocomplete suggestions for a partial query."""
        query = _normalise(query)
        if not query:
            return []
        scored = []
        for candidate in self._candidates():
            s = self._score(query, candidate)
            if s > 0:
                item = {k: candidate[k] for k in ("type", "slug", "name", "subtitle")}
                item["score"] = round(s, 2)
                scored.append(item)
        scored.sort(key=lambda c: c["score"], reverse=True)
        return scored[:limit]

    def best_match(self, query: str) -> Optional[dict]:
        """The single strongest match, used to redirect a search to a page."""
        results = self.suggest(query, limit=1)
        return results[0] if results else None

    def search(self, query: str, limit: int = 30) -> Dict[str, List[dict]]:
        """Full results grouped by type for a search-results page."""
        grouped: Dict[str, List[dict]] = {"destination": [], "hidden_gem": [], "state": []}
        for item in self.suggest(query, limit=limit):
            grouped.setdefault(item["type"], []).append(item)
        return grouped
