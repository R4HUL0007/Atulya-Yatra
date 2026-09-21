"""Server-rendered page routes."""
from __future__ import annotations

import copy
import random
import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from ..extensions import db
from ..geo import has_coords
from ..hidden_gems import collect_scored_gems, gem_view, travel_time_estimate
from ..images import (
    entity_photo_credit,
    external_photo_url,
    has_usable_hero,
    named_image_url,
    resolve_entity_gallery,
)
from ..models import ContactMessage, Feedback, HiddenGemSubmission
from ..recommend import recommend_for_preferences
from ..state_discovery import (
    DESTINATION_QUERIES,
    OFFBEAT_QUERIES,
    discover_state_places,
    discovered_card,
)

pages_bp = Blueprint("pages", __name__)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _store():
    return current_app.data_store


def _trips():
    return current_app.trip_catalog


def _cfg(key):
    return current_app.config[key]


# ----------------------------------------------------------------------- home
_NATURE_WORDS = {
    "hill station", "hill-station", "mountains", "mountain", "nature", "valley",
    "wildlife", "tea", "forest", "waterfall", "national park", "greenery",
    "hills", "scenic", "trek", "trekking", "backwaters", "meadow", "lake",
    "river", "beach", "island", "glacier", "snow", "desert",
}


def _is_nature(place):
    haystack = {str(c).lower() for c in place.get("categories", []) or []}
    haystack |= {str(t).lower() for t in place.get("tags", []) or []}
    return any(word in haystack for word in _NATURE_WORDS)


def _enrich_from_places(place):
    """Overlay live Google Places rating/reviews onto a curated record.

    Keeps displayed facts correct without inventing anything: only real values
    returned by Places are applied, and the resolver caches per process so
    repeat loads cost nothing.
    """
    resolver = getattr(current_app, "places_resolver", None)
    if not resolver or not resolver.enabled:
        return place
    query = ", ".join(p for p in (place.get("name"), place.get("state")) if p)
    resolved = resolver.resolve(query)
    if resolved:
        if resolved.get("rating") is not None:
            place["rating"] = resolved["rating"]
        if resolved.get("reviews") is not None:
            place["reviews"] = resolved["reviews"]
        place["verified"] = True
    return place


_HERO_SCENIC = {
    "mountains", "mountain", "hill station", "hill-station", "valley", "lake",
    "river", "waterfall", "beach", "nature", "backwaters", "meadow", "forest",
    "tea", "hills", "snow", "glacier", "island", "scenic",
}

# The homepage leads with landscape, not with cities: these are the four kinds of
# scenery people actually pick a trip for. Each theme gets one photogenic
# representative so the section is a picture of a place, not a category chip.
LANDSCAPE_THEMES = (
    {
        "key": "mountains",
        "title": "Mountains & valleys",
        "blurb": "Cold deserts, high passes and green valleys from the Himalaya to the Western Ghats.",
        "category": "mountains",
        "words": {
            "mountains", "mountain", "hill station", "hill-station", "valley",
            "snow", "glacier", "trek", "trekking", "hills", "pass",
        },
    },
    {
        "key": "water",
        "title": "Rivers & lakes",
        "blurb": "Backwaters, gorges and still water that holds the whole sky.",
        "category": "lake",
        "words": {"river", "lake", "waterfall", "backwaters", "wetland", "falls"},
    },
    {
        "key": "beaches",
        "title": "Beaches & islands",
        "blurb": "Coral shallows, palm coastline and islands you have to sail to.",
        "category": "beach",
        "words": {"beach", "island", "coast", "sea", "coral"},
    },
    {
        "key": "wild",
        "title": "Forests & wildlife",
        "blurb": "Tiger country, tea slopes, mangrove channels and meadow grassland.",
        "category": "wildlife",
        "words": {
            "wildlife", "forest", "national park", "tea", "meadow", "nature",
            "sanctuary", "reserve",
        },
    },
)


