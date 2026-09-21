"""Geographic coverage audit for the Atulya Yatra tourism dataset.

Run standalone:

    python scripts/geo_audit.py

Reuses the exact same haversine distance function the live application uses
(``atulya.geo.haversine_km``) so this audit reflects real production behaviour,
not an approximation. Reports, per destination:

  - nearby attractions within 30 km (the same radius the destination page uses)
  - hidden gems within 100 km (the same radius the destination page uses)

And aggregates:

  - how many destinations have 5+ attractions within 30 km
  - how many destinations have 4+ hidden gems within 100 km
  - which destinations still have empty/thin sections
  - per-state destination/hidden-gem counts
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atulya.geo import haversine_km  # noqa: E402  (after sys.path tweak)

DATA_DIR = ROOT / "data"

NEARBY_RADIUS_KM = 30
GEM_RADIUS_KM = 100
NEARBY_GOOD_THRESHOLD = 5
GEM_GOOD_THRESHOLD = 4


def load_places() -> list:
    places = []
    for pattern in ("destinations*.json", "hidden_gems*.json"):
        for path in sorted(DATA_DIR.glob(pattern)):
            data = json.loads(path.read_text(encoding="utf-8"))
            places.extend(data)
    for p in places:
        p.setdefault("is_hidden_gem", p.get("kind") == "hidden_gem")
    return places


def load_states() -> list:
    path = DATA_DIR / "states.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    places = load_places()
    states = load_states()
    destinations = [p for p in places if not p.get("is_hidden_gem")]
    gems = [p for p in places if p.get("is_hidden_gem")]

    print("=" * 72)
    print("ATULYA YATRA — GEOGRAPHIC COVERAGE AUDIT")
    print("=" * 72)
    print(f"Destinations: {len(destinations)}   Hidden gems: {len(gems)}")
    print(f"Nearby-attraction radius: {NEARBY_RADIUS_KM} km   Hidden-gem radius: {GEM_RADIUS_KM} km")
    print("-" * 72)

    good_nearby = 0
    good_gems = 0
    empty_nearby = []
    empty_gems = []
    thin_gems = []  # 1-3 gems, technically non-empty but below the "meaningful pool" bar

    rows = []
    for d in destinations:
        near = 0
        gem_count = 0
        for other in places:
            if other["slug"] == d["slug"]:
                continue
            if not all(isinstance(other.get(k), (int, float)) for k in ("lat", "lng")):
                continue
            dist = haversine_km(d["lat"], d["lng"], other["lat"], other["lng"])
            if dist <= NEARBY_RADIUS_KM:
                near += 1
            if other.get("is_hidden_gem") and dist <= GEM_RADIUS_KM:
                gem_count += 1

        rows.append((d["name"], d.get("state"), near, gem_count))
        if near >= NEARBY_GOOD_THRESHOLD:
            good_nearby += 1
        if near == 0:
            empty_nearby.append(d["name"])
        if gem_count >= GEM_GOOD_THRESHOLD:
            good_gems += 1
        if gem_count == 0:
            empty_gems.append(d["name"])
        elif gem_count < GEM_GOOD_THRESHOLD:
            thin_gems.append((d["name"], gem_count))

    rows.sort(key=lambda r: r[0])
    print(f"{'Destination':<28}{'State':<22}{'Nearby(30km)':<14}{'Gems(100km)'}")
    for name, state, near, gem_count in rows:
        print(f"{name:<28}{(state or ''):<22}{near:<14}{gem_count}")

    print("-" * 72)
    total = len(destinations) or 1
    print(f"Destinations with >= {NEARBY_GOOD_THRESHOLD} nearby attractions (30km): {good_nearby}/{len(destinations)} ({good_nearby/total:.0%})")
    print(f"Destinations with >= {GEM_GOOD_THRESHOLD} hidden gems (100km):        {good_gems}/{len(destinations)} ({good_gems/total:.0%})")
    print(f"Destinations with ZERO nearby attractions (30km): {len(empty_nearby)} -> {empty_nearby}")
    print(f"Destinations with ZERO hidden gems (100km):       {len(empty_gems)} -> {empty_gems}")
    if thin_gems:
        print(f"Destinations with 1-{GEM_GOOD_THRESHOLD - 1} hidden gems (below target): {thin_gems}")

    # Per-state coverage
    print("-" * 72)
    print("PER-STATE COVERAGE")
    dest_by_state = defaultdict(int)
    gem_by_state = defaultdict(int)
    for d in destinations:
        dest_by_state[d.get("state_slug")] += 1
    for g in gems:
        gem_by_state[g.get("state_slug")] += 1

    states_with_zero_destinations = []
    for s in sorted(states, key=lambda s: s.get("name", "")):
        slug = s["slug"]
        dc, gc = dest_by_state.get(slug, 0), gem_by_state.get(slug, 0)
        marker = "  <-- NO DESTINATIONS" if dc == 0 else ""
        print(f"  {s['name']:<40} destinations={dc:<3} hidden_gems={gc:<3}{marker}")
        if dc == 0:
            states_with_zero_destinations.append(s["name"])

    print("-" * 72)
    print(f"States/UTs with ZERO destinations: {len(states_with_zero_destinations)}/{len(states)}")
    if states_with_zero_destinations:
        print(f"  -> {states_with_zero_destinations}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
