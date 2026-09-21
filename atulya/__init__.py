"""Atulya Yatra 2.0 application factory.

A smart tourism discovery platform for India. The app is assembled here from
small, focused modules: a JSON-backed data store, geo/search/recommendation
services, an interactive assistant, and thin Flask blueprints for pages and API.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

from flask import Flask

from config import BASE_DIR, get_config
from .chatbot import ChatAssistant
from .data_store import DataStore
from .directions import directions_url_for
from .extensions import db
from .llm import LLMClient
from .images import (
    entity_photo_credit,
    has_usable_hero,
    media_bp,
    named_image_url,
    resolve_entity_gallery,
    resolve_entity_image,
    resolve_entity_srcset,
    resolve_image,
)
from .places import GooglePlacesResolver
from .search import SearchService
from .trip_catalog import TripCatalog

__version__ = "2.0.0"


@contextmanager
def _schema_init_lock():
    """Serialize SQLite schema creation across pre-fork server workers."""
    lock_path = BASE_DIR / "instance" / ".schema-init.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def create_app(config_name: str | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
    )
    app.config.from_object(get_config(config_name))

    # Extensions
    db.init_app(app)

    # Curated-content services (shared, read-only after load).
    store = DataStore(Path(app.config["DATA_DIR"]))
    store.load()
    app.data_store = store
    app.trip_catalog = TripCatalog(store)
    app.search_service = SearchService(store)
    app.places_resolver = GooglePlacesResolver(app.config.get("GOOGLE_MAPS_API_KEY", ""))
    # Optional conversational layer. Absent keys leave the assistant entirely
    # deterministic; it never supplies place facts, only phrasing.
    app.llm = LLMClient.from_config(app.config)
    app.chat_assistant = ChatAssistant(
        store,
        app.search_service,
        app.places_resolver.resolve,
        app.places_resolver.search_nearby,
        app.places_resolver.grouped_nearby,
        llm=app.llm,
    )

    # Blueprints
    from .blueprints.admin import admin_bp
    from .blueprints.api import api_bp
    from .blueprints.pages import pages_bp

    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(media_bp)
    app.register_blueprint(admin_bp)

    # Template helpers
    _register_template_helpers(app)

    # Create tables for writable content and assistant sessions. Gunicorn may
    # import the app concurrently in multiple workers, so serialize SQLite DDL.
    with _schema_init_lock(), app.app_context():
        from . import models  # noqa: F401  (register models)

        db.create_all()

    return app


def _register_template_helpers(app: Flask) -> None:
    @app.template_global()
    def img(rel_path=None, name="", category=""):
        """Resolve scalar local assets without creating an open photo proxy."""
        return resolve_image(rel_path, name=name, category=category)

    @app.template_global()
    def entity_img(entity, kind="place", photo_index=0, width=None):
        """Resolve a trusted place, state, or trip through secure media routes."""
        return resolve_entity_image(entity, kind, photo_index=photo_index, width=width)

    @app.template_global()
    def entity_srcset(entity, kind="place", photo_index=0, widths=(800, 1200, 1800)):
        """Retina-ready `srcset` companion to `entity_img`."""
        return resolve_entity_srcset(entity, kind, photo_index=photo_index, widths=widths)

    @app.template_global()
    def entity_gallery(entity, kind="place", limit=10, width=1600):
        """Distinct gallery photos for a trusted entity, never the same file twice."""
        return resolve_entity_gallery(entity, kind, limit=limit, width=width)

    @app.template_global()
    def photo_credit(entity, kind="place"):
        """Author and licence for a curated hero image, or None."""
        return entity_photo_credit(entity, kind)

    @app.template_global()
    def footer_photo():
        """A stable scenic photo used as the footer backdrop on every page.

        Resolved once per process rather than per request, so every page shows
        the same image and the browser reuses a single cached file while the
        visitor moves around the site.
        """
        cached = getattr(app, "_footer_photo", "unresolved")
        if cached != "unresolved":
            return cached

        wide_scenery = {
            "mountains", "mountain", "valley", "hill station", "hill-station",
            "lake", "river", "backwaters", "beach", "island", "nature", "tea",
        }

        def scenic(place):
            hay = {str(c).lower() for c in place.get("categories") or []}
            hay |= {str(t).lower() for t in place.get("tags") or []}
            return bool(hay & wide_scenery)

        candidates = [
            place
            for place in app.data_store.destinations() + app.data_store.hidden_gems()
            if scenic(place) and has_usable_hero(place, "place")
        ]
        candidates.sort(
            key=lambda p: (p.get("popularity", 0) or 0, p.get("slug") or ""), reverse=True
        )
        app._footer_photo = candidates[0] if candidates else None
        return app._footer_photo

    @app.template_global()
    def footer_places():
        """A few curated states for the footer, each with a real photo."""
        cached = getattr(app, "_footer_places", None)
        if cached is not None:
            return cached
        wanted = ("meghalaya", "himachal-pradesh", "kerala", "rajasthan", "sikkim")
        picks = []
        for slug in wanted:
            state = app.data_store.get_state(slug)
            if state and has_usable_hero(state, "state"):
                picks.append(state)
            if len(picks) == 3:
                break
        app._footer_places = picks
        return picks

    @app.template_global()
    def named_img(name, context="", category=""):
        """Resolve trusted nested attraction names through a signed media URL."""
        return named_image_url(name, context=context, category=category)

    @app.template_global()
    def directions_url(entity):
        """Reusable Google Maps directions link for any place-like record."""
        return directions_url_for(entity)

    @app.template_filter("truncate_words")
    def truncate_words(text, count=28):
        if not text:
            return ""
        words = str(text).split()
        if len(words) <= count:
            return text
        return " ".join(words[:count]) + "…"

    @app.context_processor
    def inject_globals():
        return {"app_version": __version__, "brand": "Atulya Yatra"}
