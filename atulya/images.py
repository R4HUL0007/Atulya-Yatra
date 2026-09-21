"""Secure image resolution with dynamic Google Places photo fallbacks."""
from __future__ import annotations

import colorsys
import hashlib
import re
from pathlib import Path

from flask import Blueprint, Response, current_app, redirect, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

media_bp = Blueprint("media", __name__)

_CATEGORY_HUES = {
    "beach": 190,
    "mountains": 205,
    "mountain": 205,
    "valley": 150,
    "waterfall": 185,
    "lake": 200,
    "heritage": 30,
    "history": 30,
    "temple": 45,
    "spiritual": 275,
    "culture": 320,
    "nature": 140,
    "wildlife": 110,
    "adventure": 15,
    "city": 210,
    "hill_station": 165,
    "fort": 25,
    "island": 195,
    "river": 195,
    "village": 95,
}

_ENTITY_KINDS = {"place", "state", "trip"}


def _static_file_exists(rel_path: str) -> bool:
    static_folder = current_app.static_folder
    if not static_folder:
        return False
    return (Path(static_folder) / "images" / rel_path).is_file()


def resolve_image(rel_path: str | None, name: str = "", category: str = "") -> str:
    """Return a remote URL, an existing local image, or a clean placeholder."""
    if rel_path and (rel_path.startswith("http://") or rel_path.startswith("https://")):
        return rel_path
    if rel_path and _static_file_exists(rel_path):
        return url_for("static", filename=f"images/{rel_path}")
    label = name or (rel_path or "Atulya Yatra")
    # url_for handles encoding. Pre-quoting here caused visible `%20` text.
    return url_for("media.placeholder", name=label, category=category or "")


def _entity_hero(entity: dict) -> str | None:
    return (entity.get("images") or {}).get("hero") or entity.get("hero_image")


def _entity_image_holder(entity: dict) -> dict:
    """Where a record keeps its image fields: nested for places, flat for states."""
    images = entity.get("images")
    return images if isinstance(images, dict) and images else entity


def _entity_card_image(entity: dict) -> str | None:
    """The smaller downloaded copy, used for cards and grids."""
    return _entity_image_holder(entity).get("hero_small")


def has_usable_hero(entity: dict, kind: str = "place") -> bool:
    """True when this record's hero photo will actually render.

    Records still carry the original hand-written hero paths (``places/x/hero.jpg``)
    even where no file was ever produced, so the declared path alone proves
    nothing. Callers use this to keep placeholders out of prominent slots.
    """
    if not isinstance(entity, dict):
        return False
    if kind == "trip" and not _usable_hero(_entity_hero(entity)):
        return _trip_photo_stand_in(entity) is not None
    return _usable_hero(_entity_hero(entity))


def _usable_hero(hero: object) -> bool:
    """A hero path only counts if it is remote or actually present on disk."""
    text = str(hero or "")
    if not text:
        return False
    if text.startswith("http://") or text.startswith("https://"):
        return True
    return _static_file_exists(text)


def _trip_photo_stand_in(entity: dict) -> dict | None:
    """A trip is an itinerary, not a place, so it owns no photograph.

    Borrow the first destination on the route that has a reviewed photo, which
    is both representative and guaranteed to render.
    """
    store = getattr(current_app, "data_store", None)
    if not store:
        return None
    for slug in entity.get("destinations") or []:
        place = store.get_place(slug)
        if place and (place.get("images") or {}).get("hero"):
            return place
    return None


def resolve_entity_image(
    entity: dict, kind: str = "place", photo_index: int = 0, width: int | None = None
) -> str:
    """Resolve a trusted stored entity to local media or a secure photo route."""
    if not isinstance(entity, dict):
        return resolve_image(None)
    hero = _entity_hero(entity)
    # A trip's own hero path may be declared but never populated. Treat an
    # unusable path the same as no path at all and borrow a destination photo.
    if kind == "trip" and not _usable_hero(hero):
        stand_in = _trip_photo_stand_in(entity)
        if stand_in:
            return resolve_entity_image(stand_in, "place", photo_index, width)
    categories = entity.get("categories") or []
    category = categories[0] if categories else ("mountains" if kind == "trip" else "city")
    # The curated hero is a single photograph, so it can only ever be photo 0.
    # Asking for a later index has to fall through to a source that genuinely
    # holds several images, otherwise a gallery repeats the same file.
    if not photo_index:
        if hero and (hero.startswith("http://") or hero.startswith("https://")):
            return hero
        if hero and _static_file_exists(hero):
            # Cards ask for a modest width; serving them the hero-sized file
            # wastes most of the bytes, so use the smaller copy when one exists.
            card = _entity_card_image(entity)
            if width and int(width) <= 1000 and card and _static_file_exists(card):
                return url_for("static", filename=f"images/{card}")
            return url_for("static", filename=f"images/{hero}")
    resolver = getattr(current_app, "places_resolver", None)
    slug = entity.get("slug")
    if kind in _ENTITY_KINDS and resolver and resolver.enabled and slug:
        params = {"kind": kind, "slug": slug, "photo": max(0, int(photo_index))}
        if width:
            params["w"] = max(320, min(int(width), 2560))
        return url_for("media.entity_photo", **params)
    return resolve_image(hero, name=entity.get("name", ""), category=category)


