"""Shared Flask extensions.

Keeping extension instances in their own module avoids circular imports between
the app factory, models and blueprints.
"""
from __future__ import annotations

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
