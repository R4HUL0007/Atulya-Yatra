"""JSON API routes for interactive features.

These endpoints power the homepage autocomplete, the AI assistant, the
"view more hidden gems" action and community submissions. Keeping them separate
from page routes makes it straightforward to reuse them from a future mobile app
or an external AI model.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime, timedelta

from flask import Blueprint, abort, current_app, jsonify, request, url_for

from ..extensions import db
from ..images import (
    entity_photo_credit,
    external_photo_url,
    named_image_url,
    resolve_entity_image,
    resolve_place_image,
)
from ..directions import directions_url_for
from ..hidden_gems import collect_scored_gems, gem_view
from ..models import AssistantSession, HiddenGemSubmission

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _store():
    return current_app.data_store


def _place_brief(place: dict) -> dict:
    """A compact, link-ready representation of a place for JSON responses."""
    categories = place.get("categories", [])
    return {
        "slug": place.get("slug"),
        "name": place.get("name"),
        "state": place.get("state"),
        "state_slug": place.get("state_slug"),
        "is_hidden_gem": place.get("is_hidden_gem", False),
        "type": place.get("type"),
        "categories": categories,
        "short_intro": place.get("short_intro", ""),
        "rating": place.get("rating"),
        "distance_km": place.get("distance_km"),
        "recommendation_score": place.get("recommendation_score"),
        "image": resolve_place_image(place),
        "url": url_for("pages.destination", slug=place.get("slug")),
        "directions_url": directions_url_for(place),
    }


@api_bp.route("/search")
def search():
    query = request.args.get("q", "").strip()[:120]
    limit = min(request.args.get("limit", 8, type=int) or 8, 20)
    suggestions = current_app.search_service.suggest(query, limit=limit)
    for suggestion in suggestions:
        if suggestion["type"] == "state":
            suggestion["url"] = url_for("pages.state", slug=suggestion["slug"])
        else:
            suggestion["url"] = url_for("pages.destination", slug=suggestion["slug"])

    # The curated index is intentionally finite. Add one live Places result so
    # towns such as Dombivli and Thane remain searchable without persisting them
    # as fake curated destinations.
    resolver = getattr(current_app, "places_resolver", None)
    known_names = {str(item.get("name") or "").lower() for item in suggestions}
    if len(query) >= 3 and resolver and resolver.enabled and len(suggestions) < limit:
        external = resolver.resolve(query)
        if external and str(external.get("name") or "").lower() not in known_names:
            suggestions.append(
                {
                    "slug": external.get("slug"),
                    "name": external.get("name"),
                    "subtitle": external.get("address") or "Live Google Places result in India",
                    "type": "external_location",
                    "score": 100,
                    "url": url_for("pages.place", q=external.get("name") or query),
                }
            )
    return jsonify({"query": query, "results": suggestions[:limit]})


def _decorate_place_card(card: dict) -> None:
    """Resolve media and links for an assistant place card in-place."""
    slug = card.get("slug")
    place = _store().get_place(slug) if slug else None
    if place:
        card["image"] = resolve_place_image(place)
        card["url"] = url_for("pages.destination", slug=slug)
        card["plan_action"] = {"type": "plan_place", "value": slug}
        if place.get("source") == "google_places_api":
            card["photo_credit"] = "Photo via Google Places"
        else:
            # Curated hero photography is licensed, so it has to stay credited
            # in the assistant too, not just on the pages.
            credit = entity_photo_credit(place, "place")
            if credit:
                card["photo_credit"] = credit["text"]
    else:
        state = _store().get_state(slug) if slug else None
        if state:
            card["image"] = resolve_entity_image(state, "state")
            card["url"] = url_for("pages.state", slug=slug)
        photo_names = card.pop("google_photo_names", []) or []
        image_url = external_photo_url(photo_names[0]) if photo_names else None
        if image_url:
            card["image"] = image_url
            card["photo_credit"] = "Photo via Google Places"
    card.pop("google_photo_names", None)


def _decorate_assistant_response(response: dict) -> None:
    if isinstance(response.get("location"), dict):
        _decorate_place_card(response["location"])
    for card in response.get("cards", []) or []:
        _decorate_place_card(card)
    for section in response.get("sections", []) or []:
        if not isinstance(section, dict):
            continue
        for card in section.get("cards", []) or []:
            _decorate_place_card(card)
    guide = response.get("guide")
    if isinstance(guide, dict):
        state_name = str(guide.get("state_name") or "India")
        visuals = []
        if guide.get("culture"):
            visuals.append(
                {
                    "title": "Culture and traditions",
                    "caption": state_name,
                    "image": named_image_url(
                        f"{state_name} traditional culture", context=state_name, category="culture"
                    ),
                }
            )
        for food in (guide.get("food") or [])[:4]:
            visuals.append(
                {
                    "title": str(food),
                    "caption": "Local food",
                    "image": named_image_url(
                        f"{food} Indian food", context=state_name, category="food"
                    ),
                }
            )
        for festival in (guide.get("festivals") or [])[:3]:
            festival_name = festival.get("name") if isinstance(festival, dict) else str(festival)
            if festival_name:
                visuals.append(
                    {
                        "title": festival_name,
                        "caption": "Festival",
                        "image": named_image_url(
                            festival_name, context=state_name, category="culture"
                        ),
                    }
                )
        guide["visuals"] = visuals
    itinerary = response.get("itinerary") or {}
    for day in itinerary.get("days", []) or []:
        if isinstance(day.get("place"), dict):
            _decorate_place_card(day["place"])
        if isinstance(day.get("hidden_gem"), dict):
            _decorate_place_card(day["hidden_gem"])


def _assistant_session(token: object) -> AssistantSession:
    token_text = str(token or "")
    record = None
    if re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token_text):
        record = AssistantSession.query.filter_by(token=token_text).first()
    if record is None:
        record = AssistantSession(token=secrets.token_urlsafe(32), state_json="{}")
        db.session.add(record)
        db.session.flush()
    return record


@api_bp.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required."}), 400

    record = _assistant_session(data.get("session_id"))
    try:
        state = json.loads(record.state_json or "{}")
    except (TypeError, json.JSONDecodeError):
        state = {}

    action = data.get("action") if isinstance(data.get("action"), dict) else {}
    context = data.get("context") if isinstance(data.get("context"), dict) else None
    response, next_state = current_app.chat_assistant.respond(
        state,
        message=str(data.get("message") or "")[:1000],
        mode=data.get("mode"),
        action=action,
        context=context,
    )
    _decorate_assistant_response(response)
    now = datetime.now(UTC).replace(tzinfo=None)
    record.state_json = json.dumps(next_state, ensure_ascii=False, separators=(",", ":"))
    record.updated_at = now

    # Opportunistic retention cleanup; conversation data is intentionally short-lived.
    AssistantSession.query.filter(
        AssistantSession.updated_at < now - timedelta(days=30)
    ).delete(synchronize_session=False)
    db.session.commit()
    response["session_id"] = record.token
    return jsonify(response)


@api_bp.route("/chat/reset", methods=["POST"])
def reset_chat():
    data = request.get_json(silent=True) or {}
    token = str(data.get("session_id") or "")
    if re.fullmatch(r"[A-Za-z0-9_-]{20,80}", token):
        record = AssistantSession.query.filter_by(token=token).first()
        if record:
            db.session.delete(record)
            db.session.commit()
    return jsonify({"ok": True})


@api_bp.route("/nearby/<slug>")
def nearby(slug):
    store = _store()
    place = store.get_place(slug)
    if not place:
        abort(404)
    radius = request.args.get(
        "radius", current_app.config["NEARBY_ATTRACTION_RADIUS_KM"], type=float
    )
    radius = max(1.0, min(radius or current_app.config["NEARBY_ATTRACTION_RADIUS_KM"], 300.0))
    limit = request.args.get("limit", current_app.config["NEARBY_ATTRACTION_LIMIT"], type=int)
    limit = max(1, min(limit or current_app.config["NEARBY_ATTRACTION_LIMIT"], 50))
    results = store.within_radius(place, radius, exclude_slug=slug)[:limit]
    return jsonify({"slug": slug, "radius_km": radius, "results": [_place_brief(p) for p in results]})


@api_bp.route("/hidden-gems/<slug>")
def hidden_gems(slug):
    """Ranked dynamic hidden gems near a curated place. Powers 'View More'."""
    store = _store()
    place = store.get_place(slug)
    if not place:
        abort(404)
    radius = request.args.get(
        "radius", current_app.config["HIDDEN_GEM_RADIUS_KM"], type=float
    )
    radius = max(1.0, min(radius or current_app.config["HIDDEN_GEM_RADIUS_KM"], 300.0))
    offset = max(0, request.args.get("offset", 0, type=int))
    limit = max(1, min(request.args.get("limit", 12, type=int) or 12, 50))

    ranked = collect_scored_gems(place, radius_km=radius, exclude_slug=slug)
    window = ranked[offset : offset + limit]
    return jsonify(
        {
            "slug": slug,
            "radius_km": radius,
            "total": len(ranked),
            "offset": offset,
            "results": [gem_view(g) for g in window],
        }
    )


@api_bp.route("/submit-gem", methods=["POST"])
def submit_gem():
    data = request.get_json(silent=True) or request.form
    name = (data.get("name") or "").strip()
    location = (data.get("location") or "").strip()
    description = (data.get("description") or "").strip()
    if not (name and location and description):
        return jsonify({"ok": False, "error": "name, location and description are required"}), 400

    def _to_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    submission = HiddenGemSubmission(
        name=name,
        location=location,
        state=(data.get("state") or "").strip() or None,
        lat=_to_float(data.get("lat")),
        lng=_to_float(data.get("lng")),
        category=(data.get("category") or "").strip() or None,
        description=description,
        why_special=(data.get("why_special") or "").strip() or None,
        photo_url=(data.get("photo_url") or "").strip() or None,
        submitted_by=(data.get("submitted_by") or "").strip() or None,
        contact_email=(data.get("contact_email") or "").strip() or None,
    )
    db.session.add(submission)
    db.session.commit()
    return jsonify({"ok": True, "status": submission.status, "id": submission.id})