def resolve_entity_srcset(
    entity: dict, kind: str = "place", photo_index: int = 0, widths=(800, 1200, 1800)
) -> str:
    """Build a `srcset` for a trusted entity so cards stay sharp on retina.

    A card that is 420 CSS px wide still needs ~840 real pixels on a 2x display.
    Serving one mid-size image is the main reason thumbnails look soft. Returns
    an empty string when the entity resolves to a static file or placeholder,
    where a single `src` is already correct.
    """
    if not isinstance(entity, dict):
        return ""
    hero = _entity_hero(entity)
    if kind == "trip" and not _usable_hero(hero):
        stand_in = _trip_photo_stand_in(entity)
        if stand_in:
            return resolve_entity_srcset(stand_in, "place", photo_index, widths)
    if hero:
        # Curated heroes are downloaded in two sizes. When both are on disk we
        # can offer a real choice, so a card is not forced to pull the
        # hero-sized file. Absolute remote URLs have no width to vary.
        card = _entity_card_image(entity)
        if (
            not hero.startswith("http")
            and card
            and _static_file_exists(hero)
            and _static_file_exists(card)
        ):
            small = url_for("static", filename=f"images/{card}")
            large = url_for("static", filename=f"images/{hero}")
            return f"{small} 900w, {large} 1600w"
        return ""
    resolver = getattr(current_app, "places_resolver", None)
    slug = entity.get("slug")
    if not (kind in _ENTITY_KINDS and resolver and resolver.enabled and slug):
        return ""
    candidates = []
    for width in sorted({max(320, min(int(w), 2560)) for w in widths}):
        url = url_for(
            "media.entity_photo",
            kind=kind,
            slug=slug,
            photo=max(0, int(photo_index)),
            w=width,
        )
        candidates.append(f"{url} {width}w")
    return ", ".join(candidates)


def resolve_entity_gallery(
    entity: dict, kind: str = "place", limit: int = 10, width: int = 1600
) -> list[str]:
    """Distinct photo URLs for a gallery, in order, with no repeats.

    Galleries previously indexed into `resolve_entity_image`, which returns the
    single downloaded hero for every index — so a "10 photo" gallery showed the
    same picture ten times. Photos are gathered from the sources that actually
    hold them: the hero (one), any curated gallery files that exist on disk, and
    then Google Places, which is the only source with several per place.
    """
    if not isinstance(entity, dict):
        return []

    urls: list[str] = []

    def add(url: str | None) -> None:
        if url and url not in urls and len(urls) < limit:
            urls.append(url)

    hero = _entity_hero(entity)
    if _usable_hero(hero):
        if str(hero).startswith("http"):
            add(str(hero))
        else:
            add(url_for("static", filename=f"images/{hero}"))

    for relative in _entity_image_holder(entity).get("gallery") or []:
        if isinstance(relative, str) and _static_file_exists(relative):
            add(url_for("static", filename=f"images/{relative}"))

    resolver = getattr(current_app, "places_resolver", None)
    slug = entity.get("slug")
    if kind in _ENTITY_KINDS and resolver and resolver.enabled and slug and len(urls) < limit:
        available = resolver.photo_count_for(entity, max_count=limit)
        for index in range(available):
            add(
                url_for(
                    "media.entity_photo",
                    kind=kind,
                    slug=slug,
                    photo=index,
                    w=max(320, min(int(width), 2560)),
                )
            )
    return urls


