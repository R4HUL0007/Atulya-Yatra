"""Read-through catalog for curated and admin-published trips."""
from __future__ import annotations

import copy

from .models import ManagedTrip


class TripCatalog:
    """Merge immutable JSON trips with published SQLite-managed trips."""

    def __init__(self, store):
        self.store = store

    def all_trips(self, *, include_drafts: bool = False) -> list[dict]:
        curated = [copy.deepcopy(trip) for trip in self.store.all_trips()]
        curated_slugs = {trip.get("slug") for trip in curated}

        query = ManagedTrip.query
        if not include_drafts:
            query = query.filter_by(status=ManagedTrip.STATUS_PUBLISHED)
        managed = [
            trip.to_public_dict()
            for trip in query.order_by(ManagedTrip.name.asc()).all()
            if trip.slug not in curated_slugs
        ]
        return sorted(curated + managed, key=lambda trip: trip.get("name", ""))

    def get_trip(self, slug: str, *, include_drafts: bool = False) -> dict | None:
        curated = self.store.get_trip(slug)
        if curated:
            return copy.deepcopy(curated)

        query = ManagedTrip.query.filter_by(slug=slug)
        if not include_drafts:
            query = query.filter_by(status=ManagedTrip.STATUS_PUBLISHED)
        managed = query.first()
        return managed.to_public_dict() if managed else None

    def trips_in_state(
        self, state_slug: str, *, include_drafts: bool = False
    ) -> list[dict]:
        return [
            trip
            for trip in self.all_trips(include_drafts=include_drafts)
            if trip.get("state_slug") == state_slug
        ]
