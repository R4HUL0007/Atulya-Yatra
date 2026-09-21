"""Database models for user-generated content.

Only content that users create needs a writable store, so the database is kept
deliberately small: visitor feedback and community hidden-gem submissions.
Curated tourism content stays in version-controlled JSON.
"""
from __future__ import annotations

from datetime import UTC, datetime

from .extensions import db


def utcnow() -> datetime:
    """Return a timezone-correct UTC value compatible with existing columns."""
    return datetime.now(UTC).replace(tzinfo=None)


class Feedback(db.Model):
    """Visitor feedback / reviews attached to a place slug."""

    __tablename__ = "feedback"

    id = db.Column(db.Integer, primary_key=True)
    place_slug = db.Column(db.String(120), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    rating = db.Column(db.Integer, nullable=True)
    comment = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "place_slug": self.place_slug,
            "name": self.name,
            "rating": self.rating,
            "comment": self.comment,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class HiddenGemSubmission(db.Model):
    """A community-submitted hidden gem awaiting moderation.

    Submissions default to ``pending`` and are never shown publicly until an
    admin sets the status to ``approved``. Once approved they can be promoted
    into the curated dataset, where they automatically join the dynamic
    recommendation ranking. This is the future-ready architecture for
    community-driven hidden gems.
    """

    __tablename__ = "hidden_gem_submission"

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    location = db.Column(db.String(200), nullable=False)
    state = db.Column(db.String(120), nullable=True)
    lat = db.Column(db.Float, nullable=True)
    lng = db.Column(db.Float, nullable=True)
    category = db.Column(db.String(80), nullable=True)
    description = db.Column(db.Text, nullable=False)
    why_special = db.Column(db.Text, nullable=True)
    photo_url = db.Column(db.String(400), nullable=True)
    submitted_by = db.Column(db.String(160), nullable=True)
    contact_email = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(20), default=STATUS_PENDING, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "location": self.location,
            "state": self.state,
            "lat": self.lat,
            "lng": self.lng,
            "category": self.category,
            "description": self.description,
            "why_special": self.why_special,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AssistantSession(db.Model):
    """Server-side state for one assistant conversation.

    Only an opaque token reaches the browser. Keeping the compact structured
    state in SQLite makes conversations consistent across Gunicorn workers and
    avoids placing itineraries or preferences in cookies.
    """

    __tablename__ = "assistant_session"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(80), unique=True, nullable=False, index=True)
    state_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utcnow, onupdate=utcnow, nullable=False, index=True
    )


class ContactMessage(db.Model):
    """A visitor enquiry retained for the private admin workflow."""

    __tablename__ = "contact_message"

    STATUS_NEW = "new"
    STATUS_CONVERTED = "converted"
    STATUS_CLOSED = "closed"
    VALID_STATUSES = {STATUS_NEW, STATUS_CONVERTED, STATUS_CLOSED}

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), nullable=False, index=True)
    request_type = db.Column(db.String(30), nullable=False, default="custom_trip")
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default=STATUS_NEW, index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    processed_at = db.Column(db.DateTime, nullable=True)


class ManagedTrip(db.Model):
    """An admin-authored trip that can remain private or be published."""

    __tablename__ = "managed_trip"

    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    VALID_STATUSES = {STATUS_DRAFT, STATUS_PUBLISHED}

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
    name = db.Column(db.String(180), nullable=False)
    state_slug = db.Column(db.String(120), nullable=False, index=True)
    duration_days = db.Column(db.Integer, nullable=False)
    overview = db.Column(db.Text, nullable=False)
    best_season = db.Column(db.JSON, nullable=False, default=list)
    themes = db.Column(db.JSON, nullable=False, default=list)
    destinations = db.Column(db.JSON, nullable=False, default=list)
    activities = db.Column(db.JSON, nullable=False, default=list)
    recommended_experiences = db.Column(db.JSON, nullable=False, default=list)
    itinerary = db.Column(db.JSON, nullable=False, default=list)
    travel_tips = db.Column(db.JSON, nullable=False, default=list)
    hero_image = db.Column(db.String(400), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_DRAFT, index=True)
    source_contact_id = db.Column(
        db.Integer,
        db.ForeignKey("contact_message.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utcnow, onupdate=utcnow, nullable=False, index=True
    )

    source_contact = db.relationship("ContactMessage", backref="managed_trips")

    def to_public_dict(self) -> dict:
        """Return the same presentation contract used by curated JSON trips."""
        return {
            "slug": self.slug,
            "name": self.name,
            "state_slug": self.state_slug,
            "duration_days": self.duration_days,
            "best_season": list(self.best_season or []),
            "hero_image": self.hero_image,
            "overview": self.overview,
            "themes": list(self.themes or []),
            "destinations": list(self.destinations or []),
            "activities": list(self.activities or []),
            "recommended_experiences": list(self.recommended_experiences or []),
            "itinerary": [dict(day) for day in (self.itinerary or [])],
            "travel_tips": list(self.travel_tips or []),
            "managed": True,
        }