def _theme_match(place, words):
    haystack = {str(c).lower() for c in place.get("categories", []) or []}
    haystack |= {str(t).lower() for t in place.get("tags", []) or []}
    haystack.add(str(place.get("type") or "").lower())
    return any(word in haystack for word in words)


def _has_curated_photo(place):
    """True when a reviewed hero photo is really on disk for this record.

    Checking the declared path is not enough: every record was authored with a
    hero path, but only the ones the enrichment script resolved have a file.
    Sections lead with these so a prominent tile never shows a placeholder.
    """
    return has_usable_hero(place, "place")


def _landscape_showcase(places):
    """One photogenic representative per landscape theme, never repeating a place."""
    used = set()
    showcase = []
    for theme in LANDSCAPE_THEMES:
        pool = [
            p for p in places
            if p.get("slug") not in used and _theme_match(p, theme["words"])
        ]
        if not pool:
            continue
        pool.sort(
            key=lambda p: (_has_curated_photo(p), p.get("popularity", 0) or 0),
            reverse=True,
        )
        pick = pool[0]
        used.add(pick.get("slug"))
        showcase.append(
            {
                "key": theme["key"],
                "title": theme["title"],
                "blurb": theme["blurb"],
                "category": theme["category"],
                "place": copy.deepcopy(pick),
            }
        )
    return showcase


def _scenic_first(places, limit):
    """Rank scenery above city size, then prefer records with a real photo."""
    return [
        copy.deepcopy(p)
        for p in sorted(
            places,
            key=lambda p: (
                _is_nature(p),
                _has_curated_photo(p),
                p.get("popularity", 0) or 0,
            ),
            reverse=True,
        )[:limit]
    ]


def _hero_candidates(destinations, count=8):
    """A rotating set of the most scenic, photogenic destinations for the hero.

    Biases to rivers, beaches, mountains and valleys (the imagery that pulls
    travellers in) and to higher-popularity places, which reliably have good
    Google Places photos — then shuffles so each visit feels fresh.
    """
    def scenic(place):
        hay = {str(c).lower() for c in place.get("categories", []) or []}
        hay |= {str(t).lower() for t in place.get("tags", []) or []}
        return any(word in hay for word in _HERO_SCENIC)

    # A hero fills the whole screen, so a placeholder there is the worst case.
    # Records with a reviewed photo are used first and only exhausted ones fall
    # back, in this order: scenic with a photo, any place with a photo, scenic.
    by_popularity = lambda pool: sorted(  # noqa: E731 - short local ordering key
        pool, key=lambda p: p.get("popularity", 0) or 0, reverse=True
    )
    scenic_shot = by_popularity([p for p in destinations if scenic(p) and _has_curated_photo(p)])
    other_shot = by_popularity([p for p in destinations if _has_curated_photo(p) and not scenic(p)])
    scenic_unshot = by_popularity([p for p in destinations if scenic(p) and not _has_curated_photo(p)])

    pool = list(scenic_shot)
    for extra in (other_shot, scenic_unshot, by_popularity(destinations)):
        if len(pool) >= count:
            break
        for place in extra:
            if place not in pool:
                pool.append(place)

    # Shuffle only within the photo-backed band so every visit feels fresh
    # without promoting a placeholder into the hero.
    shot_count = len(scenic_shot) or len(pool)
    top = pool[: max(count, min(shot_count, max(count * 2, 14)))]
    random.shuffle(top)
    return top[:count]


