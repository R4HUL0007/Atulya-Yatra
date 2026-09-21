"""Discover real tourism POIs with Google Places for moderated enrichment.

The script writes factual external-attraction candidates into the same regional
JSON pipeline used by the app. Google results are *never* automatically called
hidden gems: a curator must review and promote a candidate. Photo resource names
are stored without API keys; secret-bearing media URLs are never persisted.

Examples::

    python scripts/fetch_places.py --list-hubs
    python scripts/fetch_places.py --hub vadodara --dry-run
    python scripts/fetch_places.py --hub vadodara --yes
    python scripts/fetch_places.py --sanitize

Live fetches incur Google Places API charges. The script prints an estimated
call count and requires confirmation (or ``--yes``). Narrative fields that
cannot be verified from returned Places data are deliberately omitted.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atulya.data_store import DataStore  # noqa: E402
from atulya.geo import haversine_km  # noqa: E402
from config import get_config  # noqa: E402
from scripts.places_client import PlaceResult, PlacesClient, TOURISM_TYPES  # noqa: E402

DATA_DIR = ROOT / "data"
NEARBY_RADIUS_KM = 30
GEM_RADIUS_KM = 100
MAX_SINGLE_CIRCLE_KM = 50  # API hard limit is 50,000 m per Nearby Search call
DEDUPE_RADIUS_KM = 0.3  # skip a fetched POI if an existing place is this close
MAX_GEMS_PER_HUB = 12  # cap on moderated external-attraction candidates per hub
MAX_NEARBY_PER_HUB = 10

# Some results carry tourist_attraction only as a secondary type. Never publish
# commercial/lodging primary types as tourism candidates without manual review.
EXCLUDED_PRIMARY_TYPES = {
    "hotel", "resort_hotel", "lodging", "restaurant", "cafe", "bar",
    "shopping_mall", "event_venue", "wedding_venue", "store",
}

# Map a subset of Google Table A types to the app's own category vocabulary,
# so fetched places slot naturally into the existing tag/category system used
# by search.py and recommend.py. Anything unmapped falls back to "Nature".
GOOGLE_TYPE_TO_CATEGORY = {
    "tourist_attraction": "Culture",
    "historical_landmark": "Historical",
    "historical_place": "Historical",
    "museum": "Culture",
    "art_gallery": "Culture",
    "hindu_temple": "Spiritual",
    "church": "Spiritual",
    "mosque": "Spiritual",
    "national_park": "Wildlife",
    "state_park": "Nature",
    "wildlife_park": "Wildlife",
    "wildlife_refuge": "Wildlife",
    "natural_feature": "Nature",
    "scenic_spot": "Photography",
    "hiking_area": "Adventure",
    "monument": "Heritage",
    "cultural_landmark": "Culture",
}


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    text = re.sub(r"[\s_]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-") or "place"


def nearest_state(lat: float, lng: float, states: List[dict]) -> Optional[dict]:
    """Assign a state by nearest centroid distance (no paid geocoding needed)."""
    best, best_dist = None, math.inf
    for s in states:
        if not isinstance(s.get("lat"), (int, float)):
            continue
        d = haversine_km(lat, lng, s["lat"], s["lng"])
        if d < best_dist:
            best, best_dist = s, d
    return best


def categories_for(result: PlaceResult) -> List[str]:
    cats = []
    for t in result.types:
        cat = GOOGLE_TYPE_TO_CATEGORY.get(t)
        if cat and cat not in cats:
            cats.append(cat)
    if result.primary_type and GOOGLE_TYPE_TO_CATEGORY.get(result.primary_type):
        cat = GOOGLE_TYPE_TO_CATEGORY[result.primary_type]
        if cat not in cats:
            cats.insert(0, cat)
    return cats or ["Other"]


def popularity_score(result: PlaceResult) -> float:
    """0-100 popularity purely from Google's own rating * log(reviews)."""
    rating = result.rating or 3.5
    reviews = result.review_count or 0
    review_factor = min(1.0, math.log10(reviews + 1) / 4.0)  # ~10000 reviews -> 1.0
    return round(min(100.0, (rating / 5.0) * 70 + review_factor * 30), 1)


def hidden_candidate_score(result: PlaceResult, distance_km: float) -> float:
    """Transparent review score; it does not grant hidden-gem status."""
    review_count = max(0, result.review_count or 0)
    offbeat = 1.0 - min(1.0, math.log10(review_count + 1) / 4.0)
    rating_quality = max(0.0, min(1.0, ((result.rating or 3.5) - 3.0) / 2.0))
    outside_city = min(1.0, distance_km / GEM_RADIUS_KM)
    return round((offbeat * 0.5 + rating_quality * 0.3 + outside_city * 0.2) * 100, 1)


