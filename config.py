"""Application configuration for Atulya Yatra.

Configuration is intentionally simple and environment-driven so the app can run
locally with zero setup and still be configured for other environments without
code changes.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a listed dependency
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INSTANCE_DIR = BASE_DIR / "instance"

if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Base configuration shared across environments."""

    _explicit_secret = os.environ.get("ATULYA_SECRET_KEY", "").strip()
    SECRET_KEY = _explicit_secret or "atulya-yatra-dev-secret"

    # Admin authentication is intentionally disabled unless all three values
    # are explicitly supplied. Store only a Werkzeug password hash, never a
    # plaintext password, in the environment.
    ADMIN_USERNAME = os.environ.get("ATULYA_ADMIN_USERNAME", "").strip()
    ADMIN_PASSWORD_HASH = os.environ.get("ATULYA_ADMIN_PASSWORD_HASH", "").strip()
    ADMIN_AUTH_CONFIGURED = bool(
        _explicit_secret and ADMIN_USERNAME and ADMIN_PASSWORD_HASH
    )

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("ATULYA_SESSION_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = 8 * 60 * 60

    # User generated content (feedback + community hidden-gem submissions) is the
    # only thing that needs a writable store. Curated tourism content lives in
    # version-controlled JSON so the app stays easy to deploy and expand.
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "ATULYA_DATABASE_URI", f"sqlite:///{(INSTANCE_DIR / 'atulya.db').as_posix()}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Paths to the curated data files (source of truth).
    DATA_DIR = DATA_DIR

    # Google Places powers optional runtime destination resolution and secure
    # photo delivery, plus the explicit offline enrichment scripts. The key is
    # sent only in server-to-server request headers and is never rendered.
    GOOGLE_MAPS_API_KEY = (
        os.environ.get("GOOGLE_MAPS_API_KEY")
        or os.environ.get("Google_maps")
        or ""
    ).strip()

    # Optional language model, used only to word replies about facts the app
    # already holds — never to invent place data. Absent keys simply leave the
    # assistant fully deterministic. See atulya/llm.py for the boundary.
    LLM_PROVIDER = (os.environ.get("ATULYA_LLM_PROVIDER") or "").strip().lower()
    GEMINI_API_KEY = (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
    ).strip()
    OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
    LLM_MODEL = (os.environ.get("ATULYA_LLM_MODEL") or "").strip()
    # Measured round trips against Gemini flash-lite sat between 5s and 10s, so a
    # tighter ceiling would spend the visitor's wait and then discard the reply.
    # Lower it to favour instant deterministic wording over polished wording.
    try:
        LLM_TIMEOUT_SECONDS = float(os.environ.get("ATULYA_LLM_TIMEOUT") or 20)
    except (TypeError, ValueError):
        LLM_TIMEOUT_SECONDS = 20.0

    # Discovery / curated-display tuning. Centralised so behaviour is easy to
    # adjust without hunting through the codebase.
    NEARBY_ATTRACTION_RADIUS_KM = 30
    HIDDEN_GEM_RADIUS_KM = 100
    NEARBY_ATTRACTION_LIMIT = 8
    HIDDEN_GEM_PREVIEW_LIMIT = 6
    FOOD_LIMIT = 8
    FESTIVAL_LIMIT = 5
    ACTIVITY_LIMIT = 6
    SIMILAR_LIMIT = 6
    TRENDING_LIMIT = 6


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}


def get_config(name: str | None = None) -> type[Config]:
    name = name or os.environ.get("ATULYA_ENV", "default")
    return CONFIG_MAP.get(name, DevelopmentConfig)