def entity_photo_credit(entity: dict, kind: str = "place") -> dict | None:
    """Attribution for a curated hero image, when the data records one.

    Curated heroes sourced from Wikimedia Commons are mostly CC BY / CC BY-SA,
    which oblige us to name the author and licence wherever the photo appears.
    Returns ``None`` for Places photos and placeholders, which carry their own
    separate credit handling.
    """
    if not isinstance(entity, dict):
        return None
    if kind == "place":
        holder = entity.get("images") or {}
        hero = holder.get("hero")
    else:
        holder = entity
        hero = entity.get("hero_image")
    text = holder.get("hero_credit")
    # The credit belongs to the curated hero photo, whether that file is served
    # from our own static folder or referenced remotely.
    if not text or not hero:
        return None
    # Stored credits can carry leftover wiki-markup noise such as "( talk )".
    clean_text = re.sub(r"\s*\(\s*talk[^)]*\)", "", str(text))
    clean_text = re.sub(r"\s+", " ", clean_text).strip(" ·,")
    return {
        "text": clean_text,
        "source": holder.get("hero_credit_url") or "",
        "license_url": holder.get("hero_license_url") or "",
    }


def resolve_place_image(place: dict) -> str:
    """Backward-compatible place image helper used by assistant API cards."""
    return resolve_entity_image(place, "place")


def named_image_url(name: str, context: str = "", category: str = "") -> str:
    """Create a signed dynamic photo URL for trusted nested attraction names."""
    clean_name = " ".join(str(name or "").split())[:120]
    clean_context = " ".join(str(context or "").split())[:120]
    resolver = getattr(current_app, "places_resolver", None)
    if not clean_name or not resolver or not resolver.enabled:
        return resolve_image(None, name=clean_name, category=category)
    serializer = URLSafeTimedSerializer(current_app.secret_key, salt="atulya-named-photo")
    token = serializer.dumps({"name": clean_name, "context": clean_context, "category": category})
    return url_for("media.named_photo", token=token)


def external_photo_url(photo_name: str, width: int | None = None) -> str | None:
    """Create a signed internal URL for a transient Places photo resource."""
    resolver = getattr(current_app, "places_resolver", None)
    if not resolver or not resolver.enabled or not resolver._valid_photo_name(photo_name):
        return None
    serializer = URLSafeTimedSerializer(current_app.secret_key, salt="atulya-place-photo")
    token = serializer.dumps({"photo": photo_name})
    params = {"token": token}
    if width:
        # Matches the Places media cap so full-bleed live heroes stay sharp.
        params["w"] = max(320, min(int(width), 2560))
    return url_for("media.external_photo", **params)


def _photo_redirect(
    entity: dict, *, photo_index: int = 0, max_width: int | None = None
) -> Response:
    resolver = getattr(current_app, "places_resolver", None)
    uri = None
    if resolver and resolver.enabled:
        if max_width:
            uri = resolver.photo_uri_for(entity, index=photo_index, max_width=max_width)
        else:
            uri = resolver.photo_uri_for(entity, index=photo_index)
    if uri:
        response = redirect(uri, code=302)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response
    categories = entity.get("categories") or []
    return redirect(
        url_for(
            "media.placeholder",
            name=entity.get("name") or "Atulya Yatra",
            category=categories[0] if categories else "",
        ),
        code=302,
    )


def _trusted_entity(kind: str, slug: str) -> dict | None:
    store = current_app.data_store
    if kind == "place":
        return store.get_place(slug)
    if kind == "state":
        return store.get_state(slug)
    if kind == "trip":
        trip = current_app.trip_catalog.get_trip(slug)
        if not trip:
            return None
        # A trip's first curated destination gives a reliable representative
        # image; fall back to searching the trip title with its state context.
        destinations = store.resolve_places(trip.get("destinations") or [])
        if destinations:
            return destinations[0]
        state = store.get_state(trip.get("state_slug"))
        return {**trip, "state": (state or {}).get("name")}
    return None


@media_bp.route("/media/entity-photo/<kind>/<slug>")
def entity_photo(kind: str, slug: str) -> Response:
    """Resolve only allowlisted, server-stored entities through Google Places."""
    if kind not in _ENTITY_KINDS:
        return redirect(url_for("media.placeholder", name="Destination"), code=302)
    entity = _trusted_entity(kind, slug)
    if not entity:
        return redirect(url_for("media.placeholder", name="Destination"), code=302)
    photo_index = max(0, min(request.args.get("photo", 0, type=int) or 0, 9))
    width = request.args.get("w", type=int)
    return _photo_redirect(entity, photo_index=photo_index, max_width=width)


@media_bp.route("/media/place-photo/<slug>")
def place_photo(slug: str) -> Response:
    """Keep existing assistant image URLs compatible."""
    place = _trusted_entity("place", slug)
    if not place:
        return redirect(url_for("media.placeholder", name="Destination"), code=302)
    return _photo_redirect(place)


