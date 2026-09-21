"""Data validation for the Atulya Yatra tourism dataset.

Run standalone (no Flask app context needed):

    python scripts/validate_data.py

Validates every destination and hidden gem against the minimum required schema,
checks coordinate sanity, slug uniqueness, state references, tag/list types, and
non-empty descriptions. Prints a clear pass/fail report. Exits non-zero if any
record fails a hard requirement, so this can be wired into CI later without any
further changes.

This script intentionally does not import the Flask app — it reads the JSON data
files directly so it can validate data before the app would even be able to
start, and so it stays independent of any web-framework state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# India's approximate bounding box (generous, includes island territories).
INDIA_LAT_RANGE = (5.0, 38.0)
INDIA_LNG_RANGE = (66.0, 98.0)

REQUIRED_PLACE_FIELDS = [
    "slug", "name", "state", "state_slug", "lat", "lng",
    "short_intro", "overview", "categories", "tags",
]
LIST_FIELDS = ["categories", "tags", "best_season", "attractions", "activities", "food", "festivals"]


def load_json_glob(pattern: str) -> List[Tuple[dict, str]]:
    records = []
    for path in sorted(DATA_DIR.glob(pattern)):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"FATAL: malformed JSON in {path.name} at line {exc.lineno}: {exc.msg}")
            sys.exit(1)
        if not isinstance(data, list):
            print(f"FATAL: {path.name} must contain a JSON list at the top level.")
            sys.exit(1)
        for record in data:
            records.append((record, path.name))
    return records


def load_json(name: str) -> list:
    path = DATA_DIR / name
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FATAL: malformed JSON in {name} at line {exc.lineno}: {exc.msg}")
        sys.exit(1)


def valid_coords(lat: Any, lng: Any) -> bool:
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return False
    return INDIA_LAT_RANGE[0] <= lat <= INDIA_LAT_RANGE[1] and INDIA_LNG_RANGE[0] <= lng <= INDIA_LNG_RANGE[1]


def main() -> int:
    place_records = load_json_glob("destinations*.json") + load_json_glob("hidden_gems*.json")
    states = load_json("states.json")
    trips = load_json("trips.json")

    state_slugs = {s.get("slug") for s in states if s.get("slug")}

    errors: List[str] = []
    warnings: List[str] = []

    slug_to_sources: Dict[str, List[str]] = {}
    name_to_slugs: Dict[str, List[str]] = {}
    missing_fields_by_place: Dict[str, List[str]] = {}
    bad_coords: List[str] = []
    placeholder_hits: List[str] = []
    empty_descriptions: List[str] = []

    placeholder_phrases = [
        "beautiful place", "great destination", "perfect place",
        "lorem ipsum", "amazing place", "nice place",
    ]

    destinations_count = 0
    hidden_gems_count = 0

    for record, source in place_records:
        slug = record.get("slug", "<no-slug>")
        slug_to_sources.setdefault(slug, []).append(source)

        name = (record.get("name") or "").strip().lower()
        if name:
            name_to_slugs.setdefault(name, []).append(slug)

        if record.get("is_hidden_gem") or record.get("kind") == "hidden_gem":
            hidden_gems_count += 1
        else:
            destinations_count += 1

        missing = [f for f in REQUIRED_PLACE_FIELDS if not record.get(f) and record.get(f) != 0]
        if missing:
            missing_fields_by_place[slug] = missing

        lat, lng = record.get("lat"), record.get("lng")
        if not valid_coords(lat, lng):
            bad_coords.append(slug)

        state_slug = record.get("state_slug")
        if state_slug and state_slug not in state_slugs:
            errors.append(f"{slug} ({source}): state_slug '{state_slug}' does not exist in states.json")

        for field in LIST_FIELDS:
            value = record.get(field)
            if value is not None and not isinstance(value, list):
                errors.append(f"{slug} ({source}): field '{field}' should be a list, got {type(value).__name__}")

        overview = (record.get("overview") or "").strip()
        short_intro = (record.get("short_intro") or "").strip()
        if not overview or not short_intro:
            empty_descriptions.append(slug)
        combined = f"{overview} {short_intro}".lower()
        for phrase in placeholder_phrases:
            if phrase in combined:
                placeholder_hits.append(f"{slug}: contains generic phrase '{phrase}'")
                break

    duplicate_slugs = {s: srcs for s, srcs in slug_to_sources.items() if len(srcs) > 1}
    duplicate_names = {n: slugs for n, slugs in name_to_slugs.items() if len(slugs) > 1}

    # Trip reference validation
    place_slugs = set(slug_to_sources.keys())
    trip_ref_errors = []
    for trip in trips:
        for dest_slug in trip.get("destinations", []) or []:
            if dest_slug not in place_slugs:
                trip_ref_errors.append(f"trip '{trip.get('slug')}' references unknown destination slug '{dest_slug}'")
        for day in trip.get("itinerary", []) or []:
            for p in day.get("places", []) or []:
                if p not in place_slugs:
                    trip_ref_errors.append(
                        f"trip '{trip.get('slug')}' day {day.get('day')} references unknown place slug '{p}'"
                    )

    # ----------------------------------------------------------------- report
    print("=" * 72)
    print("ATULYA YATRA — DATA VALIDATION REPORT")
    print("=" * 72)
    print(f"States/UTs:        {len(states)}")
    print(f"Destinations:      {destinations_count}")
    print(f"Hidden gems:       {hidden_gems_count}")
    print(f"Total places:      {destinations_count + hidden_gems_count}")
    print(f"Trips:             {len(trips)}")
    print("-" * 72)

    if duplicate_slugs:
        print(f"DUPLICATE SLUGS ({len(duplicate_slugs)}):")
        for slug, srcs in duplicate_slugs.items():
            print(f"  - '{slug}' appears in: {', '.join(srcs)}")
    else:
        print("Duplicate slugs: none")

    if duplicate_names:
        print(f"\nDUPLICATE / SIMILAR NAMES ({len(duplicate_names)}):")
        for name, slugs in duplicate_names.items():
            print(f"  - '{name}' used by slugs: {', '.join(slugs)}")
    else:
        print("Duplicate names: none")

    if missing_fields_by_place:
        print(f"\nRECORDS WITH MISSING REQUIRED FIELDS ({len(missing_fields_by_place)}):")
        for slug, fields in missing_fields_by_place.items():
            print(f"  - {slug}: missing {fields}")
    else:
        print("\nMissing required fields: none")

    if bad_coords:
        print(f"\nRECORDS WITH INVALID/OUT-OF-RANGE COORDINATES ({len(bad_coords)}):")
        for slug in bad_coords:
            print(f"  - {slug}")
    else:
        print("\nInvalid coordinates: none")

    if empty_descriptions:
        print(f"\nRECORDS WITH EMPTY OVERVIEW/SHORT_INTRO ({len(empty_descriptions)}):")
        for slug in empty_descriptions:
            print(f"  - {slug}")
    else:
        print("\nEmpty descriptions: none")

    if placeholder_hits:
        print(f"\nPLACEHOLDER/GENERIC LANGUAGE DETECTED ({len(placeholder_hits)}):")
        for hit in placeholder_hits:
            print(f"  - {hit}")
    else:
        print("\nPlaceholder/generic language: none detected")

    if trip_ref_errors:
        print(f"\nINVALID TRIP REFERENCES ({len(trip_ref_errors)}):")
        for e in trip_ref_errors:
            print(f"  - {e}")
    else:
        print("\nInvalid trip references: none")

    if errors:
        print(f"\nOTHER ERRORS ({len(errors)}):")
        for e in errors:
            print(f"  - {e}")

    print("=" * 72)

    hard_failures = (
        len(duplicate_slugs) + len(missing_fields_by_place) + len(bad_coords)
        + len(trip_ref_errors) + len(errors)
    )
    if hard_failures:
        print(f"RESULT: FAILED — {hard_failures} issue(s) require attention.")
        return 1
    print("RESULT: PASSED — no blocking data issues found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