def _best_hidden_gems(gems, count=6):
    """The most attractive nature-forward hidden gems (the homepage USP).

    Records with a reviewed photo lead, because a gem shown as a placeholder
    tile persuades nobody.
    """
    def rank(pool):
        return sorted(
            pool,
            key=lambda g: ((g.get("rating") or 0), (g.get("popularity") or 0)),
            reverse=True,
        )

    # Scenic gems that have a photo first, then any gem with a photo, and only
    # then records that would render as a placeholder.
    scenic_shot = [g for g in gems if _has_curated_photo(g) and _is_nature(g)]
    other_shot = [g for g in gems if _has_curated_photo(g) and not _is_nature(g)]
    unshot = [g for g in gems if not _has_curated_photo(g)]
    return (rank(scenic_shot) + rank(other_shot) + rank(unshot))[:count]


@pages_bp.route("/")
def index():
    store = _store()
    destinations = store.destinations()

    gems = store.hidden_gems()

    hero_candidates = [copy.deepcopy(p) for p in _hero_candidates(destinations)]

    # Landscape first: the reason someone books a trip is the scenery, so the
    # page opens on mountains, water, coast and forest rather than on city size.
    landscape = _landscape_showcase(destinations + gems)

    trending = _scenic_first(destinations, _cfg("TRENDING_LIMIT"))
    featured_gems = [copy.deepcopy(g) for g in _best_hidden_gems(gems)]

    # Keep the headline facts correct with live Places data. The resolver
    # caches per process, so this is a one-off cost after a restart. Records
    # are already deep copies, so the shared in-memory store is never mutated.
    for record in featured_gems + trending:
        _enrich_from_places(record)

    # The full-width photographic break between grids. Using a curated record
    # keeps it working without the Places key and avoids a placeholder banner.
    shown = {p.get("slug") for p in hero_candidates}
    shown |= {t["place"].get("slug") for t in landscape}
    band_pool = [
        p for p in destinations + gems
        if _has_curated_photo(p) and _is_nature(p) and p.get("slug") not in shown
    ]
    band_pool.sort(key=lambda p: p.get("popularity", 0) or 0, reverse=True)
    band_place = copy.deepcopy(band_pool[0]) if band_pool else None

    # States with a reviewed photo lead, so the grid is never half placeholders.
    states = sorted(
        store.all_states(),
        key=lambda s: has_usable_hero(s, "state"),
        reverse=True,
    )

    return render_template(
        "index.html",
        hero_candidates=hero_candidates,
        hero_place=hero_candidates[0] if hero_candidates else None,
        landscape=landscape,
        band_place=band_place,
        trending=trending,
        featured_gems=featured_gems,
        states=states,
        state_total=len(store.all_states()),
        trips=_trips().all_trips()[:3],
    )


# --------------------------------------------------------------- destinations
@pages_bp.route("/destinations")
def destinations():
    store = _store()
    category = request.args.get("category", "").strip().lower()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 12

    # Top destinations across India (hidden gems have their own dynamic system).
    items = store.destinations()

    # Build type-filter chips from the most common curated categories.
    counter: dict[str, int] = {}
    for place in items:
        for cat in place.get("categories", []) or []:
            key = str(cat).lower()
            counter[key] = counter.get(key, 0) + 1
    filter_options = [c for c, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:10]]

    if category:
        items = [
            p
            for p in items
            if category in {c.lower() for c in p.get("categories", [])}
            or category in {t.lower() for t in p.get("tags", [])}
        ]

    items.sort(key=lambda p: p.get("popularity", 0), reverse=True)

    total = len(items)
    start = (page - 1) * per_page
    paged = items[start : start + per_page]
    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "destinations.html",
        places=paged,
        category=category,
        filter_options=filter_options,
        page=page,
        total_pages=total_pages,
        total=total,
    )


