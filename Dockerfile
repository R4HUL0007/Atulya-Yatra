# Atulya Yatra 2.0 — container image
#
# Multi-stage build: install dependencies in a builder layer, copy only the
# app + installed packages into a slim runtime layer. Runs as a non-root user
# and serves via gunicorn (production WSGI server) rather than Flask's dev
# server.

FROM python:3.12-slim AS builder

WORKDIR /build

# System build deps for packages that need compilation (e.g. greenlet).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

# Metadata
LABEL org.opencontainers.image.title="Atulya Yatra 2.0" \
      org.opencontainers.image.description="Smart tourism discovery platform for India"

# Create a non-root user to run the app.
RUN useradd --create-home --uid 1000 atulya
WORKDIR /app

# Bring in the pre-built dependencies from the builder stage.
COPY --from=builder /install /usr/local

# Copy application code. .dockerignore keeps venvs, caches, and the old
# reference project (Atulya-Yatra/) out of the image.
COPY . .

# instance/ holds the SQLite DB for feedback + community gem submissions and
# must be writable by the app user.
RUN mkdir -p /app/instance && chown -R atulya:atulya /app

USER atulya

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATULYA_ENV=production

EXPOSE 8000

# gunicorn serves the `app` object created in app.py (Flask app factory
# output). 2 workers is a reasonable default for a small VM/container; tune
# via the GUNICORN_WORKERS env var if needed.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-2} --timeout 60 app:app"]
