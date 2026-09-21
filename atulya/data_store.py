"""In-memory data store for curated tourism content.

Curated content (destinations, hidden gems, states, trips) is the source of
truth and lives in version-controlled JSON. Loading it once into indexed
in-memory structures keeps every page fast even as the dataset grows, and makes
the content trivial to expand: drop a new object into the JSON and it is picked
up on the next load, automatically participating in search, geo queries and the
recommendation engine.

Destinations and hidden gems share a single unified "place" schema and are held
in one collection, so a hidden gem is just a place with ``is_hidden_gem = True``.
This lets both take part in coordinate-based nearby/hidden-gem queries and each
get its own detail page.

As the dataset has grown past a single flat file, destinations and hidden gems
are now split across multiple regional JSON files (e.g. ``destinations.json``,
``destinations_north.json``, ``destinations_south.json`` ...). Any file matching
``destinations*.json`` or ``hidden_gems*.json`` in the data directory is loaded
and merged automatically — no Python changes are needed to add another regional
file. Duplicate slugs across files are detected and logged rather than silently
overwritten, so data-entry mistakes surface immediately instead of quietly
dropping a record.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional

from .geo import distance_between, has_coords

logger = logging.getLogger(__name__)


class DataStore:
    """Loads and indexes the curated JSON datasets."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._lock = RLock()
        self._places: Dict[str, dict] = {}
        self._states: Dict[str, dict] = {}
        self._trips: Dict[str, dict] = {}
        self._places_by_state: Dict[str, List[str]] = defaultdict(list)
        self._trips_by_state: Dict[str, List[str]] = defaultdict(list)
        self._loaded = False
        # Populated on load(): slugs that appeared in more than one source file,
        # and the source file each place slug actually came from (last write wins,
        # mirroring the dict-assignment behaviour, but callers/tools can inspect
        # this to see exactly where a record lives).
        self.duplicate_slugs: List[str] = []
        self.place_sources: Dict[str, str] = {}

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        """Read every dataset from disk and rebuild the indexes."""
        with self._lock:
            destination_records = self._read_json_glob("destinations*.json")
            hidden_gem_records = self._read_json_glob("hidden_gems*.json")
            states = self._read_json("states.json", default=[])
            trips = self._read_json("trips.json", default=[])

            self._places = {}
            self.place_sources = {}
            seen_once: set = set()
            duplicates: List[str] = []

            for record, source in destination_records + hidden_gem_records:
                slug = record.get("slug")
                if not slug:
                    continue
                # Defaults follow the source shard, while explicit external/
                # moderated kinds remain untouched.
                source_is_hidden = source.startswith("hidden_gems")
                record.setdefault("kind", "hidden_gem" if source_is_hidden else "destination")
                record.setdefault("is_hidden_gem", record["kind"] == "hidden_gem")
                if slug in seen_once:
                    duplicates.append(slug)
                    logger.warning(
                        "Duplicate place slug '%s' found in %s (already loaded from %s); "
                        "keeping the later record.",
                        slug, source, self.place_sources.get(slug),
                    )
                seen_once.add(slug)
                self._places[slug] = record
                self.place_sources[slug] = source

            self.duplicate_slugs = duplicates
            self._states = {s["slug"]: s for s in states if s.get("slug")}
            self._trips = {t["slug"]: t for t in trips if t.get("slug")}

            self._reindex()
            self._loaded = True

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _read_json(self, name: str, default: Any) -> Any:
        path = self.data_dir / name
        if not path.exists():
            return default
        with path.open(encoding="utf-8") as fh:
            try:
                return json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Malformed JSON in data file '{path.name}' at line {exc.lineno}, "
                    f"column {exc.colno}: {exc.msg}. Fix this file before the app can start."
                ) from exc

    def _read_json_glob(self, pattern: str) -> List[tuple]:
        """Load and merge every JSON file matching ``pattern`` in the data dir.

        Returns a list of ``(record, source_filename)`` tuples so duplicate
        detection can report exactly which file introduced a conflict. Files are
        processed in sorted filename order for deterministic, reproducible loads.
        A malformed file raises a clear, actionable error naming the file and the
        exact location of the problem, rather than crashing pages silently later.
        """
        records: List[tuple] = []
        for path in sorted(self.data_dir.glob(pattern)):
            with path.open(encoding="utf-8") as fh:
                try:
                    data = json.load(fh)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Malformed JSON in data file '{path.name}' at line {exc.lineno}, "
                        f"column {exc.colno}: {exc.msg}. Fix this file before the app can start."
                    ) from exc
            if not isinstance(data, list):
                logger.warning("Skipping %s: expected a JSON list at the top level.", path.name)
                continue
            for record in data:
                records.append((record, path.name))
        return records

    def _reindex(self) -> None:
        self._places_by_state = defaultdict(list)
        for slug, place in self._places.items():
            state_slug = place.get("state_slug")
            if state_slug:
                self._places_by_state[state_slug].append(slug)

        self._trips_by_state = defaultdict(list)
        for slug, trip in self._trips.items():
            state_slug = trip.get("state_slug")
            if state_slug:
                self._trips_by_state[state_slug].append(slug)

    # --------------------------------------------------------------- places
    def all_places(self) -> List[dict]:
        self.ensure_loaded()
        return list(self._places.values())

    def get_place(self, slug: str) -> Optional[dict]:
        self.ensure_loaded()
        return self._places.get(slug)

    def destinations(self) -> List[dict]:
        return [p for p in self.all_places() if not p.get("is_hidden_gem")]

    def hidden_gems(self) -> List[dict]:
        return [p for p in self.all_places() if p.get("is_hidden_gem")]

    def places_in_state(self, state_slug: str) -> List[dict]:
        self.ensure_loaded()
        return [self._places[s] for s in self._places_by_state.get(state_slug, [])]

    def destinations_in_state(self, state_slug: str) -> List[dict]:
        return [p for p in self.places_in_state(state_slug) if not p.get("is_hidden_gem")]

    def hidden_gems_in_state(self, state_slug: str) -> List[dict]:
        return [p for p in self.places_in_state(state_slug) if p.get("is_hidden_gem")]

    # --------------------------------------------------------------- states
    def all_states(self) -> List[dict]:
        self.ensure_loaded()
        return sorted(self._states.values(), key=lambda s: s.get("name", ""))

    def get_state(self, slug: str) -> Optional[dict]:
        self.ensure_loaded()
        return self._states.get(slug)

    def state_by_name(self, name: str) -> Optional[dict]:
        self.ensure_loaded()
        name_l = (name or "").strip().lower()
        for state in self._states.values():
            if state.get("name", "").lower() == name_l:
                return state
        return None

    def state_guide(self, slug: str) -> Optional[dict]:
        """Return normalized, presentation-ready guidance from curated state data."""
        state = self.get_state(slug)
        if not state:
            return None
        return {
            "state_name": state.get("name"),
            "intro": state.get("intro") or "",
            "languages": list(state.get("languages") or []),
            "best_time": state.get("best_time_to_visit") or "",
            "culture": state.get("culture") or "",
            "food": list(state.get("famous_food") or []),
            "festivals": [
                {
                    "name": festival.get("name"),
                    "time": festival.get("time") or "",
                }
                for festival in (state.get("festivals") or [])
                if isinstance(festival, dict) and festival.get("name")
            ],
            "travel_tips": list(state.get("travel_tips") or []),
            "activities": list(state.get("adventure_activities") or []),
            "wildlife": list(state.get("wildlife") or []),
            "spiritual_places": list(state.get("religious_destinations") or []),
            "safety_tips": [
                "Check current weather, road conditions and official local advisories before setting out.",
                "Keep identification and emergency contacts accessible, and use registered transport where available.",
                "Confirm opening hours, permits and local access rules directly before visiting.",
            ],
            "packing": [
                "Government ID, digital copies of bookings, and any required permits",
                "Weather-appropriate layers, comfortable walking shoes, and rain or sun protection",
                "Refillable water bottle, prescribed medicines, and a compact first-aid kit",
                "A modest cover for religious sites and a reusable bag for waste",
            ],
            "guidance_note": "Safety and packing are general practical guidance; check local conditions for your dates.",
        }

    # ---------------------------------------------------------------- trips
    def all_trips(self) -> List[dict]:
        self.ensure_loaded()
        return sorted(self._trips.values(), key=lambda t: t.get("name", ""))

    def get_trip(self, slug: str) -> Optional[dict]:
        self.ensure_loaded()
        return self._trips.get(slug)

    def trips_in_state(self, state_slug: str) -> List[dict]:
        self.ensure_loaded()
        return [self._trips[s] for s in self._trips_by_state.get(state_slug, [])]

    def resolve_places(self, slugs: Iterable[str]) -> List[dict]:
        """Turn a list of place slugs (as used by trips) into place records."""
        self.ensure_loaded()
        resolved = []
        for slug in slugs or []:
            place = self._places.get(slug)
            if place:
                resolved.append(place)
        return resolved

    # -------------------------------------------------------------- geo/util
    def within_radius(
        self,
        origin: dict,
        radius_km: float,
        *,
        only_hidden_gems: bool = False,
        exclude_slug: Optional[str] = None,
    ) -> List[dict]:
        """Return places within ``radius_km`` of ``origin``, annotated with distance.

        Each returned item is a shallow copy carrying a ``distance_km`` field so
        callers can rank or display it without mutating the stored record.
        """
        self.ensure_loaded()
        if not has_coords(origin):
            return []
        results = []
        for place in self._places.values():
            if exclude_slug and place.get("slug") == exclude_slug:
                continue
            if only_hidden_gems and not place.get("is_hidden_gem"):
                continue
            dist = distance_between(origin, place)
            if dist is None or dist > radius_km:
                continue
            item = dict(place)
            item["distance_km"] = round(dist, 1)
            results.append(item)
        results.sort(key=lambda p: p["distance_km"])
        return results