@pages_bp.route("/destination/<slug>")
def destination(slug):
    store = _store()
    place = store.get_place(slug)
    if not place:
        abort(404)

    # Work on a private copy: it's enriched with live Places facts below and
    # must never mutate the shared, process-wide curated record.
    place = copy.deepcopy(place)
    _enrich_from_places(place)

    nearby = store.within_radius(
        place, _cfg("NEARBY_ATTRACTION_RADIUS_KM"), exclude_slug=slug
    )
    nearby_attractions = nearby[: _cfg("NEARBY_ATTRACTION_LIMIT")]

    # Dynamic Nearby Hidden Gems (100 km): curated + community + Maps candidates,
    # scored and ranked. Kept entirely separate from the 30 km attractions above.
    scored_gems = collect_scored_gems(
        place, radius_km=_cfg("HIDDEN_GEM_RADIUS_KM"), exclude_slug=slug
    )
    gem_preview_limit = _cfg("HIDDEN_GEM_PREVIEW_LIMIT")
    hidden_gems_preview = [gem_view(g) for g in scored_gems[:gem_preview_limit]]
    has_more_gems = len(scored_gems) > gem_preview_limit

    similar = _similar_places(store, place, limit=_cfg("SIMILAR_LIMIT"))

    feedbacks = (
        Feedback.query.filter_by(place_slug=slug)
        .order_by(Feedback.created_at.desc())
        .limit(20)
        .all()
    )

    state = store.get_state(place.get("state_slug"))

    # Real, distinct photos only. Indexing into the hero used to repeat one file
    # for every tile, so the gallery is resolved from the sources that genuinely
    # hold multiple images.
    gallery = resolve_entity_gallery(place, "place", limit=10, width=1600)

    # If this destination has no culture/food/festival write-up of its own yet,
    # fall back to the trusted state-level guide rather than leaving the tab
    # silently empty.
    state_fallback = None
    if not (place.get("culture") or place.get("festivals") or place.get("food")):
        state_fallback = store.state_guide(place.get("state_slug"))

    return render_template(
        "destination.html",
        place=place,
        state=state,
        nearby_attractions=nearby_attractions,
        hidden_gems=hidden_gems_preview,
        has_more_gems=has_more_gems,
        gem_radius=_cfg("HIDDEN_GEM_RADIUS_KM"),
        nearby_radius=_cfg("NEARBY_ATTRACTION_RADIUS_KM"),
        similar=similar,
        feedbacks=feedbacks,
        food_limit=_cfg("FOOD_LIMIT"),
        festival_limit=_cfg("FESTIVAL_LIMIT"),
        activity_limit=_cfg("ACTIVITY_LIMIT"),
        gallery=gallery,
        state_fallback=state_fallback,
    )


@pages_bp.route("/destination/<slug>/feedback", methods=["POST"])
def submit_feedback(slug):
    store = _store()
    if not store.get_place(slug):
        abort(404)
    name = request.form.get("name", "").strip()
    comment = request.form.get("comment", "").strip()
    rating = request.form.get("rating", type=int)
    if not name or not comment:
        flash("Please add your name and a comment.", "error")
    else:
        db.session.add(
            Feedback(place_slug=slug, name=name, comment=comment, rating=rating)
        )
        db.session.commit()
        flash("Thanks for sharing your experience!", "success")
    return redirect(url_for("pages.destination", slug=slug) + "#feedback")


# --------------------------------------------------------------------- states
@pages_bp.route("/states")
def states():
    store = _store()
    all_states = store.all_states()
    grouped = {
        "State": [s for s in all_states if s.get("type") != "union_territory"],
        "Union Territory": [
            s for s in all_states if s.get("type") == "union_territory"
        ],
    }

    # A real photograph for the header rather than a live text lookup, which
    # returned whatever Places had for "India tourism".
    preferred = ("himachal-pradesh", "sikkim", "meghalaya", "kerala", "uttarakhand")
    hero_state = None
    for slug in preferred:
        candidate = store.get_state(slug)
        if candidate and has_usable_hero(candidate, "state"):
            hero_state = candidate
            break
    if hero_state is None:
        hero_state = next(
            (s for s in all_states if has_usable_hero(s, "state")), None
        )

    return render_template(
        "states.html",
        grouped=grouped,
        total=len(all_states),
        hero_state=hero_state,
    )