def sanitize_dynamic_records(records: List[dict]) -> List[dict]:
    """Remove persisted credentials and demote unreviewed Maps records."""
    sanitized = []
    for original in records:
        record = dict(original)
        if record.get("source") != "google_places_api":
            sanitized.append(record)
            continue
        photo_names = list(record.get("google_photo_names") or [])
        images = record.get("images") or {}
        for value in [images.get("hero"), *(images.get("gallery") or [])]:
            if not isinstance(value, str) or "/v1/places/" not in value:
                continue
            clean_path = value.split("?", 1)[0]
            resource = clean_path.split("/v1/", 1)[-1]
            if resource.endswith("/media"):
                resource = resource[:-6]
            if resource.startswith("places/") and resource not in photo_names:
                photo_names.append(resource)
        record["google_photo_names"] = photo_names
        record["images"] = {"hero": None, "gallery": []}
        record["kind"] = "external_attraction"
        record["is_hidden_gem"] = False
        record["moderation_status"] = "candidate"
        sanitized.append(record)
    return sanitized


def build_short_intro(result: PlaceResult, distance_km: float, hub_name: str) -> str:
    """A factual, non-fabricated one-line description from verifiable fields only."""
    kind = (result.primary_type or (result.types[0] if result.types else "place")).replace("_", " ")
    bits = [f"A {kind} near {hub_name}"]
    if result.rating:
        bits.append(f"rated {result.rating}/5 on Google")
        if result.review_count:
            bits.append(f"from {result.review_count:,} reviews")
    bits.append(f"about {distance_km:.1f} km away")
    return ", ".join(bits) + "."


def tiled_circle_centers(lat: float, lng: float, radius_km: float) -> List[tuple]:
    """Approximate a large-radius disk with several capped-radius circles.

    The Places API caps a single Nearby Search circle at 50 km. For the
    100 km hidden-gem radius we tile: one central 50 km circle plus six outer
    circles arranged around the origin so their union approximates the full
    disk. This is a pragmatic approximation, not exact coverage — documented
    here and in the module docstring rather than hidden.
    """
    if radius_km <= MAX_SINGLE_CIRCLE_KM:
        return [(lat, lng, radius_km)]

    centers = [(lat, lng, float(MAX_SINGLE_CIRCLE_KM))]
    ring_km = radius_km * 0.62  # empirically covers out to ~radius_km with 6 circles
    outer_radius_km = min(MAX_SINGLE_CIRCLE_KM, radius_km * 0.5)
    for bearing_deg in range(0, 360, 60):
        brg = math.radians(bearing_deg)
        dlat = (ring_km / 111.0) * math.cos(brg)
        dlng = (ring_km / (111.0 * math.cos(math.radians(lat)))) * math.sin(brg)
        centers.append((lat + dlat, lng + dlng, outer_radius_km))
    return centers


def estimate_call_count(hubs: List[dict]) -> int:
    calls_per_hub = 1 + len(tiled_circle_centers(0, 0, GEM_RADIUS_KM))  # 30km + tiled 100km
    return calls_per_hub * len(hubs)


def fetch_for_hub(client: PlacesClient, hub: dict, existing_places: List[dict]) -> List[dict]:
    """Query Places API around one hub and return moderated POI candidates."""
    lat, lng = hub["lat"], hub["lng"]
    seen_place_ids: set = set()
    collected: List[PlaceResult] = []

    # 30 km ring — genuine nearby attractions.
    for r in client.search_nearby(lat, lng, NEARBY_RADIUS_KM * 1000, included_types=TOURISM_TYPES, max_results=20):
        if r.place_id not in seen_place_ids:
            seen_place_ids.add(r.place_id)
            collected.append(r)

    # 100 km ring (tiled) — hidden-gem candidate pool.
    for clat, clng, cradius in tiled_circle_centers(lat, lng, GEM_RADIUS_KM):
        for r in client.search_nearby(clat, clng, cradius * 1000, included_types=TOURISM_TYPES, max_results=20):
            if r.place_id not in seen_place_ids:
                seen_place_ids.add(r.place_id)
                collected.append(r)

    records = []
    for r in collected:
        if not r.name or not r.lat or not r.lng:
            continue
        dist = haversine_km(lat, lng, r.lat, r.lng)
        if dist > GEM_RADIUS_KM:
            continue  # tiling can overshoot slightly; enforce the true radius

        # Dedupe against anything already in the dataset (hand-authored or
        # previously fetched), by proximity so we don't create near-duplicates
        # of e.g. "Fatehpur Sikri" under two different slugs.
        too_close = any(
            haversine_km(r.lat, r.lng, p["lat"], p["lng"]) < DEDUPE_RADIUS_KM
            for p in existing_places
            if isinstance(p.get("lat"), (int, float))
        )
        if too_close:
            continue

        if r.primary_type in EXCLUDED_PRIMARY_TYPES:
            continue
        categories = categories_for(r)
        candidate_score = hidden_candidate_score(r, dist)
        record = {
            "slug": f"{slugify(r.name)}-{r.place_id[-6:].lower()}",
            "name": r.name,
            "kind": "external_attraction",
            "is_hidden_gem": False,
            "moderation_status": "candidate",
            "hidden_candidate_score": candidate_score,
            "source": "google_places_api",
            "google_place_id": r.place_id,
            "google_photo_names": [client.photo_resource(name) for name in r.photo_names if client.photo_resource(name)],
            "lat": round(r.lat, 6),
            "lng": round(r.lng, 6),
            "type": (r.primary_type or "attraction").replace("_", " "),
            "categories": categories,
            "tags": categories + (["Offbeat candidate"] if candidate_score >= 60 else []),
            "rating": r.rating,
            "reviews": r.review_count,
            "popularity": popularity_score(r),
            "data_quality": 0.55,
            "short_intro": build_short_intro(r, dist, hub["name"]),
            "overview": build_short_intro(r, dist, hub["name"]),
            "address": r.address,
            "google_maps_url": r.maps_url,
            "nearby_major_destination": hub["slug"],
            "images": {"hero": None, "gallery": []},
        }
        records.append(record)

    records.sort(key=lambda x: (-x["hidden_candidate_score"], -(x.get("rating") or 0)))
    return records[:MAX_GEMS_PER_HUB]