@media_bp.route("/media/named-photo/<token>")
def named_photo(token: str) -> Response:
    """Resolve a server-signed attraction name; never acts as an open proxy."""
    serializer = URLSafeTimedSerializer(current_app.secret_key, salt="atulya-named-photo")
    try:
        payload = serializer.loads(token, max_age=86400)
    except (BadSignature, SignatureExpired):
        return redirect(url_for("media.placeholder", name="Attraction"), code=302)
    if not isinstance(payload, dict):
        return redirect(url_for("media.placeholder", name="Attraction"), code=302)
    name = " ".join(str(payload.get("name") or "").split())[:120]
    context = " ".join(str(payload.get("context") or "").split())[:120]
    if not name:
        return redirect(url_for("media.placeholder", name="Attraction"), code=302)
    return _photo_redirect({"name": name, "state": context, "categories": [payload.get("category") or ""]})


@media_bp.route("/media/external-photo/<token>")
def external_photo(token: str) -> Response:
    """Resolve a signed Places resource name; never acts as an open proxy."""
    serializer = URLSafeTimedSerializer(current_app.secret_key, salt="atulya-place-photo")
    try:
        payload = serializer.loads(token, max_age=86400)
    except (BadSignature, SignatureExpired):
        return redirect(url_for("media.placeholder", name="Destination"), code=302)
    photo_name = payload.get("photo") if isinstance(payload, dict) else None
    resolver = getattr(current_app, "places_resolver", None)
    if not resolver or not resolver._valid_photo_name(photo_name):
        return redirect(url_for("media.placeholder", name="Destination"), code=302)
    width = request.args.get("w", type=int)
    return _photo_redirect(
        {"name": "Destination", "google_photo_names": [photo_name]}, max_width=width
    )


def _hue_for(name: str, category: str) -> int:
    category = (category or "").lower()
    if category in _CATEGORY_HUES:
        return _CATEGORY_HUES[category]
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return int(digest[:2], 16) * 360 // 255


def _hsl(h: int, s: float, l: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h / 360.0, l, s)
    return f"#{int(r*255):02x}{int(g*255):02x}{int(b*255):02x}"


@media_bp.route("/media/placeholder.svg")
def placeholder() -> Response:
    from flask import request

    name = request.args.get("name", "Atulya Yatra")
    category = request.args.get("category", "")
    hue = _hue_for(name, category)
    c1 = _hsl(hue, 0.55, 0.55)
    c2 = _hsl((hue + 25) % 360, 0.6, 0.38)
    initials = "".join(word[0] for word in name.split()[:2]).upper() or "AY"
    safe_name = (name[:40] + "…") if len(name) > 41 else name
    cat_label = category.replace("_", " ").title()

    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='800' viewBox='0 0 1200 800'>
  <defs>
    <linearGradient id='g' x1='0' y1='0' x2='1' y2='1'><stop offset='0%' stop-color='{c1}'/><stop offset='100%' stop-color='{c2}'/></linearGradient>
    <pattern id='p' width='60' height='60' patternUnits='userSpaceOnUse' patternTransform='rotate(45)'><rect width='60' height='60' fill='none'/><circle cx='30' cy='30' r='1.6' fill='rgba(255,255,255,0.18)'/></pattern>
  </defs>
  <rect width='1200' height='800' fill='url(#g)'/><rect width='1200' height='800' fill='url(#p)'/>
  <circle cx='600' cy='320' r='120' fill='rgba(255,255,255,0.14)'/>
  <text x='600' y='352' font-family='Segoe UI, Arial, sans-serif' font-size='96' font-weight='700' fill='rgba(255,255,255,0.92)' text-anchor='middle'>{initials}</text>
  <text x='600' y='500' font-family='Segoe UI, Arial, sans-serif' font-size='58' font-weight='700' fill='#ffffff' text-anchor='middle'>{_xml_escape(safe_name)}</text>
  <text x='600' y='560' font-family='Segoe UI, Arial, sans-serif' font-size='30' fill='rgba(255,255,255,0.85)' text-anchor='middle'>{_xml_escape(cat_label)}</text>
  <text x='600' y='740' font-family='Segoe UI, Arial, sans-serif' font-size='24' letter-spacing='4' fill='rgba(255,255,255,0.7)' text-anchor='middle'>ATULYA YATRA</text>
</svg>"""
    response = Response(svg, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