def _state_or_404(slug):
    state_obj = _store().get_state(slug)
    if not state_obj:
        abort(404)
    return state_obj


def _curated_state_places(slug):
    """Curated destinations and hidden gems for a state, best first."""
    store = _store()
    major = sorted(
        store.destinations_in_state(slug),
        key=lambda p: p.get("popularity", 0),
        reverse=True,
    )
    gems = sorted(
        store.hidden_gems_in_state(slug),
        key=lambda p: p.get("rating", 0) or 0,
        reverse=True,
    )
    return major, gems


def _state_context(state_obj):
    """Context every state sub-page shares: the hero record and its sub-nav.

    The sub-nav deliberately links to real pages rather than in-page anchors, so
    each part of a state gets its own URL, title and back-button entry the same
    way a searched place does.
    """
    slug = state_obj["slug"]
    trips = _trips().trips_in_state(slug)
    has_culture = bool(
        state_obj.get("culture")
        or state_obj.get("festivals")
        or state_obj.get("famous_food")
    )
    nav = [
        ("pages.state", "Overview", True),
        ("pages.state_destinations", "Destinations", True),
        ("pages.state_gems", "Hidden gems", True),
        ("pages.state_gallery", "Gallery", True),
        ("pages.state_culture", "Culture & food", has_culture),
        ("pages.state_trips", "Trips", bool(trips)),
        ("pages.state_tips", "Travel tips", True),
    ]
    return {
        "state": state_obj,
        "state_nav": [
            {"endpoint": endpoint, "label": label}
            for endpoint, label, show in nav
            if show
        ],
        "state_trips": trips,
    }


def _discovered_for_state(state_obj, queries, *, limit, exclude=()):
    """Live Places results for a state, excluding anything already curated."""
    return discover_state_places(
        state_obj,
        queries=queries,
        limit=limit,
        exclude_names=[p.get("name") for p in exclude],
    )


@pages_bp.route("/state/<slug>")
def state(slug):
    """State overview: a short, fast introduction that links out to the rest."""
    state_obj = _state_or_404(slug)
    major, gems = _curated_state_places(slug)
    context = _state_context(state_obj)

    return render_template(
        "state.html",
        highlights=major[:6],
        gem_highlights=gems[:3],
        curated_total=len(major) + len(gems),
        **context,
    )