def attach_state(record: dict, states: List[dict]) -> None:
    state = nearest_state(record["lat"], record["lng"], states)
    if state:
        record["state"] = state["name"]
        record["state_slug"] = state["slug"]
        record["region"] = state.get("intro", "")[:0] or ""  # left blank; not fabricated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", help="Only fetch around this destination slug.")
    parser.add_argument("--all", action="store_true", help="Fetch around every existing destination hub.")
    parser.add_argument("--max-hubs", type=int, default=5, help="Cap on number of hubs processed with --all.")
    parser.add_argument("--list-hubs", action="store_true", help="List available destination hubs and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched without calling the API.")
    parser.add_argument("--sanitize", action="store_true", help="Remove persisted photo keys and demote unreviewed Maps records without API calls.")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive cost confirmation.")
    parser.add_argument(
        "--out",
        default=str(DATA_DIR / "hidden_gems_dynamic.json"),
        help="Output file (merged automatically by the app's glob loader).",
    )
    args = parser.parse_args()

    if args.sanitize:
        out_path = Path(args.out)
        records = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
        sanitized = sanitize_dynamic_records(records)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Sanitized {len(sanitized)} record(s) in {out_path}; no API calls were made.")
        return 0

    store = DataStore(DATA_DIR)
    store.load()
    destinations = store.destinations()
    states = store.all_states()

    if args.list_hubs:
        for d in sorted(destinations, key=lambda x: x["name"]):
            print(f"  {d['slug']:<28} {d['name']:<28} {d.get('state','')}")
        return 0

    if args.hub:
        hubs = [d for d in destinations if d["slug"] == args.hub]
        if not hubs:
            print(f"No destination with slug '{args.hub}'. Use --list-hubs to see options.")
            return 1
    elif args.all:
        hubs = destinations[: args.max_hubs]
    else:
        print("Specify --hub <slug>, --all, or --list-hubs. See --help.")
        return 1

    est_calls = estimate_call_count(hubs)
    print(f"Hubs to process: {len(hubs)}  ->  estimated Places API calls: ~{est_calls}")
    print("Each call is a standard Nearby Search (New) request; billed per Google's Places API pricing.")

    if args.dry_run:
        print("Dry run — no API calls made.")
        for h in hubs:
            print(f"  Would query around {h['name']} ({h['lat']}, {h['lng']})")
        return 0

    if not args.yes:
        confirm = input(f"Proceed with ~{est_calls} live Places API calls? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted — no API calls made.")
            return 0

    cfg = get_config()
    api_key = getattr(cfg, "GOOGLE_MAPS_API_KEY", "")
    if not api_key:
        print("ERROR: no Google Maps API key found. Set GOOGLE_MAPS_API_KEY (or Google_maps) in .env.")
        return 1

    client = PlacesClient(api_key)
    all_places = store.all_places()

    out_path = Path(args.out)
    existing_dynamic = []
    if out_path.exists():
        existing_dynamic = sanitize_dynamic_records(json.loads(out_path.read_text(encoding="utf-8")))
    existing_by_place_id = {r.get("google_place_id") for r in existing_dynamic if r.get("google_place_id")}

    new_records: List[dict] = []
    for hub in hubs:
        print(f"Fetching around {hub['name']} ...")
        try:
            records = fetch_for_hub(client, hub, all_places + existing_dynamic + new_records)
        except Exception as exc:  # noqa: BLE001 - surface API errors clearly, keep going
            print(f"  ERROR fetching {hub['name']}: {exc}")
            continue
        added = 0
        for rec in records:
            if rec["google_place_id"] in existing_by_place_id:
                continue
            attach_state(rec, states)
            new_records.append(rec)
            existing_by_place_id.add(rec["google_place_id"])
            added += 1
        print(f"  -> {added} new place(s) discovered")

    combined = existing_dynamic + new_records
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(new_records)} new record(s) ({len(combined)} total) to {out_path}")
    print("Run scripts/geo_audit.py to see updated coverage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
