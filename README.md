# Atulya Yatra

A travel-discovery site for India. It helps a visitor find a destination, read a
grounded guide to it, browse real photographs, and get a day-by-day itinerary
built from curated data.

Covers all 28 states and 8 union territories, each with its own set of pages.

## How it works

A Flask application built around an application factory (`atulya.create_app`)
with three blueprints: public pages, a JSON API under `/api`, and an admin area
under `/admin`.

### Facts come from curated data, not from a language model

Destinations, hidden gems, states and trips live in `data/*.json`. Itineraries,
cards, galleries and guides are assembled deterministically in Python. The Google
Places API augments state listings with live discovery so a large state is not
limited to what was curated by hand — Rajasthan, for example, has 2 curated
destinations but returns 74 once discovery is applied.

### Photographs are stored locally

Images are sourced from Wikimedia Commons and downloaded into `static/images/`
rather than hotlinked; bursts of full-size requests to Commons get rate-limited
(28 of 103 URLs returned HTTP 429 when tested). Attribution is a licence
obligation for CC BY and CC BY-SA, so authors and licences are listed on
`/credits` rather than overlaid on the images.

Not every record has a curated photograph yet. Those that don't fall back to a
regional stand-in rather than showing a broken image.

### The assistant is deterministic by default

It resolves a location, asks for a trip length and a travel style, then builds the
itinerary from curated records. A greeting is matched *before* any location
lookup, and the planner's own chip labels are never treated as place names — both
were real bugs, where "hi" prefix-matched Himachal Pradesh and tapping "Skip"
overwrote the chosen destination.

If an API key is configured, a language model additionally *rewords* those facts
conversationally. It is given a facts block and may not invent a place, price,
timing or date. Place-level and region-level facts are labelled separately, so
Assam's tea heritage is not reported as a speciality of Majuli island.

The model is never asked to choose a destination or build an itinerary. With no
key configured nothing leaves the server and the wording is fixed. The
deterministic wording is also used whenever the model errors, times out, or
returns a reply truncated at the token limit.

## Running it

### Docker

```bash
cp .env.example .env    # then fill in your keys
docker compose up -d --build
```

Served on <http://localhost:8000>. The container has a healthcheck, and
`instance/` is kept in a named volume so feedback and submissions survive
rebuilds.

### Locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Served on <http://127.0.0.1:5000> with debug enabled.

### Tests

`pytest` is not a runtime dependency, so install it first:

```bash
pip install pytest
pytest
```

## Configuration

Everything is read from the environment; see `.env.example` for the full list and
`config.py` for defaults. **Nothing is required to boot** — with no keys at all the
site serves curated data and a fully deterministic assistant.

| Variable | Purpose | Default |
| --- | --- | --- |
| `GOOGLE_MAPS_API_KEY` | Live place discovery and directions | unset |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Optional phrasing via Gemini | unset |
| `OPENAI_API_KEY` | Optional phrasing via OpenAI | unset |
| `ATULYA_LLM_PROVIDER` | Force `gemini`, `openai`, or `none` | auto-detected |
| `ATULYA_LLM_MODEL` | Pin a model | `gemini-flash-lite-latest` |
| `ATULYA_LLM_TIMEOUT` | Seconds before falling back to fixed wording | `20` |
| `ATULYA_SECRET_KEY` | Session signing; required for the admin area | dev default |
| `ATULYA_ADMIN_USERNAME` | Admin login | unset |
| `ATULYA_ADMIN_PASSWORD_HASH` | Admin password hash | unset |

`.env` is gitignored and must never be committed. The admin area fails closed
unless the secret key, username and password hash are all set.

### Notes on the model layer

The default is a *floating* alias rather than a pinned version. Pinned Gemini
models retire and then return a hard 404, which silently switches the feature
off — `gemini-2.0-flash` did exactly that. If a pinned model 404s, the client
recovers once onto the current alias.

A `-lite` model is the default deliberately: rewording needs no reasoning, and
measured round trips were 1–2s against 7–12s for the full flash models, which
spend the budget thinking first. Enabling a key means each assistant turn sends
the visitor's message plus the relevant curated facts to that provider.

## Routes

**Pages** — `/`, `/destinations`, `/destination/<slug>`, `/states`,
`/state/<slug>` plus `/destinations`, `/hidden-gems`, `/gallery`, `/culture`,
`/trips` and `/travel-tips` under it, `/place`, `/trips`, `/trip/<slug>`,
`/search`, `/assistant`, `/credits`, `/contact`, `/submit-gem`

**API** — `GET /api/search`, `POST /api/chat`, `POST /api/chat/reset`,
`GET /api/nearby/<slug>`, `GET /api/hidden-gems/<slug>`, `POST /api/submit-gem`

**Admin** — `/admin/login`, `/admin/contacts`, `/admin/trips`, `/admin/trips/new`

## Layout

```
app.py                 entry point
config.py              environment-driven configuration
atulya/
  __init__.py          application factory
  blueprints/          pages, api, admin routes
  chatbot.py           the assistant: intent, planning, itineraries
  llm.py               optional language-model layer (phrasing only)
  data_store.py        curated destination/state/trip access
  trip_catalog.py      curated itineraries
  hidden_gems.py       curated and discovered offbeat places
  places.py            Google Places client
  state_discovery.py   live augmentation of state listings
  recommend.py         ranking and recommendations
  search.py            location resolution and scoring
  images.py            image, srcset, gallery and credit resolution
  geo.py               distance and coordinate helpers
  directions.py        map and directions links
  models.py            SQLAlchemy models
  extensions.py        shared extension instances
data/                  curated destinations, hidden gems, states, trips
static/images/places/  locally stored photography, one folder per place
templates/             Jinja templates
scripts/               data fetching, validation and audit tools
tests/                 test suite
```

### Scripts

All run standalone, e.g. `python scripts/validate_data.py`.

| Script | Purpose |
| --- | --- |
| `validate_data.py` | Schema, coordinate sanity, slug uniqueness and state references. Exits non-zero on failure, so it can be wired into CI |
| `geo_audit.py` | Coverage audit: attractions within 30 km and hidden gems within 100 km per destination, using the app's own distance function |
| `fetch_wiki_images.py` | Download hero photography from Wikimedia Commons and record author and licence |
| `fetch_places.py` | Discover POI candidates via Google Places for **moderated** enrichment. Results are never auto-promoted to hidden gems; a curator reviews them. Photo resource names are stored without API keys |
| `test_recommendations.py` | Smoke test that different preferences actually produce different recommendations |

## Notes

- The SQLite database in `instance/` holds visitor feedback, enquiries, community
  submissions and short-lived assistant conversations (pruned after 30 days). It
  is gitignored.
- If you add photographs, record the author and licence so `/credits` stays
  accurate.
- The site is designed mobile-first; phones are the primary target.