@pages_bp.route("/state/<slug>/destinations")
def state_destinations(slug):
    """Every place worth visiting in the state, curated plus live discovery."""
    state_obj = _state_or_404(slug)
    major, gems = _curated_state_places(slug)
    discovered = _discovered_for_state(
        state_obj, DESTINATION_QUERIES, limit=72, exclude=major + gems
    )

    page = max(1, request.args.get("page", 1, type=int))
    per_page = 24
    total_pages = max(1, (len(discovered) + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    cards = [discovered_card(c) for c in discovered[start : start + per_page]]

    return render_template(
        "state_destinations.html",
        curated=major,
        discovered=cards,
        discovered_total=len(discovered),
        page=page,
        total_pages=total_pages,
        **_state_context(state_obj),
    )


@pages_bp.route("/state/<slug>/hidden-gems")
def state_gems(slug):
    """Quieter places across the state: curated gems plus offbeat discovery."""
    state_obj = _state_or_404(slug)
    major, gems = _curated_state_places(slug)
    discovered = _discovered_for_state(
        state_obj, OFFBEAT_QUERIES, limit=36, exclude=major + gems
    )
    cards = [discovered_card(c, label="Worth Discovering") for c in discovered]

    return render_template(
        "state_gems.html",
        curated=gems,
        discovered=cards,
        **_state_context(state_obj),
    )


@pages_bp.route("/state/<slug>/gallery")
def state_gallery(slug):
    """A photo-led tour of the state, curated records first."""
    state_obj = _state_or_404(slug)
    major, gems = _curated_state_places(slug)
    curated_tiles = major + gems
    discovered = _discovered_for_state(
        state_obj, DESTINATION_QUERIES, limit=36, exclude=curated_tiles
    )

    return render_template(
        "state_gallery.html",
        curated_tiles=curated_tiles,
        discovered_tiles=[discovered_card(c) for c in discovered],
        **_state_context(state_obj),
    )


@pages_bp.route("/state/<slug>/culture")
def state_culture(slug):
    """Culture, festivals and food for the state."""
    state_obj = _state_or_404(slug)
    return render_template(
        "state_culture.html",
        guide=_store().state_guide(slug),
        **_state_context(state_obj),
    )


@pages_bp.route("/state/<slug>/trips")
def state_trips(slug):
    """Curated itineraries that run through this state."""
    state_obj = _state_or_404(slug)
    return render_template("state_trips.html", **_state_context(state_obj))


@pages_bp.route("/state/<slug>/travel-tips")
def state_tips(slug):
    """Practical planning information and neighbouring states."""
    state_obj = _state_or_404(slug)
    store = _store()
    nearby_states = [
        s
        for s in (store.get_state(ns) for ns in state_obj.get("nearby_states", []))
        if s
    ]
    return render_template(
        "state_tips.html",
        nearby_states=nearby_states,
        **_state_context(state_obj),
    )


# ----------------------------------------------------- dynamic place detail
def _live_attraction_view(candidate: dict) -> dict:
    """Compact card for a Google Places attraction on a live place page."""
    names = candidate.get("google_photo_names") or []
    image = external_photo_url(names[0]) if names else None
    if not image:
        image = named_image_url(
            candidate.get("name") or "", context=candidate.get("address") or ""
        )
    name = candidate.get("name") or ""
    return {
        "name": name,
        "type": (candidate.get("type") or "").replace("_", " ").title(),
        "rating": candidate.get("rating"),
        "reviews": candidate.get("reviews"),
        "distance_km": candidate.get("distance_km"),
        "travel_time": travel_time_estimate(candidate.get("distance_km")),
        "image": image,
        "explore_url": url_for("pages.place", q=name) if name else None,
        "raw": candidate,
    }


@pages_bp.route("/place")
def place():
    """A focused detail page for any searched Indian location.

    Strong matches jump straight to the curated guide. Otherwise the location is
    resolved live through Google Places and shown as a real, self-contained
    detail page (never the chatbot). Falls back to curated search when Places is
    unavailable or the query cannot be resolved.
    """
    query = request.args.get("q", "").strip()[:160]
    if not query:
        return redirect(url_for("pages.destinations"))

    search_service = current_app.search_service
    match = search_service.best_match(query)
    if match and match["type"] == "state" and match["score"] >= 88:
        return redirect(url_for("pages.state", slug=match["slug"]))
    if match and match["type"] != "state" and match["score"] >= 82:
        return redirect(url_for("pages.destination", slug=match["slug"]))

    resolver = getattr(current_app, "places_resolver", None)
    resolved = resolver.resolve(query) if resolver and resolver.enabled else None
    if not resolved or not has_coords(resolved):
        grouped = search_service.search(query, limit=30)
        return render_template(
            "search_results.html", query=query, grouped=grouped, live_unavailable=True
        )

    # Live gallery (trusted Google photo resources only) — show every real
    # photo Places returned for this location rather than a fixed short list.
    photo_names = resolved.get("google_photo_names") or []
    gallery = [url for url in (external_photo_url(n, width=2000) for n in photo_names) if url]
    hero_image = gallery[0] if gallery else named_image_url(
        resolved.get("name") or query, context=resolved.get("address") or ""
    )

    # Nearby attractions (30 km) — the same system used on curated pages.
    nearby_raw = resolver.search_nearby(
        resolved,
        radius_km=_cfg("NEARBY_ATTRACTION_RADIUS_KM"),
        max_results=_cfg("NEARBY_ATTRACTION_LIMIT"),
    )
    nearby = [_live_attraction_view(item) for item in nearby_raw]

    # Nearby hidden gems (100 km) — the dedicated dynamic engine.
    scored_gems = collect_scored_gems(resolved, radius_km=_cfg("HIDDEN_GEM_RADIUS_KM"))
    hidden_gems_preview = [gem_view(g) for g in scored_gems[: _cfg("HIDDEN_GEM_PREVIEW_LIMIT")]]

    # Best-effort link to a curated state guide when the address names one.
    linked_state = None
    address_l = str(resolved.get("address") or "").lower()
    for state_obj in current_app.data_store.all_states():
        if state_obj.get("name", "").lower() in address_l:
            linked_state = state_obj
            break

    return render_template(
        "place_live.html",
        query=query,
        place=resolved,
        hero_image=hero_image,
        gallery=gallery[1:],
        nearby=nearby,
        hidden_gems=hidden_gems_preview,
        linked_state=linked_state,
    )


# ----------------------------------------------------------- travel assistant
@pages_bp.route("/assistant")
def assistant():
    """The travel assistant.

    A destination page can hand its place straight over with ``?plan=<slug>``,
    which opens the assistant already planning that place and asking how many
    days. Live (non-curated) places have no slug, so they pass ``?plan_name=``
    and the assistant resolves the name itself.
    """
    plan_slug = request.args.get("plan", "").strip()
    plan_place = _store().get_place(plan_slug) if plan_slug else None

    plan_name = ""
    if not plan_place:
        plan_name = " ".join(request.args.get("plan_name", "").split())[:120]

    return render_template(
        "assistant.html",
        plan_slug=plan_place["slug"] if plan_place else "",
        plan_place=plan_place,
        plan_name=plan_name,
    )


# ---------------------------------------------------------------------- trips
@pages_bp.route("/trips")
def trips():
    return render_template("trips.html", trips=_trips().all_trips())


@pages_bp.route("/trip/<slug>")
def trip(slug):
    store = _store()
    trip_obj = _trips().get_trip(slug)
    if not trip_obj:
        abort(404)

    # Resolve referenced places without mutating the shared trip record.
    trip_obj = copy.deepcopy(trip_obj)
    destinations_map = {
        p["slug"]: p for p in store.resolve_places(trip_obj.get("destinations", []))
    }
    for day in trip_obj.get("itinerary", []):
        day["_places"] = store.resolve_places(day.get("places", []))

    state_obj = store.get_state(trip_obj.get("state_slug"))
    return render_template(
        "trip.html",
        trip=trip_obj,
        destinations=list(destinations_map.values()),
        state=state_obj,
    )


# -------------------------------------------------------------------- search
@pages_bp.route("/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return render_template("search_results.html", query="", grouped=None)

    # If there is a single dominant match, jump straight to its page.
    matches = current_app.search_service.suggest(query, limit=25)
    if not matches:
        return redirect(url_for("pages.place", q=query))
    if matches and (
        len(matches) == 1 or matches[0]["score"] - matches[1]["score"] >= 25
    ):
        top = matches[0]
        if top["type"] == "state":
            return redirect(url_for("pages.state", slug=top["slug"]))
        return redirect(url_for("pages.destination", slug=top["slug"]))

    grouped = current_app.search_service.search(query, limit=30)
    return render_template("search_results.html", query=query, grouped=grouped)


# ------------------------------------------------------------------- contact
@pages_bp.route("/credits")
def credits():
    """Photography attribution.

    The curated photographs come from Wikimedia Commons, mostly under CC BY or
    CC BY-SA, which require the author and licence to be named. Crediting them
    here rather than over every image keeps the pages clean while still
    publishing the attribution the licences ask for.
    """
    store = _store()
    entries = []

    for place in store.destinations() + store.hidden_gems():
        credit = entity_photo_credit(place, "place")
        if credit:
            entries.append(
                {
                    "name": place.get("name"),
                    "context": place.get("state"),
                    "url": url_for("pages.destination", slug=place["slug"]),
                    **credit,
                }
            )

    for state_record in store.all_states():
        credit = entity_photo_credit(state_record, "state")
        if credit:
            entries.append(
                {
                    "name": state_record.get("name"),
                    "context": "State guide",
                    "url": url_for("pages.state", slug=state_record["slug"]),
                    **credit,
                }
            )

    entries.sort(key=lambda item: str(item.get("name") or "").lower())
    return render_template("credits.html", entries=entries)


@pages_bp.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "POST":
        name = " ".join(request.form.get("name", "").split())
        email = request.form.get("email", "").strip().lower()
        message = request.form.get("message", "").strip()
        request_type = request.form.get("request_type", "custom_trip").strip()
        allowed_types = {"custom_trip", "general", "destination_suggestion"}

        if not (2 <= len(name) <= 120):
            flash("Please enter a name between 2 and 120 characters.", "error")
        elif len(email) > 200 or not _EMAIL_RE.fullmatch(email):
            flash("Please enter a valid email address.", "error")
        elif not (10 <= len(message) <= 5000):
            flash("Please enter a message between 10 and 5,000 characters.", "error")
        elif request_type not in allowed_types:
            flash("Please choose a valid request type.", "error")
        else:
            db.session.add(
                ContactMessage(
                    name=name,
                    email=email,
                    request_type=request_type,
                    message=message,
                )
            )
            db.session.commit()
            flash(
                "Your request is saved. Our team can now review it and prepare a trip privately before publishing.",
                "success",
            )
            return redirect(url_for("pages.contact"))
    return render_template("contact.html")


# ----------------------------------------------------- community hidden gems
@pages_bp.route("/submit-gem", methods=["GET", "POST"])
def submit_gem():
    store = _store()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        location = request.form.get("location", "").strip()
        description = request.form.get("description", "").strip()
        if not (name and location and description):
            flash("Name, location and description are required.", "error")
        else:
            submission = HiddenGemSubmission(
                name=name,
                location=location,
                state=request.form.get("state", "").strip() or None,
                lat=request.form.get("lat", type=float),
                lng=request.form.get("lng", type=float),
                category=request.form.get("category", "").strip() or None,
                description=description,
                why_special=request.form.get("why_special", "").strip() or None,
                photo_url=request.form.get("photo_url", "").strip() or None,
                submitted_by=request.form.get("submitted_by", "").strip() or None,
                contact_email=request.form.get("contact_email", "").strip() or None,
            )
            db.session.add(submission)
            db.session.commit()
            flash(
                "Thank you! Your hidden gem has been submitted for review. "
                "Once approved it will join Atulya Yatra's recommendations.",
                "success",
            )
            return redirect(url_for("pages.submit_gem"))
    return render_template("submit_gem.html", states=store.all_states())


# -------------------------------------------------------------------- errors
@pages_bp.app_errorhandler(404)
def not_found(_e):
    return render_template("404.html"), 404


# -------------------------------------------------------------------- helpers
def _similar_places(store, place, limit=6):
    """Other curated destinations sharing themes with the given place.

    Deliberately excludes hidden gems: they have their own dedicated,
    dynamically-scored tab, and mixing them in here would present a
    lesser-known gem as if it were an equivalent mainstream destination.
    """
    interests = list(place.get("categories", [])) + list(place.get("tags", []))
    interests = [i.lower() for i in interests]
    pool = [
        p for p in store.destinations()
        if p["slug"] != place["slug"] and not p.get("is_hidden_gem")
    ]
    ranked = recommend_for_preferences(pool, interests, prefer="popular", limit=limit + 4)
    # Prefer places in a different spot but same theme; keep it simple and cut.
    return ranked[:limit]
