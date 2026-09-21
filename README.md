# Atulya Yatra

A travel-discovery site for India. It helps a visitor find a destination, read an
honest guide to it, browse photographs, and get a day-by-day itinerary built from
curated data.

## How it works

The site is a Flask application built around an application factory
(`atulya/create_app`) with three blueprints: public pages, a JSON API, and an
admin area.

**Facts come from curated data, not from a language model.** Destinations, hidden
gems, states and trips live in `data/*.json`. Itineraries, cards, galleries and
guides are assembled deterministically in Python. The Google Places API is used
to augment state listings with live discovery, so a large state returns more than
the handful of places curated by hand.

**Photographs are stored locally.** Images are sourced from Wikimedia Commons and
downloaded into `static/images/` rather than hotlinked, because bursts of
full-size requests to Commons get rate-limited. Licences that require it are
credited on `/credits`, which keeps the photographs themselves clean.

**The assistant is deterministic by default.** It resolves a location, asks for a
trip length and a travel style, then builds the itinerary from curated records.
If an API key is configured it will additionally use a language model to *reword*
those facts conversationally, grounded on a facts block and never allowed to
invent a place, price, timing or date. With no key, nothing leaves the server and
the wording is fixed. If the model errors, times out or returns a truncated
sentence, the deterministic wording is used instead.

## Running it

### Docker

```bash
cp .env.example .env    # then fill in your keys
docker compose up -d --build
```

The site is served on <http://localhost:8000>.

### Locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
cp .env.example .env
python app.py
```

## Configuration

All configuration is read from the environment; see `.env.example` for the full
list and `config.py` for defaults. Nothing is required to boot: without any keys
the site serves curated data and a fully deterministic assistant.

| Variable | Purpose |
| --- | --- |
| `GOOGLE_MAPS_API_KEY` | Live place discovery and directions |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | Optional conversational phrasing |
| `ATULYA_LLM_MODEL` | Pin a specific model instead of the default alias |
| `ATULYA_SECRET_KEY` | Session signing; required for the admin area |

`.env` is gitignored and must never be committed.

## Layout

```
app.py              entry point
config.py           environment-driven configuration
atulya/
  __init__.py       application factory
  blueprints/       pages, api, admin routes
  chatbot.py        the assistant: intent, planning, itineraries
  llm.py            optional language-model layer (phrasing only)
  data_store.py     curated destination/state/trip access
  places.py         Google Places client
  state_discovery.py  live augmentation of state listings
  images.py         image, srcset, gallery and credit resolution
  search.py         location resolution and scoring
data/               curated destinations, hidden gems, states, trips
static/images/      locally stored photography
templates/          Jinja templates
scripts/            data fetching, validation and audit tools
tests/              test suite
```

## Notes

- The SQLite database in `instance/` holds visitor feedback, enquiries, community
  submissions and short-lived assistant conversations. It is gitignored and
  persisted across container restarts by a Docker volume.
- Photograph attribution is a licence obligation for CC BY and CC BY-SA images.
  If you add photographs, record the author and licence so `/credits` stays
  accurate.
