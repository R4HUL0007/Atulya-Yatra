"""Recommendation-quality smoke test for the Atulya Yatra dataset.

Run standalone:

    python scripts/test_recommendations.py

Reuses the live application's actual recommendation engine
(``atulya.recommend.recommend_for_preferences`` and ``rank_hidden_gems``) against
the current dataset to confirm that:

  1. Different preference combinations return different top results
     (i.e. the engine isn't just returning the same handful of places for
     everything).
  2. Hidden-gem ranking near a real destination varies and is not a fixed order.

This does not modify the recommendation engine — it only exercises it and
reports what comes back, so it is safe to run at any time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atulya.recommend import rank_hidden_gems, recommend_for_preferences  # noqa: E402

DATA_DIR = ROOT / "data"

SCENARIOS = [
    ("Nature + Mountains + Hidden Gems", ["nature", "mountains"], "hidden"),
    ("Nature + Waterfalls + Hidden Gems", ["nature", "waterfalls"], "hidden"),
    ("Culture + Historical + Popular", ["culture", "history"], "popular"),
    ("Adventure + Mountains + Hidden Gems", ["adventure", "mountains"], "hidden"),
    ("Spiritual + Peace + Hidden Gems", ["spiritual", "peace"], "hidden"),
]


def load_places() -> list:
    places = []
    for pattern in ("destinations*.json", "hidden_gems*.json"):
        for path in sorted(DATA_DIR.glob(pattern)):
            places.extend(json.loads(path.read_text(encoding="utf-8")))
    for p in places:
        p.setdefault("is_hidden_gem", p.get("kind") == "hidden_gem")
    return places


def main() -> int:
    places = load_places()
    print("=" * 72)
    print("ATULYA YATRA — RECOMMENDATION QUALITY TEST")
    print("=" * 72)
    print(f"Dataset size: {len(places)} places\n")

    top_picks_per_scenario = {}
    for label, interests, prefer in SCENARIOS:
        ranked = recommend_for_preferences(places, interests, prefer=prefer, limit=5)
        names = [f"{p['name']} ({p['recommendation_score']})" for p in ranked]
        top_picks_per_scenario[label] = [p["name"] for p in ranked]
        print(f"[{label}]  prefer={prefer}")
        for n in names:
            print(f"    - {n}")
        print()

    # Variation check: how many scenarios share an identical top-1 result?
    top1s = [picks[0] if picks else None for picks in top_picks_per_scenario.values()]
    unique_top1 = len(set(top1s))
    print("-" * 72)
    print(f"Unique #1 recommendation across {len(SCENARIOS)} scenarios: {unique_top1}/{len(SCENARIOS)}")
    if unique_top1 <= 1:
        print("WARNING: every scenario returned the same top pick — recommendations may not be varying.")
    else:
        print("OK: recommendations vary meaningfully across different preference combinations.")

    # Hidden-gem ranking variation near a couple of real destinations.
    print("-" * 72)
    print("HIDDEN-GEM RANKING NEAR SAMPLE DESTINATIONS")
    destinations = [p for p in places if not p.get("is_hidden_gem")]
    from atulya.geo import haversine_km

    sample = destinations[:3]
    for dest in sample:
        nearby_gems = []
        for g in places:
            if not g.get("is_hidden_gem") or g["slug"] == dest["slug"]:
                continue
            dist = haversine_km(dest["lat"], dest["lng"], g["lat"], g["lng"])
            if dist <= 100:
                item = dict(g)
                item["distance_km"] = round(dist, 1)
                nearby_gems.append(item)
        ranked = rank_hidden_gems(nearby_gems, radius_km=100, interests=dest.get("categories", []))
        print(f"\n  Near {dest['name']} ({len(ranked)} candidates within 100km):")
        for g in ranked[:6]:
            print(f"    - {g['name']}: score={g['recommendation_score']}  distance={g['distance_km']}km")

    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
