"""Private administration for contact enquiries and managed trips."""
from __future__ import annotations

from functools import wraps
import hmac
import json
import re
import secrets

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from ..extensions import db
from ..models import ContactMessage, ManagedTrip, utcnow

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _configured() -> bool:
    return bool(current_app.config.get("ADMIN_AUTH_CONFIGURED"))


def _csrf_token() -> str:
    token = session.get("_admin_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_admin_csrf_token"] = token
    return token


def _is_admin() -> bool:
    return bool(
        _configured()
        and session.get("admin_username")
        == current_app.config.get("ADMIN_USERNAME")
    )


@admin_bp.context_processor
def _admin_template_context():
    return {"admin_csrf_token": _csrf_token, "admin_authenticated": _is_admin()}


@admin_bp.before_request
def _protect_admin_posts():
    if request.method != "POST":
        return None
    expected = session.get("_admin_csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description="The admin form expired. Reload the page and try again.")
    return None


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _configured():
            return render_template("admin/login.html", configuration_error=True), 503
        if not _is_admin():
            flash("Sign in to access administration.", "error")
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)

    return wrapped


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if not _configured():
        return render_template("admin/login.html", configuration_error=True), 503
    if _is_admin():
        return redirect(url_for("admin.contacts"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        valid_user = hmac.compare_digest(
            username, current_app.config.get("ADMIN_USERNAME", "")
        )
        try:
            valid_password = check_password_hash(
                current_app.config.get("ADMIN_PASSWORD_HASH", ""), password
            )
        except (TypeError, ValueError):
            valid_password = False
        if valid_user and valid_password:
            session.clear()
            session.permanent = True
            session["admin_username"] = username
            _csrf_token()
            return redirect(url_for("admin.contacts"))
        flash("Invalid admin credentials.", "error")

    return render_template("admin/login.html", configuration_error=False)


@admin_bp.post("/logout")
@admin_required
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("admin.login"))


@admin_bp.route("/contacts")
@admin_required
def contacts():
    status = request.args.get("status", "").strip().lower()
    query = ContactMessage.query
    if status in ContactMessage.VALID_STATUSES:
        query = query.filter_by(status=status)
    messages = query.order_by(ContactMessage.created_at.desc()).all()
    return render_template(
        "admin/contacts.html",
        contacts=messages,
        active_status=status,
        statuses=sorted(ContactMessage.VALID_STATUSES),
    )


@admin_bp.post("/contacts/<int:contact_id>/status")
@admin_required
def update_contact_status(contact_id: int):
    contact = db.get_or_404(ContactMessage, contact_id)
    status = request.form.get("status", "").strip().lower()
    if status not in ContactMessage.VALID_STATUSES:
        abort(400, description="Unknown contact status.")
    contact.status = status
    contact.processed_at = None if status == ContactMessage.STATUS_NEW else utcnow()
    db.session.commit()
    flash("Contact status updated.", "success")
    return redirect(url_for("admin.contacts"))


@admin_bp.route("/trips")
@admin_required
def trips():
    managed = ManagedTrip.query.order_by(ManagedTrip.updated_at.desc()).all()
    return render_template(
        "admin/trips.html",
        trips=managed,
        curated_count=len(current_app.data_store.all_trips()),
    )


@admin_bp.route("/trips/new", methods=["GET", "POST"])
@admin_required
def new_trip():
    errors: list[str] = []
    if request.method == "POST":
        values, errors = _validated_trip_form()
        if not errors:
            trip = ManagedTrip(**values)
            db.session.add(trip)
            contact = None
            if trip.source_contact_id:
                contact = db.session.get(ContactMessage, trip.source_contact_id)
            if contact:
                contact.status = ContactMessage.STATUS_CONVERTED
                contact.processed_at = utcnow()
            db.session.commit()
            flash(
                "Trip published." if trip.status == ManagedTrip.STATUS_PUBLISHED else "Draft trip saved.",
                "success",
            )
            return redirect(url_for("admin.trips"))

    open_contacts = (
        ContactMessage.query.filter_by(status=ContactMessage.STATUS_NEW)
        .order_by(ContactMessage.created_at.desc())
        .limit(100)
        .all()
    )
    return render_template(
        "admin/trip_form.html",
        errors=errors,
        states=current_app.data_store.all_states(),
        contacts=open_contacts,
        form=request.form,
    )


@admin_bp.post("/trips/<int:trip_id>/publication")
@admin_required
def toggle_publication(trip_id: int):
    trip = db.get_or_404(ManagedTrip, trip_id)
    status = request.form.get("status", "").strip().lower()
    if status not in ManagedTrip.VALID_STATUSES:
        abort(400, description="Unknown trip status.")
    trip.status = status
    db.session.commit()
    flash("Trip publication status updated.", "success")
    return redirect(url_for("admin.trips"))


def _split_list(value: str, *, limit: int = 30, item_length: int = 180) -> list[str]:
    items: list[str] = []
    for item in re.split(r"[,\r\n]+", value or ""):
        clean = " ".join(item.split())[:item_length]
        if clean and clean not in items:
            items.append(clean)
    return items[:limit]


def _validated_trip_form() -> tuple[dict, list[str]]:
    store = current_app.data_store
    errors: list[str] = []

    name = " ".join(request.form.get("name", "").split())
    slug = request.form.get("slug", "").strip().lower()
    state_slug = request.form.get("state_slug", "").strip()
    overview = request.form.get("overview", "").strip()
    hero_image = request.form.get("hero_image", "").strip() or None
    source_contact_raw = request.form.get("source_contact_id", "").strip()

    if not (3 <= len(name) <= 180):
        errors.append("Trip name must be between 3 and 180 characters.")
    if not slug and name:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug or len(slug) > 120 or not _SLUG_RE.fullmatch(slug):
        errors.append("Slug must use lowercase letters, numbers and single hyphens only.")
    if slug and (
        store.get_trip(slug) or ManagedTrip.query.filter_by(slug=slug).first()
    ):
        errors.append("That slug is already used by another trip.")
    if not store.get_state(state_slug):
        errors.append("Choose a valid state or union territory.")
    if not (30 <= len(overview) <= 5000):
        errors.append("Overview must be between 30 and 5,000 characters.")
    if hero_image and len(hero_image) > 400:
        errors.append("Hero image value is too long.")

    try:
        duration_days = int(request.form.get("duration_days", ""))
    except (TypeError, ValueError):
        duration_days = 0
    if not 1 <= duration_days <= 30:
        errors.append("Duration must be between 1 and 30 days.")

    destinations = _split_list(request.form.get("destinations", ""), limit=40, item_length=120)
    unknown_destinations = [slug for slug in destinations if not store.get_place(slug)]
    if unknown_destinations:
        errors.append("Unknown destination slugs: " + ", ".join(unknown_destinations))

    itinerary: list[dict] = []
    itinerary_raw = request.form.get("itinerary", "").strip()
    try:
        decoded = json.loads(itinerary_raw)
    except (TypeError, json.JSONDecodeError):
        decoded = None
        errors.append("Itinerary must be valid JSON containing a list of day objects.")
    if decoded is not None:
        if not isinstance(decoded, list) or not decoded:
            errors.append("Itinerary must contain at least one day.")
        else:
            seen_days: set[int] = set()
            for index, entry in enumerate(decoded, start=1):
                if not isinstance(entry, dict):
                    errors.append(f"Itinerary item {index} must be an object.")
                    continue
                try:
                    day_number = int(entry.get("day"))
                except (TypeError, ValueError):
                    day_number = 0
                title = " ".join(str(entry.get("title") or "").split())
                detail = str(entry.get("detail") or "").strip()
                places = entry.get("places") or []
                if isinstance(places, str):
                    places = _split_list(places, limit=20, item_length=120)
                if not isinstance(places, list):
                    places = []
                    errors.append(f"Day {index} places must be a list of slugs.")
                places = [str(place).strip() for place in places if str(place).strip()]
                unknown = [place for place in places if not store.get_place(place)]
                if day_number < 1 or day_number > max(duration_days, 1) or day_number in seen_days:
                    errors.append(f"Day {index} has an invalid or duplicate day number.")
                if not (2 <= len(title) <= 180):
                    errors.append(f"Day {index} needs a title between 2 and 180 characters.")
                if not (10 <= len(detail) <= 2000):
                    errors.append(f"Day {index} detail must be between 10 and 2,000 characters.")
                if unknown:
                    errors.append(f"Day {index} has unknown place slugs: " + ", ".join(unknown))
                seen_days.add(day_number)
                itinerary.append({"day": day_number, "title": title, "places": places, "detail": detail})
            if duration_days and len(itinerary) != duration_days:
                errors.append("The itinerary must contain exactly one entry for each trip day.")

    if not destinations and itinerary:
        destinations = []
        for day in itinerary:
            for place in day.get("places", []):
                if place not in destinations:
                    destinations.append(place)

    source_contact_id = None
    if source_contact_raw:
        try:
            source_contact_id = int(source_contact_raw)
        except ValueError:
            errors.append("Choose a valid source contact.")
        else:
            if not db.session.get(ContactMessage, source_contact_id):
                errors.append("The selected source contact no longer exists.")

    values = {
        "slug": slug,
        "name": name,
        "state_slug": state_slug,
        "duration_days": duration_days,
        "overview": overview,
        "best_season": _split_list(request.form.get("best_season", "")),
        "themes": _split_list(request.form.get("themes", "")),
        "destinations": destinations,
        "activities": _split_list(request.form.get("activities", "")),
        "recommended_experiences": _split_list(request.form.get("recommended_experiences", "")),
        "itinerary": itinerary,
        "travel_tips": _split_list(request.form.get("travel_tips", "")),
        "hero_image": hero_image,
        "status": ManagedTrip.STATUS_PUBLISHED if request.form.get("publish") == "on" else ManagedTrip.STATUS_DRAFT,
        "source_contact_id": source_contact_id,
    }
    return values, errors
